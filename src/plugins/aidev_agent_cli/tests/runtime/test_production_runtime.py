"""Production admission -> FIFO -> SDK snapshot -> spawned worker acceptance."""

import asyncio
import multiprocessing
import os
from dataclasses import replace

import pytest
from aidev_agent_cli.runtime.auth import AuthManager
from aidev_agent_cli.runtime.contracts import RunRequest, SessionOptions
from aidev_agent_cli.runtime.history import LocalHistory
from aidev_agent_cli.runtime.inputs import ChatInput, ResumeInterrupt
from aidev_agent_cli.runtime.service import build_runtime

FACTORIES = ("tests.runtime.sdk_worker_fixtures:install_sdk_faults",)


@pytest.fixture
def isolated_runtime_files(monkeypatch, tmp_path):
    monkeypatch.setenv("BKAI_TUI_HISTORY_FILE", str(tmp_path / "history.sqlite"))
    monkeypatch.setenv("BKAI_TUI_CHECKPOINT_FILE", str(tmp_path / "checkpoint.sqlite"))


def runtime():
    return build_runtime("acp-dsh", resources=("injected",), auth=AuthManager(access_token="", username="test"))


async def register(service, agent="ok"):
    return await service.new_session(
        SessionOptions(
            session_code="same-session",
            agent_code=agent,
            resources=("injected",),
            tool_factories=FACTORIES,
        )
    )


@pytest.mark.parametrize("stage", ["hang-build", "hang-execute"])
async def test_production_force_kill_join_then_next_turn(stage, isolated_runtime_files):
    service = runtime()
    info = await register(service, stage)
    started = asyncio.Event()
    pids = []

    async def sink(event):
        if event.get("name") == "test_worker":
            pids.append(event["value"]["pid"])
            started.set()

    try:
        first = await service.start(RunRequest(session_code=info.session_code, input=ChatInput("first")), sink)
        await asyncio.wait_for(started.wait(), 30)
        await service.cancel(info.session_code, first.run_id)
        result = await asyncio.wait_for(first.result(), 10)
        assert result.status == "cancelled"
        assert pids[0] not in {child.pid for child in multiprocessing.active_children()}
        with pytest.raises(ProcessLookupError):
            os.kill(pids[0], 0)
        await service.set_local_declaration(info.session_code, "agents", "ok")
        second = await service.start(RunRequest(session_code=info.session_code, input=ChatInput("next")), sink)
        assert (await asyncio.wait_for(second.result(), 30)).status == "finished"
        assert len(pids) == 2
        assert pids[0] != pids[1]
    finally:
        await service.close_all(grace=0)


async def test_fifo_three_and_queued_cancel_without_build(isolated_runtime_files):
    service = runtime()
    info = await register(service)
    events = []

    def sink_for(number):
        async def sink(event):
            if event.get("name") == "test_worker":
                events.append(number)

        return sink

    try:
        handles = [
            await service.start(RunRequest(session_code=info.session_code, input=ChatInput(str(i))), sink_for(i))
            for i in range(3)
        ]
        await service.cancel(info.session_code, handles[1].run_id)
        results = await asyncio.wait_for(asyncio.gather(*(h.result() for h in handles)), 60)
        assert [r.status for r in results] == ["finished", "cancelled", "finished"]
        assert events == [0, 2]
    finally:
        await service.close_all(grace=0)


async def test_local_history_survives_runtime_restart(isolated_runtime_files):
    service = runtime()
    info = await register(service)
    handle = await service.start(RunRequest(session_code=info.session_code, input=ChatInput("before restart")))
    assert (await asyncio.wait_for(handle.result(), 30)).ok
    await service.close_all(grace=0)
    restarted = runtime()
    try:
        info = await register(restarted)
        handle = await restarted.start(RunRequest(session_code=info.session_code, input=ChatInput("after restart")))
        assert (await asyncio.wait_for(handle.result(), 30)).ok
        history = LocalHistory()
        try:
            state = history.load(info.session_code)
            assert [record["content"] for record in state["records"] if record["role"] == "user"] == [
                "before restart",
                "after restart",
            ]
            assert state["turn_count"] == 2
        finally:
            history.close()
    finally:
        await restarted.close_all(grace=0)


