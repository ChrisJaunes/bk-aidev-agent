"""PaaS transport never uses the rebuilding/truncating execute API."""

from unittest.mock import MagicMock

import pytest
from requests import HTTPError, Response

from aidev_agent.core.ptc.protocol import PtcError
from aidev_agent.core.ptc.transport import PaasPtcTransport
from aidev_agent.core.tools.runtime_tools.paas_backend import ExecResult, PaasSandboxBackend


@pytest.fixture
def transport(ptc_settings):
    backend = MagicMock()
    backend._ensure_sandbox.return_value = "original-sandbox"
    backend.exec_command.return_value = ExecResult(stdout='{"type":"running"}', stderr="diagnostic", exit_code=0)
    return PaasPtcTransport(backend, ptc_settings)


class TestPtcTransport:
    async def test_fixed_sandbox_and_unmerged_stdout(self, transport):
        request = {"execution_id": "exec", "op": "start"}
        assert await transport.exchange(request) == {"type": "running"}
        await transport.exchange({**request, "op": "attach"})
        transport.backend._ensure_sandbox.assert_called_once_with()
        transport.backend.upload_file.assert_called_once()
        assert transport.backend.exec_command.call_args.args[0] == "original-sandbox"
        assert transport.backend.exec_command.call_args.kwargs["timeout"] == 30
        transport.backend.execute.assert_not_called()
        transport.backend._run.assert_not_called()

    async def test_missing_sandbox_is_not_recreated(self, transport):
        response = Response()
        response.status_code = 404
        transport.backend.exec_command.side_effect = HTTPError(response=response)
        with pytest.raises(PtcError, match="original PTC sandbox"):
            await transport.exchange({"execution_id": "exec", "op": "attach"})
        transport.backend.create_sandbox.assert_not_called()
        transport.backend._ensure_sandbox.assert_called_once_with()

    def test_backend_passes_timeout_to_actual_http_request(self):
        backend = PaasSandboxBackend(client=MagicMock(), snapshot="test", snapshot_entrypoint=[], env_vars={})
        backend.client.exec_command.request.return_value.json.return_value = {"stdout": "{}", "exit_code": 0}
        backend.exec_command("fixed-id", ["python3", "bridge.py"], timeout=30)
        assert backend.client.exec_command.request.call_args.kwargs["timeout"] == 30

    async def test_large_payload_uses_file_instead_of_oversized_argv(self, transport):
        request = {"execution_id": "exec", "op": "start", "op_id": "op", "code": "x" * 60000}
        await transport.exchange(request)
        command = transport.backend.exec_command.call_args.args[1]
        assert command[2] == "exchange-file"
        assert len(str(command)) < 1024
        assert transport.backend.upload_file.call_count == 2

    def test_provisioning_requests_have_real_timeouts(self):
        client = MagicMock()
        client.create_sandbox.request.return_value.json.return_value = {"uuid": "id"}
        backend = PaasSandboxBackend(
            client=client,
            snapshot="test",
            snapshot_entrypoint=[],
            env_vars={},
            app_code="app",
            request_timeout=30,
        )
        backend.create_sandbox()
        backend.upload_file("id", "/tmp/runner.py", b"code")
        assert client.create_sandbox.request.call_args.kwargs["timeout"] == 30
        assert client.upload_file.request.call_args.kwargs["timeout"] == 30
