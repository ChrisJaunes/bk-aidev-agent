"""Model entry point and authorized Runtime-side tool invocation."""

from __future__ import annotations

import json
from typing import Annotated, Any

from jsonschema import validators
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, InjectedToolArg, ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema
from referencing import Registry

from aidev_agent.core.nodes.tool import ToolNodeSettings
from aidev_agent.core.nodes.tool.node import build_programmatic_tool_node
from aidev_agent.core.tools.tool_lazy_loading.catalog import LazyToolCatalog
from aidev_agent.packages.interrupt_manager import is_approval_configured
from aidev_agent.utils.loop import run_coro_sync

from .protocol import RESERVED_ARGUMENTS, PtcError, encode
from .results import ProgrammaticResult, result_adapter, safe_payload

PTC_TOOL_NAME = "execute_python"
DISALLOWED_TOOLS = frozenset(
    {
        PTC_TOOL_NAME,
        "tool_call",
        "tool_describe",
        "execute",
        "ask_user_question",
        "Agent",
        "sendMessages",
        "activate_skill",
        "write_file",
        "edit_file",
    }
)


class ExecutePythonInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    code: str = Field(
        description="Python code. Use agent_tools.call(name, arguments); assign the final JSON value to result."
    )
    inputs: dict = Field(default_factory=dict, description="JSON input data available as inputs inside Python.")
    runtime: Annotated[ToolRuntime, SkipJsonSchema(), InjectedToolArg] = None


class ExecutePythonTool(BaseTool):
    name: str = PTC_TOOL_NAME
    description: str = (
        "Execute Python in an isolated sandbox and call permitted data tools through the current Runtime."
    )
    args_schema: type[BaseModel] = ExecutePythonInput
    executor: Any = Field(exclude=True)

    def _run(self, code: str, inputs: dict | None = None, runtime: Any = None):
        return run_coro_sync(lambda: self._arun(code, inputs, runtime))

    async def _arun(self, code: str, inputs: dict | None = None, runtime: Any = None):
        if runtime is None or not runtime.tool_call_id:
            raise ToolException("execute_python requires a Runtime-bound tool call")
        try:
            event = await self.executor.execute(runtime.tool_call_id, code, inputs or {}, runtime)
            return ToolMessage(
                content=json.dumps(event, ensure_ascii=False, allow_nan=False),
                name=self.name,
                tool_call_id=runtime.tool_call_id,
                status="success" if event["type"] == "finished" else "error",
            )
        except PtcError as error:
            raise ToolException(f"PTC {error.code}: {error}") from error


class PtcToolExecutor:
    """Same target instances, hooks and approvals as ToolNode; private messages."""

    def __init__(self, manager, settings, security, middleware=(), hook_runner=None, known_secrets=()):
        self.manager = manager
        self.settings = settings
        if (
            security is None
            or not security.redaction.enable_redact_secrets
            or not security.redaction.enable_redact_registered_secrets
        ):
            raise ValueError("PTC requires credential redaction to be enabled")
        known = [
            security.redaction.known_sensitive_values,
            *(value for value in known_secrets if isinstance(value, str)),
        ]
        redaction = security.redaction.model_copy(update={"known_sensitive_values": ",".join(filter(None, known))})
        self.security = security.model_copy(update={"redaction": redaction})
        self.middleware = middleware
        self.hook_runner = hook_runner
        self.tools = {}
        self.validators = {}
        self.node = None

    def bind(self, tools):
        self.tools = {
            tool.name: tool
            for tool in tools
            if tool.name in self.settings.tool_codecs
            and tool.name not in DISALLOWED_TOOLS
            and not is_approval_configured(tool)
            and not tool.return_direct
        }
        for name, tool in self.tools.items():
            schema = LazyToolCatalog.parameters_schema(tool)
            # Explicit registry prevents JSON Schema references from causing network I/O.
            validator = validators.validator_for(schema)
            validator.check_schema(schema)
            self.validators[name] = validator(schema, registry=Registry())
        options = ToolNodeSettings(
            enable_tool_loop_guard=False,
            use_json_repair_on_error=False,
            use_tool_redaction=False,
            use_tool_untrusted_sanitize=False,
            use_result_limit=False,
        )
        self.node = build_programmatic_tool_node(
            list(self.tools.values()),
            node_options=options,
            security_settings=self.security,
            wrappers=[
                m.wrap_tool_call
                for m in self.middleware
                if m.__class__.wrap_tool_call is not AgentMiddleware.wrap_tool_call
            ],
            async_wrappers=[
                m.awrap_tool_call
                for m in self.middleware
                if m.__class__.awrap_tool_call is not AgentMiddleware.awrap_tool_call
            ],
            hook_runner=self.hook_runner,
            result_adapter=result_adapter(self.settings.tool_codecs, self.settings, self.security),
        )

    async def execute(self, key, code, inputs, runtime):
        execute_kwargs = runtime.config.get("configurable", {}).get("execute_kwargs")
        if getattr(execute_kwargs, "resume", None):
            raise PtcError("RESUME_UNSUPPORTED", "PTC does not yet support human-interrupt or cross-Worker resume")

        async def invoke(event):
            return await self._invoke(event, runtime)

        result = await self.manager.execute(key, code, inputs, invoke)
        return safe_payload(result, self.security)

    async def _invoke(self, event, runtime):
        name, arguments = event["name"], event["arguments"]
        if name not in self.tools:
            return {
                "ok": False,
                "error": {"code": "TOOL_NOT_ALLOWED", "message": "Tool is not available to this PTC execution"},
            }
        forbidden = RESERVED_ARGUMENTS | {"authorization", "access_token", "app_secret", "x_bkapi_authorization"}
        if any(key.lower().replace("-", "_").split("__")[-1] in forbidden for key in arguments):
            return {
                "ok": False,
                "error": {"code": "INVALID_ARGUMENTS", "message": "Runtime-owned parameters cannot be supplied"},
            }
        try:
            encode(arguments, 64 * 1024)
            self.validators[name].validate(arguments)
        except Exception:
            return {
                "ok": False,
                "error": {"code": "INVALID_ARGUMENTS", "message": "Tool arguments do not match the schema"},
            }
        call = {"id": event["call_id"], "name": name, "args": arguments, "type": "tool_call"}
        state = {**runtime.state, "messages": [AIMessage(content="", tool_calls=[call])]}
        config = {
            **runtime.config,
            "metadata": {
                **runtime.config.get("metadata", {}),
                "parent_tool_call_id": runtime.tool_call_id,
                "ptc_internal": True,
            },
        }
        output = await self.node.ainvoke(state, config)
        messages = output.get("messages", []) if isinstance(output, dict) else []
        if len(messages) != 1 or not isinstance(messages[0].artifact, ProgrammaticResult):
            raise PtcError("UNSUPPORTED_RESULT", "PTC tool did not produce a programmatic result")
        return messages[0].artifact.payload