async def test_closing_gate_and_no_child_leaks(isolated_runtime_files):
    service = runtime()
    info = await register(service, "hang-execute")
    started = asyncio.Event()
    pid = None

    async def sink(event):
        nonlocal pid
        if event.get("name") == "test_worker":
            pid = event["value"]["pid"]
            started.set()

    handle = await service.start(RunRequest(session_code=info.session_code), sink)
    await asyncio.wait_for(started.wait(), 30)
    shutdown = asyncio.create_task(service.close_all(grace=0))
    await asyncio.sleep(0)
    from aidev_agent_cli.runtime.errors import RuntimeFault

    with pytest.raises(RuntimeFault) as exc:
        await service.start(RunRequest(session_code=info.session_code))
    assert exc.value.code == "runtime.closing"
    with pytest.raises(RuntimeFault) as exc:
        await register(service)
    assert exc.value.code == "runtime.closing"
    await asyncio.wait_for(asyncio.gather(shutdown, service.close_all(grace=0)), 10)
    assert (await handle.result()).status == "cancelled"
    assert pid not in {child.pid for child in multiprocessing.active_children()}


async def test_sdk_run_error_is_failed_not_success(isolated_runtime_files):
    service = runtime()
    info = await register(service, "error")
    try:
        handle = await service.start(RunRequest(session_code=info.session_code))
        assert (await asyncio.wait_for(handle.result(), 30)).status == "failed"
    finally:
        await service.close_all(grace=0)


async def test_pending_interrupt_and_hook_memo_restore_after_restart(isolated_runtime_files):
    from aidev_agent_cli.runtime.contracts import ResumeRequest
    from aidev_agent_cli.runtime.sdk.hooks import HookConfig

    hooks = HookConfig(points=frozenset())

    async def hook_handler(request, token):
        from aidev_agent_cli.runtime.models import HookReply

        return HookReply("reply", {"ok": True, "result": {}})

    service = runtime()
    info = await service.new_session(
        SessionOptions(
            session_code="pending-session",
            agent_code="interrupt",
            resources=("injected",),
            tool_factories=FACTORIES,
        )
    )
    handle = await service.start(RunRequest(session_code=info.session_code), hooks=hooks, hook_handler=hook_handler)
    outcome = await asyncio.wait_for(handle.result(), 30)
    assert outcome.status == "interrupted", outcome
    await service.close_all(grace=0)
    history = LocalHistory()
    try:
        state = history.load(info.session_code)
        assert state["interrupts"][0]["id"] == "pending-1"
        assert state["hook_memo"] == {"memo": "saved"}
    finally:
        history.close()
    restarted = runtime()
    try:
        await restarted.new_session(
            SessionOptions(
                session_code=info.session_code,
                agent_code="ok",
                resources=("injected",),
                tool_factories=FACTORIES,
            )
        )
        handle = await restarted.resume(
            ResumeRequest(
                session_code=info.session_code,
                input=ResumeInterrupt({"interruptId": "pending-1", "decision": "allow"}, ""),
            )
        )
        assert (await asyncio.wait_for(handle.result(), 30)).status == "finished"
        history = LocalHistory()
        try:
            assert history.load(info.session_code)["interrupts"] == []
        finally:
            history.close()
    finally:
        await restarted.close_all(grace=0)


async def test_session_start_hook_round_trip_uses_ipc(isolated_runtime_files):
    from aidev_agent_cli.runtime.models import HookReply
    from aidev_agent_cli.runtime.sdk.hooks import HookConfig

    requests = []

    async def hook_handler(request, token):
        requests.append({"id": request.line_id, "params": request.params})
        return HookReply("reply", {"ok": True, "result": {"context": "from host"}})

    service = runtime()
    try:
        info = await service.new_session(
            SessionOptions(
                session_code="hook-session",
                agent_code="ok",
                resources=("injected",),
                tool_factories=FACTORIES,
            )
        )
        handle = await service.start(
            RunRequest(session_code=info.session_code),
            hooks=HookConfig(points=frozenset({"session_start"})),
            hook_handler=hook_handler,
        )
        assert (await asyncio.wait_for(handle.result(), 30)).status == "finished"
        assert len(requests) == 1
        assert requests[0]["params"]["point"] == "session_start"

    finally:
        await service.close_all(grace=0)


