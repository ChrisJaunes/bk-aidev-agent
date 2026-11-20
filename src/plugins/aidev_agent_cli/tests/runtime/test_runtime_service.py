# -*- coding: utf-8 -*-
"""跨协议契约：**同一组生命周期不变量**在两种执行 backend 上都必须成立。

这是「不只改名目录」的证据：``RuntimeService`` 是两条执行路径
（platform 异步 runner / injected 同步 headless）共用的生命周期所有者，因此
``create -> start -> busy 拒绝 -> cancel 命中 -> cancel 未知 = False -> close 幂等 ->
close_all`` 这串不变量对**两者**都要成立，且语义一致。

统一执行路径下只有一个策略（``SDKRunner``）；参数化的是两种资源形状（含平台来源 /
无平台来源），会话 agent 换成脚本替身（``tests/integration/_scripted.py``，与 dispatch 测试同一注入缝）。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aidev_agent_cli import __main__ as entry
from aidev_agent_cli.resources.sources import PLATFORM_SOURCES
from aidev_agent_cli.runtime.contracts import RunOutcome, RunRequest, SessionOptions
from aidev_agent_cli.runtime.inputs import ChatInput
from aidev_agent_cli.runtime.service import RuntimeService


class _RecordingSink:
    """记录事件的内存 sink（两种策略都用同一面：``await sink.send(payload)``）。"""

    def __init__(self) -> None:
        self.payloads: list[Any] = []

    async def send(self, payload: Any) -> None:
        self.payloads.append(payload)


async def _slow_platform_run(params: dict[str, Any], is_cancelled: Any) -> AsyncIterator[str]:
    """一轮会「慢慢产出事件」并轮询取消标志的平台运行。"""
    for index in range(400):
        if is_cancelled():
            yield json.dumps({"type": "RUN_FINISHED", "reason": "cancelled"})

            return
        await asyncio.sleep(0.005)
        yield json.dumps({"type": "TEXT_MESSAGE_CONTENT", "delta": str(index)})

    yield json.dumps({"type": "RUN_FINISHED", "reason": "completed"})


def _scripted_service(resources: tuple[str, ...]) -> RuntimeService:
    from aidev_agent_cli.runtime.auth import ProcessConfig
    from aidev_agent_cli.runtime.contracts import LaunchOptions
    return RuntimeService(config=ProcessConfig(defaults=LaunchOptions(resources=resources)))


def _platform_service() -> RuntimeService:
    return _scripted_service(PLATFORM_SOURCES)


def _injected_service() -> RuntimeService:
    return _scripted_service(("injected",))


STRATEGIES = [
    pytest.param(_platform_service, id="platform"),
    pytest.param(_injected_service, id="injected"),
]


# ============================================================================
# 不变量 1：create 建立会话，未知会话被明确拒绝
# ============================================================================


@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_create_registers_the_session_and_unknown_sessions_are_rejected(make_service: Any) -> None:
    service = make_service()
    info = await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s1"))

    assert info.session_code == "s1"
    assert (await service.session_info("s1")) is not None
    assert (await service.session_info("nope")) is None

    from aidev_agent_cli.runtime.errors import RuntimeFault
    with pytest.raises(RuntimeFault) as exc:
        await service.start(RunRequest(session_code="nope", input=ChatInput("hi")), _RecordingSink())
    assert exc.value.code == "session.not_found"

    await service.close_all("test")


# ============================================================================
# 不变量 2：同会话 busy 被拒（第二个 run 得到 rejected，不是并发跑起来）
# ============================================================================


@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_a_second_run_on_the_same_session_settles_while_the_first_is_live(make_service: Any) -> None:
    service = make_service()
    await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s1"))

    first = await service.start(RunRequest(session_code="s1", input=ChatInput("first")), _RecordingSink())
    second = await service.start(RunRequest(session_code="s1", input=ChatInput("second")), _RecordingSink())

    outcome = await asyncio.wait_for(second.result(), timeout=10)
    assert outcome.status == "rejected"
    assert outcome.error_code == "session.busy"

    await service.cancel("s1")
    await asyncio.wait_for(first.result(), timeout=10)
    await service.close_all("test")


# ============================================================================
# 不变量 3：cancel 只命中目标会话（B1 的跨协议版本）
# ============================================================================


@pytest.mark.slow
@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_cancel_is_scoped_to_the_target_session(make_service: Any) -> None:
    service = make_service()
    await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s1"))
    await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s2"))

    first = await service.start(RunRequest(session_code="s1", input=ChatInput("one")), _RecordingSink())
    second = await service.start(RunRequest(session_code="s2", input=ChatInput("two")), _RecordingSink())

    outcome = await service.cancel("s1")
    assert outcome.cancelled is True
    assert outcome.run_ids == [first.run_id]

    await asyncio.wait_for(first.result(), timeout=10)
    # s2 的 run **不受影响**。判据是「它没有被取消」而不是「它还在跑」：
    # 注入式 backend 的假 agent 可能在取消 s1 之前就跑完了。
    second_outcome = await asyncio.wait_for(second.result(), timeout=10)
    assert second_outcome.status != "cancelled", "取消 s1 不得把 s2 也取消掉"

    await service.close_all("test")


@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_cancel_of_an_unknown_session_reports_false_instead_of_raising(make_service: Any) -> None:
    service = make_service()

    outcome = await service.cancel("never-created")

    assert outcome.cancelled is False
    assert outcome.reason == "no active run"

    await service.close_all("test")


# ============================================================================
# 不变量 4：close 幂等；close_all 幂等
# ============================================================================


@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_close_is_idempotent_and_reports_the_second_call(make_service: Any) -> None:
    service = make_service()
    await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s1"))

    first = await service.close("s1")
    second = await service.close("s1")

    assert first.closed is True
    assert first.already_closed is False
    # 第二次关闭不抛异常、不重复关闭后端，且该会话**没有**活的 run。
    # （``close`` 刻意保留会话条目："关闭过"与"从未存在"对宿主是两件事，见其 docstring。）
    assert second.session_code == "s1"
    assert not service.has_live_runs("s1")
    assert (await service.session_info("nope")) is None

    await service.close_all("test")


@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_close_all_is_idempotent_and_settles_live_runs(make_service: Any) -> None:
    service = make_service()
    await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s1"))
    handle = await service.start(RunRequest(session_code="s1", input=ChatInput("work")), _RecordingSink())

    await service.close_all("first")
    await service.close_all("second")

    outcome = await asyncio.wait_for(handle.result(), timeout=10)
    assert outcome.status in {"cancelled", "finished", "failed"}
    assert not any(service.has_live_runs(code) for code in service.run_handles())


# ============================================================================
# 不变量 5：终态只落定一次（重复 settle 不覆盖）
# ============================================================================


@pytest.mark.parametrize("make_service", STRATEGIES)
async def test_a_run_outcome_is_settled_exactly_once(make_service: Any) -> None:
    service = make_service()
    await service.new_session(SessionOptions(tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",), session_code="s1"))
    handle = await service.start(RunRequest(session_code="s1", input=ChatInput("work")), _RecordingSink())

    await service.cancel("s1")
    first = await asyncio.wait_for(handle.result(), timeout=10)

    # 第二次落定必须被忽略（不是覆盖，也不是抛异常）。
    handle.settle(RunOutcome(status="finished", run_id=handle.run_id, session_code="s1"))
    second = await handle.result()

    assert second is first
    assert first.status in {"cancelled", "finished", "failed"}

    await service.close_all("test")


# ============================================================================
# 不变量 6：agent_code 是**会话级**选项，不写进程环境
# ============================================================================


async def test_two_sessions_keep_their_own_agent_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个会话各自的 agent_code 互不影响，进程环境也**没有**被写。

    这是 D5 的第二半。旧实现是 旧平台策略的 ``open()`` 里的
    ``os.environ.setdefault(AGENT_TUI_AGENT_CODE, options.agent_code)``：

    * 第一个会话的 code 成为全进程默认值——于是「谁是谁」取决于创建顺序；
    * 第二个会话因为 ``setdefault`` **被静默忽略**——没有报错，只是跑了别人的 agent；
    * 任何在进程里读这个变量的代码（包括 SDK 自己的 settings）看到的是陈旧的会话值。

    现在 code 落在 ``Session.agent_code`` 上，由 ``_resolve_agent_code`` 优先读取。
    """
    monkeypatch.delenv("AGENT_TUI_AGENT_CODE", raising=False)
    monkeypatch.delenv("BKPAAS_APP_CODE", raising=False)

    service = _scripted_service(("injected",))
    await service.new_session(SessionOptions( session_code="s1", agent_code="code-a"))
    await service.new_session(SessionOptions( session_code="s2", agent_code="code-b"))

    first = service._sessions.get("s1")
    second = service._sessions.get("s2")
    assert first.options.agent_code == "code-a"
    assert second.options.agent_code == "code-b"

    # 解析结果跟着**各自的**会话走（同一个解析函数，两条不同的答案）。
    assert service._snapshot("start", first, RunRequest("s1"), None).session.agent_code == "code-a"
    assert service._snapshot("start", second, RunRequest("s2"), None).session.agent_code == "code-b"

    # 进程环境全程干净：会话不得写它。
    assert "AGENT_TUI_AGENT_CODE" not in os.environ


