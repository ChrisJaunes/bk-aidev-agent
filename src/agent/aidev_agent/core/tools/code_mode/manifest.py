# -*- coding: utf-8 -*-
"""code mode 的精简工具清单与代码桩生成。

清单与代码桩都从同一个「内层工具集合」派生（装配层的结果），保证提示里列出的
工具名集合与代码环境内真实可调用的集合完全相同（REQ-3 的「不多不少」）。

- :func:`build_manifest` 把 ``BaseTool`` 的输入 schema 归一化为
  :class:`ToolManifestEntry`；
- :func:`render_manifest_prompt` 产出注入 system prompt 的精简清单；
- :func:`generate_stub_source` 产出由 schema 派生的同步函数桩（``compile()`` 可过），
  桩函数保留 ``target_runtime`` 等全部参数。
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass
from typing import Iterable, List

from langchain_core.tools import BaseTool

#: 生成代码里已被占用的名字，工具名与之冲突时加前缀避免覆盖桥客户端。
_RESERVED_NAMES = frozenset({"json", "os", "time", "sys", "bridge", "_aidev_call"})

_JSON_TYPE_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


@dataclass(frozen=True)
class ToolManifestEntry:
    """单个可调用工具的精简描述。"""

    name: str
    description: str
    parameters: dict
    required: tuple[str, ...]

    @property
    def properties(self) -> dict:
        return self.parameters.get("properties") or {}

    def ordered_parameters(self) -> List[tuple[str, dict, bool]]:
        """按「必填在前（保持 schema 顺序）、可选在后」返回 ``(名字, schema, 必填)``。"""

        required = set(self.required)
        props = self.properties
        required_items = [(name, spec, True) for name, spec in props.items() if name in required]
        optional_items = [(name, spec, False) for name, spec in props.items() if name not in required]
        return required_items + optional_items


def _annotation_type(annotation: object) -> str:
    """从 Python 注解推断 JSON 类型（用于无法生成 JSON Schema 的工具）。"""

    import types
    import typing

    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _annotation_type(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return _annotation_type(args[0]) if args else "object"
    if origin in (list, set, tuple):
        return "array"
    if origin is dict:
        return "object"
    return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(annotation, "object")


def _schema_from_model_fields(model: type) -> dict:
    """按 pydantic 模型字段构造最小 schema（不触发 JSON Schema 生成）。"""

    properties = {}
    required = []
    for name, field in getattr(model, "model_fields", {}).items():
        spec: dict = {"type": _annotation_type(field.annotation)}
        if field.description:
            spec["description"] = field.description
        properties[name] = spec
        if field.is_required():
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def _schema_of(tool: BaseTool) -> dict:
    """取工具的输入 JSON schema；无法生成时按模型字段退化为最小 schema。"""

    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        try:
            return args_schema.model_json_schema()
        except Exception:  # noqa: BLE001 - 含 Callable 等无法 JSON Schema 化的字段
            return _schema_from_model_fields(args_schema)
    return {"type": "object", "properties": {}, "required": []}


def _first_line(description: str | None) -> str:
    text = (description or "").strip()
    if not text:
        return ""
    return text.splitlines()[0].strip()


def build_manifest(tools: Iterable[BaseTool]) -> List[ToolManifestEntry]:
    """把内层工具集合转成精简清单条目（顺序保持装配结果）。"""

    entries: List[ToolManifestEntry] = []
    for tool in tools:
        schema = _schema_of(tool)
        entries.append(
            ToolManifestEntry(
                name=tool.name,
                description=_first_line(getattr(tool, "description", "")),
                parameters=schema,
                required=tuple(schema.get("required") or ()),
            )
        )
    return entries


def _py_type(spec: dict) -> str:
    """把 JSON schema 的单个属性映射为 Python 注解文本。"""

    json_type = spec.get("type")
    if isinstance(json_type, list):
        json_type = next((item for item in json_type if item != "null"), None)
    if json_type is None and isinstance(spec.get("anyOf"), list):
        json_type = next((item.get("type") for item in spec["anyOf"] if item.get("type") != "null"), None)
    return _JSON_TYPE_TO_PY.get(json_type, "object")


def _safe_ident(name: str) -> str:
    ident = name if name.isidentifier() and not keyword.iskeyword(name) else f"arg_{name}"
    return f"tool_{ident}" if ident in _RESERVED_NAMES else ident


def _safe_doc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"""', "'''").replace("\n", " ")


