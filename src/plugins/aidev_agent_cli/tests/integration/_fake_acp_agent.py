# -*- coding: utf-8 -*-
"""确定性的**假执行后端**：真 ACP wire + 真 SDK 生命周期，模型调用是本地 stub。

为什么需要它
------------
取消隔离（B1）的线上证据不能靠「cancelled **或** error」这种断言——把取消实现成直接失败
同样能过。要真正承重，一轮必须**卡在等待**里、只有取消能结束它，同时另一会话在旁边正常
跑完。真实平台那条路做不到：没有凭据时 ``session/prompt`` 快速失败，进程内也拿不到真实的
``session_code``。

本模块的做法
------------
* **真** ACP 服务端：``adapters.acp.run_acp_server`` + ``acp`` SDK 的 ``AgentSideConnection``
  + stdin/stdout NDJSON；
* **真** 生命周期：``RuntimeService`` → ``RunQueue`` → ``execute_turn`` →
  ``Session``（含 ``_resolve_agent_code``）；
* 只有两处在**进程内**被替身化：
  1. 平台管理器：``LocalResourceManager.build`` 返回一个内存替身（不触达平台）；
  2. 模型执行：agent 的 ``execute`` 换成一个**可被 cancel 打断**的确定性生成器。

第 2 条就是「假执行后端」的全部内容。取消仍走**真实**通道：``RunManager.cancel`` 翻转
``handle.token``，会话的 ``_cancel_requested_now`` 读的就是它。

用法：``python -m tests.integration._fake_acp_agent``（``PYTHONPATH`` 含插件根与 SDK 根）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

#: 慢轮每步停顿（秒）：给取消一个稳定的到达窗口。
SLOW_STEP_SECONDS = 0.05

#: 慢轮步数上限（20s）：远长于测试等待窗口——只有取消能让它提前结束。
SLOW_STEPS = 400

#: 快轮文本。
FAST_TEXT = "ok"


class _FakeManager:
    """``LocalResourceManager`` 的替身：只提供构建路径真正读到的几个面，**不触达平台**。"""

    username = "fake-user"
    app_code = "fake-app"
    access_token = ""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_agent_code(self) -> str:
        return "fake-entry-agent"

    def get_client(self) -> Any:
        return object()

    def set_access_token(self, token: str, *, persist: bool = True) -> None:
        self.access_token = token

    def get_agent_config(self, agent_code: str, version: Any = None, **kwargs: Any) -> Any:
        """最小可用的 SDK ``AgentConfig``；模型端点指向回环，意外请求也只会本地失败。"""
        from aidev_agent.dto.agent_config import AgentConfig

        self.calls.append((agent_code, str(version)))

        return AgentConfig.model_validate(
            {
                "agent_code": agent_code,
                "chat_model": "fake-model",
                "non_thinking_llm": "fake-model",
                "prompt_setting": {"content": "你是测试替身。"},
                "knowledgebase_settings": {},
                "intent_recognition": {},
                "conversation_settings": {"commands": []},
            }
        )

    def retrieve_agent_config(self, agent_code: str, version: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {}

    def get_chat_session_contents(self, session_code: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def ensure_session(self, session_code: str, **kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        """其余面一律空实现：本模块只走构建与取消，不需要平台能力。"""

        def _noop(*args: Any, **kwargs: Any) -> Any:
            return None

        return _noop


class _CancellableAgent:
    """真 SDK agent 的**执行面**替身：慢轮一直等，只有取消能结束它。

    事件经会话的 event_handler 写出（与生产 agent 同一出口）；取消读的是会话的
    ``_cancel_requested_now``——``RunManager.cancel`` 翻转的那个 token。
    """

    def __init__(self, *, session: Any, handler: Any) -> None:
        self._session = session
        self.event_handler = handler

    def execute(self, execute_kwargs: Any) -> Any:
        slow = "slow" in (execute_kwargs.input or "").lower()
        steps = SLOW_STEPS if slow else 1
        run_id = "fake-run"
        emit = self.event_handler
        emit({"type": "RUN_STARTED", "runId": run_id, "threadId": "fake-thread"})
        for index in range(steps):
            if self._session._cancel_requested_now():
                # 不产出 RUN_FINISHED：会话把这一轮收尾成取消终止事件对。
                return iter(())
            text = f"chunk-{index}" if slow else FAST_TEXT
            emit({"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": text})
            if slow:
                time.sleep(SLOW_STEP_SECONDS)
        emit({"type": "RUN_FINISHED", "runId": run_id, "threadId": "fake-thread"})
        return iter(())

    def stop(self) -> None:
        return None


def install_fake_sdk():
    """Install the deterministic SDK execution face inside the spawned child."""
    from aidev_agent_cli.sdk.executor import AgentRun as SDKSession

    SDKSession.ensure_platform_session = lambda self: None
    SDKSession._event_handler = lambda self: self.writer
    SDKSession.build_agent = lambda self, **params: _CancellableAgent(
        session=self, handler=params.get("event_handler") or self.writer,
    )
    return None


async def main() -> int:
    """在**本进程**跑 ACP 服务端，执行后端是 :func:`_stub_strategy`（确定性、可取消）。

    这里不 monkeypatch 任何生产模块：替身是**显式传入**的 strategy，生产代码一行不改。
    """
    from dataclasses import replace

    from aidev_agent_cli.adapters.acp import run_acp_server
    from aidev_agent_cli.utils.runs import LaunchOptions
    from aidev_agent_cli.runtime.process_config import ProcessConfig
    from aidev_agent_cli.runtime.settings import BkAiConfig
    from aidev_agent_cli.bootstrap.startup import install_startup_hooks
    from aidev_agent_cli.adapters.streams import ProtocolChannel
    from aidev_agent_cli.runtime.service import build_runtime

    # 与真实入口同一条启动路径：日志 / guard / 环境 / OTel 都在建 runtime 之前就位。
    # 少了它，本进程的诊断只会落到 stderr，日志文件里的 cwd 断言就成了空断言。
    bkai = BkAiConfig.read()
    install_startup_hooks(bkai.env)
    bkai.auth["access_token"] = "fake-token"
    service = build_runtime("acp", ProcessConfig(bkai=bkai, launch=LaunchOptions(resources=("injected",))))
    original_create = service.new_session

    async def create(options=None):
        options = replace(options, tool_factories=("tests.integration._fake_acp_agent:install_fake_sdk",))
        return await original_create(options)

    service.new_session = create

    return await run_acp_server(service, ProtocolChannel.stdio())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))