"""Tests for :mod:`worker.dispatch` — routing, D-06 concurrency, EOF shutdown."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

from typing import Any


from aidev_agent.headless import frame_protocol as writer_mod
from aidev_agent.headless.dispatch import Dispatcher

AGENT_TUI_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The worker's OWN venv (`agent_tui/.venv`), not the repository root's. This
# subproject manages its Python independently — see `agent_tui/pyproject.toml`.
VENV_PYTHON = sys.executable


class _RecordingWriter:
    """Captures every frame sent, preserving order."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True

    async def close(self, **_kwargs: Any) -> None:
        self.closed = True


def _install_stub(dispatcher: Dispatcher, impl: Any) -> None:
    """把两参替身装到 ``runner._run``（三参单一签名：params, is_cancelled, state）。"""
    async def _run(params: dict[str, Any], is_cancelled: Any, _state: Any) -> Any:
        async for chunk in impl(params, is_cancelled):
            yield chunk

    dispatcher.runner._run = _run


async def _slow_run(params: dict[str, Any], is_cancelled):
    """A run that cooperatively polls the cancel flag for a while."""
    for i in range(200):
        if is_cancelled():
            yield json.dumps({"type": "RUN_FINISHED", "reason": "cancelled"})
            return
        await asyncio.sleep(0.01)
        yield json.dumps({"type": "TEXT_MESSAGE_CONTENT", "delta": str(i)})


async def test_chat_start_returns_immediately() -> None:
    """D-06: handle() must return before the run completes."""
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, _slow_run)

    await dispatcher.handle("session.start", {"input": "hi"}, "req-1")

    # The very first frame is `started` and carries a runId.
    assert writer.frames, "expected at least the started frame"
    first = writer.frames[0]
    assert first["kind"] == "started"
    assert first["id"] == "req-1"
    run_id = first["runId"]
    assert run_id.startswith("run-")

    # The run task is registered and still pending — handle() did not await it.
    task = dispatcher.runs[run_id]
    assert not task.done(), "session.start must not block until the run finishes"

    # Clean up.
    await dispatcher.shutdown()


async def test_cancel_sets_flag_and_cancels() -> None:
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, _slow_run)

    await dispatcher.handle("session.start", {"input": "hi"}, "req-1")
    run_id = writer.frames[0]["runId"]
    task = dispatcher.runs[run_id]
    assert not task.done()

    await dispatcher.handle("session.cancel", {"runId": run_id}, "req-2")

    assert run_id in dispatcher.cancelled

    # The cancel result frame is emitted.
    result_frames = [f for f in writer.frames if f["kind"] == "result"]
    assert any(f["payload"].get("cancelled") is True for f in result_frames)

    # The task settles promptly.
    for _ in range(100):
        if task.done():
            break
        await asyncio.sleep(0.01)
    assert task.done()

    await dispatcher.shutdown()


async def test_unknown_method_errors() -> None:
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)

    await dispatcher.handle("definitely.not.a.method", {}, "req-9")

    assert len(writer.frames) == 1
    err = writer.frames[0]
    assert err["kind"] == "error"
    assert err["error"]["code"] == "UNKNOWN_METHOD"
    assert err["id"] == "req-9"


async def test_cancel_unknown_run_reports_not_cancelled() -> None:
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)

    await dispatcher.handle("session.cancel", {"runId": "nope"}, "req-3")

    assert writer.frames[0]["kind"] == "result"
    assert writer.frames[0]["payload"]["cancelled"] is False


async def test_agent_info_returns_the_platform_payload() -> None:
    """agent.info answers with a single `result` frame carrying the raw config."""
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, _slow_run)

    class _StubState:
        def describe_agent_config(self) -> dict[str, Any]:
            return {
                "conversation_settings": {"commands": [{"id": "c1", "name": "cmd"}]},
                "resources": [{"code": "weather_query", "name": "天气查询", "type": "tool"}],
                "related_skills": [{"skill_code": "office-kit", "skill_name": "office-kit"}],
            }

    dispatcher.state = _StubState()  # type: ignore[assignment]

    await dispatcher.handle("agent.info", {}, "req-a")

    assert len(writer.frames) == 1
    frame = writer.frames[0]
    assert frame["kind"] == "result"
    assert frame["id"] == "req-a"
    assert frame["payload"]["agentInfo"]["resources"][0]["code"] == "weather_query"
    # T-02-02: the payload key set is exactly the one documented key.
    assert set(frame["payload"]) == {"agentInfo"}


