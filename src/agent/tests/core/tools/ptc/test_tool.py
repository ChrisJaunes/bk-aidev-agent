"""End-to-end model tool, real code process, Runtime ToolNode and data boundary."""

import json
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState

from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.tools.ptc.session import PtcSessionManager
from aidev_agent.core.tools.ptc.tool import ExecutePythonTool, PtcToolExecutor
from aidev_agent.pydantic_models import AgentExecutorKwargs, SecuritySettings


@pytest.fixture
def tool_env(bridge, ptc_settings):
    secret = "unique-runtime-secret-123"
    security = SecuritySettings(redaction={"known_sensitive_values": secret})
    calls = []

    def echo(value: int) -> dict:
        """Return structured data in the Runtime process."""
        calls.append(value)
        return {"value": value, "pid": os.getpid(), "secret": secret}

    target = StructuredTool.from_function(echo)
    executor = PtcToolExecutor(PtcSessionManager(bridge, ptc_settings), ptc_settings, security)
    executor.bind([target])
    node = build_tool_node([ExecutePythonTool(executor=executor)], security_settings=security)
    graph = StateGraph(MessagesState)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile(), calls, executor, target


async def execute(app, code):
    request = AIMessage(content="", tool_calls=[{"name": "execute_python", "id": "parent", "args": {"code": code}}])
    output = await app.ainvoke({"messages": [request]})
    return output, json.loads(output["messages"][-1].content)


class TestPtcTool:
    async def test_structured_data_and_runtime_identity(self, tool_env):
        app, calls, _, _ = tool_env
        output, event = await execute(app, "from agent_tools import call\nresult = call('echo', {'value': 7})")
        assert event["type"] == "finished"
        assert event["result"]["value"] == 7
        assert event["result"]["pid"] == os.getpid() != event["worker_pid"]
        assert "unique-runtime-secret-123" not in event["result"]["secret"]
        assert calls == [7]
        assert len(output["messages"]) == 2

    @pytest.mark.parametrize(
        "call, code",
        [
            ("call('missing', {})", "TOOL_NOT_ALLOWED"),
            ("call('echo', {'value': 'wrong type'})", "INVALID_ARGUMENTS"),
            ("call('echo', {'value': 1, 'config': {}})", "INVALID_ARGUMENTS"),
        ],
    )
    async def test_rejects_before_real_tool_execution(self, tool_env, call, code):
        app, calls, _, _ = tool_env
        program = f"from agent_tools import call, ToolCallError\ntry:\n    {call}\nexcept ToolCallError as e:\n    result = e.code"
        _, event = await execute(app, program)
        assert event["result"] == code
        assert calls == []

    def test_approval_tools_never_enter_ptc_catalog(self, tool_env):
        _, _, executor, target = tool_env
        target.metadata = {"approval": {"enabled": True}}
        executor.bind([target])
        assert executor.tools == {}

    def test_runtime_is_not_in_model_schema(self, tool_env):
        _, _, executor, _ = tool_env
        schema = ExecutePythonTool(executor=executor).tool_call_schema.model_json_schema()
        assert set(schema["properties"]) == {"code", "inputs"}


class RecordingModel:
    model_name = "gpt-4o"

    def __init__(self, code):
        self.responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_python",
                        "id": "parent",
                        "args": {"code": code},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
        self.bound_names = []

    def invoke(self, messages, config=None, **kwargs):
        return self.responses.pop(0)

    def with_config(self, config):
        return RunnableLambda(self.invoke).with_config(config)

    def bind_tools(self, tools, **kwargs):
        self.bound_names = [tool.name for tool in tools]
        return RunnableLambda(self.invoke)


@pytest.mark.parametrize("lazy", [False, True])
async def test_react_graph_assembles_and_executes_ptc(tool_env, monkeypatch, lazy):
    from aidev_agent.core.graphs.react import graph as module

    _, calls, executor, target = tool_env
    monkeypatch.setattr(module.settings, "BKAI_TOOL_LAZY_LOADING_ENABLED", lazy)
    monkeypatch.setattr(module.settings, "BKAI_TOOL_LAZY_LOADING_THRESHOLD", 1)
    model = RecordingModel("from agent_tools import call\nresult = call('echo', {'value': 9})['value']")
    options = AgentExecutorKwargs(
        llm=model,
        security_settings=executor.security,
        extra_tools=[target],
        ptc_settings=executor.settings,
        ptc_session_manager=executor.manager,
    )
    app, config = ReActAgentBuilder().set_bkai_options(options).build()
    config["configurable"]["thread_id"] = "ptc-test"
    output = await app.ainvoke({"messages": [HumanMessage(content="run code")]}, config)
    result = json.loads(next(message.content for message in output["messages"] if message.type == "tool"))
    assert result["result"] == 9 and calls == [9]
    assert "execute_python" in model.bound_names
    assert ("echo" in model.bound_names) is not lazy


@pytest.mark.parametrize("event_type", ["on_tool_end", "on_custom_event", "on_chain_end"])
async def test_internal_events_do_not_become_ui_or_history(event_type):
    from aidev_agent.core.ag_ui.agent import LangGraphAgent

    agent = LangGraphAgent.__new__(LangGraphAgent)
    event = {"event": event_type, "metadata": {"ptc_internal": True}, "data": {"output": "private data"}}
    assert [value async for value in agent._handle_single_event(event, {})] == []


async def test_code_failure_has_error_tool_status(tool_env):
    app, _, _, _ = tool_env
    output, event = await execute(app, "raise ValueError('expected')")
    assert event["type"] == "failed"
    assert output["messages"][-1].status == "error"