@pytest.mark.parametrize("protocol", ["agui", "acp", "acp-dsh"])
async def test_three_adapters_use_production_process_worker(protocol, isolated_runtime_files):
    from aidev_agent_cli.adapters.acp import AcpAdapter
    from aidev_agent_cli.adapters.acp_dsh import AcpDshAdapter
    from aidev_agent_cli.adapters.agui import Dispatcher, FrameWriter

    service = build_runtime(protocol, resources=("injected",), auth=AuthManager(access_token="", username="test"))

    class Connection:
        def __init__(self):
            self.updates = []

        async def session_update(self, session_id, update):
            self.updates.append((session_id, update))

    class Writer(FrameWriter):
        def __init__(self):
            self.frames = []

        async def send(self, frame):
            self.frames.append(frame)
            return True

    try:
        if protocol == "agui":
            info = await register(service)
            writer = Writer()
            dispatcher = Dispatcher(writer, service)
            await dispatcher.handle("session.start", {"sessionCode": info.session_code, "input": "hello"}, "req")
            run_id = writer.frames[0]["runId"]
            handle = service.run_handles()[info.session_code][run_id]
            assert (await asyncio.wait_for(handle.result(), 30)).ok
            assert any(frame.get("payload", {}).get("delta") == "sdk worker" for frame in writer.frames)
        else:
            conn = Connection()
            adapter = (
                AcpAdapter(service=service) if protocol == "acp" else AcpDshAdapter(service, tool_factories=FACTORIES)
            )
            adapter.on_connect(conn)
            original_new = service.new_session

            async def new_with_faults(options):
                return await original_new(replace(options, tool_factories=FACTORIES))

            service.new_session = new_with_faults
            created = await asyncio.wait_for(adapter.new_session(cwd="", aidev={"agent": "ok"}), 30)
            response = await asyncio.wait_for(
                adapter.prompt(created.session_id, [{"type": "text", "text": "hello"}]),
                30,
            )
            assert response.stop_reason == "end_turn"
            assert any(
                getattr(getattr(update, "content", None), "text", None) == "sdk worker" for _, update in conn.updates
            )
    finally:
        await service.close_all(grace=0)


@pytest.mark.parametrize("stage", ["build", "execute"])
async def test_real_sdk_force_kill_then_continue(stage, isolated_runtime_files, monkeypatch, tmp_path):
    from tests.integration._fake_llm import FakeLLMServer

    marker = tmp_path / "hang.pid"
    monkeypatch.setenv("RUNTIME_HANG_MARKER", str(marker))
    calls = [[{"name": "hanging_tool", "args": {"q": "hang"}, "id": "hang-call"}]] if stage == "execute" else []
    with FakeLLMServer(["next", "next"], tool_calls=calls) as llm:
        service = runtime()
        factory = "hanging_build_factory" if stage == "build" else "real_tool_factory"
        info = await service.new_session(
            SessionOptions(
                session_code="real-sdk-session",
                agent_code="agent-hook",
                resources=("injected",),
                model="fake-model",
                base_url=llm.base_url,
                tool_factories=(f"tests.runtime.sdk_worker_fixtures:{factory}",),
            )
        )
        try:
            handle = await service.start(RunRequest(session_code=info.session_code, input=ChatInput("use tool")))

            async def wait_for_hang():
                while not marker.exists():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_hang(), 30)
            pid = int(marker.read_text())
            await service.cancel(info.session_code, handle.run_id)
            assert (await asyncio.wait_for(handle.result(), 15)).status == "cancelled"
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            next_handle = await service.start(RunRequest(session_code=info.session_code, input=ChatInput("continue")))
            result = await asyncio.wait_for(next_handle.result(), 30)
            assert result.status == "finished", result
        finally:
            await service.close_all(grace=0)


async def test_cleanup_crash_never_publishes_success_terminal(isolated_runtime_files):
    service = runtime()
    info = await register(service, "crash-close")
    events = []

    async def sink(event):
        events.append(event)

    try:
        handle = await service.start(RunRequest(session_code=info.session_code), sink)
        assert (await asyncio.wait_for(handle.result(), 30)).status == "failed"
        assert [event["type"] for event in events if event["type"] in ("RUN_ERROR", "RUN_FINISHED")] == ["RUN_ERROR"]
    finally:
        await service.close_all(grace=0)


async def test_configuration_is_rejected_while_run_is_queued(isolated_runtime_files):
    from aidev_agent_cli.runtime.errors import RuntimeFault

    service = runtime()
    info = await register(service)
    try:
        handle = await service.start(RunRequest(session_code=info.session_code))
        with pytest.raises(RuntimeFault, match="运行期间不能修改配置"):
            await service.set_local_declaration(info.session_code, "models", "other")
        await asyncio.wait_for(handle.result(), 30)
    finally:
        await service.close_all(grace=0)


async def test_three_fifo_runs_complete_in_submission_order(isolated_runtime_files):
    service = runtime()
    info = await register(service)
    order = []

    def numbered_sink(number):
        async def sink(event):
            if event.get("name") == "test_worker":
                order.append(number)

        return sink

    try:
        handles = [await service.start(RunRequest(session_code=info.session_code), numbered_sink(i)) for i in range(3)]
        outcomes = await asyncio.wait_for(asyncio.gather(*(handle.result() for handle in handles)), 45)
        assert all(outcome.ok for outcome in outcomes)
        assert order == [0, 1, 2]
    finally:
        await service.close_all(grace=0)


