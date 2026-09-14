# -*- coding: utf-8 -*-
"""Tests for RuntimeBackendResolver.

This module contains tests for filesystem tools and runtime routing.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.core.tools.runtime_tools.provider import (
    DEFAULT_READ_LIMIT,
    DEFAULT_READ_OFFSET,
    RuntimeBackendResolver,
    _get_sensitive_values,
    get_client_tools_with_runtime,
    get_edit_file_tool,
    get_execute_tool,
    get_glob_tool,
    get_grep_tool,
    get_ls_tool,
    get_read_file_tool,
    get_write_file_tool,
)
from aidev_agent.core.tools.runtime_tools.types import ExecuteResult
from aidev_agent.packages.security.command.command_security import validate_path


def _local_provider(backend: FilesystemBackend) -> RuntimeBackendResolver:
    return RuntimeBackendResolver(default_runtime="local").register_runtime("local", backend)


def _schema_properties(tool) -> dict:
    # pydantic v1: schema(); pydantic v2: model_json_schema()
    if getattr(tool, "args_schema", None) is None:
        return {}

    schema = None
    if hasattr(tool.args_schema, "schema"):
        schema = tool.args_schema.schema()  # type: ignore[attr-defined]
    elif hasattr(tool.args_schema, "model_json_schema"):
        schema = tool.args_schema.model_json_schema()  # type: ignore[attr-defined]

    if not isinstance(schema, dict):
        return {}
    return schema.get("properties", {}) or {}


class TestValidatePath:
    """Test validate_path function."""

    def test_validate_path_simple(self):
        """Test validating simple path (relative paths kept as-is)."""
        result = validate_path("foo/bar")
        assert result == "foo/bar"

    def test_validate_path_with_leading_slash(self):
        """Test validating path with leading slash."""
        result = validate_path("/foo/bar")
        assert result == "/foo/bar"

    def test_validate_path_normalizes(self):
        """Test that path is normalized."""
        result = validate_path("/./foo//bar")
        assert result == "/foo/bar"

    def test_validate_path_prevents_traversal(self):
        """Test that path traversal is prevented."""
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            validate_path("../etc/passwd")

        with pytest.raises(ValueError, match="Path traversal not allowed"):
            validate_path("foo/../../etc/passwd")

    def test_validate_path_allows_tilde(self):
        """Test that tilde paths are passed through without expansion (SEC-03)."""
        # ~ 路径不做本地展开，由沙箱环境解析
        result = validate_path("~/.bashrc")
        assert result == "~/.bashrc"

    def test_validate_path_windows_absolute(self):
        """Test that Windows absolute paths are rejected."""
        with pytest.raises(ValueError, match="Windows absolute paths are not supported"):
            validate_path("C:/Users/file.txt")

        with pytest.raises(ValueError, match="Windows absolute paths are not supported"):
            validate_path("D:\\Users\\file.txt")

    def test_validate_path_with_allowed_prefixes(self):
        """Test validating path with allowed prefixes."""
        result = validate_path("/data/file.txt", allowed_prefixes=["/data/", "/workspace/"])
        assert result == "/data/file.txt"

    def test_validate_path_not_in_allowed_prefixes(self):
        """Test that paths outside allowed prefixes are rejected."""
        with pytest.raises(ValueError, match="must start with one of"):
            validate_path("/etc/file.txt", allowed_prefixes=["/data/", "/workspace/"])


class TestRuntimeBackendResolver:
    def test_single_runtime_hides_runtime_param(self):
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tools = get_client_tools_with_runtime(provider)

            ls_tool = next(t for t in tools if t.name == "ls")
            props = _schema_properties(ls_tool)
            assert "target_runtime" in props

    def test_multi_runtime_includes_runtime_param(self):
        with TemporaryDirectory() as d1, TemporaryDirectory() as d2:
            provider = RuntimeBackendResolver(default_runtime="local")
            provider.register_runtime("local", FilesystemBackend(root_dir=d1))
            provider.register_runtime("sandbox_1", FilesystemBackend(root_dir=d2))

            tools = get_client_tools_with_runtime(provider)
            execute_tool = next(t for t in tools if t.name == "execute")
            props = _schema_properties(execute_tool)
            assert "target_runtime" in props

            runtime_desc = props["target_runtime"].get("description", "")
            assert "local" in runtime_desc
            assert "sandbox_1" in runtime_desc

    def test_runtime_routing(self):
        class FakeBackend:
            def __init__(self, label: str):
                self.label = label

            def execute(self, command: str, **kwargs) -> ExecuteResult:
                return ExecuteResult(output=f"{self.label}:{command}", exit_code=0, truncated=False)

        provider = RuntimeBackendResolver(default_runtime="sandbox_1")
        provider.register_runtime("local", FakeBackend("local"))
        provider.register_runtime("sandbox_1", FakeBackend("sandbox"))

        execute_tool = next(t for t in get_client_tools_with_runtime(provider) if t.name == "execute")

        # explicit routing to sandbox_1
        res = execute_tool.invoke({"command": "echo test", "target_runtime": "sandbox_1"})
        assert "sandbox:echo test" in res

        # explicit routing
        res = execute_tool.invoke({"command": "echo test", "target_runtime": "local"})
        assert "local:echo test" in res

    def test_invalid_runtime_raises_value_error(self):
        class FakeBackend:
            def execute(self, command: str, **kwargs) -> ExecuteResult:
                return ExecuteResult(output=command, exit_code=0, truncated=False)

        provider = RuntimeBackendResolver(default_runtime="local")
        provider.register_runtime("local", FakeBackend())
        provider.register_runtime("sandbox_1", FakeBackend())

        execute_tool = next(t for t in get_client_tools_with_runtime(provider) if t.name == "execute")

        with pytest.raises(ValueError, match="Unknown runtime"):
            execute_tool.invoke({"command": "echo test", "target_runtime": "nope"})


class TestLsToolGenerator:
    """Test get_ls_tool function."""

    def testget_ls_tool_creates_tool(self):
        """Test that ls tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_ls_tool(provider)

            assert tool.name == "ls"
            assert "列出目录中的所有文件" in tool.description

    def test_ls_tool_with_custom_description(self):
        """Test ls tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom ls description"
            tool = get_ls_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_ls_tool_execution(self):
        """Test executing ls tool."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "file1.txt").write_text("content1")
            (tmppath / "file2.py").write_text("content2")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_ls_tool(provider)

            result = tool.invoke({"path": "/", "target_runtime": "local"})

            assert "file1.txt" in result
            assert "file2.py" in result

    def test_ls_tool_with_virtual_mode(self):
        """Test ls tool in virtual mode."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "test.txt").write_text("content")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_ls_tool(provider)

            result = tool.invoke({"path": "/", "target_runtime": "local"})

            assert "/test.txt" in result


class TestReadFileToolGenerator:
    """Test get_read_file_tool function."""

    def testget_read_file_tool_creates_tool(self):
        """Test that read_file tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_read_file_tool(provider)

            assert tool.name == "read_file"
            assert "从文件系统读取文件" in tool.description

    def test_read_file_with_custom_description(self):
        """Test read_file tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom read description"
            tool = get_read_file_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_read_file_execution(self):
        """Test executing read_file tool."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            test_file = tmppath / "test.txt"
            test_file.write_text("line1\nline2\nline3")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_read_file_tool(provider)

            result = tool.invoke({"file_path": "/test.txt", "target_runtime": "local"})

            assert "line1" in result
            assert "line2" in result
            assert "line3" in result

    def test_read_file_with_offset_and_limit(self):
        """Test read_file tool with offset and limit parameters."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            test_file = tmppath / "test.txt"
            test_file.write_text("line1\nline2\nline3\nline4\nline5")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_read_file_tool(provider)

            result = tool.invoke({"file_path": "/test.txt", "target_runtime": "local", "offset": 2, "limit": 2})

            assert "line3" in result
            assert "line4" in result
            assert "line1" not in result
            assert "line5" not in result

    def test_read_file_defaults(self):
        """Test read_file tool default parameters."""
        assert DEFAULT_READ_OFFSET == 0
        assert DEFAULT_READ_LIMIT == 100


class TestWriteFileToolGenerator:
    """Test get_write_file_tool function."""

    def testget_write_file_tool_creates_tool(self):
        """Test that write_file tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_write_file_tool(provider)

            assert tool.name == "write_file"
            assert "在文件系统中新建文件" in tool.description

    def test_write_file_with_custom_description(self):
        """Test write_file tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom write description"
            tool = get_write_file_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_write_file_execution(self):
        """Test executing write_file tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_write_file_tool(provider)

            result = tool.invoke({"file_path": "/test.txt", "content": "test content", "target_runtime": "local"})

            assert "Updated file /test.txt" in result or "Updated file test.txt" in result
            assert (Path(tmpdir) / "test.txt").read_text() == "test content"

    def test_write_file_existing_file(self):
        """Test write_file tool on existing file."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "test.txt").write_text("old content")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_write_file_tool(provider)

            with pytest.raises(ValueError, match="already exists"):
                tool.invoke({"file_path": "/test.txt", "content": "new content", "target_runtime": "local"})

            assert (Path(tmpdir) / "test.txt").read_text() == "old content"


class TestEditFileToolGenerator:
    """Test get_edit_file_tool function."""

    def testget_edit_file_tool_creates_tool(self):
        """Test that edit_file tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_edit_file_tool(provider)

            assert tool.name == "edit_file"
            assert "精确字符串替换" in tool.description

    def test_edit_file_with_custom_description(self):
        """Test edit_file tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom edit description"
            tool = get_edit_file_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_edit_file_execution(self):
        """Test executing edit_file tool."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            test_file = tmppath / "test.txt"
            test_file.write_text("hello world\nhello python")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_edit_file_tool(provider)

            result = tool.invoke(
                {
                    "file_path": "/test.txt",
                    "old_string": "world",
                    "new_string": "universe",
                    "target_runtime": "local",
                }
            )

            assert "replaced" in result.lower()
            assert test_file.read_text() == "hello universe\nhello python"

    def test_edit_file_replace_all(self):
        """Test edit_file tool with replace_all=True."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            test_file = tmppath / "test.txt"
            test_file.write_text("hello world\nhello python")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_edit_file_tool(provider)

            result = tool.invoke(
                {
                    "file_path": "/test.txt",
                    "old_string": "hello",
                    "new_string": "hi",
                    "replace_all": True,
                    "target_runtime": "local",
                }
            )

            assert "2" in result  # 2 occurrences replaced
            assert test_file.read_text() == "hi world\nhi python"


class TestGlobToolGenerator:
    """Test get_glob_tool function."""

    def testget_glob_tool_creates_tool(self):
        """Test that glob tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_glob_tool(provider)

            assert tool.name == "glob"
            assert "按 glob 模式查找文件" in tool.description

    def test_glob_with_custom_description(self):
        """Test glob tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom glob description"
            tool = get_glob_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_glob_execution(self):
        """Test executing glob tool."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "file1.txt").write_text("content1")
            (tmppath / "file2.txt").write_text("content2")
            (tmppath / "script.py").write_text("content3")

            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_glob_tool(provider)

            result = tool.invoke({"pattern": "*.txt", "target_runtime": "local"})

            assert "file1.txt" in result
            assert "file2.txt" in result
            assert ".py" not in result

    def test_glob_with_path_parameter(self):
        """Test glob tool with path parameter."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "file.txt").write_text("content")
            (tmppath / "subdir").mkdir()
            (tmppath / "subdir" / "nested.txt").write_text("nested")

            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = _local_provider(backend)
            tool = get_glob_tool(provider)

            result = tool.invoke({"pattern": "*.txt", "target_runtime": "local", "path": "/subdir"})

            assert "nested.txt" in result


class TestGrepToolGenerator:
    """Test get_grep_tool function."""

    def testget_grep_tool_creates_tool(self):
        """Test that grep tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_grep_tool(provider)

            assert tool.name == "grep"
            assert "搜索文本模式" in tool.description

    def test_grep_with_custom_description(self):
        """Test grep tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom grep description"
            tool = get_grep_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_grep_execution(self):
        """Test executing grep tool."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "file1.txt").write_text("hello world\nhello python")
            (tmppath / "file2.txt").write_text("goodbye world")

            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_grep_tool(provider)

            result = tool.invoke({"pattern": "hello", "target_runtime": "local"})

            assert "file1.txt" in result

    def test_grep_with_glob_filter(self):
        """Test grep tool with glob filter."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "test.py").write_text("import os")
            (tmppath / "test.txt").write_text("import os")

            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_grep_tool(provider)

            result = tool.invoke({"pattern": "import", "target_runtime": "local", "glob": "*.py"})

            assert ".py" in result
            assert ".txt" not in result

    def test_grep_content_mode(self):
        """Test grep tool with content output mode."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "file.txt").write_text("line1\nline2")

            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_grep_tool(provider)

            result = tool.invoke({"pattern": "line", "target_runtime": "local", "output_mode": "content"})

            assert "line1" in result or "line2" in result


