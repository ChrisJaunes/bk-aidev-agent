# -*- coding: utf-8 -*-
"""T-3：精简清单与代码桩生成（REQ-3 / REQ-4）。"""

from __future__ import annotations

import inspect
from typing import List, Optional

import pytest
from aidev_agent.core.tools.code_mode import build_manifest, generate_stub_source, render_manifest_prompt
from aidev_agent.core.tools.code_mode.prelude import build_prelude_source
from langchain_core.tools import StructuredTool


def _runtime_tool(name: str, description: str) -> StructuredTool:
    def _run(
        file_path: str,
        target_runtime: str,
        offset: int = 0,
        limit: Optional[int] = None,
        flag: bool = False,
        items: List[str] = None,
    ) -> str:
        return "ok"

    return StructuredTool.from_function(func=_run, name=name, description=description)


@pytest.fixture
def entries():
    return build_manifest(
        [
            _runtime_tool("read_file", "读取文件。\n第二行说明不应进入清单。"),
            _runtime_tool("grep", "搜索文件内容。"),
        ]
    )


def test_manifest_names_match_tool_set(entries):
    assert [entry.name for entry in entries] == ["read_file", "grep"]


def test_manifest_uses_first_description_line(entries):
    assert entries[0].description == "读取文件。"


def test_manifest_parameters_keep_schema_order_and_required(entries):
    names = [name for name, _, required in entries[0].ordered_parameters()]
    assert names[:2] == ["file_path", "target_runtime"]
    assert set(entries[0].required) == {"file_path", "target_runtime"}


def _compile_stubs(source: str) -> dict:
    """在独立编译标志下执行桩源码（避免测试模块的 future 注解影响签名）。"""

    namespace: dict = {}
    exec(compile(source, "<stubs>", "exec", dont_inherit=True), namespace)  # noqa: S102
    return namespace


def test_stub_source_compiles_and_signature_matches(entries):
    namespace = _compile_stubs(generate_stub_source(entries))

    signature = inspect.signature(namespace["read_file"])
    params = list(signature.parameters)
    assert params[:2] == ["file_path", "target_runtime"]
    assert {"offset", "limit", "flag", "items"} <= set(params)
    assert signature.parameters["file_path"].default is inspect.Parameter.empty
    assert signature.parameters["target_runtime"].default is inspect.Parameter.empty
    assert signature.parameters["offset"].default == 0


def test_stub_source_without_parameters_compiles():
    def _noop() -> str:
        """无参工具。"""
        return "ok"

    entries = build_manifest([StructuredTool.from_function(func=_noop, name="noop", description="无参工具。")])
    namespace = _compile_stubs(generate_stub_source(entries))
    assert str(inspect.signature(namespace["noop"])) == "() -> str"


def test_manifest_prompt_lists_every_tool_and_target_runtime(entries):
    prompt = render_manifest_prompt(entries, runtime_description="target_runtime 可选值: local", max_tool_calls=7)
    for entry in entries:
        assert entry.name in prompt
    assert "target_runtime" in prompt
    assert "最多调用 7 次" in prompt


def test_prelude_exposes_tool_functions(entries, tmp_path):
    source = build_prelude_source(entries, max_tool_calls=5, call_timeout=10, poll_interval=0.01)
    namespace = {"__file__": str(tmp_path / "prelude.py")}
    exec(compile(source, "<prelude>", "exec", dont_inherit=True), namespace)  # noqa: S102
    assert set(namespace["TOOL_NAMES"]) == {"read_file", "grep"}
    assert set(namespace["TOOL_FUNCTIONS"]) == {"read_file", "grep"}
