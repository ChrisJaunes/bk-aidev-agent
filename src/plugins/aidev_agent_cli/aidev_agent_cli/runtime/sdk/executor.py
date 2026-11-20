# -*- coding: utf-8 -*-
"""worker 内的 SDK 执行体：构造会话、跑一轮、收尾。**只在子进程里被装载。**

本模块顶部的 SDK 依赖就是本包「SDK 何时进来」的唯一定义点：它被
``aidev_agent_cli.runtime.sdk.executor:build`` 这个 import target 直接装载，而装载发生在
``runtime.worker._worker_entry`` 完成全部保护之后（env 发布 → 出网守卫 →
``install_worker_startup``：stdout guard / ``sys.path`` 加固 / OTel）。因此：

* 本模块**不得**再有任何函数内 import——模块级 import 在这里就已经是"晚"的；
* 包根 ``runtime.sdk.__init__`` **不得** import 本模块（那会把 SDK 拉回到父进程与 target
  装载之前）；
* 父进程侧的值对象在 :mod:`aidev_agent_cli.runtime.models`，那边的模块不得反向引用这里。
"""

from __future__ import annotations

import copy
import importlib
import os
from dataclasses import asdict, replace
from typing import Any

from .factory import validate_message_handler
from .hooks import HookConfig
from .session import SDKSession
from .workspace import install_workspace_agent
from ..auth import AuthManager, ProcessConfig
from ..contracts import LaunchOptions
from ..history import LocalHistory
from ..models import HookRequest, event_payload
from ..worker import WorkerContext
from ...infrastructure.startup import install_worker_startup
from ...infrastructure.stdio import apply_worker_stdio_guard


class _Cancel:
    def __init__(self, context: WorkerContext, stop) -> None:
        self.context = context
        self.stop = stop
        self.requested = False

    def is_cancelled(self) -> bool:
        cancelled = self.context.cancelled()
        if cancelled and not self.requested:
            self.requested = True
            self.stop()
        return cancelled


class _HookChannel:
    def __init__(self, context: WorkerContext) -> None:
        self.context = context

    def request(self, **payload: Any) -> tuple[str, dict[str, Any] | None]:
        payload.pop("cancel_check", None)
        reply = self.context.request("hook", asdict(HookRequest(**payload)))
        return reply["status"], reply.get("payload")


def _tool(target: str) -> Any:
    module, separator, name = target.partition(":")
    if not separator or not module or not name or "<locals>" in name:
        raise ValueError("tool factories must be import targets module:name")
    factory = importlib.import_module(module)
    for component in name.split("."):
        factory = getattr(factory, component)
    return factory()