class TestExecuteToolGenerator:
    """Test get_execute_tool function."""

    def testget_execute_tool_creates_tool(self):
        """Test that execute tool generator creates a valid tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider)

            assert tool.name == "execute"
            assert "执行 shell 命令" in tool.description

    def test_execute_with_custom_description(self):
        """Test execute tool with custom description."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            custom_desc = "Custom execute description"
            tool = get_execute_tool(provider, custom_description=custom_desc)

            assert tool.description == custom_desc

    def test_execute_execution(self):
        """Test executing execute tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider)

            result = tool.invoke({"command": "echo test", "target_runtime": "local"})

            assert "test" in result

    @pytest.mark.asyncio
    async def test_execute_async_execution(self):
        """Test async execution of execute tool."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider)

            result = await tool.ainvoke({"command": "echo test", "target_runtime": "local"})

            assert "test" in result


class TestExecuteToolSecurity:
    """Test get_execute_tool security functionality."""

    def test_execute_with_enable_security_none_default_enabled(self):
        """测试 enable_security=None 时默认启用校验（危险命令抛出 ValueError）"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            # enable_security=None 是默认值，应该启用安全校验
            tool = get_execute_tool(provider, enable_security=None)

            # rm 是危险命令，不在白名单中，应抛出 ValueError
            with pytest.raises(ValueError, match="命令执行被拒绝"):
                tool.invoke({"command": "rm -rf /some/path", "target_runtime": "local"})

    def test_execute_with_enable_security_true(self):
        """测试 enable_security=True 时启用校验（危险命令抛出 ValueError）"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider, enable_security=True)

            # rm 是危险命令，不在白名单中，应抛出 ValueError
            with pytest.raises(ValueError, match="命令执行被拒绝"):
                tool.invoke({"command": "rm -rf /some/path", "target_runtime": "local"})

    def test_execute_with_enable_security_false(self):
        """测试 enable_security=False 时禁用校验（危险命令可执行）"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider, enable_security=False)

            # 即使是危险命令，禁用安全校验后也应执行
            result = tool.invoke({"command": "echo dangerous_test", "target_runtime": "local"})
            assert "dangerous_test" in result

    def test_execute_allowed_command_with_security_enabled(self):
        """测试启用安全校验时，白名单命令可正常执行"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider, enable_security=True)

            # ls 是白名单命令，应正常执行
            result = tool.invoke({"command": "ls -la", "target_runtime": "local"})
            assert "命令执行被拒绝" not in result

    @pytest.mark.asyncio
    async def test_execute_async_with_security_enabled(self):
        """测试异步执行时安全校验同样生效"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider, enable_security=True)

            # 危险命令在异步执行时应抛出 ValueError
            with pytest.raises(ValueError, match="命令执行被拒绝"):
                await tool.ainvoke({"command": "rm -rf /", "target_runtime": "local"})

    @pytest.mark.asyncio
    async def test_execute_async_with_security_disabled(self):
        """测试异步执行时禁用安全校验"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tool = get_execute_tool(provider, enable_security=False)

            # 禁用安全校验后异步执行也应正常
            result = await tool.ainvoke({"command": "echo async_test", "target_runtime": "local"})
            assert "async_test" in result