def _default_literal(spec: dict) -> str:
    """把 schema 的默认值渲染成源码字面量；无法安全渲染时用 ``None``（调用时省略）。"""

    if "default" not in spec:
        return "None"
    value = spec["default"]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return repr(value)
    if isinstance(value, list) and not value:
        return "[]"
    if isinstance(value, dict) and not value:
        return "{}"
    return "None"


def _render_stub(entry: ToolManifestEntry) -> str:
    params: list[str] = []
    call_args: list[str] = []
    for name, spec, is_required in entry.ordered_parameters():
        safe = _safe_ident(name)
        if is_required:
            params.append(f"{safe}: {_py_type(spec)}")
        else:
            params.append(f"{safe}: {_py_type(spec)} | None = {_default_literal(spec)}")
        call_args.append(f'"{name}": {safe}')

    signature = ", ".join(params)
    mapping = "{" + ", ".join(call_args) + "}" if call_args else "{}"
    doc = _safe_doc(entry.description)
    func_name = (
        entry.name if entry.name.isidentifier() and not keyword.iskeyword(entry.name) else _safe_ident(entry.name)
    )
    return (
        f"def {func_name}({signature}) -> str:\n"
        f'    """{doc}"""\n'
        f"    args = {mapping}\n"
        f'    return _aidev_call("{entry.name}", {{key: value for key, value in args.items() if value is not None}})\n'
    )


def generate_stub_source(entries: Iterable[ToolManifestEntry]) -> str:
    """生成工具函数的源码片段（每个工具一个同名同步函数）。"""

    return "\n".join(_render_stub(entry) for entry in entries)


def render_manifest_prompt(
    entries: Iterable[ToolManifestEntry],
    *,
    runtime_description: str = "",
    max_tool_calls: int = 50,
    execute_timeout: int = 120,
    max_retries: int = 3,
) -> str:
    """生成注入 system prompt 的 code mode 说明与精简清单。"""

    entries = list(entries)
    lines: list[str] = [
        "\n\n# code mode（PTC）工具使用方式\n",
        "本轮你只能输出**一段 Python 代码**（不要输出 JSON 工具调用）。代码会在沙箱中执行，",
        "执行输出会返回给你；你可以在同一轮内多次「写代码 → 执行 → 看输出」。\n",
        f"代码环境内已注入下列 {len(entries)} 个同步工具函数（函数名 = 工具名），直接用普通函数调用即可：\n",
    ]
    for entry in entries:
        if entry.properties:
            params = []
            for name, spec, is_required in entry.ordered_parameters():
                suffix = "必填" if is_required else "可选"
                params.append(f"{name}: {_py_type(spec)}（{suffix}）")
            param_text = "，".join(params)
        else:
            param_text = "无参数"
        lines.append(f"- {entry.name}: {entry.description}\n  参数：{param_text}\n")

    if runtime_description:
        lines.append(f"\n{runtime_description}\n")

    lines.extend(
        [
            "\n约束：\n",
            f"- 单段代码内最多调用 {max_tool_calls} 次工具，单次执行超时 {execute_timeout} 秒；\n",
            "- 用 print 输出你需要看到的结果；未 print 的中间变量不会回传；\n",
            f"- 代码报错、工具异常或超时会连同错误信息回传，最多重试 {max_retries} 次后本轮失败；\n",
            "- 只能调用清单内的工具；调用其它名字会抛异常，禁止绕过清单访问外部能力。\n",
        ]
    )
    return "".join(lines)


__all__ = [
    "ToolManifestEntry",
    "build_manifest",
    "generate_stub_source",
    "render_manifest_prompt",
]