class SDKExecutor:
    def __init__(self, snapshot: dict[str, Any], context: WorkerContext) -> None:
        # 环境与 cwd 的唯一设置点（``_worker_entry`` 用同一份快照先发布过一次，这里以
        # ``_worker_entry`` 加锁后的快照为准重放——idempotent，且保证执行体看到的 env
        # 就是快照 env，不受进程内其他线程并发改动影响）。
        settings = snapshot["session"]["settings"]
        os.environ.clear()
        os.environ.update(settings["env"])
        os.chdir(settings["cwd"])

        # 启动顺序是 worker 的正确性契约，集中在 infrastructure.startup 一处：
        # bootstrap（SDK 导入期要读 settings）→ guard（SDK 导入期的 print 落 stderr）→
        # 出网限制 → sys.path 加固 → OTel（必须在首次 aidev_agent import 之前 patch）。
        # 本函数被调用时，SDK 已在 import target 装载时进入 ``sys.modules``，但这些步骤
        # 都是进程级状态（fd / environ / socket / 模块属性），顺序仍然必须自上而下执行完。
        install_worker_startup(settings["env"])

        validate_message_handler()
        install_workspace_agent()

        self.snapshot = snapshot
        self.history = LocalHistory()
        self.session = None
        try:
            data = snapshot["session"]
            self.history.record_run(snapshot["run_id"], data["session_code"], "building")
            identity = data["identity"]
            settings = data["settings"]
            if settings["virtual_mode"] is not True:
                raise ValueError("worker filesystem must use virtual_mode")
            self.session = SDKSession(
                session_code=data["session_code"],
                agent_code=data["agent_code"],
                resources=tuple(data["resources"]),
                model=data["model"],
                config=ProcessConfig(env=settings["env"], defaults=LaunchOptions()),
                auth=AuthManager(access_token=identity["access_token"], username=identity["username"]),
                username=identity["username"],
                base_url=data["base_url"],
                auth_headers=data["auth_headers"],
                temperature=data["temperature"],
                max_tokens=data["max_tokens"],
                hooks=HookConfig.parse(snapshot["hooks"]) if snapshot["hooks"] is not None else None,
                tools=[_tool(target) for target in data["tool_factories"]],
                hook_channel=_HookChannel(context),
            )
            self.session.bind_cancel_token(_Cancel(context, self.session.request_stop))
            # 阶段标记挂在实例的**类属性查找结果**上——``_build_guarded`` 是 staticmethod，
            # 实例赋值的 ``self.session._build_guarded = ...`` 只影响这一个会话，正好是我们要的
            # 粒度（无泄漏、无需还原）。
            guarded = self.session._build_guarded

            def build_agent(builder):
                context.phase("building")
                try:
                    return guarded(builder)
                finally:
                    context.phase("running")

            self.session._build_guarded = build_agent
            state = self.history.load(self.session.session_code)
            self.previous_state = copy.deepcopy(
                {
                    "records": [],
                    "interrupts": [],
                    "turn_count": 0,
                    "hook_memo": {},
                    "hook_sequence": 0,
                    **state,
                }
            )
            if self.session.ledger is not None:
                for record in state.get("records", []):
                    self.session.ledger.append(record)
            self.session._pending_interrupts = state.get("interrupts", [])
            self.session._turn_count = state.get("turn_count", 0)
            if self.session.hook_bus is not None:
                self.session.hook_bus._ask_memo = (
                    copy.deepcopy(state.get("hook_memo", {})) if snapshot["input"]["kind"] != "context" else {}
                )
                self.session.hook_bus._seq = state.get("hook_sequence", 0)
        except BaseException:
            self.close()
            raise

    def execute(self, context: WorkerContext) -> dict[str, Any]:
        session = self.session
        request = self.snapshot
        typed_input = request["input"]

        def deliver(event: Any) -> None:
            context.emit(event_payload(event))

        if request["kind"] in ("describe_agent_config", "list_llms", "describe_config_snapshot"):
            return getattr(session, request["kind"])()
        if request["kind"] == "prepare":
            session._build_guarded(session.prepare)
            return {"status": "finished", "session_code": session.session_code}
        if request["kind"] == "resume":
            outcome = session.resume(
                request_id=request["correlation_id"],
                input_text=typed_input["text"],
                resume_payload=typed_input["payload"],
                sink=deliver,
            )
        else:
            outcome = session.turn(
                request_id=request["correlation_id"],
                input_text=typed_input["text"],
                messages=typed_input.get("messages"),
                context_mode=typed_input["kind"] == "context",
                execute_kwargs=request["execute_options"],
                sink=deliver,
            )
        if context.cancelled():
            outcome = replace(outcome, status="cancelled")
            session._pending_interrupts = self.previous_state.get("interrupts", [])
        if outcome.status in ("failed", "cancelled"):
            session._pending_interrupts = self.previous_state.get("interrupts", [])
            if session.hook_bus is not None:
                session.hook_bus._ask_memo = copy.deepcopy(self.previous_state.get("hook_memo", {}))
        if outcome.status in ("failed", "cancelled") and session.ledger is not None:
            for record in session.ledger.records():
                if record.get("turn_id") == outcome.turn_id:
                    session.ledger.drop(record["message_id"])
        state = {
            "records": session.ledger.records() if session.ledger is not None else [],
            "interrupts": session._pending_interrupts,
            "turn_count": session._turn_count,
            "hook_memo": session.hook_bus._ask_memo if session.hook_bus is not None else {},
            "hook_sequence": session.hook_bus._seq if session.hook_bus is not None else 0,
        }
        if typed_input["kind"] == "context":
            state = self.previous_state
        self.history.stage(request["run_id"], session.session_code, state)
        return {"outcome": asdict(outcome)}

    def close(self) -> None:
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.history.close()


def build(snapshot: dict[str, Any], context: WorkerContext) -> SDKExecutor:
    # Worker traffic is IPC only, including native-library stdout and stdin reads.
    # fd 重定向必须早于**任何** SDK 调用：本函数的 import target 已被 import，但真正的
    # 执行（构造执行体）在整段重定向 + guard 之后才开始。
    apply_worker_stdio_guard()
    return SDKExecutor(snapshot, context)


__all__ = ["SDKExecutor", "build"]
