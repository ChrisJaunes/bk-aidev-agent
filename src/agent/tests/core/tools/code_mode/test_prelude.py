# -*- coding: utf-8 -*-
"""T-4：沙箱侧 prelude 桥客户端（REQ-4 / REQ-7 / REQ-8）。"""

from __future__ import annotations

import importlib.util
import json

import pytest
from aidev_agent.core.tools.code_mode import build_manifest
from aidev_agent.core.tools.code_mode.prelude import CALLS_DIRNAME, build_prelude_source
from langchain_core.tools import StructuredTool


def _add(a: int, b: int) -> str:
    """把两个整数相加。"""
    return str(a + b)


def _entries():
    return build_manifest([StructuredTool.from_function(func=_add, name="add", description="把两个整数相加。")])


def _load_prelude(tmp_path, *, max_tool_calls: int = 5, call_timeout: int = 2):
    source = build_prelude_source(
        _entries(), max_tool_calls=max_tool_calls, call_timeout=call_timeout, poll_interval=0.01
    )
    path = tmp_path / "_aidev_bridge.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"prelude_{max_tool_calls}_{call_timeout}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_response(tmp_path, seq: int, payload: dict) -> None:
    calls_dir = tmp_path / CALLS_DIRNAME
    calls_dir.mkdir(exist_ok=True)
    (calls_dir / f"resp_{seq:04d}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_unknown_tool_raises_without_request_file(tmp_path):
    module = _load_prelude(tmp_path)

    with pytest.raises(module.CodeModeToolError):
        module._aidev_call("not_installed", {})

    assert not (tmp_path / CALLS_DIRNAME).exists()


def test_round_trip_returns_content_and_writes_request(tmp_path):
    module = _load_prelude(tmp_path)
    _write_response(tmp_path, 1, {"seq": 1, "ok": True, "content": "3"})

    assert module.add(a=1, b=2) == "3"

    request = json.loads((tmp_path / CALLS_DIRNAME / "req_0001.json").read_text(encoding="utf-8"))
    assert request == {"seq": 1, "tool": "add", "args": {"a": 1, "b": 2}}
    assert module.tool_call_count() == 1


def test_error_response_raises_tool_error(tmp_path):
    module = _load_prelude(tmp_path)
    _write_response(tmp_path, 1, {"seq": 1, "ok": False, "error": "tool exploded", "error_type": "tool_error"})

    with pytest.raises(module.CodeModeToolError, match="tool exploded"):
        module.add(a=1, b=2)


def test_budget_exceeded_after_limit(tmp_path):
    module = _load_prelude(tmp_path, max_tool_calls=1)
    _write_response(tmp_path, 1, {"seq": 1, "ok": True, "content": "1"})
    _write_response(tmp_path, 2, {"seq": 2, "ok": True, "content": "2"})

    assert module.add(a=0, b=1) == "1"
    with pytest.raises(module.CodeModeBudgetExceeded):
        module.add(a=1, b=1)
    assert not (tmp_path / CALLS_DIRNAME / "req_0002.json").exists()