async def test_agent_llms_returns_the_platform_list() -> None:
    """agent.llms answers with a single `result` frame wrapping the model list."""
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, _slow_run)

    class _StubState:
        def list_llms(self) -> list[dict[str, Any]]:
            return [{"llm_code": "m1", "llm_name": "M1"}]

    dispatcher.state = _StubState()  # type: ignore[assignment]

    await dispatcher.handle("agent.llms", {}, "req-b")

    assert len(writer.frames) == 1
    frame = writer.frames[0]
    assert frame["kind"] == "result"
    assert frame["id"] == "req-b"
    assert frame["payload"]["items"] == [{"llm_code": "m1", "llm_name": "M1"}]
    assert set(frame["payload"]) == {"items"}


async def test_unknown_method_still_errors() -> None:
    """T-02-01: adding the agent.* branch must not open the fallthrough."""
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)

    await dispatcher.handle("agent.definitely.not.a.method", {}, "req-x")

    assert len(writer.frames) == 1
    assert writer.frames[0]["kind"] == "error"
    assert writer.frames[0]["error"]["code"] == "UNKNOWN_METHOD"


# ============================================================================
# Phase 6 (06-03, D-19) — chat-helper's execute_kwargs threading
# ============================================================================


async def test_chat_start_forwards_execute_kwargs_to_the_runner() -> None:
    """D-19: `params.executeKwargs` must reach the run impl — the SAME defect shape
    as the `model` bug documented above `_run` (the frontend always sent it and this
    function always discarded it).

    The dict is forwarded verbatim; `resume` is a MEMBER of it (never a sibling of
    `input`/`model`), which is what carries a UserQuestion answer back to the agent.
    """
    writer = _RecordingWriter()
    seen: list[dict[str, Any]] = []

    async def recording_run(params: dict[str, Any], _is_cancelled):
        seen.append(params)
        yield json.dumps({"type": "RUN_FINISHED"})

    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, recording_run)

    resume = {"interruptId": "i1", "reason": "aidev:user_question", "status": "resolved"}
    await dispatcher.handle(
        "session.start",
        {"sessionCode": "s1", "input": "hi", "executeKwargs": {"stream": True, "resume": resume}},
        "req-1",
    )

    run_id = writer.frames[0]["runId"]
    task = dispatcher.runs[run_id]
    await asyncio.wait_for(task, timeout=3)

    assert len(seen) == 1, "the run impl was not reached"
    assert seen[0]["executeKwargs"] == {"stream": True, "resume": resume}


async def test_chat_start_without_execute_kwargs_passes_none() -> None:
    """T-06-10: the ABSENT case must not raise — an ordinary send is unchanged.

    `_run` reads `params.get("executeKwargs")`, so an omitted key yields `None`
    rather than a `KeyError`, and the ordinary turn is byte-for-byte what it was.
    """
    writer = _RecordingWriter()
    seen: list[dict[str, Any]] = []

    async def recording_run(params: dict[str, Any], _is_cancelled):
        seen.append(params)
        yield json.dumps({"type": "RUN_FINISHED"})

    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, recording_run)

    await dispatcher.handle("session.start", {"sessionCode": "s1", "input": "hi"}, "req-1")

    run_id = writer.frames[0]["runId"]
    await asyncio.wait_for(dispatcher.runs[run_id], timeout=3)

    assert seen[0]["executeKwargs"] is None


