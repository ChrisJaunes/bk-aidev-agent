# -*- coding: utf-8 -*-
"""注入式资源下的 ``runtime`` 会话/策略/工作区根契约（主测 ``runtime``）。

从 ``tests/resources/test_resource_manager.py`` 按断言目标拆出：那一侧只留
``resources.manager.LocalResourceManager`` 的注入模式与 loader 链，本文件承载
``Session`` / ``SDKRunner`` / ``workspace_agent`` 在注入式资源下的行为。
"""

from __future__ import annotations


def test_session_state_with_injected_sources_needs_no_credentials():
    from aidev_agent_cli.runtime.sdk.session import SDKSession as Session

    state = Session(resources=("injected",), agent_code="agent-hook")
    rm = state.ensure_manager()

    assert rm.uses_platform is False
    assert rm.get_agent_config("agent-hook").agent_code == "agent-hook"


async def test_injected_strategy_applies_launch_defaults():
    from aidev_agent_cli.runtime.auth import ProcessConfig
    from aidev_agent_cli.runtime.contracts import LaunchOptions, RunRequest, SessionOptions
    from aidev_agent_cli.runtime.sdk.session import SDKSession
    from aidev_agent_cli.runtime.service import RuntimeService

    service = RuntimeService(config=ProcessConfig(
        defaults=LaunchOptions(model="default-model", resources=("injected", "runtime")),
    ))
    try:
        info = await service.new_session(SessionOptions(session_code="s1", agent_code="agent-hook"))
        snapshot = service._snapshot("start", service._sessions.get(info.session_code), RunRequest("s1"), None)
        assert snapshot.session.model == "default-model"
        worker_session = SDKSession(model=snapshot.session.model, resources=snapshot.session.resources)
        try:
            assert worker_session.ensure_manager().sources == ("injected", "runtime")
        finally:
            worker_session.close()
    finally:
        await service.close_all()


def test_a_turn_no_longer_uses_a_workspace_root_contextvar() -> None:
    """工作区根固定为 os.getcwd()：会话不再接受 workspace_root，一轮仍可正常装配与执行。"""
    from aidev_agent_cli.runtime.sdk import workspace as workspace_agent
    from aidev_agent_cli.runtime.sdk.session import SDKSession as Session

    seen: list[object] = []

    class _Agent:
        event_handler = None

        def execute(self, _kwargs):
            seen.append("execute")
            return iter(())

        def stop(self) -> None:
            return None

    session = Session(session_code="s1", resources=("injected",), agent_code="a")

    def _build(**_params):
        seen.append("build")
        return _Agent()

    session.build_agent = _build  # type: ignore[method-assign]
    outcome = session.turn(request_id="r1", input_text="hi")

    assert outcome.status == "finished"
    assert seen == ["build", "execute"]
    # CURRENT_WORKSPACE_ROOT 已删除：模块上不应再有该名字。
    assert not hasattr(workspace_agent, "CURRENT_WORKSPACE_ROOT")
    # Session 构造签名也不再带 workspace_root。
    assert "workspace_root" not in Session.__init__.__code__.co_varnames