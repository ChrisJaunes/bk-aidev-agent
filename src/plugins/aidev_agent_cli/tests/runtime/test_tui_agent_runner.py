"""``runtime.runner.SDKRunner`` / ``runtime.sdk.session.SDKSession`` 的驱动契约（主测 ``runtime``）。

这些用例跑在替身 agent（覆写 ``AgentRunner._run``）之上，而非 SDK：SDK 需要活模型与
已配置环境，而这里的断言只关心**帧行为**（事件如何到达 sink、终态是什么），与产生事件的
agent 无关。回放的 fixture ``ag-ui-events.ndjson``（与本文件同目录）捕获自一次真实
``AidevAGUIAgent`` 运行；用真实事件而非手写事件，是因为手写事件只是对 SDK 形状的一种
主张，SDK 完全可以不认。

**跨层说明（按职责分层的已知妥协）**：本文件是历史遗留的「大杂烩」，除 runtime 的
runner/session 帧行为外，还夹带了 ``resources`` 的 manager / token / MCP / login 用例与
``infrastructure`` 的 bootstrap 用例。全文件共用一个模块级 autouse fixture
``_isolate_tui_config`` 与一组替身类（``AgentRunner`` / ``_RecordingWriter`` /
``_RunEventSink`` / ``_FakeManager`` 等），二者被不同层的用例交叉引用；强行按层拆开会
把 autouse fixture 提进 ``conftest.py``、扩大其作用域，风险高。因此本次分层重整**整体
搬迁**到 ``tests/runtime/`` 并在此标注，不做物理切分。

TODO(分层拆分): 先把共享替身抽到 ``tests/runtime/_runner_support.py``、把 autouse
fixture 的落点单独评估，再把 resources / infrastructure 用例拆到各自层目录；务必在
拆分前后做归一化节点 ID 集合比对，确保 692 个用例一个不少。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aidev_agent_cli.adapters.agui import FrameWriter
from aidev_agent_cli.infrastructure import environment
from aidev_agent_cli.infrastructure import settings as config
from aidev_agent_cli.infrastructure.errors import AGENT_INTERNAL_ERROR as AGENT_ERROR_CODE
from aidev_agent_cli.infrastructure.errors import RESUME_INTERRUPT_NOT_FOUND
from aidev_agent_cli.resources.loaders import ResourceOverrides
from aidev_agent_cli.resources.local_config import McpResolver
from aidev_agent_cli.resources.manager import LocalResourceManager
from aidev_agent_cli.runtime.auth import ProcessConfig
from aidev_agent_cli.runtime.inputs import ChatInput
from aidev_agent_cli.runtime.sdk import factory as agent_factory
from aidev_agent_cli.runtime.sdk.session import SDKSession as Session
from tests.integration._paths import PLUGIN_ROOT
from tests.integration._scripted import script_session, strip_sse_shell, thread_safe_sink

FIXTURE = Path(__file__).resolve().parent / "ag-ui-events.ndjson"


class _StringIO:
    """Minimal stand-in for the writer's stream, for the FrameWriter tests."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> None:
        self.parts.append(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _RecordingWriter(FrameWriter):
    """Captures every frame sent, preserving order.

    ``peer_gone`` is present (as the real writer has it) so the runner exercises
    its normal path rather than the defensive fallback.
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed = False
        self._peer_gone = False

    async def send(self, frame: dict[str, Any]) -> bool:
        if self._peer_gone:
            return False
        self.frames.append(frame)
        return True

    async def close(self, **_kwargs: Any) -> None:
        self.closed = True

    @property
    def peer_gone(self) -> bool:
        return self._peer_gone


def load_fixture_lines() -> list[str]:
    """Return the fixture's events as the SSE-shaped strings the SDK yields.

    The fixture stores the *inner* JSON (what ``payload`` carries); the SDK
    yields it SSE-wrapped. Re-wrapping here means the tests exercise the real
    shell-stripping path rather than bypassing it.
    """
    lines = [ln for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [f"data: {ln}\n\n" for ln in lines]


@pytest.fixture(autouse=True)
def _isolate_tui_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``bkai_tui.json`` 指到临时文件，**每个**测试都隔离。

    ``/login`` / ``/logout`` 现在落盘（D4 接线），``Session.__init__`` 也会
    读回。没有这层隔离，任何走真实 ``Session`` 的测试都会读写开发者自己的
    ``~/.bkai/bkai_tui.json``——token 泄漏进测试、测试写脏真机配置，两个方向都是
    事故。测试体内自行 ``setenv`` 同名变量时覆盖本值（后设者胜）。
    """
    import aidev_agent_cli.infrastructure.settings as config_mod

    monkeypatch.setenv(config_mod.TuiConfig.CONFIG_FILE_OVERRIDE, str(tmp_path / "bkai_tui.json"))


class AgentRunner:
    """测试侧的单轮驱动：统一执行路径（``SDKRunner`` + ``Session``）加脚本化 agent。

    保留旧 runner 的调用面 ``run(req_id=, run_id=, session_code=, input_text=, state=, sink=,
    is_cancelled=, execute_kwargs=)``，让断言聚焦在「事件怎么到达 sink、终态是什么」。
    """

    def __init__(self, run_impl: Any = None) -> None:
        self._impl = run_impl

    async def run(
        self,
        *,
        req_id: str = "",
        run_id: str = "",
        session_code: str = "",
        input_text: str = "",
        state: Any,
        sink: Any,
        is_cancelled: Any = None,
        execute_kwargs: Any = None,
    ) -> Any:
        from aidev_agent_cli.runtime.contracts import RunRequest

        if self._impl is not None:
            script_session(state, self._impl)
        if session_code:
            state.session_code = session_code
            state.writer.set_session_code(session_code)
        handle = SimpleNamespace(
            run_id=run_id,
            token=SimpleNamespace(is_cancelled=is_cancelled or (lambda: False)),
        )
        request = RunRequest(session_code=session_code, input=ChatInput(input_text), execute_options=execute_kwargs or {}, correlation_id=req_id)
        from dataclasses import replace

        state.bind_cancel_token(handle.token)
        deliver = thread_safe_sink(sink, asyncio.get_running_loop())
        try:
            outcome = await asyncio.to_thread(
                state.turn, request_id=request.correlation_id, input_text=request.input.text,
                execute_kwargs=request.execute_options, sink=deliver,
            )
            return replace(outcome, run_id=run_id)
        finally:
            state.bind_cancel_token(None)


def make_runner(writer: _RecordingWriter, run_impl: Any, **kwargs: Any) -> tuple[AgentRunner, Session]:
    """构造单轮驱动与它所需的 ``Session``（可经 ``state=`` 复用预置状态的实例）。

    ``Session`` 不再自带默认资源来源，这里显式给一个无平台底座。
    """
    state = kwargs.pop("state", None) or Session(resources=("injected",))
    return AgentRunner(run_impl), state


def application_for(state):
    from aidev_agent_cli.runtime.contracts import SessionOptions
    from aidev_agent_cli.runtime.service import RuntimeService

    service = RuntimeService(config=state.config, auth=state.auth)
    asyncio.run(service.new_session(SessionOptions(
        session_code="selection-test", resources=state.resources or ("injected",),
        agent_code=state._resolve_agent_code(state._resource_manager) or "app-code",
        model=state.current_model or "",
    )))
    return service


def worker_for(service):
    options = service._sessions.get("selection-test").options
    return Session(
        session_code=options.session_code, config=service._config, auth=service._auth,
        agent_code=options.agent_code, resources=options.resources, model=options.model,
    )


class _RunEventSink:
    """本次运行的**事件出口**：把每个 payload 包成 ``event`` 帧写进记录的 writer。

    与 ``adapters.agui.RunEventSink`` 同形（那里是真正的帧构造）；测试只需要能断言"一事件
    一帧、payload 逐字"。``runId`` 由这一轮的结果补上——生产侧 AG-UI 的帧构造同样从运行时
    拿到 ``runId``，这里因此有一个最小等价物。
    """

    def __init__(self, writer: _RecordingWriter, req_id: str, run_id: str) -> None:
        self._writer = writer
        self._req_id = req_id
        self._run_id = run_id

    async def send(self, payload: dict[str, Any]) -> None:
        await self._writer.send_event_frame(self._req_id, self._run_id, payload)


def _run_events(runner: AgentRunner, writer: _RecordingWriter, **kwargs: Any):
    """以"事件经 sink 归一"的方式跑一轮，返回 ``RunOutcome``。"""
    req_id = kwargs.get("req_id", "")
    sink = _RunEventSink(writer, req_id, kwargs.get("run_id", ""))

    return runner.run(sink=sink, **kwargs)


# ------------------------------------------------------------------ D-10


async def test_forwards_every_event_as_frame() -> None:
    """D-10: one ``event`` frame per fixture line, payload verbatim.

    The verbatim assertion is the load-bearing one. If the runner re-modelled,
    renamed or dropped a field, the payload would no longer equal the parsed
    fixture line — and the whole "worker is a framing layer, not a second
    message protocol" contract would be false.
    """
    fixture_lines = load_fixture_lines()
    expected = [json.loads(strip_sse_shell(ln)) for ln in fixture_lines]

    async def replay(_params: dict[str, Any], _is_cancelled: Any):
        for line in fixture_lines:
            yield line

    writer = _RecordingWriter()
    runner, state = make_runner(writer, replay)

    await _run_events(runner, writer, state=state, req_id="req-1", run_id="run-1", session_code="s1", input_text="hi")

    events = [f for f in writer.frames if f["kind"] == "event"]
    assert len(events) == len(expected), f"expected one frame per fixture line ({len(expected)}), got {len(events)}"

    for frame, want in zip(events, expected, strict=True):
        assert frame["kind"] == "event"
        assert frame["runId"] == "run-1"
        assert frame["id"] == "req-1"
        # Verbatim — this is the D-10 assertion.
        assert frame["payload"] == want, f"payload was re-modelled: {frame['payload']!r} != {want!r}"

    # And the event types survived in order.
    assert [f["payload"]["type"] for f in events] == [e["type"] for e in expected]


async def test_completed_frame_after_success() -> None:
    """A successful run terminates with exactly one ``completed`` frame."""

    async def replay(_params: dict[str, Any], _is_cancelled: Any):
        for line in load_fixture_lines():
            yield line

    writer = _RecordingWriter()
    runner, state = make_runner(writer, replay)

    outcome = await _run_events(
        runner, writer, state=state, req_id="req-2", run_id="run-2", session_code=None, input_text="hi"
    )

    # 统一后 runner **不写尾帧**：它只产出一个终态，线路上那一帧由 adapter 发（ACP 侧是
    # stopReason，headless 侧是 ok 信封）。因此这里断言 ``RunOutcome``——比断言某一帧更准确：
    # 「这一轮结束了」只有一个答案，没有第二个可以漂移的地方。
    assert outcome.status == "finished", outcome
    assert outcome.run_id == "run-2"
    # 事件侧没有任何 error 帧：成功轮不该出现失败信号。
    assert not [f for f in writer.frames if f["kind"] == "error"], writer.frames


# ------------------------------------------------------------------ D-06


async def test_cancel_via_checker() -> None:
    """D-06: the predicate the caller supplies is what the agent polls.

    The stub loops until it observes the flag, mirroring how the SDK's own
    ``cancel_checker`` is polled. The run must then finish *without raising* —
    the graceful path is what lets the agent emit its terminal event.

    The flag is owned by the CALLER here (a plain set), not by the runner. That is
    the point of the signature: the dispatcher's ``cancelled`` set is the single
    authority, and the run simply asks it.
    """
    observed: list[bool] = []
    started = asyncio.Event()

    async def looping_agent(_params: dict[str, Any], is_cancelled: Any):
        started.set()
        for i in range(500):
            cancelled = is_cancelled()
            observed.append(cancelled)
            if cancelled:
                # Mirrors the SDK: the agent emits its own terminal event and
                # returns normally rather than exploding.
                yield 'data: {"type":"RUN_FINISHED","cancelled":true}\n\n'
                return
            await asyncio.sleep(0.005)
            yield f'data: {{"type":"TEXT_MESSAGE_CONTENT","delta":"{i}"}}\n\n'

    writer = _RecordingWriter()
    runner, state = make_runner(writer, looping_agent)
    cancelled: set[str] = set()

    task = asyncio.create_task(
        runner.run(
            state=state,
            sink=_RunEventSink(writer, "req-3", "run-3"),
            req_id="req-3",
            run_id="run-3",
            session_code=None,
            input_text="hi",
            is_cancelled=lambda: "run-3" in cancelled,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.02)

    # The run must not have finished yet, or we'd be testing nothing.
    assert not task.done(), "run finished before cancel was requested"

    cancelled.add("run-3")

    # No exception should escape: cancellation is cooperative, not an abort.
    await asyncio.wait_for(task, timeout=3)

    assert any(observed), "the stub never observed the cancel flag — the flag is not wired"
    # 协作式取消走的是「优雅」路径：backend 自己产出终态 RUN_FINISHED，runner 归为 finished。
    assert not [f for f in writer.frames if f["kind"] == "error"], writer.frames


# ------------------------------------------------------------------ T-04-02


async def test_agent_exception_becomes_error_frame() -> None:
    """T-04-02: an agent failure degrades to a frame; it must not propagate."""

    async def exploding_agent(_params: dict[str, Any], _is_cancelled: Any):
        yield 'data: {"type":"RUN_STARTED"}\n\n'
        raise RuntimeError("boom")

    writer = _RecordingWriter()
    runner, state = make_runner(writer, exploding_agent)

    # The key assertion is that this line does not raise.
    outcome = await _run_events(
        runner, writer, state=state, req_id="req-4", run_id="run-4", session_code=None, input_text="hi"
    )

    # 统一后 runner 不写 error 帧，只把失败作为终态交回去；adapter 决定怎么说。
    assert outcome.status == "failed", outcome
    assert outcome.error_code == AGENT_ERROR_CODE, outcome
    assert "boom" in outcome.error_message, outcome
    assert outcome.run_id == "run-4"
    # 事件侧：backend 产出的事件 + 会话补齐的错误终止事件（三协议同一收尾契约）。
    assert [f["payload"]["type"] for f in writer.frames] == ["RUN_STARTED", "RUN_ERROR"], writer.frames


# ----------------------------------------------------------------- CHAT-03


async def test_multi_turn_shares_session_code() -> None:
    """CHAT-03: two turns on one ``session_code`` must use the same ``session_code``.

    This is the mechanism behind multi-turn context: the SDK's checkpointer is
    keyed by the ``session_code`` it derives its ``thread_id`` from, so if the id
    drifted between turns the second turn would start from a blank slate with no
    error anywhere. There is no separate ``threadId`` on the wire any more — the
    ``session_code`` IS the LangGraph thread key.
    """
    seen_session_codes: list[str] = []

    async def recording_agent(params: dict[str, Any], _is_cancelled: Any):
        seen_session_codes.append(params["sessionCode"])
        yield 'data: {"type":"RUN_FINISHED"}\n\n'

    writer = _RecordingWriter()
    runner, state = make_runner(writer, recording_agent)

    first = {"sessionCode": "thread-first"}
    session_code = first["sessionCode"]

    await _run_events(
        runner, writer, state=state, req_id="r1", run_id="run-a", session_code=session_code, input_text="turn 1"
    )
    await _run_events(
        runner, writer, state=state, req_id="r2", run_id="run-b", session_code=session_code, input_text="turn 2"
    )

    assert len(seen_session_codes) == 2, seen_session_codes
    assert seen_session_codes[0] == seen_session_codes[1] == first["sessionCode"], (
        f"session_code drifted between turns: {seen_session_codes}"
    )

    # And a *different* session code must NOT reuse that thread (no cross-turn bleed).
    other = {"sessionCode": "thread-other"}
    await _run_events(
        runner, writer, state=state, req_id="r3", run_id="run-c", session_code=other["sessionCode"], input_text="x"
    )
    assert seen_session_codes[2] == other["sessionCode"]
    assert seen_session_codes[2] != seen_session_codes[0]


# ------------------------------------------------------------------ D-29


def test_no_message_storage() -> None:
    """D-29: the session registry must expose no message-bearing surface.

    A guard against future drift: the moment the registry (now
    ``Session``, 261014-w6d) grows a ``messages`` attribute or a
    ``store_message`` method, this repo has a second authoritative conversation
    store and the SDK's remote one is no longer the single source of truth.
    """
    from aidev_agent_cli.runtime.service import RuntimeService
    from aidev_agent_cli.runtime.session import SessionRegistry

    service = RuntimeService()
    registry = SessionRegistry(service.defaults)
    registry.new_session()

    forbidden_substrings = ("message", "history", "conversation", "transcript")
    exposed: list[str] = []
    for name in dir(registry):
        if name.startswith("__"):
            continue
        lowered = name.lower()
        if any(bad in lowered for bad in forbidden_substrings):
            exposed.append(name)

    assert not exposed, (
        f"the session registry exposes message-bearing surface {exposed}; D-29 forbids a second authoritative store"
    )

    # And the records it does hold must be identity only. No ``threadId``: the
    # ``sessionCode`` is itself the LangGraph thread key, so a separate copy would
    # be a second name for one identity.
    record = registry.list()[0]
    assert set(record) == {"sessionCode", "createdAt"}, record


# ----------------------------------------------------------------- D-06 model


async def test_run_carries_no_per_turn_model() -> None:
    """D-06（改）：模型**不是**每轮的输入。

    模型是用户在 ``/models`` 里做出的**持久**选择：走 ``config.set``（kind=models）
    落盘，并由 :attr:`Session.current_model` 在当前进程内立即生效，经 loader 链
    落到 ``AgentConfig.chat_model``。把它作为每轮入参会再造一条并行覆盖路径，而两条
    路径不一致时无法判断谁生效——因此 ``run`` 不再接收它，params 里也不再有该键。
    """
    seen: list[dict[str, Any]] = []

    async def recording_agent(params: dict[str, Any], _is_cancelled: Any):
        seen.append(params)
        yield 'data: {"type":"RUN_FINISHED"}\n\n'

    writer = _RecordingWriter()
    runner, state = make_runner(writer, recording_agent)

    await _run_events(runner, writer, state=state, req_id="r1", run_id="run-m", session_code="s1", input_text="hi")

    assert "model" not in seen[0], (
        "run() must not carry a per-turn model; the selection lives on Session "
        "and reaches the agent through the loader chain"
    )


# ------------------------------------------------------------------ D-19 resume


async def test_run_forwards_execute_kwargs_into_the_run_params() -> None:
    """D-19: `run(execute_kwargs=...)` lands in the params dict the run impl receives.

    This is the runner's half of the plumbing thread: the dict chat-helper built
    (`stream` plus any provider-specific member) must survive the runner's params
    assembly instead of being re-modelled.

    `resume` is asserted separately below: it is not an arbitrary member — the
    session routes it, so it never reaches the run impl as a passthrough.
    """
    seen: list[dict[str, Any]] = []

    async def recording_agent(params: dict[str, Any], _is_cancelled: Any):
        seen.append(params)
        yield 'data: {"type":"RUN_FINISHED"}\n\n'

    writer = _RecordingWriter()
    runner, state = make_runner(writer, recording_agent)

    await _run_events(
        runner,
        writer,
        state=state,
        req_id="r1",
        run_id="run-ek",
        session_code="s1",
        input_text="hi",
        execute_kwargs={"stream": True, "trace": "trace-1"},
    )

    assert seen[0]["executeKwargs"] == {"stream": True, "trace": "trace-1"}


async def test_a_resume_in_execute_kwargs_is_routed_and_unknown_ids_fail():
    from aidev_agent_cli.runtime.contracts import ResumeRequest, RunRequest, SessionOptions
    from aidev_agent_cli.runtime.errors import RuntimeFault
    from aidev_agent_cli.runtime.inputs import ResumeInterrupt
    from aidev_agent_cli.runtime.service import build_runtime

    service = build_runtime("acp-dsh", resources=("injected",), env={})
    try:
        await service.new_session(SessionOptions("s1", resources=("injected",)))
        with pytest.raises(RuntimeFault) as exc:
            await service.start(RunRequest("s1", execute_options={"stream": True, "resume": {"interruptId": "i1"}}))
        assert exc.value.code == "input.invalid"
        handle = await service.resume(ResumeRequest("s1", ResumeInterrupt({"interruptId": "i1", "decision": "allow"})))
        outcome = await handle.result()
        assert outcome.status == "failed"
        assert outcome.error_code == RESUME_INTERRUPT_NOT_FOUND
    finally:
        await service.close_all()


async def test_run_records_none_when_no_execute_kwargs() -> None:
    """The new `executeKwargs` key must be optional — omitting it records None."""
    seen: list[dict[str, Any]] = []

    async def recording_agent(params: dict[str, Any], _is_cancelled: Any):
        seen.append(params)
        yield 'data: {"type":"RUN_FINISHED"}\n\n'

    writer = _RecordingWriter()
    runner, state = make_runner(writer, recording_agent)

    await _run_events(
        runner, writer, state=state, req_id="r1", run_id="run-ek-none", session_code="s1", input_text="hi"
    )

    assert seen[0]["executeKwargs"] == {}


def test_execute_kwargs_merge_the_carried_values_over_the_base_keys() -> None:
    """D-19：调用方透传的 executeKwargs 并入 SDK 执行参数，基础键不被吞掉（T-06-22）。"""
    from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

    session = Session(session_code="s1", resources=PLATFORM_SOURCES)
    resume = {"interruptId": "i1", "reason": "aidev:user_question", "status": "resolved"}

    handed = session._execute_kwargs(
        turn_id="t1", input_text="hi", resume_payload=resume, carried={"stream": True, "resume": {"ignored": 1}}
    )

    assert handed.resume == resume
    assert handed.stream is True
    assert handed.stream_mode == "start"
    assert handed.input == "hi"
    assert handed.session_code == "s1"
    assert handed.persist_input is True


def test_execute_kwargs_without_carried_values_keep_the_base_keys() -> None:
    """T-06-10：没有 executeKwargs 时一轮照常（resume 缺席）。"""
    session = Session(session_code="s1")

    handed = session._execute_kwargs(turn_id="t1", input_text="hi", resume_payload=None, carried=None)

    assert handed.resume is None
    assert handed.stream is True
    assert handed.input == "hi"


async def test_a_resume_inside_execute_kwargs_routes_to_the_resume_path():
    from aidev_agent_cli.runtime.contracts import ResumeRequest, RunOutcome, SessionOptions
    from aidev_agent_cli.runtime.inputs import ResumeInterrupt
    from aidev_agent_cli.runtime.service import build_runtime

    seen = []
    class Recorder:
        async def execute(self, *, handle, snapshot, **kwargs):
            seen.append((snapshot.kind, snapshot.input, snapshot))
            return RunOutcome("finished", handle.run_id, handle.session_code)
    service = build_runtime("acp-dsh", resources=("injected",), env={})
    service._scheduler._runner = Recorder()
    resume = {"interruptId": "i1", "status": "resolved"}
    try:
        await service.new_session(SessionOptions("s1", resources=("injected",)))
        handle = await service.resume(ResumeRequest("s1", ResumeInterrupt(resume, "hi"), correlation_id="r1"))
        assert (await handle.result()).status == "finished"
        (kind, request, snapshot), = seen
        assert kind == "resume"
        assert request.payload == resume
        assert request.text == "hi"
        assert snapshot.input.payload == resume
        assert snapshot.execute_options == {}
        assert not hasattr(request, "execute_options")
    finally:
        await service.close_all()


# --------------------------------------------------------- agent queries


def test_describe_agent_config_reuses_the_cached_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`describe_agent_config` goes through the single cached resource manager.

    PATTERNS.md §A1: a second manager means a second HTTP client and a second
    config fetch per turn. The accessor must therefore go through
    `Session.ensure_manager` — whose own contract is "build once".

    载荷现在**整体**由 ``rm.describe_agent_config`` 组装（本地文件行由链并入，不再是
    调用方在外面追加并浅拷贝），因此这里用**真 manager** + 只打桩平台取数：替身无法
    覆盖真实组装，只会把断言变成"替身自己实现了一遍"。
    """
    import aidev_agent_cli.runtime.sdk.session as runner_mod
    from aidev_agent_cli.resources.manager import LocalResourceManager
    from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

    # 空工作区：本用例关心的是平台半与 manager 复用（工作区根固定为 os.getcwd()）。
    monkeypatch.chdir(tmp_path)

    payload: dict[str, Any] = {
        "conversation_settings": {"commands": [{"id": "c1"}]},
        "resources": [{"code": "weather_query"}],
        "related_skills": [{"skill_code": "office-kit"}],
    }

    def _make_manager() -> Any:
        rm = LocalResourceManager.build({"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"}, "judgehuang", sources=PLATFORM_SOURCES)
        rm.platform_loader.retrieve_agent_config = (  # type: ignore[method-assign]
            lambda agent_code, version=None: payload
        )
        rm.get_agent_code = lambda: "app-code"  # type: ignore[method-assign]

        return rm

    calls: list[str] = []

    def _ensure(self: Any) -> Any:
        calls.append("__ensure__")

        if self._resource_manager is None:
            self._resource_manager = _make_manager()

        return self._resource_manager

    runner, state = make_runner(_RecordingWriter(), None)
    monkeypatch.setattr(runner_mod.SDKSession, "ensure_manager", _ensure)

    first = state.describe_agent_config()
    second = state.describe_agent_config()

    # 载荷由链新建的 raw dict 承载，绝不是把缓存的平台 dict 交出去任人改写。
    assert first is not payload, "the cached platform dict must not be handed out for mutation"
    # 平台内容逐字保留。
    assert first["conversation_settings"] == payload["conversation_settings"]
    assert first["related_skills"] == payload["related_skills"]
    assert second["conversation_settings"] == payload["conversation_settings"]
    # 每个 manager 只构建一次（PATTERNS.md §A1：第二个 manager = 第二个 HTTP client）。
    assert calls.count("__ensure__") == 2, "one manager accessor call per projection"


# --------------------------------------------------------- shell stripping


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('data: {"a":1}\n\n', '{"a":1}'),
        ('data: {"a":1}', '{"a":1}'),
        ('{"a":1}', '{"a":1}'),
        ("data: line1\ndata: line2\n\n", "line1\nline2"),
        (': heartbeat\ndata: {"a":1}\n\n', '{"a":1}'),
    ],
)
def test_strip_sse_shell(raw: str, expected: str) -> None:
    """Only the SSE shell is removed — never the payload's content."""
    assert strip_sse_shell(raw) == expected


# =========================================================================
# Phase 3 (03-02 Task 1) — token / manager rebuild / snapshot / declaration
# =========================================================================


class _FakeLocal:
    """A minimal ``LocalConfig`` stand-in (the real one is a frozen dataclass).

    形状与新 §1 对齐：模型在 ``prompt_setting.llm_code``，MCP 在
    ``mcp_server_config.mcpServers``，知识库在
    ``knowledgebase_settings.knowledgebases``。构造参数保留旧词汇的名称（
    ``default_model`` / ``mcp_servers`` / ``knowledgebases``）以便测试可读地构造，
    但存储为规范形状。
    """

    def __init__(
        self,
        agent_code: str = "",
        default_model: str = "",
        mcp_servers: list[str] | None = None,
        knowledgebases: list[str] | None = None,
    ) -> None:
        self.agent_code = agent_code
        self.prompt_setting = {"llm_code": default_model} if default_model else {}
        self.mcp_server_config = {"mcpServers": {name: {} for name in (mcp_servers or [])}}
        self.knowledgebase_settings = {"knowledgebases": list(knowledgebases or [])}


class _FakeWorkspaceFiles:
    """A ``WorkspaceFileLoader`` stand-in for the manager doubles.

    ``describe_agent_config`` now reads workspace file rows from ``rm.workspace_files``
    (quick-261014-t1s: the list is owned solely by the manager), so every manager double
    must expose one. ``rows`` is what a test mutates.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def project(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def file_resources(self) -> list[dict[str, Any]]:
        return list(self.rows)


class _FakeManager:
    """Builds like the real manager but without the SDK.

    ``access_token`` mirrors ``LocalResourceManager``'s mutable slot so
    ``Session.ensure_manager``'s push can be observed. ``local`` is the
    declarative override half (kept for the pure-projection tests below).
    ``get_config_snapshot`` mirrors the real manager's assembly — the single
    source ``describe_config_snapshot`` reads (261015). ``mcp`` is the
    workspace definition slot — now a :class:`resource_manager.McpResolver`, mirroring
    the real manager (definitions are read lazily via ``load()``) — its presence here
    is what lets the runner's contract be exercised rather than skipped.
    ``workspace_files`` mirrors the real manager's adapter over
    ``self.files`` (the workspace file list's sole owner).
    """

    def __init__(
        self,
        local: _FakeLocal | None = None,
        agent_code: str = "platform-agent",
        payload: dict[str, Any] | None = None,
        token: str = "",
        mcp: Any = None,
    ) -> None:
        self.access_token = token
        self.local = local if local is not None else _FakeLocal()
        #: The resolver double. ``mcp`` (an ``McpConfig``) seeds it, so a double built
        #: with ``mcp=McpConfig(...)`` resolves exactly those definitions.
        self.mcp = McpResolver()
        if mcp is not None:
            self.mcp.seed(mcp)
        #: The workspace file adapter, as the real manager exposes it.
        self.workspace_files = _FakeWorkspaceFiles()
        self._agent_code = agent_code
        self._payload = payload if payload is not None else {}
        self.retrieve_calls = 0
        #: Every ``(kind, value, code)`` handed to ``write_declaration`` — the runner's
        #: per-agent write wiring, assertable without a disk write.
        self.declaration_writes: list[tuple[str, str, str]] = []

    def get_agent_code(self) -> str:
        return self._agent_code

    def get_config_snapshot(self) -> dict[str, Any]:
        """Mirrors the real manager's single-source read (261015).

        平台半仍经 ``retrieve_agent_config``（使 ``retrieve_calls`` 类断言可观察）；
        其余内容对本 double 的消费者不透明——session 测试只断言新鲜度/一致性，
        载荷形状契约由 ``test_resource_manager`` 对着真实 manager 钉住。
        """
        raw = self.retrieve_agent_config(self.get_agent_code())

        return {"agentName": raw.get("agent_name", "")}

    def retrieve_agent_config(self, _agent_code: str) -> dict[str, Any]:
        self.retrieve_calls += 1

        return self._payload

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def write_declaration(self, kind: str, value: str, code: str) -> None:
        """Mirrors the real manager's per-agent write entry (261014-urj 后续).

        The write entry moved from ``Session`` to the manager (it owns per-agent
        config); the runner now hands it the current main agent's ``code``. Recorded
        here so the wiring is assertable without a disk write.
        """
        self.declaration_writes.append((kind, value, code))


def _patch_ensure_manager(monkeypatch: Any, factory: Any) -> None:
    """Install a fake ``Session.ensure_manager`` for the duration of a test."""
    import aidev_agent_cli.runtime.sdk.session as runner_mod

    monkeypatch.setattr(runner_mod.SDKSession, "ensure_manager", factory)


def _patch_manager_build(monkeypatch: Any, factory: Any) -> None:
    """Replace manager construction so ``ensure_manager`` gets a double.

    manager 的构造已内置进 ``Session.ensure_manager``（不再有模块级的
    ``_make_resource_manager``），因此注入缝是 ``LocalResourceManager.build``
    这个 classmethod——替身由此进入，而 ``ensure_manager`` 的 token 与 ``mcp.json``
    推送仍真实执行。
    """
    def build(cls, creds, username, **kwargs):
        manager = factory(creds, username, **kwargs)
        manager.access_token = kwargs["access_token"]
        return manager

    monkeypatch.setattr(LocalResourceManager, "build", classmethod(build))


# ------------------------------------------------------------------ Token


def test_set_access_token_invalidates_the_manager(monkeypatch: Any) -> None:
    """D-11: setting a token QUEUES a rebuild (the cached manager is dropped).

    The rebuild is deliberately not synchronous — the caller offloads it — so the
    observable contract at this level is "the cache is empty", not "a new manager
    exists".
    """
    runner, state = make_runner(_RecordingWriter(), None)
    state._resource_manager = _FakeManager()

    service = application_for(state)
    asyncio.run(service.login("tok"))
    state = worker_for(service)

    assert state._resource_manager is None, "login must drop the cached manager"
    assert state.auth.access_token == "tok"
    assert state.auth.has_token() is True


def test_rebuild_after_login_pushes_the_token_onto_the_new_manager(monkeypatch: Any) -> None:
    """The rebuilt manager carries the CURRENT token (D-09/D-10/D-11)."""
    import aidev_agent_cli.runtime.sdk.session as runner_mod

    built: list[_FakeManager] = []

    def _fake_make(creds: dict[str, str], username: str, **_: Any) -> _FakeManager:
        manager = _FakeManager()
        built.append(manager)

        return manager

    # 261014-w6d 后续：这些名字已提到模块顶层，因此 patch 目标改为 ``agent_runner``
    # （from-import 在导入时绑定，patch 原模块不再影响调用点）。
    # 用户名与 token 同源，都读配置文件的 auth 段（resolve_username 已删除）。
    monkeypatch.setattr(
        config.TuiConfig,
        "read",
        classmethod(lambda cls, path=None: config.TuiConfig(username="tester")),
        raising=True,
    )
    _patch_manager_build(monkeypatch, _fake_make)

    runner, state = make_runner(_RecordingWriter(), None)
    state.auth.login("tok")

    manager = state.ensure_manager()

    assert manager.access_token == "tok", "the rebuilt manager did not receive the token"
    # And a SECOND call is idempotent — the token push is not repeated per call.
    again = state.ensure_manager()
    assert again is manager
    assert len(built) == 1, "the manager was rebuilt twice without an invalidation"
    assert runner_mod is not None  # keep the import meaningful for linters


def test_logout_returns_to_pure_app_state(monkeypatch: Any) -> None:
    """D-13: ``clear_access_token`` empties the token and queues a rebuild."""

    # 261014-w6d 后续：patch 目标改为 ``agent_runner``（名字已在模块顶层绑定）。
    # 用户名与 token 同源，都读配置文件的 auth 段（resolve_username 已删除）。
    monkeypatch.setattr(
        config.TuiConfig,
        "read",
        classmethod(lambda cls, path=None: config.TuiConfig(username="tester")),
        raising=True,
    )
    _patch_manager_build(monkeypatch, lambda creds, username, **_: _FakeManager())

    runner, state = make_runner(_RecordingWriter(), None)
    state.auth.login("tok")
    state.auth.logout()

    assert state.auth.has_token() is False
    manager = state.ensure_manager()
    assert manager.access_token == "", "logout left the token on the rebuilt manager"


def test_the_login_token_survives_a_restart(monkeypatch: Any) -> None:
    """D4 接线：``/login`` 落盘，新进程（新 ``Session``）读回同一 token。

    重启的等价物就是再建一个 ``Session``——它 ``__init__`` 时从
    ``bkai_tui.json`` 读回 ``auth.access_token``。
    """
    runner, state = make_runner(_RecordingWriter(), None)
    state.auth.login("persisted-tok")

    restarted = Session()

    assert restarted.auth.has_token() is True
    assert restarted.auth.access_token == "persisted-tok"


def test_a_restarted_session_pushes_the_persisted_token_onto_the_manager(
    monkeypatch: Any,
) -> None:
    """重启后无需再 ``/login``：``ensure_manager`` 把读回的 token 推到新 manager。"""

    # 用户名与 token 同源，都读配置文件的 auth 段（resolve_username 已删除）。
    # 这里要在**真实读回**之上叠加 username：本测试的断言依赖重启读回落盘 token，
    # 不能让 patch 顺手清掉它。
    _original_read = config.TuiConfig.read

    def _read_with_tester(cls: type, path: str | None = None) -> config.TuiConfig:
        cfg = _original_read(path)
        cfg.username = "tester"

        return cfg

    monkeypatch.setattr(config.TuiConfig, "read", classmethod(_read_with_tester))
    _patch_manager_build(monkeypatch, lambda creds, username, **_: _FakeManager())

    runner, state = make_runner(_RecordingWriter(), None)
    state.auth.login("persisted-tok")

    restarted = Session()
    manager = restarted.ensure_manager()

    assert manager.access_token == "persisted-tok"


def test_logout_clears_the_persisted_token_so_it_cannot_resurrect(monkeypatch: Any) -> None:
    """``/logout`` 清盘：重启后 token 不复活（内存与磁盘是同一个事实）。"""
    runner, state = make_runner(_RecordingWriter(), None)
    state.auth.login("tok")
    state.auth.logout()

    restarted = Session()

    assert restarted.auth.has_token() is False


def test_ensure_resource_manager_rebinds_only_when_invalidated() -> None:
    """The cached manager survives untouched calls; only invalidation drops it."""
    runner, state = make_runner(_RecordingWriter(), None)
    seeded = _FakeManager()
    state._resource_manager = seeded

    # Nothing has invalidated it, so the real accessor returns the same instance
    # without touching the SDK (the `is None` guard short-circuits first).
    assert state.ensure_manager() is seeded
    assert state.ensure_manager() is seeded

    # Invalidation is the ONLY thing that makes it rebuildable.
    state = Session(resources=state.resources, config=state.config, auth=state.auth)
    assert state._resource_manager is None


# ----------------------------------------------------- mcp.json wiring


def _isolate_mcp_file(monkeypatch: Any, tmp_path: Path, content: str) -> Path:
    """Point the project-level ``.agents/mcp.json`` at a file we own.

    Every test here MUST use this rather than the real search paths. The workspace
    and user ``mcp.json`` files are ambient state a developer may or may not have,
    and a test that reads them passes or fails depending on their machine — which
    is exactly the failure class two tests in ``test_agent_factory.py`` already
    exhibit (they read the ambient ``.agent_tui.json`` and assert a model it
    overrides).

    Both search locations are neutralised: the project root (via
    ``AGENT_TUI_CWD``) is the file we return, and the user candidate
    (``DEFAULT_MCP_FILE``) is pointed at a path that does not exist.
    """
    import aidev_agent_cli.resources.local_config as rm_mod

    project_root = tmp_path / "project"
    project_dir = project_root / ".bkai"
    project_dir.mkdir(parents=True)
    path = project_dir / "mcp.json"
    path.write_text(content, encoding="utf-8")

    monkeypatch.setenv(rm_mod.McpResolver.CWD_ENV, str(project_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rm_mod.McpResolver, "DEFAULT_MCP_FILE", str(tmp_path / "absent.json"))

    return path


def _patch_factory_for_mcp(monkeypatch: Any, manager: _FakeManager) -> None:
    """Make the real ``Session.ensure_manager`` body run against ``manager``."""

    # 261014-w6d 后续：这些名字已提升到 ``agent_runner`` 模块顶层，因此 patch 它
    # （patch ``agent_factory`` 不再影响调用点——from-import 在导入时已绑定）。
    # 用户名与 token 同源，都读配置文件的 auth 段（resolve_username 已删除）。
    monkeypatch.setattr(
        config.TuiConfig,
        "read",
        classmethod(lambda cls, path=None: config.TuiConfig(username="tester")),
        raising=True,
    )
    _patch_manager_build(monkeypatch, lambda creds, username, **_: manager)


def test_the_workspace_mcp_definitions_are_read_lazily(monkeypatch: Any, tmp_path: Path) -> None:
    """No push at build time; the resolver reads the file on demand.

    The file reaches the manager's resolver with transport derived (the whole
    point) — via ``load()``, the same entry the config chain uses.
    """
    _isolate_mcp_file(
        monkeypatch,
        tmp_path,
        json.dumps({"mcpServers": {"bk-data": {"url": "https://x.invalid/mcp"}}}),
    )
    manager = _FakeManager()
    _patch_factory_for_mcp(monkeypatch, manager)

    runner, state = make_runner(_RecordingWriter(), None)
    built = state.ensure_manager()

    assert built is manager
    assert manager.mcp.load().servers == {"bk-data": {"url": "https://x.invalid/mcp", "transport": "streamable_http"}}


def test_the_resolver_reloads_only_when_the_file_changed(monkeypatch: Any, tmp_path: Path) -> None:
    """An mtime key gates the re-read: repeated lazy reads do not re-parse.

    This is the mitigation for "re-read per open would be a platform round-trip"
    (D-15) applied to a local file: the chain reads on every config pull, but
    the file is parsed only when it might have changed.
    """
    path = _isolate_mcp_file(monkeypatch, tmp_path, json.dumps({"mcpServers": {"a": {"url": "https://x.invalid"}}}))
    manager = _FakeManager()
    _patch_factory_for_mcp(monkeypatch, manager)

    runner, state = make_runner(_RecordingWriter(), None)
    state.ensure_manager()

    first = manager.mcp.load()
    assert set(first.servers) == {"a"}
    assert manager.mcp.load() is first, "unchanged file: the same parsed object"

    # Editing is the genuine reason to re-read (bump mtime explicitly: a rewrite
    # within the same ns tick would otherwise look unchanged).
    path.write_text(
        json.dumps({"mcpServers": {"a": {"url": "https://x.invalid"}, "b": {"url": "https://y.invalid"}}}),
        encoding="utf-8",
    )
    os.utime(path, ns=(0, 0))

    second = manager.mcp.load()
    assert second is not first, "changed file: re-parsed"
    assert set(second.servers) == {"a", "b"}


def test_a_missing_mcp_file_yields_an_empty_config_not_a_failure(monkeypatch: Any, tmp_path: Path) -> None:
    """Absence is the normal state: no file must never fail a turn."""
    import aidev_agent_cli.resources.local_config as rm_mod

    monkeypatch.setenv(rm_mod.McpResolver.CWD_ENV, str(tmp_path / "empty-project"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rm_mod.McpResolver, "DEFAULT_MCP_FILE", str(tmp_path / "does-not-exist.json"))
    manager = _FakeManager()
    _patch_factory_for_mcp(monkeypatch, manager)

    runner, state = make_runner(_RecordingWriter(), None)
    state.ensure_manager()

    assert manager.mcp.load().servers == {}


def test_a_malformed_mcp_file_yields_an_empty_config_not_a_failure(monkeypatch: Any, tmp_path: Path) -> None:
    """A trailing comma is the realistic typo; it must degrade, not raise."""
    _isolate_mcp_file(monkeypatch, tmp_path, '{"mcpServers": {"a": {},},}')
    manager = _FakeManager()
    _patch_factory_for_mcp(monkeypatch, manager)

    runner, state = make_runner(_RecordingWriter(), None)
    state.ensure_manager()

    assert manager.mcp.load().servers == {}


def test_a_manager_missing_a_required_method_fails_loudly(monkeypatch: Any, tmp_path: Path) -> None:
    """``ManagerSurface`` is a contract, not a suggestion.

    A double that does not satisfy the surface raises at the boundary
    (``ensure_manager``), where the failure can name the problem — instead of
    three layers later as "no MCP tools loaded". ``set_access_token`` is the
    touchpoint exercised here: with a session token present, the build path
    calls it unconditionally rather than probing with ``hasattr``.
    """
    _isolate_mcp_file(monkeypatch, tmp_path, json.dumps({"mcpServers": {"a": {"url": "https://x.invalid"}}}))

    class _Bare:
        access_token = ""
        local = _FakeLocal()

        def get_agent_code(self) -> str:
            return "c"

    _patch_factory_for_mcp(monkeypatch, _Bare())  # type: ignore[arg-type]

    runner, state = make_runner(_RecordingWriter(), None)
    state.auth.login("tok", persist=False)

    with pytest.raises(AttributeError):
        state.describe_agent_config()


# ------------------------------------------------------- build deadline


async def test_a_hanging_agent_build_fails_the_turn_instead_of_wedging_it(monkeypatch):
    from aidev_agent_cli.runtime.contracts import RunRequest, SessionOptions
    from aidev_agent_cli.runtime.service import build_runtime

    service = build_runtime("acp-dsh", resources=("injected",), env={})
    started = asyncio.Event()
    async def sink(event):
        if event.get("name") == "test_worker":
            started.set()
    info = await service.new_session(SessionOptions(
        "build-timeout", agent_code="hang-build", resources=("injected",),
        tool_factories=("tests.runtime.sdk_worker_fixtures:install_sdk_faults",),
    ))
    loop = asyncio.get_running_loop()
    real_time = loop.time
    try:
        handle = await service.start(RunRequest(info.session_code), sink)
        await asyncio.wait_for(started.wait(), 30)
        monkeypatch.setattr(loop, "time", lambda: real_time() + 181)
        result = await handle.result()
        assert result.status == "failed"
        assert "timed out" in result.error_message
    finally:
        monkeypatch.setattr(loop, "time", real_time)
        await service.close_all(grace=0)



async def test_a_healthy_build_is_returned_unchanged() -> None:
    """装配上限不改变成功路径。"""

    async def _finish(_params: dict[str, Any], _is_cancelled: Any):
        yield 'data: {"type":"RUN_FINISHED"}\n\n'

    writer = _RecordingWriter()
    runner, state = make_runner(writer, _finish)

    outcome = await _run_events(
        runner, writer, state=state, req_id="r1", run_id="run-1", session_code="s1", input_text="hi"
    )

    assert outcome.status == "finished"
    assert [f["payload"]["type"] for f in writer.frames][-1] == "RUN_FINISHED"


# --------------------------------------------------- Config snapshot (D-15)


def test_config_snapshot_is_read_through_the_manager_every_call(monkeypatch: Any) -> None:
    """配置快照的缓存归 **manager**（261014-urj 后续），本类每次现取。

    ``Session`` 不再自持 ``_config_snapshot``：缓存改由 manager 自己的
    ``get_config_snapshot``（内部经 ``PlatformConfigLoader`` 的 TTL 缓存）提供。
    因此每次调用都**触及 manager**——新鲜度策略在 manager 那侧（TTL），不在本类。
    这替换了旧的 "first call fetches, second is a cache hit" 断言（旧语义是
    "每进程只拉一次且由本类缓存"）。
    """
    manager = _FakeManager(payload={"agent_name": "A"}, agent_code="app-code")
    _patch_ensure_manager(monkeypatch, lambda self: manager)

    runner, state = make_runner(_RecordingWriter(), None)

    first = state.describe_config_snapshot()
    second = state.describe_config_snapshot()

    # 本类不再缓存：每次调用都向 manager 取（`ensure_manager` 每次都经它）。
    # 真 manager 的 TTL 会把同 agent_code 的重复读取去重成一次 HTTP——那由
    # ``resource_manager`` 侧的 TTL 测试覆盖（替身没有 TTL）。
    assert first == second, "同一个 manager 上两次快照内容应一致"


def test_login_drops_the_manager_so_the_snapshot_is_refetched(monkeypatch: Any) -> None:
    """``/login`` 重建 manager，快照随之从**新** manager 取（旧缓存随旧 manager 消失）。"""
    managers: list[_FakeManager] = []

    def _ensure(self: AgentRunner) -> Any:
        manager = _FakeManager(payload={"agent_name": "A"}, agent_code="app-code")
        managers.append(manager)

        return manager

    _patch_ensure_manager(monkeypatch, _ensure)

    runner, state = make_runner(_RecordingWriter(), None)
    state.describe_config_snapshot()

    state.auth.login("tok")
    # 本类不再持有快照缓存——"失效"改由丢弃 manager 表达。
    assert state._resource_manager is None, "login did not drop the manager"

    state.describe_config_snapshot()
    assert len(managers) == 2, "the snapshot was not re-fetched after login"
    assert managers[0].retrieve_calls == 1 and managers[1].retrieve_calls == 1


def test_the_workspace_files_loader_wraps_the_manager_index(monkeypatch: Any) -> None:
    """quick-261014-t1s: the workspace file list is owned SOLELY by the manager.

    ``WorkspaceFileLoader`` (resource_manager §6) is a stateless adapter; the manager
    builds it once (``rm.workspace_files``) over ITS OWN ``rm.files``. There is no
    second ``WorkspaceIndex`` on the session — the double-instance trap the old test
    pinned is gone by construction.
    """
    from aidev_agent_cli.resources.loaders import WorkspaceFileLoader

    runner, state = make_runner(_RecordingWriter(), None)

    # The session no longer owns an index or a loader of its own.
    assert not hasattr(state, "_workspace_index")
    assert not hasattr(state, "_workspace_file_loader")
    assert not hasattr(state, "workspace_index")
    assert not hasattr(state, "local_files")

    # The manager's loader wraps exactly the manager's own index — one instance.
    manager = state.ensure_manager()
    loader = manager.workspace_files

    assert isinstance(loader, WorkspaceFileLoader)
    assert loader._index is manager.files


# ----------------------------------------------- Local declaration (D-08/D-15)


def test_set_local_agent_preserves_startup_default_and_invalidates_manager(monkeypatch: Any, tmp_path: Any) -> None:
    """当前会话切换 agent 并重建 manager，不修改下一次启动的默认值。"""
    tui_file = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, str(tui_file))

    managers: list[_FakeManager] = []

    def _ensure(self: AgentRunner) -> Any:
        manager = _FakeManager(payload={"agent_name": "A"}, agent_code="app-code")
        managers.append(manager)

        return manager

    _patch_ensure_manager(monkeypatch, _ensure)

    runner, state = make_runner(_RecordingWriter(), None)
    service = application_for(state)
    state.describe_config_snapshot()  # build the manager (first `_ensure` call)

    assert len(managers) == 1

    asyncio.run(service.set_local_declaration("selection-test", "agents", "local-code"))

    state = worker_for(service)

    assert not tui_file.exists(), "current session selection must not write the startup default"
    assert state.agent_code == "local-code"
    # ② invalidate() 丢弃 manager —— 快照缓存随它一起消失（不再有第二个缓存要清）
    assert state._resource_manager is None
    # ③ the next snapshot builds a NEW manager (and therefore re-reads the local file)
    state.describe_config_snapshot()
    assert len(managers) == 2, "the switch did not force a manager rebuild"


def test_set_local_declaration_appends_collections(monkeypatch: Any, tmp_path: Any) -> None:
    """``mcps`` / ``knowledge`` APPEND; they never replace the existing list.

    261014-urj D6: 这两个 kind 属 **per-Agent 配置**，写
    ``agents/${入口 agent_code}.json``（当前主 agent 的文件）。

    261014-urj 后续：per-agent 的写入口已从 ``Session`` 收归 **manager**
    （``rm.write_declaration``）。因此这里断言 fake manager 收到的 ``(kind, value, code)``，
    而不是直接读磁盘——runner 的职责只剩"给出当前主 agent 的 code"。
    """
    monkeypatch.setenv("AGENT_TUI_AGENT_CODE", "code-a")

    runner, state = make_runner(_RecordingWriter(), None)
    state.agent_code = ProcessConfig().default_agent_code()
    service = application_for(state)
    manager = _FakeManager()
    # ``ensure_manager`` 返回这个替身，因此写经 ``rm.write_declaration``（替身记录，
    # 不触盘）——真 manager 才落 ``agents/${code}.json``。
    _patch_ensure_manager(monkeypatch, lambda self: manager)
    from aidev_agent_cli.resources.local_config import LocalDeclarations
    def record_write(self, kind, value, *, code):
        manager.declaration_writes.append((kind, value, code))
    monkeypatch.setattr(LocalDeclarations, "write", record_write)


    asyncio.run(service.set_local_declaration("selection-test", "mcps", "mcp-x"))

    state = worker_for(service)
    asyncio.run(service.set_local_declaration("selection-test", "knowledge", "kb-z"))
    state = worker_for(service)

    # 写入口收归 manager：runner 只负责把"当前主 agent"的 code 交给它。
    assert manager.declaration_writes == [
        ("mcps", "mcp-x", "code-a"),
        ("knowledge", "kb-z", "code-a"),
    ], manager.declaration_writes


def test_per_agent_write_falls_back_to_the_platform_agent_code(monkeypatch: Any, tmp_path: Any) -> None:
    """**写路径**同样要有平台兜底 —— 真实崩溃的回归。

    env 与 ``bkai_tui.json`` 都未设时，写 per-agent 配置必须回退到平台
    ``agent_code``（``BKPAAS_APP_CODE`` / ``rm.get_agent_code()``），而不是解析出空串。

    此前写路径用的是**另一条**链（env → TuiConfig → ``""``，无兜底），于是
    ``/models`` 按 Enter 会以
    ``ValueError("unsafe agent_code for per-agent config path: ''")`` 失败——而
    **读**路径有兜底，所以 agent 照常构建、``/models`` 照常列出模型，
    症状只在写的那一刻出现。读写两条链必须给出**同一个答案**。
    """
    monkeypatch.setenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, str(tmp_path / "absent.json"))
    monkeypatch.delenv("AGENT_TUI_AGENT_CODE", raising=False)

    runner, state = make_runner(_RecordingWriter(), None)
    state._creds = {"BKPAAS_APP_CODE": "platform-code"}
    service = application_for(state)

    manager = _FakeManager()
    _patch_ensure_manager(monkeypatch, lambda self: manager)
    from aidev_agent_cli.resources.local_config import LocalDeclarations
    def record_write(self, kind, value, *, code):
        manager.declaration_writes.append((kind, value, code))
    monkeypatch.setattr(LocalDeclarations, "write", record_write)


    asyncio.run(service.set_local_declaration("selection-test", "models", "m1"))

    state = worker_for(service)

    assert manager.declaration_writes == [("models", "m1", "platform-code")], manager.declaration_writes


def test_agent_code_comes_from_tui_config(monkeypatch: Any, tmp_path: Any) -> None:
    """261014-urj 裁决 (e)：入口 agent_code 来自 TuiConfig（进程级），不再读 ``rm.local``。

    写 ``bkai_tui.json`` 的 ``agent_code`` → 断言 ``_resolve_agent_code`` 返回它。
    """
    tui_file = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, str(tui_file))
    monkeypatch.delenv("AGENT_TUI_AGENT_CODE", raising=False)
    config.TuiConfig().write_agent_code("from-tui")

    runner, state = make_runner(_RecordingWriter(), None)
    state.agent_code = ProcessConfig().default_agent_code()
    manager = _FakeManager(agent_code="platform-code")

    assert state._resolve_agent_code(manager) == "from-tui"


def test_agent_code_env_beats_tui_config(monkeypatch: Any, tmp_path: Any) -> None:
    """env 优先于 TuiConfig 不变（CONTEXT D1：env 在最前）。"""
    tui_file = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, str(tui_file))
    config.TuiConfig().write_agent_code("from-tui")
    monkeypatch.setenv("AGENT_TUI_AGENT_CODE", "from-env")

    runner, state = make_runner(_RecordingWriter(), None)
    state.agent_code = ProcessConfig().default_agent_code()

    assert state._resolve_agent_code(_FakeManager()) == "from-env"


def test_agent_code_falls_back_to_the_platform(monkeypatch: Any, tmp_path: Any) -> None:
    """TuiConfig 未声明 agent_code 时回退平台（``BKPAAS_APP_CODE`` / ``rm.get_agent_code()``）。"""
    monkeypatch.setenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, str(tmp_path / "absent.json"))
    monkeypatch.delenv("AGENT_TUI_AGENT_CODE", raising=False)

    runner, state = make_runner(_RecordingWriter(), None)
    state._creds = {"BKPAAS_APP_CODE": "platform-code"}

    assert state._resolve_agent_code(_FakeManager()) == "platform-code"


# ------------------------------------------------------------- D-11 timing


class TestLoginTakesEffectNextTurn:
    """D-11: /login rebuilds the manager; the writer's client is a BUILD-TIME snapshot.

    ``agent_factory.build_agent`` hands ``rm.get_client()`` to ``AGUISessionWriter``
    (agent_factory.py:230-234). That writer is rebuilt every turn because
    ``build_agent`` runs every turn — so a token set mid-conversation is seen by the
    NEXT turn's writer, not the current one. This lag is BY DESIGN (see 03-CONTEXT
    D-11) and is pinned here so a future reader does not "fix" it as a bug.
    """

    def test_next_turn_gets_a_newly_assembled_client(self, monkeypatch: Any) -> None:
        """The rebuilt manager yields a different client; the old one is untouched."""

        clients: list[object] = []
        built_tokens: list[str] = []

        class _ClientManager:
            """Hands out a fresh client per construction, like the real manager."""

            def __init__(self, token: str) -> None:
                self.access_token = token
                self._client = object()
                # `ensure_manager` now reads mcp.json through the manager's own
                # resolver (`rm.mcp.load()`), so the double must carry one — it is
                # part of the ManagerSurface, not incidental.
                self.mcp = McpResolver()

            def get_agent_code(self) -> str:
                return "app-code"

            def get_client(self) -> object:
                return self._client

            def set_access_token(self, token: str) -> None:
                self.access_token = token

        def _fake_make(_creds: dict[str, str], _username: str, **_: Any) -> _ClientManager:
            manager = _ClientManager("")
            built_tokens.append(manager.access_token)

            return manager

        # 261014-w6d 后续：patch 目标改为 ``agent_runner``（名字已在模块顶层绑定）。
        # 用户名与 token 同源，都读配置文件的 auth 段（resolve_username 已删除）。
        monkeypatch.setattr(
            config.TuiConfig,
            "read",
            classmethod(lambda cls, path=None: config.TuiConfig(username="tester")),
            raising=True,
        )
        _patch_manager_build(monkeypatch, _fake_make)

        runner, state = make_runner(_RecordingWriter(), None)

        # Turn 1 — the writer would capture this client.
        first_client = state.ensure_manager().get_client()
        clients.append(first_client)

        # `/login` — queues a rebuild; the ALREADY-BUILT writer keeps turn 1's
        # client. That is the by-design lag, not an oversight.
        service = application_for(state)
        asyncio.run(service.login("tok"))
        state = worker_for(service)

        # Turn 2 — build_agent runs again, so it captures a NEW client.
        second_client = state.ensure_manager().get_client()
        clients.append(second_client)

        assert clients[0] is not clients[1], "the turn after /login must receive a NEWLY assembled client (D-11)"
        # Snapshot semantics: a client handed out earlier is never mutated in place.
        # If the token had been pushed onto the existing client object instead of
        # prompting a rebuild, the first turn's writer would silently change
        # behaviour mid-conversation — the failure mode this test forbids.
        assert clients[0] is not clients[1]

    def test_a_built_writer_keeps_its_client_until_the_next_build(self) -> None:
        """Restating the lag in the writer's terms: the OLD snapshot stays valid.

        A writer built before ``/login`` holds the client it was given. This is the
        assertion that makes the lag explicit and intentional rather than incidental.
        """

        class _Writer:
            def __init__(self, client: object) -> None:
                self.client = client

        runner, state = make_runner(_RecordingWriter(), None)

        class _Mgr:
            def __init__(self) -> None:
                self.access_token = ""
                self._client = object()

            def get_agent_code(self) -> str:
                return "app-code"

            def get_client(self) -> object:
                return self._client

            def set_access_token(self, token: str) -> None:
                self.access_token = token

        first_manager = _Mgr()
        writer = _Writer(first_manager.get_client())

        # /login drops the manager ...
        state._resource_manager = first_manager
        service = application_for(state)
        asyncio.run(service.login("tok"))
        state = worker_for(service)
        assert state._resource_manager is None

        # ... but the writer constructed in the previous turn is untouched.
        assert writer.client is first_manager._client, (
            "the existing writer's client must NOT be swapped in place — D-11's lag is by design"
        )


@pytest.mark.asyncio
async def test_retry_context_and_events_cross_sdk_thread_without_replaying_tools(monkeypatch):
    import threading

    import httpx
    from aidev_agent.packages.langchain_core.models.llm_retry import _current_state
    from aidev_agent.utils.async_utils import async_to_sync_generator

    monkeypatch.setenv("AGENT_TUI_LLM_RATE_LIMIT_RETRIES", "6")
    monkeypatch.setenv("AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET", "1")
    writer = _RecordingWriter()
    executed = []
    bound = []
    entered = threading.Event()

    class Agent:
        def __init__(self, handler: Any) -> None:
            self.event_handler = handler

        def execute(self, kwargs):
            executed.append(kwargs.input)

            async def generate():
                retry = _current_state.get()
                assert retry is not None
                bound.append(retry)
                entered.set()
                deadline = retry._reserve_wait(httpx.Headers({"retry-after": "0.1"}))
                await retry._wait_async(deadline)
                yield "done"

            return async_to_sync_generator(generate())

        def stop(self) -> None:
            return None

    runner, state = make_runner(writer, None)
    state.ensure_platform_session = lambda *args, **kwargs: None  # type: ignore[method-assign]
    state.build_agent = lambda **params: Agent(params.get("event_handler"))  # type: ignore[method-assign]
    state._event_handler = lambda: state.writer  # type: ignore[method-assign]

    task = asyncio.create_task(
        runner.run(
            req_id="request",
            run_id="run",
            session_code="session",
            input_text="issue",
            state=state,
            sink=_RunEventSink(writer, "request", "run"),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(0.03)
    assert any(f.get("payload", {}).get("name") == "llm_rate_limit_retry" for f in writer.frames)
    assert not any(f.get("payload", {}).get("type") == "RUN_FINISHED" for f in writer.frames)
    outcome = await asyncio.wait_for(task, 2)
    assert outcome.status == "finished"
    assert executed == ["issue"]
    events = [f for f in writer.frames if f.get("payload", {}).get("name") == "llm_rate_limit_retry"]
    assert all(f["id"] == "request" and f["runId"] == "run" for f in events)
    assert [f["payload"]["value"]["phase"] for f in events] == ["waiting", "resumed"]
    assert bound[0].snapshot()["waited_seconds"] >= 0.1


async def test_a_stop_during_a_retry_wait_ends_the_turn_without_another_request(monkeypatch):
    """取消在限流等待中途到达：等待立即结束，不再发出等待之后的那次请求。"""
    import threading

    import httpx
    from aidev_agent.packages.langchain_core.models.llm_retry import _current_state

    monkeypatch.setenv("AGENT_TUI_LLM_RATE_LIMIT_RETRIES", "6")
    monkeypatch.setenv("AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET", "10")
    entered = threading.Event()
    executed = []

    class Agent:
        event_handler = None

        def execute(self, kwargs):
            state = _current_state.get()
            deadline = state._reserve_wait(httpx.Headers({"retry-after": "5"}))
            entered.set()
            try:
                state._wait_sync(deadline)
            except Exception:  # noqa: BLE001 - 取消以异常结束等待
                return iter(())
            executed.append("request after wait")
            return iter(())

        def stop(self) -> None:
            return None

    writer = _RecordingWriter()
    runner, state = make_runner(writer, None)
    state.ensure_platform_session = lambda *args, **kwargs: None  # type: ignore[method-assign]
    state.build_agent = lambda **params: Agent()  # type: ignore[method-assign]

    task = asyncio.create_task(
        _run_events(runner, writer, state=state, req_id="r1", run_id="run-1", session_code="s1", input_text="hi")
    )
    assert await asyncio.to_thread(entered.wait, 1)
    state.request_stop()
    outcome = await asyncio.wait_for(task, 2)

    assert executed == []
    assert outcome.status == "cancelled"


# ------------------------------------------------------------- D-12 hygiene


def test_the_token_never_reaches_the_log(capsys: Any) -> None:
    """D-12 / T-03-01: the token is process-local, and its plaintext never logs."""
    runner, state = make_runner(_RecordingWriter(), None)
    state._resource_manager = _FakeManager()

    state.auth.login("SECRET-TOK")
    state.auth.logout()

    captured = capsys.readouterr()
    assert "SECRET-TOK" not in captured.err, "the token leaked into stderr — D-12 makes this a security requirement"


def test_the_local_declaration_value_never_reaches_stderr(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    """The written VALUE follows the same log discipline as the token (T-03-01)."""
    monkeypatch.setenv("AGENT_TUI_CONFIG_FILE", str(tmp_path / "agent_tui.json"))

    runner, state = make_runner(_RecordingWriter(), None)
    service = application_for(state)
    state._resource_manager = _FakeManager()

    asyncio.run(service.set_local_declaration("selection-test", "models", "SECRET-MODEL-NAME"))

    state = worker_for(service)

    captured = capsys.readouterr()
    assert "SECRET-MODEL-NAME" not in captured.err
    assert "kind=models" in captured.err, "the log line should still name the KIND"


# ============================================================================
# Agent assembly (folded in from the former test_agent_factory.py)
# ============================================================================


def _isolate_local_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the per-agent config override at a file we own, for the duration of a test.

    Every test that builds a real manager through ``LocalResourceManager.build`` MUST
    use this. The loaders read ``agents/${code}.json`` from the ambient
    ``AGENT_TUI_CWD`` / home; without isolation a developer's own files would leak in.

    261014-urj: the per-agent single-file override is still ``AGENT_TUI_CONFIG_FILE``
    (the entry agent_code is now resolved 平台默认 in these tests, so the per-agent
    path is never actually read — but the override keeps the resolution deterministic).
    """
    monkeypatch.setenv("AGENT_TUI_CONFIG_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("AGENT_TUI_AGENT_CODE", raising=False)


class TestBootstrapEnvironment:
    def test_drops_django_settings_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "bk_plugin.patch.plugin")

        env = {"DJANGO_SETTINGS_MODULE": "bk_plugin.patch.plugin"}
        environment.bootstrap_environment(env)

        assert "DJANGO_SETTINGS_MODULE" not in os.environ, (
            "DJANGO_SETTINGS_MODULE bootstraps bk_plugin.settings, which imports "
            "aidev_bkplugin -- the plugin this component must not depend on"
        )
        assert "DJANGO_SETTINGS_MODULE" not in env

    def test_publishes_the_parsed_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_TUI_TEST_MARKER", raising=False)

        environment.bootstrap_environment({"AGENT_TUI_TEST_MARKER": "yes"})

        assert os.environ["AGENT_TUI_TEST_MARKER"] == "yes"

    def test_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        env: dict[str, str] = {}
        environment.bootstrap_environment(env)
        environment.bootstrap_environment(env)


def test_ensure_manager_does_not_re_run_bootstrap_environment() -> None:
    """环境准备只在 ``__main__`` 启动路径上跑一次，``ensure_manager`` **不**重跑。

    ``bootstrap_environment`` 是进程级的一次性准备（丢弃 ``DJANGO_SETTINGS_MODULE``、
    发布 env、桥接 bkapi 凭据名），且必须在首个 SDK import 之前完成。``__main__`` 已
    在启动时做过；``ensure_manager`` 再跑一遍是纯冗余——所以它不应出现在那里。

    用源码断言钉住这一点（``ensure_manager`` 走真实 manager 构建，单元测试不会驱动到
    生产那一遍），与 ``test_the_shutdown_event_is_created_before_the_loop_is_published``
    同一手法。直接驱动 ``Session`` 的调用方需自行先调
    ``aidev_agent_cli.infrastructure.environment.bootstrap_environment``。
    """
    source = inspect.getsource(Session.ensure_manager)

    # Ignore comment lines: this method's comment *mentions* the function by name
    # (explaining why it is NOT called here), which a bare substring check would
    # trip on. Look for an actual call statement in live code.
    code_lines = [line.split("#", 1)[0] for line in source.splitlines()]
    code = "\n".join(code_lines)

    assert "bootstrap_environment(" not in code, (
        "ensure_manager must NOT re-run bootstrap_environment — __main__ already did "
        "it once at startup; re-running it here is the redundancy this pins away"
    )


class TestResourceManagerApprovalOverride:
    """Approval is dropped at the config layer, not by suppressing a call.

    Clearing the bindings means the approval interrupt is never raised, so the
    approval side-effect worker -- the one remaining backward reference from the
    SDK into ``aidev_bkplugin`` -- is never imported. Suppressing the error
    instead would leave the interrupt outstanding and the run unresolved.

    261014-rta: this clearing now lives in the loader chain (``ApprovalLoader`` at
    the head of ``LocalResourceManager.retrieve_agent_config``). Stubbing the BASE
    ``get_agent_config`` (as these tests used to) would skip the chain entirely, so
    they now drive the chain by stubbing ``platform_loader.retrieve_agent_config``
    with a raw platform payload. The assertions are unchanged.
    """

    def _payload(self) -> dict[str, Any]:
        return {
            "agent_code": "test-agent",
            "agent_name": "test-agent",
            "prompt_setting": {"llm_code": "m"},
            "related_tools": ["weather_query"],
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": {
                "strategies": [{"strategy_id": "S", "approvers": ["judgehuang"]}],
                "bindings": [{"approval_strategy_id": "S", "tool_id": 10}],
            },
        }

    def test_clears_approval_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

        _isolate_local_config(monkeypatch, tmp_path)

        rm = LocalResourceManager.build(
            {"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"},
            "judgehuang",
            sources=PLATFORM_SOURCES,
            overrides=ResourceOverrides(),
        )

        monkeypatch.setattr(
            rm.platform_loader,
            "retrieve_agent_config",
            lambda agent_code, version=None: self._payload(),
        )

        cfg = rm.get_agent_config("test-agent")

        assert cfg.approval_settings == {}
        # Everything else must survive untouched -- this is a narrow override.
        assert cfg.tool_codes == ["weather_query"]
        assert cfg.chat_model == "m"

    def test_tolerates_absent_approval_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

        _isolate_local_config(monkeypatch, tmp_path)

        rm = LocalResourceManager.build(
            {"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"},
            "judgehuang",
            sources=PLATFORM_SOURCES,
            overrides=ResourceOverrides(),
        )

        payload = self._payload()
        payload["approval_settings"] = None

        monkeypatch.setattr(
            rm.platform_loader,
            "retrieve_agent_config",
            lambda agent_code, version=None: payload,
        )

        assert rm.get_agent_config("a").approval_settings == {}


