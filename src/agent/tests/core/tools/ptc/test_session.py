"""Runtime execution ledger and lost-response recovery against real processes."""

import os

import pytest

from aidev_agent.core.tools.ptc.protocol import PtcError
from aidev_agent.core.tools.ptc.session import PtcSessionManager


class TestSession:
    @pytest.mark.parametrize("drop", [None, "start", "resume"])
    async def test_lost_response_never_reexecutes_tool(self, bridge, ptc_settings, drop):
        bridge.drop_operation = drop
        manager = PtcSessionManager(bridge, ptc_settings)
        calls = []

        async def invoke(event):
            calls.append((event["call_id"], os.getpid()))
            return {"ok": True, "value": {"number": 7}}

        code = "from agent_tools import call\nresult = call('echo', {})['number']"
        event = await manager.execute("parent", code, {}, invoke)
        assert event["result"] == 7
        assert len(calls) == 1 and calls[0][1] == os.getpid()
        assert await manager.execute("parent", code, {}, invoke) == event
        assert len(calls) == 1

    async def test_same_parent_with_changed_code_is_a_conflict(self, bridge, ptc_settings):
        manager = PtcSessionManager(bridge, ptc_settings)
        await manager.execute("parent", "result = 1", {}, None)
        with pytest.raises(PtcError, match="different PTC code"):
            await manager.execute("parent", "result = 2", {}, None)

    async def test_closed_runtime_cannot_start(self, bridge, ptc_settings):
        manager = PtcSessionManager(bridge, ptc_settings)
        manager.close()
        with pytest.raises(PtcError, match="Runtime has ended"):
            await manager.execute("parent", "result = 1", {}, None)

    async def test_runtime_execution_budget_preserves_cached_results(self, bridge, ptc_settings):
        settings = ptc_settings.model_copy(update={"max_executions": 1})
        manager = PtcSessionManager(bridge, settings)
        first = await manager.execute("first", "result = 1", {}, None)
        with pytest.raises(PtcError, match="execution budget"):
            await manager.execute("second", "result = 2", {}, None)
        assert await manager.execute("first", "result = 1", {}, None) == first


async def test_result_budget_failure_is_cached_without_restarting(bridge, ptc_settings):
    settings = ptc_settings.model_copy(update={"max_cached_bytes": 1})
    manager = PtcSessionManager(bridge, settings)
    with pytest.raises(PtcError, match="result cache budget"):
        await manager.execute("parent", "result = 42", {}, None)
    count = len(bridge.requests)
    assert (await manager.execute("parent", "result = 42", {}, None))["type"] == "failed"
    assert len(bridge.requests) == count
