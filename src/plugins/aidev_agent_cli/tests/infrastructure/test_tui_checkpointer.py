"""Regression guards for the worker's checkpoint saver and SESSION build path.

These tests pin the CORRECT design, replacing an earlier suite that asserted the
wrong saver. The resume failure had THREE causes, and each gets a guard here:

1. ``build_type`` must be ``AgentBuildType.SESSION`` (not ``DIRECT``) and must
   carry ``session_code``, so the SDK loads history itself via
   ``resource_manager.get_chat_session_contents``. ``session_context_data`` is
   consumed ONLY by the DIRECT path and must NOT be passed.
2. The saver must be the worker's own :class:`aidev_agent_cli.runtime.sdk.checkpoints.LoopIndependentSqliteSaver`
   — a SQLite saver with a SYNCHRONOUS core and an async skin. NEITHER packaged
   saver works, and both failures were reproduced against the real worker:
   the sync ``SqliteSaver`` raises ``NotImplementedError`` from the async methods
   the SDK calls (``agraph.astream_events`` / ``aget_state``); the
   ``AsyncSqliteSaver`` binds ``asyncio.Lock`` to its constructing loop while the
   SDK runs the graph on a per-run loop, giving "Lock is bound to a different
   event loop".
3. The saver must be a process-level singleton, and the underlying
   ``from_conn_string`` generator must be held so the DB is never closed
   mid-process.

Test style matches ``test_agent_runner.py``: bare ``async def test_...`` functions
(``asyncio_mode = auto`` in ``pytest.ini``), no explicit markers.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from aidev_agent_cli.runtime.sdk import factory as agent_factory
from aidev_agent_cli.runtime.sdk import session as agent_runner
from aidev_agent_cli.runtime.sdk.checkpoints import LoopIndependentSqliteSaver

#: ``build_agent`` 的源码在 factory；checkpoint 单例住在 runner 的 Session。
_FACTORY_PATH = Path(agent_factory.__file__)
#: ``ensure_checkpointer` / ``close_exit_stack`` 现在住在 ``runtime.session``（Session）。
_RUNNER_PATH = Path(agent_runner.__file__)
_SESSION_PATH = Path(_RUNNER_PATH.parent / "checkpoints.py")


@pytest.fixture(autouse=True)
def _isolated_checkpointer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect the DB to ``tmp_path``.

    The saver singleton lives on :class:`Session` (261015), so isolation is
    simply "each test builds its own state" — no module globals to save/reset.
    """
    monkeypatch.setenv("BKAI_TUI_CHECKPOINT_FILE", str(tmp_path / "cp.sqlite"))

    yield


def _code_only(source: str) -> str:
    """Return ``source`` with every docstring stripped, as unparsed code.

    A call or import of ``MemorySaver`` is forbidden; merely NAMING it in prose
    (a docstring explaining why the old value was wrong) is not. Stripping
    docstrings before the assertion is what keeps the two apart.
    """
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)

    return ast.unparse(tree)