@pytest.mark.parametrize("agent", ["ok", "error", "hang-execute"])
async def test_terminal_sink_is_bounded_and_next_turn_succeeds(agent, isolated_runtime_files, monkeypatch):
    monkeypatch.setattr("aidev_agent_cli.runtime.runner.TERMINAL_DELIVERY_SECONDS", 0.05)
    service = runtime()
    info = await register(service, agent)
    started = asyncio.Event()
    terminal_started = asyncio.Event()

    async def sink(event):
        if event.get("name") == "test_worker":
            started.set()
        if event.get("type") in ("RUN_ERROR", "RUN_FINISHED"):
            terminal_started.set()
            await asyncio.Event().wait()

    try:
        handle = await service.start(RunRequest(session_code=info.session_code), sink)
        await asyncio.wait_for(started.wait(), 30)
        if agent == "hang-execute":
            await service.cancel(info.session_code, handle.run_id)
        outcome = await asyncio.wait_for(handle.result(), 10)
        assert terminal_started.is_set()
        assert outcome.status == "failed"
        assert outcome.error_code == "transport.delivery_failed"
        assert (
            outcome.data["execution_status"]
            == {"ok": "finished", "error": "failed", "hang-execute": "cancelled"}[agent]
        )
        await service.set_local_declaration(info.session_code, "agents", "ok")
        next_handle = await service.start(RunRequest(session_code=info.session_code))
        assert (await asyncio.wait_for(next_handle.result(), 30)).ok
    finally:
        await asyncio.wait_for(service.close_all(grace=0), 5)


@pytest.mark.parametrize("crash_close", [True, False])
async def test_resume_history_commit_requires_successful_close(crash_close, isolated_runtime_files):
    from aidev_agent_cli.runtime.contracts import ResumeRequest
    from aidev_agent_cli.runtime.hook_contract import HOOK_ASK_REASON
    from aidev_agent_cli.runtime.models import HookReply
    from aidev_agent_cli.runtime.sdk.hooks import HookConfig

    seed = {
        "records": [],
        "interrupts": [{"id": "pending-1", "reason": HOOK_ASK_REASON}],
        "turn_count": 2,
        "hook_memo": {"memo": "saved"},
        "hook_sequence": 3,
    }
    history = LocalHistory()
    history.save("same-session", seed)
    history.close()
    service = runtime()
    info = await register(service, "crash-close" if crash_close else "ok")

    async def hook(request, token):
        return HookReply("reply", {"ok": True, "result": {}})

    try:
        handle = await service.resume(
            ResumeRequest(
                session_code=info.session_code,
                input=ResumeInterrupt({"interruptId": "pending-1", "decision": "allow"}, ""),
            ),
            hooks=HookConfig(points=frozenset()),
            hook_handler=hook,
        )
        outcome = await asyncio.wait_for(handle.result(), 30)
        assert outcome.status == ("failed" if crash_close else "finished")
        history = LocalHistory()
        try:
            state = history.load(info.session_code)
            if crash_close:
                assert state == seed
                assert service._sessions.get(info.session_code).pending == seed["interrupts"]
            else:
                assert state["interrupts"] == []
                assert state["hook_memo"] == {}
                assert state["turn_count"] == 3
        finally:
            history.close()
    finally:
        await service.close_all(grace=0)


async def test_snapshot_identity_revision_workspace_and_session_only_agent(monkeypatch, tmp_path):
    from aidev_agent_cli.infrastructure.settings import TuiConfig

    current = {"username": "first"}
    monkeypatch.setattr(
        TuiConfig,
        "read",
        lambda: type(
            "Config", (), {"username": current["username"], "access_token": "", "env": {}, "agent_code": "default"}
        )(),
    )
    root = os.getcwd()
    service = build_runtime("acp-dsh", resources=("injected",), auth=AuthManager(access_token=""))
    info = await register(service)
    session = service._sessions.get(info.session_code)
    request = RunRequest(session_code=info.session_code)
    first = service._snapshot("start", session, request, None)
    current["username"] = "second"
    monkeypatch.chdir(tmp_path)
    second = service._snapshot("start", session, request, None)
    assert second.session.settings.cwd == root
    assert first.session.identity.username == "first"
    assert second.session.identity.username == "second"
    assert second.session.identity.revision == first.session.identity.revision + 1
    assert service._snapshot("start", session, request, None).session.identity == second.session.identity
    monkeypatch.setattr(service._config, "write_agent_code", lambda value: pytest.fail("startup default changed"))
    await service.set_local_declaration(info.session_code, "agents", "different")
    assert session.options.agent_code == "different"
    assert await service.default_agent_code() == "default"
    await service.close_all()
