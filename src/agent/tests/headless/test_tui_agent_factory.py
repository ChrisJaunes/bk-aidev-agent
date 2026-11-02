"""``TuiFileAgent`` 的本地文件 runtime 启用（agent_cls 扩展点，261015）。

真实组件：``AgentExecutorKwargs`` / ``RuntimeBackendResolver`` / ``FilesystemBackend``
用真的——断言对象正是"SDK 那几个组件被正确地组合"。替身只有 ``ReActAgentBuilder``
（钉住 enable_runtime_local / set_enable_runtime_tool 确实被调用、且顺序在 build 前）。
"""

from __future__ import annotations

from typing import Any

import pytest

import aidev_agent.headless.agent_factory as agent_factory
from aidev_agent.core.tools.runtime_tools import RuntimeBackendResolver
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.pydantic_models import AgentExecutorKwargs
from aidev_agent.services.common_agent import CommonQAAgent


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
            default_runtime="local", agent_code="ag", session_code="sess",
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
    """AGENT_TUI_CWD 存在：backend 注册进会话 resolver，SDK 原生开关在 build 前打开。"""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGENT_TUI_CWD", str(project))
    kwargs = _kwargs()

    agent, cfg = agent_factory.TuiFileAgent.get_agent_executor(**kwargs)

    assert agent is not None and cfg is not None
    assert _recorder.calls.index("enable_runtime_local:True") < _recorder.calls.index("build")
    assert _recorder.calls.index("set_enable_runtime_tool:True") < _recorder.calls.index("build")

    backend = kwargs["runtime_backend_resolver"].resolve_backend("local")
    assert isinstance(backend, FilesystemBackend)
    assert str(backend.cwd) == str(project)
    assert backend.virtual_mode is True, "virtual_mode=False = 全盘读写，见 SDK 安全警告"


def test_paths_cannot_escape_the_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _recorder: _RecordingBuilder
) -> None:
    """virtual_mode 的实际效果：root 外的读取被拒，root 内可读。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.md").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setenv("AGENT_TUI_CWD", str(project))
    kwargs = _kwargs()

    agent_factory.TuiFileAgent.get_agent_executor(**kwargs)
    backend = kwargs["runtime_backend_resolver"].resolve_backend("local")

    assert backend.read("note.md").lines == ["hello"]
    assert backend.read("../secret.txt").startswith("Error"), "路径遍历必须被 virtual_mode 拦截"


def test_without_a_project_root_the_parent_behaviour_is_kept(
    monkeypatch: pytest.MonkeyPatch, _recorder: _RecordingBuilder
) -> None:
    """AGENT_TUI_CWD 未设置：不开 runtime、不注册 backend——绝不回退 os.getcwd()。"""
    monkeypatch.delenv("AGENT_TUI_CWD", raising=False)
    kwargs = _kwargs()

    agent_factory.TuiFileAgent.get_agent_executor(**kwargs)

    assert "enable_runtime_local:True" not in _recorder.calls
    assert "set_enable_runtime_tool:True" not in _recorder.calls
    with pytest.raises(ValueError):
        kwargs["runtime_backend_resolver"].resolve_backend("local")


def test_without_a_resolver_it_skips_rather_than_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _recorder: _RecordingBuilder
) -> None:
    """DIRECT 构建路径没有 resolver：跳过（那不是错误），而非崩溃。"""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGENT_TUI_CWD", str(project))

    agent, cfg = agent_factory.TuiFileAgent.get_agent_executor()

    assert agent is not None and cfg is not None
    assert "enable_runtime_local:True" not in _recorder.calls


def test_it_is_a_common_qa_agent_subclass() -> None:
    """结构兼容：工厂协议按 get_agent_executor 匹配，继承关系让意图可被 isinstance 验证。"""
    assert issubclass(agent_factory.TuiFileAgent, CommonQAAgent)
    assert agent_factory.TuiFileAgent.__dict__.get("get_agent_executor") is not None
