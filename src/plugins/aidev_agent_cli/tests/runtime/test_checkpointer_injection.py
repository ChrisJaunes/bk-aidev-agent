# -*- coding: utf-8 -*-
"""``sdk.factory.build_agent`` 的 SESSION 构建路径与注入 saver（主测 ``runtime``）。

从 ``tests/runtime/test_tui_checkpointer.py`` 按断言目标拆出：那一侧只留
``sdk.checkpoints.LoopIndependentSqliteSaver`` 的 saver 本体与源码守卫，本文件承载
「build_agent 如何把 saver / session_code / agent_code 交给 SDK」这一 runtime 契约。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aidev_agent_cli.sdk import factory as agent_factory
from aidev_agent_cli.sdk import executor as agent_runner
from tests._sdk import make_run
from aidev_agent_cli.sdk.checkpoints import LoopIndependentSqliteSaver


@pytest.fixture(autouse=True)
def _isolated_checkpointer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect the DB to ``tmp_path`` (与拆出前同款隔离，逐测试生效)。"""
    monkeypatch.setenv("BKAI_TUI_CHECKPOINT_FILE", str(tmp_path / "cp.sqlite"))
    yield


class TestBuildAgentInjection:
    """``build_agent`` uses SESSION + an injected worker saver."""

    @staticmethod
    def _patch_sdk(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> Any:
        """打桩 SDK 侧，并返回要**注入**给 ``build_agent`` 的 manager。

        ``build_agent`` 不再自己构造 manager（由 AgentRunner 提供），因此这里不再
        patch ``_make_resource_manager``——直接把 manager 交给它。
        """

        class _RM:
            #: ``build_agent`` 在 ``username`` 缺省时取 manager 的身份。
            username = "judgehuang"

            def get_client(self) -> Any:
                return object()

            def get_agent_code(self) -> str:
                return "app-code"

        manager = _RM()

        class _Factory:
            @staticmethod
            def create(**kwargs: Any) -> Any:
                calls.append(kwargs)

                return object()

        import aidev_agent.services.event_handlers.agui_writer as agui_writer

        # 唯一构建入口是 SDK 的 agent_factory 注入点。
        monkeypatch.setattr(agent_factory, "sdk_agent_factory", lambda: _Factory)
        monkeypatch.setattr(agui_writer, "AGUISessionWriter", lambda **kw: object())

        return manager

    async def test_uses_session_build_type_with_session_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aidev_agent.enums import AgentBuildType

        calls: list[dict[str, Any]] = []
        manager = self._patch_sdk(monkeypatch, calls)
        state = make_run()
        saver = await state.checkpoints.ensure()

        agent_factory.build_agent(
            manager,
            "thread-1",
            "app-code",
            checkpointer=saver,
        )

        assert calls[0]["build_type"] is AgentBuildType.SESSION
        assert calls[0]["session_code"] == "thread-1"

    async def test_does_not_pass_session_context_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []
        manager = self._patch_sdk(monkeypatch, calls)
        state = make_run()
        saver = await state.checkpoints.ensure()

        agent_factory.build_agent(
            manager,
            "thread-1",
            "app-code",
            checkpointer=saver,
        )

        assert "session_context_data" not in calls[0] or calls[0]["session_context_data"] is None

    async def test_injects_the_worker_saver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []
        manager = self._patch_sdk(monkeypatch, calls)
        state = make_run()
        saver = await state.checkpoints.ensure()

        agent_factory.build_agent(
            manager,
            "thread-1",
            "app-code",
            checkpointer=saver,
        )

        assert isinstance(calls[0]["checkpointer"], LoopIndependentSqliteSaver)

    async def test_two_builds_share_the_same_saver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The regression guard for "not recreated per build"."""
        calls: list[dict[str, Any]] = []
        manager = self._patch_sdk(monkeypatch, calls)
        state = make_run()
        saver = await state.checkpoints.ensure()

        agent_factory.build_agent(
            manager,
            "thread-1",
            "app-code",
            checkpointer=saver,
        )
        agent_factory.build_agent(
            manager,
            "thread-1",
            "app-code",
            checkpointer=saver,
        )

        assert calls[0]["checkpointer"] is calls[1]["checkpointer"]

    async def test_agent_code_is_passed_through_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """261014-trk: ``build_agent`` no longer resolves ``agent_code`` itself.

        The code given by the caller must reach ``AgentInstanceFactory`` verbatim —
        NOT the env/config/platform value it would have derived before. This is the
        contract that makes sub-agents possible (per-call-site agent_code).
        """
        calls: list[dict[str, Any]] = []
        manager = self._patch_sdk(monkeypatch, calls)
        state = make_run()
        saver = await state.checkpoints.ensure()

        agent_factory.build_agent(
            manager,
            "thread-1",
            "caller-chosen-code",
            checkpointer=saver,
        )

        assert calls[0]["agent_code"] == "caller-chosen-code"