async def test_files_index_method_is_gone_and_does_not_fall_through() -> None:
    """The `files.index` RPC channel is DELETED — and the allowlist must not widen (T-02-01).

    The old ``files.index`` method used to answer from a crawl cached on the dispatcher.
    Both are gone (the local file list now travels on ``agent.info``), so the only thing
    left to assert here is that removing the branch did not leave a gap: an unknown
    ``files.<anything>`` still takes the UNKNOWN_METHOD path.
    """
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)

    await dispatcher.handle("files.definitely.not.a.method", {}, "req-f3")

    assert writer.frames[0]["kind"] == "error"
    assert writer.frames[0]["error"]["code"] == "UNKNOWN_METHOD"


def test_ready_frame_no_longer_advertises_a_files_capability() -> None:
    """Negative control: the `files` capability must not creep back (D-28 -> agent.info).

    It used to announce the `files.index` dispatch method. That method is gone, so a
    capability flag for it would advertise something the worker does not serve. This is
    deliberately the INVERSE of the assertion it replaces: a test that pinned its
    presence could not catch the flag outliving its method.
    """
    assert "files" not in writer_mod.DEFAULT_CAPABILITIES
    assert "files" not in writer_mod.ready_frame()["capabilities"]


class _AuthStubState:
    """A ``SessionState`` double whose auth/config members are observable, offline, sync.

    261014-w6d: the dispatcher's auth/config handlers now talk to ``self.state``
    (``SessionState``) directly — the runner's 8 state-forwarding methods are gone — so
    this double stands in for the state object, not the runner.

    The real members are synchronous and are reached through
    ``asyncio.to_thread``, so the doubles here must be plain sync functions too —
    an ``async def`` double would be returned as a coroutine object and the
    assertions would silently compare against the wrong thing.
    """

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.cleared = 0
        self.declarations: list[tuple[str, str]] = []
        self.snapshot_calls = 0
        self.agent_config_calls = 0
        self._has_token = False

    # --- auth ------------------------------------------------------------
    def set_access_token(self, token: str) -> None:
        self.tokens.append(token)
        self._has_token = bool(token)

    def clear_access_token(self) -> None:
        self.cleared += 1
        self._has_token = False

    def has_access_token(self) -> bool:
        return self._has_token

    # --- config ----------------------------------------------------------
    def set_local_declaration(self, kind: str, value: str) -> None:
        self.declarations.append((kind, value))

    def describe_config_snapshot(self) -> dict[str, Any]:
        self.snapshot_calls += 1

        return {
            "currentModel": "m",
            "currentAgent": "a",
            "boundMcps": [],
            "localMcps": [],
            "boundKbs": [],
            "localKbs": [],
        }

    def describe_agent_config(self) -> dict[str, Any]:
        self.agent_config_calls += 1

        return {"agent_name": "from-the-platform"}


