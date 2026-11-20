"""File-backed observations across the production spawn boundary."""

import json
import os
from pathlib import Path


class WorkerRecords:
    def __init__(self, variable):
        self.variable = variable

    def records(self):
        path = Path(os.environ[self.variable])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def append(self, value):
        with Path(os.environ[self.variable]).open("a") as stream:
            stream.write(json.dumps(value, default=str) + "\n")

    def clear(self):
        Path(os.environ[self.variable]).write_text("")

    def __iter__(self):
        return iter(self.records())

    def __bool__(self):
        return bool(self.records())

    def __getitem__(self, index):
        return self.records()[index]

    def __eq__(self, other):
        return self.records() == other


def record_builds():
    from aidev_agent.services.agent.factory import DefaultAgentFactory, agent_factory

    class RecordingFactory(DefaultAgentFactory):
        def create(self, **params):
            WorkerRecords("RUNTIME_TEST_BUILDS").append({
                "session_code": params.get("session_code"),
                "build_type": params["build_type"].value,
                "session_context_data": params.get("session_context_data"),
            })
            return super().create(**params)

    agent_factory.replace_defaults(RecordingFactory())
    from langchain_core.tools import tool

    @tool
    def observation_marker() -> str:
        """Test-only build observation marker; never requested by the fake model."""
        return "observed"

    return observation_marker

def probe_tool_factory():
    import sys

    from langchain_core.tools import tool

    from aidev_agent_cli.resources.manager import LocalResourceManager
    from aidev_agent_cli.runtime.sdk.session import SDKSession

    observations = WorkerRecords("RUNTIME_TEST_PLATFORM")
    for name in ("get_client", "create_tool_approval"):
        original = getattr(LocalResourceManager, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            observations.append({"call": _name})
            return _original(self, *args, **kwargs)

        setattr(LocalResourceManager, name, counted)
    original_close = SDKSession.close

    def close(self):
        try:
            return original_close(self)
        finally:
            observations.append({"modules": sorted(
                name for name in sys.modules
                if name in ("django", "blueapps") or name.startswith(("django.", "blueapps."))
            )})

    SDKSession.close = close

    @tool
    def probe_tool(q: str) -> str:
        """Record exactly one real worker tool invocation."""
        WorkerRecords("RUNTIME_TEST_CALLS").append(q)
        return f"probe:{q}"

    return probe_tool

def execute_error_factory():
    from aidev_agent.services.agent.factory import DefaultAgentFactory, agent_factory
    from langchain_core.tools import tool

    class BoomFactory(DefaultAgentFactory):
        def create(self, **params):
            inner = super().create(**params)
            class BoomAgent:
                event_handler = inner.event_handler
                def execute(self, kwargs):
                    raise RuntimeError("kaboom")
                def stop(self):
                    return inner.stop()
            return BoomAgent()

    agent_factory.replace_defaults(BoomFactory())
    @tool
    def error_marker() -> str:
        """Child-local execution fault fixture."""
        return "unused"
    return error_marker