class TestGetClientToolsWithRuntimeSecurity:
    """Test get_client_tools_with_runtime security parameter."""

    def test_get_client_tools_with_runtime_security_none(self):
        """测试 get_client_tools_with_runtime 默认启用安全校验"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tools = get_client_tools_with_runtime(provider, enable_security=None)

            execute_tool = next(t for t in tools if t.name == "execute")
            # 危险命令应抛出 ValueError
            with pytest.raises(ValueError, match="命令执行被拒绝"):
                execute_tool.invoke({"command": "rm -rf /", "target_runtime": "local"})

    def test_get_client_tools_with_runtime_security_true(self):
        """测试 get_client_tools_with_runtime enable_security=True 启用校验"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tools = get_client_tools_with_runtime(provider, enable_security=True)

            execute_tool = next(t for t in tools if t.name == "execute")
            # 危险命令应抛出 ValueError
            with pytest.raises(ValueError, match="命令执行被拒绝"):
                execute_tool.invoke({"command": "rm -rf /", "target_runtime": "local"})

    def test_get_client_tools_with_runtime_security_false(self):
        """测试 get_client_tools_with_runtime enable_security=False 禁用校验"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tools = get_client_tools_with_runtime(provider, enable_security=False)

            execute_tool = next(t for t in tools if t.name == "execute")
            # 禁用安全校验后命令可执行
            result = execute_tool.invoke({"command": "echo no_security_check", "target_runtime": "local"})
            assert "no_security_check" in result

    def test_get_client_tools_with_runtime_returns_seven_tools(self):
        """测试 get_client_tools_with_runtime 返回 7 个工具"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = _local_provider(backend)
            tools = get_client_tools_with_runtime(provider)

            tool_names = {t.name for t in tools}
            expected_names = {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}
            assert tool_names == expected_names


