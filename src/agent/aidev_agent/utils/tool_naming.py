# -*- coding: utf-8 -*-
"""MCP 工具命名与去重纯函数。

装配阶段（``BaseResourceManager.construct_mcp()``）汇总多个 MCP Server 的工具后，
需要保证模型请求中的工具名唯一，否则上游返回 ``400 duplicate tool name``。

本模块只做内存字符串处理与列表后处理：无 I/O、无日志、无全局状态。
同服务判定与改名规则见 ``docs/superpowers/plans/mcp-duplicate-tool-name/plan.md``。
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

SEP = "__"
MAX_TOOL_NAME_LEN = 64
SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")
# 同服务判定分界：Server 名中恰好出现一次的 "-prod-"
SERVICE_MARKER = "-prod-"

# 超长改名追加的短哈希长度（含分隔下划线共 9 字符）
_SHORT_HASH_LEN = 8
# 与已占用名冲突时改用更长的哈希
_FALLBACK_HASH_LEN = 12

ApprovalMatcher = Callable[[str, str], bool]


@dataclass(frozen=True)
class ToolNameChange:
    """一条工具名变更记录。

    - ``kind == "rename"``：``original`` 改名为 ``final``，``server`` 为所属 Server。
    - ``kind == "merge"``：``original`` 保持不变（keeper），``server`` 为保留的 Server，
      ``merged_servers`` 为被合并丢弃的 Server 列表。
    """

    original: str
    final: str
    server: str
    kind: str
    merged_servers: tuple[str, ...] = ()
    skip_merge_reason: str | None = None
    schema_mismatch: bool = False


@dataclass(frozen=True)
class DedupeResult:
    """去重结果：保留下来的工具列表与全部变更记录。"""

    tools: list[Any] = field(default_factory=list)
    changes: tuple[ToolNameChange, ...] = ()


def split_service(server_name: str) -> str | None:
    """取 Server 名中 ``-prod-`` 之前的服务前缀；不满足「恰好一个 ``-prod-``」时返回 ``None``。"""
    if not server_name or server_name.count(SERVICE_MARKER) != 1:
        return None
    head, _, tail = server_name.partition(SERVICE_MARKER)
    if not head or not tail:
        return None
    return head


def is_same_service(server_a: str, server_b: str) -> bool:
    """两个不同 Server 名是否属同一服务的不同端点（``-prod-`` 之前的前缀相同）。"""
    if not server_a or not server_b or server_a == server_b:
        return False
    service_a = split_service(server_a)
    if service_a is None:
        return False
    return service_a == split_service(server_b)


def sanitize(segment: str) -> str:
    """把工具名/Server 名中的非 ``[A-Za-z0-9_-]`` 字符替换为 ``_``。"""
    return SANITIZE_RE.sub("_", segment or "")


def _short_hash(text: str, length: int = _SHORT_HASH_LEN) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def compose_tool_name(mcp_name: str, tool_name: str) -> str:
    """拼出 ``<MCP_NAME>__<TOOL_NAME>``，超长时截断前缀并追加稳定短哈希。

    输出恒满足 ``^[A-Za-z0-9_-]{1,64}$``，同一输入重复调用结果相同。
    """
    mcp_part = sanitize(mcp_name)
    tool_part = sanitize(tool_name)
    base = f"{mcp_part}{SEP}{tool_part}"
    if len(base) <= MAX_TOOL_NAME_LEN:
        return base

    suffix = "_" + _short_hash(base)
    # 先截断 Server 前缀，为工具名与后缀留出空间
    budget = MAX_TOOL_NAME_LEN - len(suffix) - len(SEP) - len(tool_part)
    if budget >= 1:
        return f"{mcp_part[:budget]}{SEP}{tool_part}{suffix}"
    # 工具名自身过长：丢弃 Server 前缀
    return f"{tool_part[: MAX_TOOL_NAME_LEN - len(suffix)]}{suffix}"


def _mcp_name_of(tool: Any) -> str:
    metadata = getattr(tool, "metadata", None) or {}
    return str(metadata.get("mcp_name") or "")


def _schema_signature(tool: Any) -> Any:
    """取工具的入参 schema 摘要，用于同服务合并时的差异提示。"""
    args = getattr(tool, "args", None)
    return args if isinstance(args, dict) else None


def _unique_name(candidate: str, used: set[str], base: str) -> str:
    """``candidate`` 已被占用时按短哈希、再按序号回退，保证确定性。"""
    if candidate not in used:
        return candidate
    hashed = f"{candidate[: MAX_TOOL_NAME_LEN - _FALLBACK_HASH_LEN - 1]}_{_short_hash(base, _FALLBACK_HASH_LEN)}"
    if hashed not in used:
        return hashed
    index = 2
    while True:
        suffix = f"_{index}"
        numbered = f"{candidate[: MAX_TOOL_NAME_LEN - len(suffix)]}{suffix}"
        if numbered not in used:
            return numbered
        index += 1


def dedupe_mcp_tools(
    tools: Sequence[Any],
    approval_matcher: ApprovalMatcher | None = None,
) -> DedupeResult:
    """消除 MCP 工具之间的重名，返回保留下来的工具与变更记录。

    规则（输入按配置顺序排好，``metadata["mcp_name"]`` 为来源 Server）：

    1. 按 ``tool.name`` 分组并保持首次出现位置，组内第一个为 keeper，keeper 永不改名。
    2. ``approval_matcher(mcp_name, 原始工具名)`` 对组内任一成员为真时整组放弃合并：
       成员全部保留，非 keeper 成员按 ``<MCP_NAME>__<TOOL_NAME>`` 改名。
    3. 未命中审批豁免时，与 keeper 同服务的成员被丢弃（记入 ``merged_servers``），
       不同服务的成员改名保留。
    4. 输出保持输入顺序，改名不改变相对位置。
    """
    tools = list(tools or [])
    if len(tools) < 2:
        return DedupeResult(tools=tools, changes=())

    groups: dict[str, list[Any]] = {}
    for tool in tools:
        groups.setdefault(getattr(tool, "name", ""), []).append(tool)

    # keeper 的名字先占位，避免改名结果与其它工具的既有名字冲突
    used: set[str] = set(groups.keys())

    # id(tool) -> 最终名字；None 表示被合并丢弃
    decisions: dict[int, str | None] = {}
    changes: list[ToolNameChange] = []

    for name, members in groups.items():
        if len(members) < 2:
            continue

        keeper = members[0]
        keeper_server = _mcp_name_of(keeper)
        approval_exempt = bool(approval_matcher) and any(
            approval_matcher(_mcp_name_of(member), name) for member in members
        )

        merged_servers: list[str] = []
        keeper_schema = _schema_signature(keeper)
        schema_mismatch = False

        for member in members[1:]:
            server = _mcp_name_of(member)
            if not approval_exempt and is_same_service(keeper_server, server):
                merged_servers.append(server)
                if _schema_signature(member) != keeper_schema:
                    schema_mismatch = True
                decisions[id(member)] = None
                continue
            final = _unique_name(compose_tool_name(server, name), used, f"{server}{SEP}{name}")
            used.add(final)
            decisions[id(member)] = final
            changes.append(
                ToolNameChange(
                    original=name,
                    final=final,
                    server=server,
                    kind="rename",
                    skip_merge_reason="approval" if approval_exempt else None,
                )
            )

        if merged_servers:
            changes.append(
                ToolNameChange(
                    original=name,
                    final=name,
                    server=keeper_server,
                    kind="merge",
                    merged_servers=tuple(merged_servers),
                    schema_mismatch=schema_mismatch,
                )
            )

    deduped: list[Any] = []
    for tool in tools:
        if id(tool) not in decisions:
            deduped.append(tool)
            continue
        final = decisions[id(tool)]
        if final is None:
            continue
        tool.name = final
        deduped.append(tool)

    return DedupeResult(tools=deduped, changes=tuple(changes))
