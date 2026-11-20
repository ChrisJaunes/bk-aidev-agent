# -*- coding: utf-8 -*-
"""T-4：``LocalResourceManager`` 注入模式 —— 不触达平台的最小 RM（REQ-8 / REQ-11）。"""

from unittest.mock import patch

import pytest
from aidev_agent.packages.resource_manager.registry import ResourceManagerProtocol

from aidev_agent_cli.utils.errors import HeadlessUnsupportedError
from aidev_agent_cli.resources.manager import HeadlessLedger, LocalResourceManager

PROTOCOL_METHODS = [
    name
    for name in dir(ResourceManagerProtocol)
    if not name.startswith("_") and callable(getattr(ResourceManagerProtocol, name, None))
]


def _rm(**kwargs) -> LocalResourceManager:
    params = {"agent_code": "agent-hook", "chat_model": "fake-model", "username": "tester"}
    params.update(kwargs)
    chat_model = params.pop("chat_model")
    return LocalResourceManager.build(sources=("injected",), model=chat_model, **params)


def test_implements_every_protocol_method():
    manager = _rm()
    missing = [name for name in PROTOCOL_METHODS if not callable(getattr(manager, name, None))]
    assert missing == []
    assert isinstance(manager, ResourceManagerProtocol)


def test_agent_config_is_synthesised_from_injected_params():
    config = _rm().get_agent_config("agent-hook")
    assert config.agent_code == "agent-hook"
    assert config.agent_name == "agent-hook"
    assert config.chat_model == "fake-model"
    assert config.non_thinking_llm == "fake-model"
    assert config.tool_codes == []
    assert config.knowledgebase_ids == []
    assert config.knowledge_ids == []
    assert _rm().retrieve_agent_config("agent-hook")["prompt_setting"]["llm_code"] == "fake-model"


def test_ledger_is_returned_as_copy():
    ledger = HeadlessLedger()
    ledger.append({"role": "user", "content": "hi"})
    manager = _rm(ledger=ledger)

    contents = manager.get_chat_session_contents("s1")
    contents.append({"role": "assistant", "content": "tampered"})

    assert manager.get_chat_session_contents("s1") == [{"role": "user", "content": "hi"}]
    assert manager.get_chat_session_context("s1") == [{"role": "user", "content": "hi"}]


def test_event_publishing_is_disabled():
    manager = _rm()
    assert manager.event_publishing_enabled() is False
    assert manager.publish_event(object()) is None


PLATFORM_CALLS = [
    ("get_client", (), {}),
    ("retrieve_knowledgebase", (1,), {}),
    ("retrieve_knowledge", (1,), {}),
    ("is_resume_session", ("s1",), {}),
    ("create_tool_approval", ({"x": 1},), {}),
    ("update_chat_session_sandbox_pv_id", ("s1", "pv"), {}),
    ("update_chat_session_caller_context", ("s1", {}), {}),
    ("get_or_create_session", ("s1", "name"), {}),
    ("retrieve_skill", ("skill", "v1", "agent"), {}),
    ("check_agent_call_permission", ("app",), {}),
    ("construct_tool", ("tool",), {}),
    ("get_paas_sbx_client", ({},), {}),
    ("knowledge_query", ({"q": 1},), {}),
    ("start_flow_agent", ({},), {}),
    ("get_flow_agent_task_info", ("t1",), {}),
    ("retry_flow_agent_node", ("s1", "n1"), {}),
    ("skip_flow_agent_node", ("s1", "n1"), {}),
    ("stop_flow_agent_task", ("s1",), {}),
    ("pause_flow_agent_task", ("s1",), {}),
    ("resume_flow_agent_task", ("s1",), {}),
    ("get_flow_agent_task_node_info", ("t1", "n1"), {}),
]


@pytest.mark.parametrize("method,args,kwargs", PLATFORM_CALLS)
def test_platform_methods_fail_explicitly_without_network(method, args, kwargs):
    manager = _rm()
    with patch("requests.post") as requests_post, patch("httpx.post") as httpx_post:
        with pytest.raises(HeadlessUnsupportedError) as exc_info:
            getattr(manager, method)(*args, **kwargs)
        assert exc_info.value.code == "platform_api_unsupported"
        assert exc_info.value.detail["method"] == method
        requests_post.assert_not_called()
        httpx_post.assert_not_called()


def test_empty_mcp_config_returns_empty_result_without_platform():
    with patch("requests.post") as requests_post:
        result = _rm().construct_mcp({})
        assert result.tools == []
        assert result.fetch_failures == []
        requests_post.assert_not_called()


# ============================================================================
# 子资源加载器叠加（--resources）
# ============================================================================


def test_sources_are_stacked_in_order_and_runtime_model_wins():
    from aidev_agent_cli.resources.loaders import ResourceOverrides

    rm = LocalResourceManager.build(
        sources=("injected", "runtime"),
        overrides=ResourceOverrides(model="selected"),
        agent_code="agent-hook",
        model="injected-model",
    )

    assert [type(loader).name for loader in rm._loaders] == ["injected", "runtime"]
    assert rm.uses_platform is False
    assert rm.get_agent_config("agent-hook").chat_model == "selected"


def test_every_given_source_gets_a_loader():
    from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

    rm = LocalResourceManager.build({"BKPAAS_APP_CODE": "app"}, "tester", sources=PLATFORM_SOURCES)

    assert rm.sources == ("platform", "approval", "local", "mcp", "knowledge", "workspace", "runtime")
    assert rm.uses_platform is True
    assert rm.platform_loader is rm._loaders[0]
    assert rm.workspace_files in rm._loaders