class TestToolIntegration:
    """Integration tests for tools working together."""

    def test_write_then_read_flow(self):
        """Test write file followed by read file workflow."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            tools = get_client_tools_with_runtime(_local_provider(backend))

            write_tool = next(t for t in tools if t.name == "write_file")
            read_tool = next(t for t in tools if t.name == "read_file")

            write_tool.invoke({"file_path": "/test.txt", "content": "hello world", "target_runtime": "local"})
            result = read_tool.invoke({"file_path": "/test.txt", "target_runtime": "local"})

            assert "hello world" in result

    def test_ls_glob_and_grep_flow(self):
        """Test using ls, glob, and grep together."""
        with TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "test1.txt").write_text("import os")
            (tmppath / "test2.txt").write_text("import sys")
            (tmppath / "main.py").write_text("print('hello')")

            backend = FilesystemBackend(root_dir=tmpdir)
            tools = get_client_tools_with_runtime(_local_provider(backend))

            ls_tool = next(t for t in tools if t.name == "ls")
            glob_tool = next(t for t in tools if t.name == "glob")
            grep_tool = next(t for t in tools if t.name == "grep")

            ls_result = ls_tool.invoke({"path": "/", "target_runtime": "local"})
            assert len(ls_result) > 0

            glob_result = glob_tool.invoke({"pattern": "*.py", "target_runtime": "local"})
            assert ".py" in glob_result

            grep_result = grep_tool.invoke({"pattern": "import", "target_runtime": "local"})
            assert "txt" in grep_result or "test" in grep_result

    def test_write_edit_read_flow(self):
        """Test write, edit, and read workflow."""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            tools = get_client_tools_with_runtime(_local_provider(backend))

            write_tool = next(t for t in tools if t.name == "write_file")
            edit_tool = next(t for t in tools if t.name == "edit_file")
            read_tool = next(t for t in tools if t.name == "read_file")

            write_tool.invoke(
                {"file_path": "/test.txt", "content": "hello world\nhello python", "target_runtime": "local"}
            )
            edit_tool.invoke(
                {"file_path": "/test.txt", "old_string": "world", "new_string": "universe", "target_runtime": "local"}
            )
            result = read_tool.invoke({"file_path": "/test.txt", "target_runtime": "local"})

            assert "hello universe" in result
            assert "hello python" in result


class TestOutputRedaction:
    """测试工具返回值脱敏功能。"""

    def test_ls_tool_redacts_sensitive_value(self):
        """ls 工具应脱敏输出中的敏感值。"""
        from aidev_agent.config import settings

        original = settings.SBX_SENSITIVE_VALUES
        settings.SBX_SENSITIVE_VALUES = ["secret_dir"]
        try:
            with TemporaryDirectory() as tmpdir:
                tmppath = Path(tmpdir)
                (tmppath / "secret_dir").mkdir()

                backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
                provider = RuntimeBackendResolver(default_runtime="local").register_runtime("local", backend)
                tool = get_ls_tool(provider)

                result = tool.invoke({"path": "/", "target_runtime": "local"})
                assert "secret_dir" not in result
                assert "__BKAI_AGENT_REDACTED__" in result
        finally:
            settings.SBX_SENSITIVE_VALUES = original

    def test_ls_tool_raises_value_error_for_unknown_runtime(self):
        """ls 工具在 _resolve_backend 抛出 ValueError 时传播异常。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = RuntimeBackendResolver(default_runtime="local").register_runtime("local", backend)
            tool = get_ls_tool(provider)

            # 传入不存在的 runtime，_resolve_backend 抛出 ValueError
            with pytest.raises(ValueError, match="Unknown runtime"):
                tool.invoke({"path": "/", "target_runtime": "secret_runtime"})