async def test_the_session_agent_code_wins_over_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话级优先：进程环境是「用户显式 export」的兜底，不是会话写入的残留。"""
    monkeypatch.setenv("AGENT_TUI_AGENT_CODE", "from-env")

    service = _scripted_service(("injected",))
    await service.new_session(SessionOptions( session_code="s1", agent_code="explicit"))
    await service.new_session(SessionOptions( session_code="s2"))

    assert service._snapshot("start", service._sessions.get("s1"), RunRequest("s1"), None).session.agent_code == "explicit"
    # 没有会话级值时，进程环境仍然有效（兼容用户 export）。
    assert service._snapshot("start", service._sessions.get("s2"), RunRequest("s2"), None).session.agent_code == "from-env"



@pytest.mark.parametrize(
    ("protocol", "busy_mode", "resources"),
    [("agui", "reject", "platform"), ("acp", "reject", "injected"), ("acp-dsh", "wait", "injected")],
)
def test_build_runtime_is_the_single_assembly_point(protocol: str, busy_mode: str, resources: str) -> None:
    """入口唯一的组装点：命令行默认值进 ProcessConfig，协议默认值（busy / 资源兜底）只在这一张表里。"""
    from aidev_agent_cli.runtime.service import build_runtime

    given = entry.resolve_resources(resources)

    service = build_runtime(protocol, model="m1", session="s1", resources=given, env={})

    assert service.defaults.model == "m1"
    assert service.defaults.session == "s1"
    assert service.defaults.resources == given
    assert service._sessions._defaults is service.defaults
    assert service._scheduler._busy_mode == busy_mode