class TestDbLocation:
    """DB 路径解析住在 ``ensure_checkpointer`` 里；这里经它打开的文件钉住。"""

    async def test_defaults_to_dot_bkai_under_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("BKAI_TUI_CHECKPOINT_FILE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        state = agent_runner.SDKSession()
        await state.checkpoints.ensure()

        assert (tmp_path / ".bkai" / "checkpoints.sqlite").exists()

    async def test_the_env_override_replaces_the_whole_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "custom" / "my-checkpoints.sqlite"
        assert not target.parent.exists()
        monkeypatch.setenv("BKAI_TUI_CHECKPOINT_FILE", str(target))

        state = agent_runner.SDKSession()
        await state.checkpoints.ensure()

        # The override wins, and the parent directory is created.
        assert target.exists()


class TestSaverSingleton:
    """One ``LoopIndependentSqliteSaver`` per process, loop-agnostic."""

    async def test_two_awaits_return_the_same_object(self) -> None:
        state = agent_runner.SDKSession()
        first = await state.checkpoints.ensure()
        second = await state.checkpoints.ensure()

        assert first is second

    async def test_it_is_the_worker_saver(self) -> None:
        state = agent_runner.SDKSession()
        saver = await state.checkpoints.ensure()

        assert isinstance(saver, LoopIndependentSqliteSaver)

    async def test_it_is_not_a_memory_saver(self) -> None:
        from langgraph.checkpoint.memory import MemorySaver

        state = agent_runner.SDKSession()
        saver = await state.checkpoints.ensure()

        assert not isinstance(saver, MemorySaver)

    async def test_it_is_not_a_packaged_async_saver(self) -> None:
        """The packaged async saver binds ``asyncio.Lock`` to its constructing loop.

        The SDK runs the graph on a per-run loop, so an ``AsyncSqliteSaver`` dies
        with "Lock is bound to a different event loop". The worker's saver must
        NOT be one.
        """
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        state = agent_runner.SDKSession()
        saver = await state.checkpoints.ensure()

        assert not isinstance(saver, AsyncSqliteSaver)

    async def test_packaged_sync_saver_raises_but_ours_does_not(self) -> None:
        """Pins WHY the worker saver extends rather than reuses ``SqliteSaver``.

        The packaged sync saver's async methods raise; ours must not, because the
        SDK drives the graph with ``astream_events``.
        """
        from langgraph.checkpoint.sqlite import SqliteSaver

        assert issubclass(LoopIndependentSqliteSaver, SqliteSaver), "we extend the proven sync core"

        state = agent_runner.SDKSession()
        saver = await state.checkpoints.ensure()
        # The inherited sync method works, and the OVERRIDDEN async one does too.
        assert saver.get_tuple({"configurable": {"thread_id": "nope"}}) is None
        assert await saver.aget_tuple({"configurable": {"thread_id": "nope"}}) is None

    async def test_async_methods_work_on_a_different_loop(self) -> None:
        """THE regression guard for the failure that forced this saver's shape.

        A fresh loop must be able to await the saver that another loop built.
        ``AsyncSqliteSaver`` fails exactly here.
        """
        state = agent_runner.SDKSession()
        saver = await state.checkpoints.ensure()
        cfg = {"configurable": {"thread_id": "thread-x", "checkpoint_ns": ""}}

        def _on_a_fresh_loop() -> Any:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(saver.aget_tuple(cfg))
            finally:
                loop.close()

        # And from a separate THREAD as well, matching the SDK's per-run loop.
        assert await asyncio.to_thread(_on_a_fresh_loop) is None

    async def test_close_unwinds_the_stack_for_real(self) -> None:
        """``close_exit_stack`` 真的执行 ``__exit__``——不是"永不关闭"。

        关闭后连接不可用（``sqlite3.ProgrammingError``）；再调一次是幂等 NO-OP；
        未创建时（第三个 state）调它也不抛。
        """
        import sqlite3

        state = agent_runner.SDKSession()
        saver = await state.checkpoints.ensure()
        state.close()

        try:
            saver.get_tuple({"configurable": {"thread_id": "nope"}})
            raise AssertionError("connection survived close_exit_stack")
        except sqlite3.ProgrammingError:
            pass

        # Idempotent, and a NO-OP on a state that never built one.
        state.close()
        agent_runner.SDKSession().close()

    async def test_the_backing_generator_is_held(self) -> None:
        """Without the held generator, its ``finally`` closes the DB immediately."""
        state = agent_runner.SDKSession()
        await state.checkpoints.ensure()

        assert state.checkpoints._exit_stack is not None


class TestNoMemorySaverFallback:
    """Source guards over the docstring-stripped AST of the saver's owner.

    checkpoint 单例与 DB 路径解析（``CheckpointStore.ensure``）
    住在 ``agent_runner``，因此"构建哪个 saver"的守卫读 **runner**；``build_agent``
    相关的守卫仍在 factory。
    """

    def test_memory_saver_is_absent_from_executable_code(self) -> None:
        code = _code_only(_FACTORY_PATH.read_text())

        assert "MemorySaver" not in code, (
            "MemorySaver is referenced in agent_factory's executable code; a "
            "per-build in-memory saver cannot survive into the next turn and "
            "resurrects the resume bug"
        )

    def test_worker_saver_is_used(self) -> None:
        # saver 的构造点在进程级 ``runtime.checkpoints.CheckpointStore``。
        code = _code_only(_SESSION_PATH.read_text())

        assert "LoopIndependentSqliteSaver" in code

    def test_packaged_savers_are_not_constructed(self) -> None:
        """Neither packaged saver works — both were measured failing.

        ``AsyncSqliteSaver`` binds its ``asyncio.Lock`` to the constructing loop
        and the SDK runs the graph on a per-run loop; ``SqliteSaver`` lacks the
        async methods the SDK calls. Only our own saver may be built here.
        """
        code = _code_only(_FACTORY_PATH.read_text())

        assert "AsyncSqliteSaver.from_conn_string" not in code
        # `LoopIndependentSqliteSaver` ENDS with `SqliteSaver`, so an unqualified substring
        # check would false-positive; assert the bare class is never imported.
        assert "from langgraph.checkpoint.sqlite import SqliteSaver" not in code

    def test_env_var_is_zero_hits_in_source(self) -> None:
        """The literal ``MemorySaver`` must not appear ANYWHERE in the file.

        This mirrors the shell-level ``grep -c 'MemorySaver' agent_factory.py == 0``
        criterion (which cannot itself be expressed in pytest) so the two agree.
        (The SUMMARY records the literal shell count.)
        """
        assert "MemorySaver" not in _FACTORY_PATH.read_text()

    def test_build_type_is_session_and_no_session_context_data(self) -> None:
        code = _code_only(_FACTORY_PATH.read_text())

        assert "build_type: AgentBuildType=AgentBuildType.SESSION" in code
        assert "session_context_data=" not in code

    def test_ensure_checkpointer_is_async(self) -> None:
        code = _code_only(_SESSION_PATH.read_text())

        assert "async def ensure(" in code


class TestCrossConnectionRoundTrip:
    """State written under one saver is readable from a FRESH saver on the file."""

    async def test_state_survives_a_new_connection(self) -> None:
        path = Path(os.environ["BKAI_TUI_CHECKPOINT_FILE"])
        config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
        checkpoint = {
            "v": 1,
            "id": "cp-1",
            "ts": "2026-09-30T00:00:00+00:00",
            "channel_values": {"marker": "survived"},
            "channel_versions": {"marker": "1"},
            "versions_seen": {},
            "pending_sends": [],
        }
        metadata = {"source": "input", "step": 0, "writes": {}, "parents": {}}

        # Write under the module singleton...
        state = agent_runner.SDKSession()
        writer = await state.checkpoints.ensure()
        await writer.aput(config, checkpoint, metadata, {})

        # ...then read it back from a FRESH connection on the same file. Both
        # connections live on this one test loop, so no separate asyncio.run is
        # needed.
        with LoopIndependentSqliteSaver.from_conn_string(str(path)) as reader:
            reader.setup()
            read_back = await reader.aget_tuple(config)

        assert read_back is not None
        assert read_back.checkpoint["channel_values"] == {"marker": "survived"}

    async def test_a_second_thread_does_not_see_the_first(self) -> None:
        path = Path(os.environ["BKAI_TUI_CHECKPOINT_FILE"])
        config_1 = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
        config_2 = {"configurable": {"thread_id": "thread-2", "checkpoint_ns": ""}}
        checkpoint = {
            "v": 1,
            "id": "cp-1",
            "ts": "2026-09-30T00:00:00+00:00",
            "channel_values": {"marker": "thread-1-only"},
            "channel_versions": {"marker": "1"},
            "versions_seen": {},
            "pending_sends": [],
        }
        metadata = {"source": "input", "step": 0, "writes": {}, "parents": {}}

        state = agent_runner.SDKSession()
        writer = await state.checkpoints.ensure()
        await writer.aput(config_1, checkpoint, metadata, {})

        with LoopIndependentSqliteSaver.from_conn_string(str(path)) as reader:
            reader.setup()
            other = await reader.aget_tuple(config_2)

        assert other is None