class TestEmptyOutputHint:
    """测试空输出友好提示功能。"""

    def test_ls_empty_result_returns_hint(self):
        """ls 工具空结果应返回友好提示。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir, virtual_mode=True)
            provider = RuntimeBackendResolver(default_runtime="local").register_runtime("local", backend)
            tool = get_ls_tool(provider)

            # 列出不存在的目录，结果为空列表
            result = tool.invoke({"path": "/nonexistent", "target_runtime": "local"})
            assert "[harness]" in result

    def test_execute_empty_output_returns_hint(self):
        """execute 工具空输出应返回友好提示。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = RuntimeBackendResolver(default_runtime="local").register_runtime("local", backend)
            tool = get_execute_tool(provider, enable_security=False)

            # true 命令无输出
            result = tool.invoke({"command": "true", "target_runtime": "local"})
            assert "[harness]" in result

    def test_non_empty_output_no_hint(self):
        """非空输出不应包含提示。"""
        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            provider = RuntimeBackendResolver(default_runtime="local").register_runtime("local", backend)
            tool = get_execute_tool(provider, enable_security=False)

            result = tool.invoke({"command": "echo hello", "target_runtime": "local"})
            assert "[harness]" not in result
            assert "hello" in result


class TestConfigStateInjection:
    """测试工具函数 config/state 注入签名。"""

    def test_ls_tool_has_config_param(self):
        """ls 工具函数签名应包含 config: RunnableConfig 参数（无 Optional）。"""
        from typing import get_type_hints

        from langchain_core.runnables import RunnableConfig

        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            resolver = RuntimeBackendResolver(default_runtime="local")
            resolver.register_runtime("local", backend)
            tool = get_ls_tool(resolver)

            hints = get_type_hints(tool.func, include_extras=True)
            assert "config" in hints, "ls 工具函数缺少 config 参数"
            assert hints["config"] is RunnableConfig, f"config 类型应为 RunnableConfig，实际为 {hints['config']}"

    def test_ls_tool_has_state_param(self):
        """ls 工具函数签名应包含 state: Annotated[dict, InjectedState] 参数。"""
        from typing import get_type_hints

        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            resolver = RuntimeBackendResolver(default_runtime="local")
            resolver.register_runtime("local", backend)
            tool = get_ls_tool(resolver)

            hints = get_type_hints(tool.func, include_extras=True)
            assert "state" in hints, "ls 工具函数缺少 state 参数"

    def test_execute_tool_has_config_state_params(self):
        """execute 工具函数签名应包含 config 和 state 参数。"""
        from typing import get_type_hints

        from langchain_core.runnables import RunnableConfig

        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            resolver = RuntimeBackendResolver(default_runtime="local")
            resolver.register_runtime("local", backend)
            tool = get_execute_tool(resolver, enable_security=False)

            hints = get_type_hints(tool.func, include_extras=True)
            assert "config" in hints
            assert hints["config"] is RunnableConfig
            assert "state" in hints

    def test_async_execute_has_config_state_params(self):
        """async_execute 签名应与 execute 完全一致。"""
        from typing import get_type_hints

        from langchain_core.runnables import RunnableConfig

        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            resolver = RuntimeBackendResolver(default_runtime="local")
            resolver.register_runtime("local", backend)
            tool = get_execute_tool(resolver, enable_security=False)

            # 验证 coroutine (async_execute) 的签名
            assert tool.coroutine is not None, "execute 工具缺少 coroutine"
            sync_hints = get_type_hints(tool.func, include_extras=True)
            async_hints = get_type_hints(tool.coroutine, include_extras=True)
            assert "config" in async_hints
            assert async_hints["config"] is RunnableConfig
            assert "state" in async_hints
            # 确保同步/异步 config 类型一致
            assert sync_hints["config"] is async_hints["config"]

    def test_tool_invoke_backward_compatible(self):
        """工具不传 config/state 时仍可正常调用（向后兼容）。"""
        with TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("hello")
            backend = FilesystemBackend(root_dir=tmpdir)
            resolver = RuntimeBackendResolver(default_runtime="local")
            resolver.register_runtime("local", backend)
            tool = get_ls_tool(resolver)

            # LangChain 自动注入空 RunnableConfig，不传 config/state 不会报错
            result = tool.invoke({"path": "/", "target_runtime": "local"})
            assert isinstance(result, str)

    def test_all_tools_have_config_state(self):
        """所有 7 个工具函数均应包含 config 和 state 参数。"""
        from typing import get_type_hints

        from langchain_core.runnables import RunnableConfig

        with TemporaryDirectory() as tmpdir:
            backend = FilesystemBackend(root_dir=tmpdir)
            resolver = RuntimeBackendResolver(default_runtime="local")
            resolver.register_runtime("local", backend)

            tools = get_client_tools_with_runtime(resolver, enable_security=False)
            for tool in tools:
                hints = get_type_hints(tool.func, include_extras=True)
                assert "config" in hints, f"{tool.name} 缺少 config 参数"
                assert hints["config"] is RunnableConfig, f"{tool.name} 的 config 类型应为 RunnableConfig"
                assert "state" in hints, f"{tool.name} 缺少 state 参数"