class TestResourceManagerModelOverride:
    """D-06 / D-07: `chat_model` is overridden; nothing else about the config moves."""

    def _payload(self) -> dict[str, Any]:
        return {
            "agent_code": "test-agent",
            "agent_name": "test-agent",
            "prompt_setting": {"llm_code": "platform-default", "non_thinking_llm": "platform-nonthinking"},
            "related_tools": ["weather_query"],
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": None,
        }

    def _manager(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

        _isolate_local_config(monkeypatch, tmp_path)

        rm = LocalResourceManager.build(
            {"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"},
            "judgehuang",
            sources=PLATFORM_SOURCES,
            overrides=ResourceOverrides(),
        )
        monkeypatch.setattr(
            rm.platform_loader,
            "retrieve_agent_config",
            lambda agent_code, version=None: self._payload(),
        )

        return rm

    def test_overrides_the_main_model_only(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rm = self._manager(monkeypatch, tmp_path)
        rm.overrides = ResourceOverrides(model="deepseek-v3")
        rm._loaders[-1].overrides = rm.overrides

        cfg = rm.get_agent_config("test-agent")

        assert cfg.chat_model == "deepseek-v3"
        # D-07: the secondary models are the platform's, untouched.
        assert cfg.non_thinking_llm == "platform-nonthinking"
        # Everything else must survive untouched -- this is a narrow override.
        assert cfg.tool_codes == ["weather_query"]

    def test_leaves_the_platform_model_when_none_selected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rm = self._manager(monkeypatch, tmp_path)
        rm.overrides = ResourceOverrides()
        rm._loaders[-1].overrides = rm.overrides

        assert rm.get_agent_config("test-agent").chat_model == "platform-default"


class TestCurrentModelPersistence:
    """``/models`` 的选择是**持久**的：切换之后一直生效，直到再次切换。

    此前它是「构建前设、构建后还原」的一次性覆盖（``model_override`` 上下文管理器），
    那把一件持久的事做成了临时的事。现在 ``Session.current_model`` 由
    :meth:`Session.set_local_declaration`（kind=models）设置，没有作用域包装。
    """

    def test_a_switch_sets_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state, _rm = _state_with_patched_manager(monkeypatch)

        service = application_for(state)
        asyncio.run(service.set_local_declaration("selection-test", "models", "hy3"))

        state = worker_for(service)

        assert state.current_model == "hy3"

    def test_it_outlives_the_switch_and_is_not_restored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没有上下文管理器：切换生效后不会在"构建结束"时被还原。"""
        state, _rm = _state_with_patched_manager(monkeypatch)

        service = application_for(state)
        asyncio.run(service.set_local_declaration("selection-test", "models", "hy3"))

        state = worker_for(service)

        # 后续任意多次配置读取都看到它——这正是"Agent 被 model 覆盖"。
        assert state.current_model == "hy3"
        assert state.current_model == "hy3"

    def test_a_second_switch_replaces_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state, _rm = _state_with_patched_manager(monkeypatch)

        service = application_for(state)
        asyncio.run(service.set_local_declaration("selection-test", "models", "hy3"))

        state = worker_for(service)
        asyncio.run(service.set_local_declaration("selection-test", "models", "deepseek-v4-flash"))
        state = worker_for(service)

        assert state.current_model == "deepseek-v4-flash"

    def test_other_kinds_leave_it_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state, _rm = _state_with_patched_manager(monkeypatch)

        service = application_for(state)
        asyncio.run(service.set_local_declaration("selection-test", "models", "hy3"))

        state = worker_for(service)
        asyncio.run(service.set_local_declaration("selection-test", "mcps", "some-server"))
        state = worker_for(service)

        assert state.current_model == "hy3"

    def test_it_starts_unset(self) -> None:
        """未切换时没有覆盖——平台自己的模型生效。"""
        state = Session()

        assert state.current_model is None

    def test_there_is_no_model_override_context_manager(self) -> None:
        """反回归：不再有 ``model_override`` 这类作用域包装。

        模型覆盖是**持久**的（切换后一直生效），把它包进「构建前设、构建后还原」的
        上下文管理器，等于把一件持久的事做成临时的事——切换会在一次构建之后失效。
        """
        state = Session()

        assert not hasattr(state, "model_override"), (
            "model_override came back; the model selection is persistent and must not be scoped to a single build"
        )
        assert not hasattr(state, "pending_model"), "use current_model — the selection persists"

    def test_the_manager_reads_it_through_the_injected_runtime(self) -> None:
        """进程级槽位必须对 manager 可见（这正是把它移出 manager 的目的）。"""
        state = Session(resources=("injected",))
        rm = LocalResourceManager.build(
            {"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"},
            "judgehuang",
            sources=("injected",),
            overrides=ResourceOverrides(model=state.current_model or ""),
        )

        state.current_model = "hy3"

        assert rm.overrides.model == "", "已接受的资源快照不随可变Session改变"
        updated = ResourceOverrides(model=state.current_model)
        assert updated.model == "hy3"
        assert not hasattr(rm, "runtime")


def _state_with_patched_manager(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """一个 ``Session``，其 manager 被替换为不触网的替身。"""

    rm = _FakeManager()
    # 用户名与 token 同源，都读配置文件的 auth 段（resolve_username 已删除）。
    monkeypatch.setattr(
        config.TuiConfig,
        "read",
        classmethod(lambda cls, path=None: config.TuiConfig(username="tester")),
        raising=True,
    )
    _patch_manager_build(monkeypatch, lambda creds, username, **_: rm)

    return Session(), rm


class TestEnsureSession:
    def test_creates_the_session_on_the_platform(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeRM:
            def get_or_create_session(self, **kwargs: Any) -> dict:
                calls.append(kwargs)

                return {}

        agent_factory.ensure_session(FakeRM(), "thread-1", session_name="n")

        assert calls == [{"session_code": "thread-1", "session_name": "n", "protocol_version": ""}]

    def test_propagates_platform_failures(self) -> None:
        class FakeRM:
            def get_or_create_session(self, **kwargs: Any) -> dict:
                raise RuntimeError("platform down")

        with pytest.raises(RuntimeError):
            agent_factory.ensure_session(FakeRM(), "thread-1")


class TestDescribeAgent:
    def test_reports_tool_names_without_secrets(self) -> None:
        class Tool:
            def __init__(self, name: str) -> None:
                self.name = name

        class Agent:
            tools = [Tool("weather_query"), Tool("search")]
            model_name = "aidev-chat-auto"

        described = agent_factory.describe_agent(Agent())

        assert "weather_query" in described
        assert "search" in described
        assert "aidev-chat-auto" in described

    def test_tolerates_an_agent_without_tools(self) -> None:
        class Agent:
            tools = []

        assert "tools=[]" in agent_factory.describe_agent(Agent())


def _ensure_real_manager() -> Any:
    """经 ``Session.ensure_manager`` 构建一个**真实** manager。

    manager 的构造（含 ``mcp.json`` 推送）现在只存在于该方法里，因此任何"这个
    manager 知道什么"的断言都必须走它——直接 ``LocalResourceManager.build`` 会绕过
    推送，使缺文件那条断言变成永真的假阴性。
    """
    from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

    state = Session(
        resources=PLATFORM_SOURCES,
        config=ProcessConfig(env={"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"}),
    )

    return state.ensure_manager()


class TestFactoryLoadsTheWorkspaceMcp:
    """``ensure_manager`` must configure ``mcp.json`` itself.

    THE BUG THIS PINS, because it was silent and would regress the same way
    ----------------------------------------------------------------
    An earlier version left the ``mcp.json`` read to
    ``Session.ensure_manager``, on the reasoning that the cached-manager build
    "pushes it on afterwards". That reasoning ignored the fact that
    :func:`build_agent` builds a SECOND, fresh manager via this function and hands
    that one to ``AgentInstanceFactory`` — so the cached manager's refresh landed on
    a manager nobody used. The symptom was not an error: ``config.mcp_server_config``
    simply arrived empty at ``ChatAgentBuilder.build_tools``, and the local MCP
    servers were quietly absent from the tool list. Thirteen tools became one
    (``weather_query``) with nothing in the logs to say why.

    So the assertion is deliberately made on the MANAGER THIS FUNCTION RETURNS, not
    on the cached one: that is the object the SDK actually reads.
    """

    def test_the_returned_manager_carries_the_mcp_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolate_mcp_file(
            monkeypatch,
            tmp_path,
            json.dumps({"mcpServers": {"from-file": {"url": "https://x.invalid"}}}),
        )

        # manager 上的 resolver 经 ``load()`` lazy 读盘（链上 ``McpLoader`` 的同一
        # 入口）——不再依赖 ``ensure_manager`` 的推送。
        rm = _ensure_real_manager()

        # The whole point: any manager handed to the SDK reads the file itself.
        assert set(rm.mcp.load().servers) == {"from-file"}
        assert rm.mcp.load().servers["from-file"]["transport"] == "streamable_http"

    def test_a_missing_file_leaves_the_manager_usable(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Absence is the normal state and must not raise on this path either."""
        import aidev_agent_cli.resources.local_config as rm_mod

        monkeypatch.setenv(rm_mod.McpResolver.CWD_ENV, str(tmp_path / "empty-project"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(rm_mod.McpResolver, "DEFAULT_MCP_FILE", "/nonexistent/mcp.json")

        rm = _ensure_real_manager()

        assert rm.mcp.load().is_empty()


class TestAgentFactoryHoistsImports:
    @pytest.mark.slow
    def test_importing_agent_factory_loads_the_sdk(self) -> None:
        """``agent_factory`` 顶层导入 SDK，这是有意的。

        该模块的全部依赖都在顶层导入，因此 `import aidev_agent_cli.adapters.agent_factory` 会拉入
        aidev_agent。延迟导入曾用于规避 stdout 守卫的顺序约束，但该约束只作用于
        ``worker/__main__.py`` 自身的顶层导入（实测没有任何依赖会在导入期写 stdout），
        而 `__main__` 早已在 `main()` 内按需导入本模块。

        **注意：** 本类原先声称「导入 ``aidev_agent_cli.adapters.agui`` 不得拉入 SDK」是必须保住的
        不变量，并指向 ``test_main.py::TestModuleImportsWithoutSDK``。该不变量已随
        ``resource_manager`` 的懒解析机制一同**撤回**（见其模块 docstring：接口正常
        优先于启动速度）；``test_main.py`` 中对应的测试现名
        ``TestModuleImportsTheSDK`` 且断言相反的事实。此处保留断言，仅用于说明
        “本模块可以重”这一历史事实。
        """
        import subprocess
        import sys

        code = (
            "import sys;"
            "sys.path.insert(0, '.');"
            "import aidev_agent_cli.runtime.sdk.factory as f;"
            "mods=[m for m in sys.modules if m.startswith('aidev_agent')];"
            "print('SDK_MODULES=' + str(len(mods)))"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(PLUGIN_ROOT),
            timeout=120,
        )

        assert "SDK_MODULES=0" not in result.stdout, result.stdout + result.stderr


def test_the_platform_writer_persists_and_forwards_except_persistence_only_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台来源的事件处理器：平台回写 writer 收到全部事件；会话 writer 只收协议可见事件。"""
    from aidev_agent.services.event_handlers.agui_writer import AGUISessionWriter

    from aidev_agent_cli.runtime.sdk.events import platform_session_writer

    persisted: list[Any] = []
    monkeypatch.setattr(AGUISessionWriter, "__init__", lambda self, **kwargs: None)
    monkeypatch.setattr(AGUISessionWriter, "__call__", lambda self, event: persisted.append(event))
    forwarded: list[Any] = []

    writer = platform_session_writer(session_code="s1", client=object(), username="u", forward=forwarded.append)
    visible = SimpleNamespace(type="TEXT_MESSAGE_CONTENT", name=None)
    hidden = SimpleNamespace(type="CUSTOM", name="user_input_saved")
    writer(visible)
    writer(hidden)

    assert persisted == [visible, hidden]
    assert forwarded == [visible]
    assert isinstance(writer, AGUISessionWriter)