async def test_agent_info_carries_the_local_file_rows_on_the_resources_key(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The positive evidence that local files reach the menu (quick-261012-voy).

    Driven through the REAL ``SessionState`` — not a double returning a canned
    dict — so this pins the whole chain: the workspace file rows come from the manager
    (``rm.workspace_files``, over a real first-level ``os.scandir`` of ``tmp_path``) and
    come back out of ``agent.info``'s ``agentInfo.resources[]`` on the same key as the
    platform's own rows (T-261012-01).

    261014-w6d: ``agent.info`` now serves from ``dispatcher.state`` (the runner no longer
    carries a state-forwarding method), so this installs the real ``SessionState`` on the
    dispatcher — which IS where it lives in production.
    """
    from aidev_agent.headless.agent_runner import SessionState
    from aidev_agent.headless.local_resources import LocalConfig, LocalResourceManager, WorkspaceIndex

    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x")
    monkeypatch.setenv(WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))

    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, _slow_run)

    state = SessionState()
    # A REAL manager (so `rm.workspace_files` wraps `rm.files`), with only the
    # platform fetch stubbed out — offline, so no HTTP is attempted.
    #
    # 打桩点在**链首的 platform fetch**，不是 `rm.retrieve_agent_config` 本身：本地文件行
    # 现在由链中的 :class:`WorkspaceFileLoader` 贡献，替换整个 `retrieve_agent_config`
    # 会绕过链，于是本地行根本不会进入载荷——那正是本用例要断言的东西。
    manager = LocalResourceManager(LocalConfig())
    manager.platform_loader.retrieve_agent_config = (  # type: ignore[method-assign]
        lambda agent_code, version=None: {
            "resources": [{"code": "weather_query", "name": "天气查询", "type": "tool"}]
        }
    )
    state.ensure_manager = lambda: manager  # type: ignore[method-assign]

    dispatcher.state = state  # type: ignore[assignment]

    await dispatcher.handle("agent.info", {}, "req-local")

    frame = writer.frames[0]
    rows = frame["payload"]["agentInfo"]["resources"]

    assert [row["name"] for row in rows] == ["天气查询", "src/", "README.md"]
    assert rows[1] == {"code": "src", "name": "src/", "type": "file"}
    # 本地文件行只走 `resources[]` —— 不再有 `local_files` 兄弟键（那会让前端把同一个
    # 文件投影进两个分组）。
    assert "local_files" not in frame["payload"]["agentInfo"]


class TestAuthDispatch:
    """auth.login / auth.logout over the frame protocol (D-11 / D-12 / D-13 / T-03-07)."""

    async def test_login_sets_the_token_and_replies_without_echoing_it(self) -> None:
        """The reply is `{ok, hasToken}` — never the token itself (T-03-07)."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("auth.login", {"token": "SECRET-TOK"}, "req-1")

        assert stub.tokens == ["SECRET-TOK"]
        assert len(writer.frames) == 1
        frame = writer.frames[0]
        assert frame["kind"] == "result"
        assert frame["id"] == "req-1"
        assert frame["payload"] == {"ok": True, "hasToken": True}

        dumped = json.dumps(writer.frames)
        assert "SECRET-TOK" not in dumped, (
            "the token must never appear in any frame — UI-SPEC §/login 掩码契约 "
            "makes this a security requirement, not a style rule"
        )

    async def test_no_frame_ever_carries_the_token_across_a_full_flow(self) -> None:
        """Whole-flow leak check: login, logout, config.info — no frame holds it."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        dispatcher.state = _AuthStubState()  # type: ignore[assignment]

        await dispatcher.handle("auth.login", {"token": "SECRET-TOK"}, "req-1")
        await dispatcher.handle("config.info", {}, "req-2")
        await dispatcher.handle("auth.logout", {}, "req-3")

        dumped = json.dumps(writer.frames)
        assert "SECRET-TOK" not in dumped, f"token leaked into a frame: {dumped}"

    async def test_login_without_a_token_is_tolerated_not_an_error(self) -> None:
        """A missing `token` param yields `{ok: False}`, not an error frame."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("auth.login", {}, "req-1")

        assert len(writer.frames) == 1
        assert writer.frames[0]["kind"] == "result"
        assert writer.frames[0]["payload"] == {"ok": False, "hasToken": False}
        assert stub.tokens == [], "a missing token must not reach the setter"

    async def test_empty_token_is_a_noop_and_does_not_clear_an_existing_token(self) -> None:
        """UI-SPEC §规划注意事项 #5: an empty submit must NOT drop a valid token."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("auth.login", {"token": "tok"}, "req-1")
        await dispatcher.handle("auth.login", {"token": ""}, "req-2")

        assert len(stub.tokens) == 1, "the empty submit reached the setter"
        assert stub.has_access_token() is True
        assert writer.frames[1]["payload"] == {"ok": False, "hasToken": True}

    async def test_logout_clears_the_token(self) -> None:
        """D-13: logout drops the token and returns to pure app state."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("auth.login", {"token": "tok"}, "req-1")
        await dispatcher.handle("auth.logout", {}, "req-2")

        assert stub.cleared == 1
        assert stub.has_access_token() is False
        assert writer.frames[-1]["payload"] == {"ok": True, "hasToken": False}

    async def test_logout_does_not_clear_the_config_snapshot(self) -> None:
        """D-13 scopes logout to the TOKEN — the bound config is not the token's."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("auth.login", {"token": "tok"}, "req-1")
        await dispatcher.handle("auth.logout", {}, "req-2")
        await dispatcher.handle("config.info", {}, "req-3")

        payload = writer.frames[-1]["payload"]["config"]
        assert payload["currentModel"] == "m"
        assert payload["currentAgent"] == "a"
        assert "currentModelSource" not in payload, "the stub returns the raw snapshot verbatim"


class TestConfigDispatch:
    """config.info / config.set over the frame protocol (D-08 / D-15)."""

    async def test_config_info_returns_the_snapshot(self) -> None:
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("config.info", {}, "req-1")

        assert len(writer.frames) == 1
        frame = writer.frames[0]
        assert frame["kind"] == "result"
        assert frame["id"] == "req-1"
        for key in ("currentModel", "currentAgent", "boundMcps", "localMcps", "boundKbs", "localKbs"):
            assert key in frame["payload"]["config"], f"snapshot is missing {key}"

    async def test_config_info_and_agent_info_use_different_state_methods(self) -> None:
        """D-15: cached snapshot vs LIVE fetch must stay two distinct code paths.

        261014-w6d: both now go through the SHARED ``dispatcher.state`` (the runner no
        longer forwards them), so the assertion is unchanged in substance — the two RPCs
        still reach two different state methods.
        """
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("config.info", {}, "req-1")
        await dispatcher.handle("agent.info", {}, "req-2")

        assert stub.snapshot_calls == 1, "config.info did not go through describe_config_snapshot"
        assert stub.agent_config_calls == 1, "agent.info did not go through describe_agent_config"

    async def test_config_set_writes_the_declaration_and_echoes_it_back(self) -> None:
        """D-08: the switch writes the local declaration via set_local_declaration."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("config.set", {"kind": "models", "value": "gpt-4"}, "req-1")

        assert stub.declarations == [("models", "gpt-4")]
        assert writer.frames[0]["payload"] == {"ok": True, "kind": "models", "value": "gpt-4"}

    async def test_config_set_rejects_an_unknown_kind_without_writing(self) -> None:
        """An unknown kind answers `{ok: False}` and must not touch the file."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("config.set", {"kind": "bogus", "value": "x"}, "req-1")

        assert stub.declarations == []
        assert writer.frames[0]["kind"] == "result"
        assert writer.frames[0]["payload"] == {"ok": False}

    async def test_config_set_rejects_a_missing_value_without_writing(self) -> None:
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)
        stub = _AuthStubState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("config.set", {"kind": "agents"}, "req-1")

        assert stub.declarations == []
        assert writer.frames[0]["payload"] == {"ok": False}

    async def test_config_set_accepts_every_declared_kind(self) -> None:
        """All four families route through the SAME method — `kind` is the discriminant."""
        for kind in ("models", "agents", "mcps", "knowledge"):
            writer = _RecordingWriter()
            dispatcher = Dispatcher(writer)
            stub = _AuthStubState()
            dispatcher.state = stub  # type: ignore[assignment]

            await dispatcher.handle("config.set", {"kind": kind, "value": "v"}, "req-1")

            assert stub.declarations == [(kind, "v")], f"{kind} was not routed to the writer"

    async def test_config_set_invalidates_the_snapshot(self) -> None:
        """A switch refreshes the snapshot — the third member of D-15's refresh set.

        If it did not, the very next `/models` would render the pre-switch value
        labelled `local`: the lie D-08's observable-switch requirement forbids.
        """
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)

        class _InvalidatingState(_AuthStubState):
            """Mirrors SessionState: set_local_declaration drops the cached snapshot."""

            def __init__(self) -> None:
                super().__init__()
                self._snapshot: dict[str, Any] | None = None

            def describe_config_snapshot(self) -> dict[str, Any]:
                self.snapshot_calls += 1

                if self._snapshot is None:
                    self._snapshot = {"currentModel": "m", "currentAgent": "a"}

                return self._snapshot

            def set_local_declaration(self, kind: str, value: str) -> None:
                super().set_local_declaration(kind, value)
                self._snapshot = None

        stub = _InvalidatingState()
        dispatcher.state = stub  # type: ignore[assignment]

        await dispatcher.handle("config.info", {}, "req-1")
        await dispatcher.handle("config.set", {"kind": "models", "value": "gpt-4"}, "req-2")
        await dispatcher.handle("config.info", {}, "req-3")

        assert stub.snapshot_calls == 2, (
            "config.set did not invalidate the snapshot — the switch would be invisible"
        )

    async def test_config_methods_do_not_fall_through_to_unknown_method(self) -> None:
        """T-02-01: the new branches must not widen the allowlist."""
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)

        await dispatcher.handle("config.definitely.not.a.method", {}, "req-x")

        assert writer.frames[0]["kind"] == "error"
        assert writer.frames[0]["error"]["code"] == "UNKNOWN_METHOD"

    async def test_auth_methods_do_not_fall_through_to_unknown_method(self) -> None:
        writer = _RecordingWriter()
        dispatcher = Dispatcher(writer)

        await dispatcher.handle("auth.definitely.not.a.method", {}, "req-x")

        assert writer.frames[0]["kind"] == "error"
        assert writer.frames[0]["error"]["code"] == "UNKNOWN_METHOD"


def test_ready_frame_advertises_the_auth_and_config_capabilities() -> None:
    """Phase 3's new surface is discoverable from the handshake (D-11/D-15).

    The ``"files"`` entry is GONE (quick-261012-voy): it announced the ``files.index``
    method, which no longer exists. The list is asserted exactly, so a capability can
    neither quietly disappear nor quietly return.
    """
    assert "auth" in writer_mod.DEFAULT_CAPABILITIES
    assert "config" in writer_mod.DEFAULT_CAPABILITIES
    assert writer_mod.DEFAULT_CAPABILITIES == [
        "chat",
        "cancel",
        "session",
        "agent",
        "auth",
        "config",
    ]
    advertised = writer_mod.ready_frame()["capabilities"]
    assert "auth" in advertised and "config" in advertised


def test_ready_frame_reports_the_resolved_sdk_path_when_supplied() -> None:
    """Phase 5 D-10: which aidev_agent tree is patched must be observable, not inferred.

    PITFALLS.md Pitfall 6: this environment resolves one of several `aidev_agent`
    trees through the installed `.pth`, so "we patched a different tree than you
    think" is a real failure mode. The handshake is where it becomes visible.

    Two things are pinned here, and the SECOND is the one that protects the wire:
    the field is present when supplied, and it is ABSENT when not — so an older
    caller's frame is byte-for-byte what it was and `test_ready_frame_advertises_*`
    above keeps passing unchanged.
    """
    pinned = "/tmp/fake/aidev_agent/__init__.py"

    frame = writer_mod.ready_frame(sdk_file=pinned)

    assert frame["sdk_file"] == pinned
    # Still a sibling key, NOT a capability: `capabilities` is the fixed D-09 method set.
    assert "sdk_file" not in frame["capabilities"]
    assert frame["capabilities"] == writer_mod.DEFAULT_CAPABILITIES

    # ADDITIVE degradation: omitting the argument leaves the frame exactly as before.
    assert "sdk_file" not in writer_mod.ready_frame()


async def test_parse_error_does_not_crash_loop() -> None:
    """A malformed line yields an error frame; a following valid frame works."""
    from aidev_agent.headless.runtime import _process_line

    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)

    await _process_line("not json", dispatcher)
    assert writer.frames[0]["kind"] == "error"
    assert writer.frames[0]["error"]["code"] == "PARSE_ERROR"

    # The loop is still alive: a valid frame after the bad one is processed.
    await _process_line(json.dumps({"v": 1, "id": "r1", "method": "session.list", "params": {}}), dispatcher)
    assert writer.frames[1]["kind"] == "result"
    assert writer.frames[1]["id"] == "r1"


async def test_a_raising_handler_does_not_crash_loop() -> None:
    """A handler that RAISES yields an error frame; the loop survives (fix (a)).

    Regression: ``/models`` → Enter called ``config.set``, whose per-agent write
    raised ``ValueError("unsafe agent_code for per-agent config path: ''")`` when
    no ``AGENT_TUI_AGENT_CODE`` / ``bkai_tui.json`` was configured. That exception
    used to propagate out of ``dispatch.handle`` into the stdin loop's outer
    ``except Exception: break`` — killing the worker for the rest of the session
    (every following interaction, e.g. a plain ``hello``, went unanswered).

    Driven through the REAL ``_process_line`` so the production path is pinned:
    a handler exception must become ONE correlated error frame, and the next
    frame must still be handled.
    """
    from aidev_agent.headless.runtime import _process_line

    class _RaisingState:
        """Mirrors the production failure: the per-agent write raises."""

        def set_local_declaration(self, kind: str, value: str) -> None:
            raise ValueError("unsafe agent_code for per-agent config path: ''")

        def describe_config_snapshot(self) -> dict[str, Any]:
            return {"currentModel": "m"}

    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    dispatcher.state = _RaisingState()  # type: ignore[assignment]

    # The bad request: it must NOT raise out of _process_line.
    await _process_line(
        json.dumps({"v": 1, "id": "bad-1", "method": "config.set",
                    "params": {"kind": "models", "value": "gpt-4"}}),
        dispatcher,
    )

    assert writer.frames[0]["kind"] == "error"
    assert writer.frames[0]["id"] == "bad-1", "the error must be correlated to the request"
    assert writer.frames[0]["error"]["code"] == "INTERNAL_ERROR"
    assert "agent_code" in writer.frames[0]["error"]["message"]

    # The loop is still alive: a following valid frame is still processed.
    await _process_line(
        json.dumps({"v": 1, "id": "after-1", "method": "config.info", "params": {}}),
        dispatcher,
    )
    assert writer.frames[1]["kind"] == "result"
    assert writer.frames[1]["id"] == "after-1"


async def test_process_shutdown_invokes_callback_and_result() -> None:
    writer = _RecordingWriter()
    called = asyncio.Event()

    dispatcher = Dispatcher(writer, shutdown_cb=called.set)
    await dispatcher.handle("process.shutdown", {}, "r1")

    assert writer.frames[0]["kind"] == "result"
    assert writer.frames[0]["payload"] == {"ok": True}
    assert called.is_set()


async def test_shutdown_cancels_all_runs() -> None:
    """D-04: shutdown cancels every outstanding run, then closes the writer."""
    writer = _RecordingWriter()
    dispatcher = Dispatcher(writer)
    _install_stub(dispatcher, _slow_run)

    await dispatcher.handle("session.start", {"input": "a"}, "r1")
    await dispatcher.handle("session.start", {"input": "b"}, "r2")
    assert len(dispatcher.runs) == 2

    await dispatcher.shutdown()

    assert writer.closed is True
    assert dispatcher.runs == {}


def test_eof_shutdown() -> None:
    """Spawn the worker, send shutdown, close stdin: exit 0, no orphan (D-04).

    This is the subprocess-level proof of IPC-03's "parent and child exit
    together". It uses the project venv because that is the only interpreter
    where ``aidev_agent`` resolves correctly.
    """
    proc = subprocess.Popen(
        [VENV_PYTHON, "-u", "-m", "aidev_agent.headless", "--protocol", "tui"],
        cwd=AGENT_TUI_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    start = time.monotonic()
    try:
        # Hand the frame to communicate() so it owns the write+close+read
        # sequence; closing stdin ourselves would race communicate()'s own
        # flush. Reaching EOF after this frame is exactly the D-04 path.
        out, err = proc.communicate(
            input=b'{"v":1,"id":"r1","method":"process.shutdown","params":{}}\n',
            timeout=5,
        )
        elapsed = time.monotonic() - start
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("worker did not exit within 5s of stdin close (orphan risk)")

    assert proc.returncode == 0, f"expected exit 0, got {proc.returncode}; stderr={err!r}"

    lines = [ln for ln in out.decode("utf-8", "replace").split("\n") if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]

    assert parsed[0]["kind"] == "ready", f"first stdout line must be ready, got {parsed[0]}"
    assert any(f["kind"] == "result" for f in parsed), f"expected a result frame, got {parsed}"

    # Every stdout line must be valid JSON — nothing leaked onto stdout.
    assert all(isinstance(f, dict) for f in parsed)

    # No surviving child of this test process.
    assert proc.poll() is not None
    assert elapsed < 5.0, f"shutdown took {elapsed:.2f}s"


def test_sigterm_shutdown_exits_within_5s(tmp_path) -> None:
    """A SIGTERMed worker must exit within the grace window, with no orphan (D-04 / T-07-01).

    Regression guard. The original handler raised ``SystemExit`` from the signal
    handler, which looked correct and was not: the read loop is parked in a
    blocking ``os.read`` on a daemon-less executor thread, so ``asyncio.run``
    could not shut the loop down and the process survived indefinitely. Observed
    for real — SIGTERM left a live worker past the deadline on every run.

    Two changes fix it, and this test covers BOTH:

    1. the handler only SETS an event (the read is raced against it), and
    2. the read runs on a daemon-thread executor, so a parked read cannot hold
       the interpreter open once ``main`` returns.

    Reverting either one reintroduces the hang, which is why the assertion is
    on the WALL CLOCK and not on the log output: a "graceful log, live process"
    is exactly the failure this catches.

    The deadline is a TOLERANCE, not a promise. It was 5s, measured while this
    run DIED INSTANTLY (a broken checkpointer import aborted ``session.start``
    before it did any work, so teardown was trivial). With a correct interpreter
    the run is real — an in-flight model call plus five MCP servers — and
    cancelling it takes ~5s, straddling the old bound and making this flaky. The
    guarded failure is an UNBOUNDED hang, so a looser bound still catches it.
    """
    # Keep the real process/dispatcher and block only SDK assembly. This exercises
    # cancellation of an uninterruptible executor thread without platform writes.
    probe = """
import asyncio
import time
from aidev_agent.headless import runtime
runtime.install_stdout_guard()
from aidev_agent.headless.agent_runner import AgentRunner

def slow_build(*args, **kwargs):
    time.sleep(60)

AgentRunner._build_session_agent = slow_build
asyncio.run(runtime.main())
"""
    proc = subprocess.Popen(
        [VENV_PYTHON, "-u", "-c", probe],
        cwd=AGENT_TUI_DIR,
        env={**os.environ,
             "AGENT_TUI_TUI_CONFIG_FILE": str(tmp_path / "bkai_tui.json"),
             "BKAI_TUI_CHECKPOINT_FILE": str(tmp_path / "checkpoints.sqlite"),
             "BKAI_TUI_LOG_PATH": str(tmp_path / "headless.log"),
             "AGENT_TUI_OTEL": "0"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Wait for readiness so the signal lands during the read loop, not
        # during interpreter startup.
        assert proc.stdout is not None
        first = proc.stdout.readline()
        assert json.loads(first.decode("utf-8", "replace"))["kind"] == "ready"

        # Start a run, then interrupt it — the mid-run SIGTERM is the case the
        # TUI's shutdown grace window actually hits.
        assert proc.stdin is not None
        proc.stdin.write(
            b'{"v":1,"id":"s1","method":"session.start",'
            b'"params":{"sessionCode":null,"input":"hi"}}\n'
        )
        proc.stdin.flush()
        time.sleep(1.0)

        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)

        # The bound is a TOLERANCE for a real teardown, not a 5s promise — see
        # the docstring. A genuine orphan never exits at all, so this still
        # catches the regression it exists for.
        grace_s = 15
        try:
            returncode = proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError(
                f"worker did not exit within {grace_s}s of SIGTERM (orphan risk): "
                "the signal handler and/or the daemon stdin executor regressed"
            )

        elapsed = time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert returncode == 0, f"expected a clean exit 0, got {returncode}"
    assert elapsed < grace_s, f"SIGTERM shutdown took {elapsed:.2f}s"