class TestGetSensitiveValues:
    """测试 _get_sensitive_values 融合逻辑。"""

    def test_with_backend_having_extra(self):
        """_get_sensitive_values 应正确融合全局和额外敏感值。"""
        from unittest.mock import MagicMock

        from aidev_agent.config import settings

        original = settings.SBX_SENSITIVE_VALUES
        settings.SBX_SENSITIVE_VALUES = ["global1", "global2"]
        try:
            backend = MagicMock()
            backend.extra_sensitive_values = ["extra1", "extra2"]
            result = _get_sensitive_values(backend)
            assert result == ["global1", "global2", "extra1", "extra2"]
        finally:
            settings.SBX_SENSITIVE_VALUES = original

    def test_without_extra(self):
        """_get_sensitive_values 对无 extra_sensitive_values 的 backend 应返回全局值。"""
        from aidev_agent.config import settings

        original = settings.SBX_SENSITIVE_VALUES
        settings.SBX_SENSITIVE_VALUES = ["global1"]
        try:
            # FilesystemBackend 没有 extra_sensitive_values 属性
            with TemporaryDirectory() as tmpdir:
                backend = FilesystemBackend(root_dir=tmpdir)
                result = _get_sensitive_values(backend)
                assert result == ["global1"]
        finally:
            settings.SBX_SENSITIVE_VALUES = original

    def test_with_error_string(self):
        """_get_sensitive_values 对错误字符串（resolve_backend 返回 str）应返回全局值。"""
        from aidev_agent.config import settings

        original = settings.SBX_SENSITIVE_VALUES
        settings.SBX_SENSITIVE_VALUES = ["global1"]
        try:
            result = _get_sensitive_values("Error: Unknown runtime")
            assert result == ["global1"]
        finally:
            settings.SBX_SENSITIVE_VALUES = original

    def test_backend_with_extra_sensitive_values_redacts(self):
        """PaasSandboxBackend 的 extra_sensitive_values 应与 SBX_SENSITIVE_VALUES 融合脱敏。"""
        from unittest.mock import MagicMock

        from aidev_agent.config import settings
        from aidev_agent.core.tools.runtime_tools.paas_backend import PaasSandboxBackend

        original = settings.SBX_SENSITIVE_VALUES
        settings.SBX_SENSITIVE_VALUES = ["global_secret"]
        try:
            mock_backend = MagicMock(spec=PaasSandboxBackend)
            mock_backend.extra_sensitive_values = ["skill_secret"]
            mock_backend.execute.return_value = ExecuteResult(
                output="global_secret and skill_secret exposed", exit_code=0, truncated=False
            )

            provider = RuntimeBackendResolver(default_runtime="sandbox")
            provider.register_runtime("sandbox", mock_backend)
            tool = get_execute_tool(provider, enable_security=False)

            result = tool.invoke({"command": "echo test", "target_runtime": "sandbox"})
            assert "global_secret" not in result
            assert "skill_secret" not in result
            assert "__BKAI_AGENT_REDACTED__" in result
        finally:
            settings.SBX_SENSITIVE_VALUES = original


