# -*- coding: utf-8 -*-
"""T-9：运行时契约与四类覆盖（REQ-9 / REQ-10）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.nodes.tool.pydantic_models import ToolNodeSettings
from aidev_agent.core.tools.code_mode import CodeModeBridge, CodeModeInvoker, build_manifest
from aidev_agent.core.tools.code_mode.bridge import ensure_code_mode_capable
from aidev_agent.core.tools.code_mode.errors import STATUS_OK, CodeModeUnsupported
from aidev_agent.core.tools.runtime_tools.bubblewrap_backend import BubblewrapFilesystemBackend
from aidev_agent.core.tools.runtime_tools.e2b_backend import E2BSandboxBackend
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.core.tools.runtime_tools.paas_backend import PaasSandboxBackend
from aidev_agent.core.tools.runtime_tools.types import RuntimeBackend
from aidev_agent.pydantic_models import SecuritySettings
from langchain_core.tools import StructuredTool

CONTRACT_METHODS = ("ls_info", "upload_files", "download_files", "execute", "scratch_dir")


def _paas_backend() -> PaasSandboxBackend:
    return PaasSandboxBackend(
        client=MagicMock(),
        snapshot="snapshot",
        snapshot_entrypoint=["python", "-m", "server"],
        env_vars={"STORAGE_PATH": "/app/storage"},
    )


@pytest.mark.parametrize(
    "factory, expected",
    [
        (lambda tmp: FilesystemBackend(root_dir=tmp), ".aidev_code_mode/run1"),
        (lambda tmp: BubblewrapFilesystemBackend(root_dir=tmp), ".aidev_code_mode/run1"),
        (lambda tmp: E2BSandboxBackend(), "/tmp/.aidev_code_mode/run1"),
        (lambda tmp: _paas_backend(), "$STORAGE_PATH/.aidev_code_mode/run1"),
    ],
)
def test_backends_implement_code_mode_contract(tmp_path, factory, expected):
    backend = factory(tmp_path)

    assert backend.scratch_dir("run1") == expected
    for name in CONTRACT_METHODS:
        assert callable(getattr(backend, name, None))


def test_base_class_declares_contract():
    for name in CONTRACT_METHODS:
        assert hasattr(RuntimeBackend, name)


def test_missing_capability_raises_unsupported():
    class ExecOnlyBackend(RuntimeBackend):
        def ls_info(self, path, **kwargs):  # noqa: ARG002
            return []

        def execute(self, command, timeout=120, max_output_size=100000, **kwargs):  # noqa: ARG002
            return SimpleNamespace(output="", exit_code=0, truncated=False)

    with pytest.raises(CodeModeUnsupported):
        ensure_code_mode_capable(ExecOnlyBackend())


class _ContractOnlyBackend(RuntimeBackend):
    """只暴露 code mode 契约方法的后端（模拟 E2B / paas 的能力面）。"""

    def __init__(self, delegate: FilesystemBackend) -> None:
        self._delegate = delegate

    def ls_info(self, path, **kwargs):
        return self._delegate.ls_info(path, **kwargs)

    def upload_files(self, files, **kwargs):
        return self._delegate.upload_files(files, **kwargs)

    def download_files(self, paths, **kwargs):
        return self._delegate.download_files(paths, **kwargs)

    def execute(self, command, timeout=120, max_output_size=100000, **kwargs):
        return self._delegate.execute(command, timeout, max_output_size, **kwargs)

    def scratch_dir(self, name):
        return self._delegate.scratch_dir(name)


@pytest.mark.asyncio
async def test_contract_only_backend_completes_round_trip(tmp_path):
    def _echo(text: str) -> str:
        """原样返回文本。"""
        return text

    tools = [StructuredTool.from_function(func=_echo, name="echo", description="原样返回文本。")]
    backend = _ContractOnlyBackend(FilesystemBackend(root_dir=tmp_path))
    invoker = CodeModeInvoker(
        tools=tools,
        tool_node=build_tool_node(
            tools, node_options=ToolNodeSettings(use_timer=False), security_settings=SecuritySettings()
        ),
    )
    bridge = CodeModeBridge(backend=backend, invoker=invoker, manifest=build_manifest(tools), poll_interval=0.01)

    result = await bridge.arun('print(echo(text="contract"))')

    assert result.status == STATUS_OK
    assert "contract" in result.output
