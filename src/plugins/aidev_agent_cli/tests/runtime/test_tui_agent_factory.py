"""``WorkspaceAgent`` 的本地文件 runtime 启用（``sdk.workspace``，261015）。

真实组件：``AgentExecutorKwargs`` / ``RuntimeBackendResolver`` / ``FilesystemBackend``
用真的——断言对象正是"SDK 那几个组件被正确地组合"。替身只有 ``ReActAgentBuilder``
（钉住 enable_runtime_local / set_enable_runtime_tool 确实被调用、且顺序在 build 前）。

工作区根固定为 ``os.getcwd()``：测试用 ``monkeypatch.chdir`` 把进程 cwd 指到临时目录。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from aidev_agent.core.tools.runtime_tools import RuntimeBackendResolver
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.pydantic_models import AgentExecutorKwargs
from aidev_agent.services.common_agent import CommonQAAgent

import aidev_agent_cli.sdk.workspace as agent_factory


class _RecordingBuilder:
    """ReActAgentBuilder 的最小替身：记录链式调用，供断言。"""

    def __init__(self) -> None:
        self.options: AgentExecutorKwargs | None = None
        self.calls: list[str] = []

    def set_bkai_options(self, options: AgentExecutorKwargs) -> "_RecordingBuilder":
        self.options = options
        self.calls.append("set_bkai_options")
        return self

    def enable_security_runtime(self, enable: bool) -> "_RecordingBuilder":
        self.calls.append(f"enable_security_runtime:{enable}")
        return self

    def enable_runtime_local(self, enable: bool) -> "_RecordingBuilder":
        self.calls.append(f"enable_runtime_local:{enable}")
        return self

    def set_enable_runtime_tool(self, enable: bool) -> "_RecordingBuilder":
        self.calls.append(f"set_enable_runtime_tool:{enable}")
        return self

    def build(self) -> tuple[Any, Any]:
        self.calls.append("build")
        return object(), object()


def _kwargs(**overrides: Any) -> dict[str, Any]:
    """与 chat.py 每会话产物同形的 executor kwargs：自带 resolver。"""
    base: dict[str, Any] = {
        "runtime_backend_resolver": RuntimeBackendResolver(
            default_runtime="local",
            agent_code="ag",
            session_code="sess",
        ),
    }
    base.update(overrides)
    return base


@pytest.fixture()
def _recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingBuilder:
    recorder = _RecordingBuilder()
    monkeypatch.setattr(agent_factory, "ReActAgentBuilder", lambda: recorder)
    return recorder


def test_the_local_runtime_is_enabled_before_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _recorder: _RecordingBuilder
) -> None:
    """cwd 恒为目录：backend 注册进会话 resolver，SDK 原生开关在 build 前打开。"""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    kwargs = _kwargs()

    agent, cfg = agent_factory.WorkspaceAgent.get_agent_executor(**kwargs)

    assert agent is not None and cfg is not None
    assert _recorder.calls.index("enable_runtime_local:True") < _recorder.calls.index("build")
    assert _recorder.calls.index("set_enable_runtime_tool:True") < _recorder.calls.index("build")

    backend = kwargs["runtime_backend_resolver"].resolve_backend("local")
    assert isinstance(backend, FilesystemBackend)
    assert str(backend.cwd) == os.getcwd()
    assert backend.virtual_mode is True, "virtual_mode=False = 全盘读写，见 SDK 安全警告"


def test_paths_cannot_escape_the_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _recorder: _RecordingBuilder
) -> None:
    """virtual_mode 的实际效果：root 外的读取被拒，root 内可读。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.md").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    monkeypatch.chdir(project)
    kwargs = _kwargs()

    agent_factory.WorkspaceAgent.get_agent_executor(**kwargs)
    backend = kwargs["runtime_backend_resolver"].resolve_backend("local")

    assert backend.read("note.md").lines == ["hello"]
    assert backend.read("../secret.txt").startswith("Error"), "路径遍历必须被 virtual_mode 拦截"


def test_the_cwd_is_always_used_as_the_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _recorder: _RecordingBuilder
) -> None:
    """工作区根固定为 os.getcwd()：即使没有环境变量，runtime 也照常启用。"""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    kwargs = _kwargs()

    agent_factory.WorkspaceAgent.get_agent_executor(**kwargs)

    assert "enable_runtime_local:True" in _recorder.calls
    assert "set_enable_runtime_tool:True" in _recorder.calls
    backend = kwargs["runtime_backend_resolver"].resolve_backend("local")
    assert isinstance(backend, FilesystemBackend)
    assert str(backend.cwd) == str(project)


def test_without_a_resolver_it_skips_rather_than_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _recorder: _RecordingBuilder
) -> None:
    """DIRECT 构建路径没有 resolver：跳过（那不是错误），而非崩溃。"""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    agent, cfg = agent_factory.WorkspaceAgent.get_agent_executor()

    assert agent is not None and cfg is not None
    assert "enable_runtime_local:True" not in _recorder.calls


def test_it_is_a_common_qa_agent_subclass() -> None:
    """结构兼容：工厂协议按 get_agent_executor 匹配，继承关系让意图可被 isinstance 验证。"""
    assert issubclass(agent_factory.WorkspaceAgent, CommonQAAgent)
    assert agent_factory.WorkspaceAgent.__dict__.get("get_agent_executor") is not None
    assert not hasattr(agent_factory, "TuiFileAgent")