class TestExtractPaasParamsEnvsMask:
    """测试 _extract_paas_params 的 envs_mask 解析。"""

    @staticmethod
    def _get_extract_paas_params():
        from aidev_agent.core.graphs.react.skill_middleware import _extract_paas_params

        return _extract_paas_params

    def test_envs_mask_extracts_sensitive_values(self):
        """envs_mask 指定的 env 变量值应被提取到 extra_sensitive_values。"""
        _extract_paas_params = self._get_extract_paas_params()

        skill = {
            "name": "test_skill",
            "metadata": {
                "bkai_paas_sandbox": {
                    "image": "test-image:1.0",
                    "envs": {
                        "API_KEY": "my-secret-key",
                        "NORMAL_VAR": "normal-value",
                        "DB_PASSWORD": "db-pass-123",
                    },
                    "envs_mask": ["API_KEY", "DB_PASSWORD"],
                }
            },
        }
        result = _extract_paas_params(skill, {"executor": "test_user"})
        assert result["extra_sensitive_values"] == ["my-secret-key", "db-pass-123"]
        assert "normal-value" not in result["extra_sensitive_values"]

    def test_envs_mask_with_missing_key(self):
        """envs_mask 中的 key 不在 envs 中时不应报错。"""
        _extract_paas_params = self._get_extract_paas_params()

        skill = {
            "name": "test_skill",
            "metadata": {
                "bkai_paas_sandbox": {
                    "image": "test-image:1.0",
                    "envs": {"API_KEY": "secret123"},
                    "envs_mask": ["API_KEY", "NONEXISTENT"],
                }
            },
        }
        result = _extract_paas_params(skill, {})
        assert result["extra_sensitive_values"] == ["secret123"]

    def test_envs_mask_with_empty_value(self):
        """envs_mask 对应的 env 值为空字符串时应被过滤。"""
        _extract_paas_params = self._get_extract_paas_params()

        skill = {
            "name": "test_skill",
            "metadata": {
                "bkai_paas_sandbox": {
                    "image": "test-image:1.0",
                    "envs": {"API_KEY": "secret", "EMPTY_VAR": ""},
                    "envs_mask": ["API_KEY", "EMPTY_VAR"],
                }
            },
        }
        result = _extract_paas_params(skill, {})
        assert result["extra_sensitive_values"] == ["secret"]

    def test_envs_mask_empty_or_missing(self):
        """envs_mask 为空列表或不存在时，extra_sensitive_values 应为空列表。"""
        _extract_paas_params = self._get_extract_paas_params()

        # 空 envs_mask
        skill1 = {
            "name": "test_skill",
            "metadata": {
                "bkai_paas_sandbox": {
                    "image": "test-image:1.0",
                    "envs": {"API_KEY": "secret"},
                    "envs_mask": [],
                }
            },
        }
        result1 = _extract_paas_params(skill1, {})
        assert result1["extra_sensitive_values"] == []

        # 无 envs_mask
        skill2 = {
            "name": "test_skill",
            "metadata": {
                "bkai_paas_sandbox": {
                    "image": "test-image:1.0",
                    "envs": {"API_KEY": "secret"},
                }
            },
        }
        result2 = _extract_paas_params(skill2, {})
        assert result2["extra_sensitive_values"] == []


