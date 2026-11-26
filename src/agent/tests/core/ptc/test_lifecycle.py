"""Standard ChatCompletionAgent owns a separate, credential-free PTC backend."""

from unittest.mock import MagicMock

from aidev_agent.core.tools.runtime_tools.paas_backend import PaasSandboxBackend
from aidev_agent.services.agent.chat import ChatCompletionAgent


def test_chat_owns_and_closes_its_dedicated_ptc_backend(ptc_settings, monkeypatch):
    resource_manager = MagicMock()
    close = MagicMock()
    monkeypatch.setattr(PaasSandboxBackend, "close", close)
    info = {"app_code": "test-app", "access_token": "runtime-only-token"}
    chat = ChatCompletionAgent(ptc_settings=ptc_settings, resource_manager=resource_manager, executor_info=info)
    manager = chat._get_ptc_session_manager()
    backend = manager.transport.backend
    assert backend._env_vars == {} and backend._snapshot == "test-image"
    assert backend._request_timeout == ptc_settings.http_timeout
    resource_manager.get_paas_sbx_client.assert_called_once_with(info)
    assert chat._get_ptc_session_manager() is manager
    chat.release_resources()
    close.assert_called_once()
    assert chat.ptc_session_manager is None


def test_disabled_ptc_does_not_construct_backend():
    chat = ChatCompletionAgent()
    assert chat._get_ptc_session_manager() is None
