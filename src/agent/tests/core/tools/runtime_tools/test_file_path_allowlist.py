# -*- coding: utf-8 -*-
"""文件路径 root jail 白名单与 glob/grep 遍历防护测试。

覆盖：
- ``_resolve_allowed_paths`` 的解析行为（空/未配置 -> 不限制，非空 -> 前缀列表）
- 文件工具在显式配置 ``file_allowed_paths`` 时拒绝白名单外的系统路径（root jail）
- ``$STORAGE_PATH`` 前缀按原文放行（PaaS 沙箱路径）
- glob/grep 工具的路径遍历防护（``..`` 拒绝，独立于白名单，默认生效）
"""

from __future__ import annotations

from tempfile import TemporaryDirectory

import pytest
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.core.tools.runtime_tools.provider import (
    RuntimeBackendResolver,
    _resolve_allowed_paths,
    get_glob_tool,
    get_grep_tool,
    get_read_file_tool,
)
from aidev_agent.packages.security.command.command_security import validate_path
from aidev_agent.pydantic_models import SecuritySettings


def _local_provider(backend: FilesystemBackend, security_settings=None) -> RuntimeBackendResolver:
    return RuntimeBackendResolver(default_runtime="local", security_settings=security_settings).register_runtime(
        "local", backend
    )


class TestResolveAllowedPaths:
    """_resolve_allowed_paths 解析行为。"""

    def test_empty_string_returns_none(self):
        """空字符串 -> 不限制。"""
        assert _resolve_allowed_paths(SecuritySettings(file_allowed_paths="")) is None

    def test_resolver_security_settings_defaults_to_none(self):
        """resolver 未注入 security_settings 时 property 为 None —— 工具不做路径限制。

        （与旧工厂形参默认 None 语义一致。）
        """
        assert RuntimeBackendResolver(default_runtime="local").security_settings is None

    def test_parses_comma_separated_and_strips(self):
        """逗号分隔 + 首尾空格去除。"""
        ss = SecuritySettings(file_allowed_paths=" /workspace , /home , $STORAGE_PATH ")
        assert _resolve_allowed_paths(ss) == ["/workspace", "/home", "$STORAGE_PATH"]


class TestFileRootJail:
    """文件工具 root jail（白名单外拒绝）。"""

    def test_blocks_path_outside_allowlist(self):
        """root jail 启用时拒绝白名单外的系统路径。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            ss = SecuritySettings(file_allowed_paths="/workspace,/home")
            provider = _local_provider(backend, ss)
            tool = get_read_file_tool(provider)

            with pytest.raises(ValueError, match="must start with one of"):
                tool.invoke({"file_path": "/etc/passwd", "target_runtime": "local"})

    def test_validate_path_allows_storage_prefix(self):
        """$STORAGE_PATH 前缀按原文放行（PaaS 沙箱路径）。"""
        result = validate_path("$STORAGE_PATH/session/x", allowed_prefixes=["$STORAGE_PATH"])
        assert result == "$STORAGE_PATH/session/x"


class TestGlobGrepTraversalGuard:
    """glob/grep 工具补上的路径遍历防护。"""

    def test_glob_rejects_traversal(self):
        """glob 的搜索目录不允许路径遍历（默认生效，无需配置白名单）。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_glob_tool(provider)

            with pytest.raises(ValueError, match="Path traversal not allowed"):
                tool.invoke({"pattern": "*.txt", "path": "../etc", "target_runtime": "local"})

    def test_grep_rejects_traversal(self):
        """grep 的搜索目录不允许路径遍历（默认生效，无需配置白名单）。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_grep_tool(provider)

            with pytest.raises(ValueError, match="Path traversal not allowed"):
                tool.invoke({"pattern": "root", "path": "../etc", "target_runtime": "local"})

    def test_glob_rejects_path_outside_allowlist(self):
        """glob 在 root jail 启用时同样拒绝白名单外路径。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            ss = SecuritySettings(file_allowed_paths="/workspace")
            provider = _local_provider(backend, ss)
            tool = get_glob_tool(provider)

            with pytest.raises(ValueError, match="must start with one of"):
                tool.invoke({"pattern": "*.txt", "path": "/etc", "target_runtime": "local"})