class TestPaasSandboxBackendExtraSensitiveValues:
    """测试 PaasSandboxBackend 的 extra_sensitive_values 属性。"""

    def test_default_extra_sensitive_values(self):
        """不传 extra_sensitive_values 时默认为空列表。"""
        from unittest.mock import patch

        from aidev_agent.core.tools.runtime_tools.paas_backend import PaasSandboxBackend

        with patch.object(PaasSandboxBackend, "__init__", lambda self, **kw: None):
            backend = PaasSandboxBackend.__new__(PaasSandboxBackend)
            backend._extra_sensitive_values = []
            assert backend.extra_sensitive_values == []

    def test_explicit_extra_sensitive_values(self):
        """显式传入 extra_sensitive_values 时应可读取。"""
        from unittest.mock import patch

        from aidev_agent.core.tools.runtime_tools.paas_backend import PaasSandboxBackend

        with patch.object(PaasSandboxBackend, "__init__", lambda self, **kw: None):
            backend = PaasSandboxBackend.__new__(PaasSandboxBackend)
            backend._extra_sensitive_values = ["secret1", "secret2"]
            assert backend.extra_sensitive_values == ["secret1", "secret2"]


class _FakeRiskLLM:
    """Fake LLM for risk assessment (returns fixed risk)."""

    def __init__(self, risk: str) -> None:
        self._risk = risk

    def with_structured_output(self, schema):  # noqa: ANN001
        return self

    def invoke(self, prompt: str):  # noqa: ANN001
        from aidev_agent.packages.security.command.command_risk_assessor import RiskAssessment

        return RiskAssessment(risk=self._risk, reason="")  # type: ignore[arg-type]


class TestSmartCommandApproval:
    """Test smart approval mode (LLM pre-triage) in command security."""

    @staticmethod
    def _smart_settings():
        from aidev_agent.pydantic_models import SecuritySettings

        return SecuritySettings(
            command_approval=True,
            command_approval_mode="smart",
            command_approval_approvers="u1",
        )

    def test_smart_low_auto_approves(self):
        """smart 模式：low 风险灰名单命令自动放行（无需审批）。"""
        from aidev_agent.packages.security.command.command_risk_assessor import CommandRiskAssessor
        from aidev_agent.packages.security.command.command_security import enforce_command_security

        # touch 不在白名单（灰名单），风险评估 low → 放行（不抛异常）
        enforce_command_security(
            "touch /tmp/foo.txt",
            "local",
            self._smart_settings(),
            CommandRiskAssessor(_FakeRiskLLM("low")),
        )

    def test_smart_high_rejects(self):
        """smart 模式：high 风险灰名单命令直接拒绝。"""
        from aidev_agent.packages.security.command.command_risk_assessor import CommandRiskAssessor
        from aidev_agent.packages.security.command.command_security import enforce_command_security

        with pytest.raises(ValueError, match="风险评估：high"):
            enforce_command_security(
                "touch /tmp/foo.txt",
                "local",
                self._smart_settings(),
                CommandRiskAssessor(_FakeRiskLLM("high")),
            )

    def test_smart_uncertain_falls_back_to_itsm(self, monkeypatch):
        """smart 模式：uncertain 灰名单命令升级 ITSM 审批。"""
        from aidev_agent.packages.security.command.command_risk_assessor import CommandRiskAssessor
        from aidev_agent.packages.security.command.command_security import enforce_command_security

        monkeypatch.setattr(
            "aidev_agent.packages.security.command.command_approval.interrupt",
            lambda value: {"payload": {"approved": True}},
        )
        # uncertain 升级 ITSM，审批通过 → 放行（不抛异常）
        enforce_command_security(
            "touch /tmp/foo.txt",
            "local",
            self._smart_settings(),
            CommandRiskAssessor(_FakeRiskLLM("uncertain")),
        )
