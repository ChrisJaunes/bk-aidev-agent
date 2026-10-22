# MCP 重名工具去重与改名 技术方案

- 变更名: mcp-duplicate-tool-name
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: docs/superpowers/specs/mcp-duplicate-tool-name/spec.md

## 背景与目标

生产 LLM Gateway 在 `chat/completions` 返回 `400 invalid_request_error: duplicate tool name get_task_list`。根因是装配阶段把多个 MCP Server 的工具原样拼接，同名工具被重复序列化进模型请求。

目标：在 `construct_mcp()` 汇总处保证 MCP 工具名唯一，且
1）同一服务的多个端点同名工具合并为一个；
2）不同服务的同名工具各自可区分、可执行；
3）用户可见展示仍是原始工具名 + 来源 MCP；
4）未重名的配置行为逐项不变。

## 非目标

- 不改平台侧 `mcp_server_config` 下发格式，不改 MCP Server 自身工具定义。
- 不对平台工具（`tool_codes`）与内置工具（`knowledge_retrieval`、`read_image`、`activate_skill` 等）去重或改名；它们与 MCP 工具、彼此之间的重名不在本次范围。
- 不合并同名工具的 `inputSchema`，不改描述与参数定义。
- 不改 LLM Gateway 入参校验，不改前端卡片样式与文案。
- 不改 `_merge_state()`（`services/agent/chat.py:794`）：它只作用于 ag-ui state，与模型请求是两条链路，保持现状。

## 现状

**装配链（唯一入口）**
- `packages/resource_manager/base.py:603` `construct_mcp()`：按 Server 并发 `_load_tool`（673）、`asyncio.gather`（736-740）后按配置顺序 `extend`（742-749），全程无名字处理。
- `selected_tools` 过滤在 `base.py:701-702`，发生在每个 Server 内部、汇总之前。
- `metadata["mcp_name"]` / `metadata["mcp_transport"]` 在 `base.py:710-716` 注入。
- `packages/langchain_core/tools/base.py:571-586` `make_mcp_tools()` 直接转发 `resource_manager().construct_mcp()`，是兼容层，无独立逻辑。

**下发链**
- `services/agent/chat.py:1973` `build_tools()`：`construct_mcp()` 结果与 `construct_tool()` 结果拼接后返回。
- `core/nodes/model/model_chain.py:157` `llm.bind_tools(tools)`：名字原样进入请求体，无校验。
- `services/agent/chat.py:2007` `_apply_tool_approval_settings()` 在拼接后立即执行，按名字绑定审批。

**展示链**
- `core/ag_ui/event_builders.py:144` `build_tool_calls_with_approval_filter()` 构造 `ExtendFunctionCall(name=...)`，写入 DB 账本 `builtin_property.tool_calls`，同时下发 SSE。
- `core/ag_ui/event_builders.py:55` `enhance_tool_call()` 为 TOOL_CALL_START 补 `description/mcp_name/function_type`，在 `core/ag_ui/aidev_agent.py:451` 合并进事件。
- `core/ag_ui/event_builders.py:74` `build_tool_result_event()`、`core/ag_ui/utils.py:778/795`（续流重放）各自独立产出工具名。
- 前端标题为 `${mcpName} / ${name}`（`src/frontend/ai-blueking/packages/chat-x/src/components/tool-call/toolcall-render/toolcall-render.vue:103-111`），只读 `function.name`。

**关键事实（决定方案可行性的两条）**
1. `langchain_mcp_adapters==0.1.12` 的 `convert_mcp_tool_to_langchain_tool()` 用闭包捕获的 MCP `tool.name` 发起 `session.call_tool()`，不读 `StructuredTool.name`。因此只改 `StructuredTool.name` 不会破坏执行路由，REQ-4 无需新增路由代码。
2. DB 账本 `builtin_property.tool_calls[].function.name` 会被 `core/nodes/model/chat_history_assembly.py:68` `_extract_tool_calls()` 还原成下一轮模型历史；而首帧 `MESSAGES_SNAPSHOT` 与尾帧快照都取自 DB 会话内容（`core/ag_ui/aidev_agent.py:172-176`、`_run_end_snapshot_builder`）。**即：展示名与模型历史名当前是同一份数据**，这是本次唯一需要拆开的耦合点。

## 方案对比

**展示名落点（用户已确认取方案一）**

- 方案一（选定）：事件与 DB 都写原始名，另在 `function.model_name` 存模型名，仅供历史装配使用。展示/刷新/回放全部一致，后端单仓可完成；代价是 DB `function` 多一个可选字段，且历史装配要多读一个键。
- 方案二：仅 SSE 流式替换展示名，DB 保留模型名。改动最小，但刷新页面、续流回放、MESSAGES_SNAPSHOT 仍会露出 `<MCP_NAME>__` 前缀，不满足 REQ-5 的验收口径。
- 方案三：DB 加 `display_name` 字段 + 前端 `toolTitle` 回退渲染。体验最完整，但要联动前端包发布，与规格「单仓变更、无跨仓依赖」冲突，且历史装配仍需另存模型名——相对方案一没有净收益。

**去重与改名位置**

- 选定：在 `construct_mcp()` 汇总后做一次纯内存后处理。`make_mcp_tools()` 自动继承同一套规则，满足「不得出现两套去重规则」。
- 排除：在 `ChatAgentBuilder.build_tools()` 里做——`make_mcp_tools()` 兼容入口会绕过去，形成两套规则。
- 排除：在 `bind_tools()` 前做——此时工具对象已与 metadata、审批绑定、执行路由分离，改名会同时打断审批匹配和 ToolMessage 命名。
- 排除：只做「丢弃重复项」不改名——不同服务的同名工具会丢功能，违反 REQ-3。

**「同一服务」判定（用户已确认：保持前缀语义，以 `-prod-` 为分界）**

- 选定：两侧 Server 名都**恰好包含一个** `-prod-` 分段，且该分段之前的部分完全相同，即视为同一服务。
  - `msdk-console-prod-mcp` / `msdk-console-prod-mcp2` → 同服务（`msdk-console`）。
  - `foo-mcp` / `foo-mcp-admin` → 无 `-prod-`，不是同一服务，按不同服务改名。
  - `a-prod-x-prod-y` → 出现两个 `-prod-`，判定失败，按不同服务改名。
- 该规则是 REQ-2「互为前缀」的收紧实现：同名同服务被合并的前提是先能确认同服务，判定失败时退化为「改名保留」——不会产生 400，也不会丢工具，只多一个前缀。
- 排除：纯字符串前缀（会把 `foo-mcp` 与 `foo-mcp-admin` 误合并，丢工具）。
- 排除：剩余部分必须为纯数字（`-mcp2` 通过、`-prod-admin` 被误判为不同服务），与用户答复的 `-prod-` 语义不符。

## 决策

### 1. 新增纯函数模块 `utils/tool_naming.py`

无 I/O、无日志、无全局状态，全部函数可独立单测；不放在 `packages/resource_manager/` 下，避免与资源装配实现耦合。

```python
SEP = "__"
MAX_TOOL_NAME_LEN = 64
SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")

def split_service(server_name: str) -> str | None      # 恰好一个 -prod- 时返回前半段，否则 None
def is_same_service(a: str, b: str) -> bool            # a != b 且 split_service(a) == split_service(b) != None
def sanitize(segment: str) -> str                      # 非法字符替换为 "_"
def compose_tool_name(mcp_name: str, tool_name: str) -> str
def dedupe_mcp_tools(tools: list, approval_matcher: Callable[[str, str], bool] | None = None) -> tuple[list, list[ToolNameChange]]
```

`compose_tool_name` 规则：

1. `base = f"{sanitize(mcp_name)}__{sanitize(tool_name)}"`，长度 ≤ 64 直接返回（绝大多数情况走这条）。
2. 超长时 `suffix = "_" + sha1(base).hexdigest()[:8]`（9 字符），先截断 `mcp_name` 前缀：`budget = 64 - len(suffix) - 2 - len(sanitized_tool)`，`budget >= 1` 时返回 `f"{mcp[:budget]}__{tool}{suffix}"`。
3. `budget < 1`（工具名自身过长）时丢弃 MCP 前缀，返回 `f"{tool[:64 - len(suffix)]}{suffix}"`。
4. 与已占用名冲突时改用 `sha1` 前 12 位，仍冲突再追加 `_2`、`_3`…（同一输入必然得到同一结果）。

`dedupe_mcp_tools` 规则（输入为按配置顺序排好的工具列表，`metadata["mcp_name"]` 为来源 Server）：

1. 按 `tool.name` 分组，保持首次出现位置；组内第一个为 keeper。
2. **审批豁免（用户确认）**：组内任一成员 `approval_matcher(mcp_name, 原始工具名)` 为真时，整组放弃合并——成员全部保留，非 keeper 成员走改名。理由：合并会丢弃某一端点的实例，而审批绑定是「MCP 名 + 工具名」维度的，被丢弃端点的审批工具一旦消失，调用会落到未绑定审批的同名工具上，等于绕过审批。
3. 组内其余成员（仅当整组未命审批豁免）：与 keeper 同服务 → 丢弃，记入 `ToolNameChange.merged_servers`；不同服务 → 重命名 `compose_tool_name(mcp_name, name)`。
4. keeper 永不改名（REQ-2 / 验收「同时存在 `get_task_list` 与 `<MCP>__get_task_list`」）。
5. 输出列表保持输入顺序，被丢弃项直接移除（重命名不改变相对位置）。
6. `ToolNameChange` 为 dataclass：`original` / `final` / `server` / `merged_servers` / `kind ∈ {rename, merge}`。

### 2. `construct_mcp()` 接线

在 `base.py:742-749` 汇总 `tools_list` 之后、`return McpToolsResult(...)` 之前调用 `dedupe_mcp_tools(tools_list, approval_matcher)`：

- **审批豁免的上下文注入**：`construct_mcp()` 新增可选参数 `approval_matcher: Callable[[str, str], bool] | None = None`。`ChatAgentBuilder.build_tools()`（`chat.py:1973`）在调用前先算 `approval_items = self._normalize_tool_approval_bindings()`，并传入
  `lambda mcp_name, tool_name: bool(self._match_approval_item({"mcp_name": mcp_name}, tool_name, approval_items))`，
  即**复用同一套审批匹配逻辑**，不新增第二套判定。`_apply_tool_approval_settings()` 改为接收同一份 `approval_items`，避免重复计算（行为不变）。
- `make_mcp_tools()` 兼容层没有审批上下文，`approval_matcher` 为 `None`，此时按「可合并」处理；去重实现仍是同一份代码，仅输入不同。
- 对每个返回的工具写 `metadata["original_name"]` 与 `metadata["display_name"]`（均等于改名前名字）；keeper 额外写 `metadata["mcp_merged_servers"]`；被审批豁免而未合并的组不写该键。
- 仅对 `ToolNameChange` 记日志：`rename` 记 INFO（原始名、最终名、来源 Server），`merge` 记 INFO（原始名、保留 Server、被丢弃 Server 列表）；同服务判定命中但因审批豁免而未合并时记一条 INFO（含 `skip_merge_reason=approval`）；同一服务的合并若两侧 `args_schema` 不一致，额外记 WARNING 并在文案中提示 schema 差异。未发生重名时不产生任何新增日志。
- 不触碰 `selected_tools` 过滤位置、并发拉取方式、span 属性与 `McpToolFetchFailure` 语义。

### 3. 展示名下沉（REQ-5）

统一取 `metadata["display_name"]`，一律通过 `tools_mapping` 解析（`tools_mapping` 仍以**模型名**为键，改名后唯一）：

- `core/ag_ui/types.py` `ExtendFunctionCall` 增 `model_name: str | None = None`。
- `event_builders.build_tool_calls_with_approval_filter()`：`function.name` 写 display name，`function.model_name` 写模型名（仅当两者不同才写，保证未改名时 DB 逐字不变）。
- `event_builders.enhance_tool_call()`：返回值增加 `tool_call_name`（display name），由 `aidev_agent.py:451` 的 `ExtendToolCallStartEvent(**{**ev.model_dump(), **enhanced})` 覆盖；`is_tool_approval_required()` 等反查仍发生在覆盖之前，用模型名，不受影响。
- `event_builders.build_tool_result_event()`：增加可选参数 `tools_mapping`，`tool_call_name` 写 display name；调用点 `aidev_agent.py:483/486` 传入 `self._tool_mapping`。
- `core/ag_ui/utils.py:778`（`_message_to_streaming_events` 的 tool_call 三元组）与 `:795` `_tool_message_to_event()`：同样改为 display name，映射取调用方已有的 `tools_mapping` / 新增可选参数（续流重放路径）。
- `services/event_handlers/agui_writer.py:425-450` 的续流回填 `name` 保持取事件名（此时已是 display name），`_resolve_function_type(name, ...)` 在查不到时返回 `None`；MCP 工具本就没有 `function_type`，不产生行为差异。

### 4. DB 账本与历史装配（REQ-4 多轮一致性）

- `extend` 侧不改 `builtin_property` 结构，只多出 `function.model_name`。
- `core/nodes/model/chat_history_assembly.py:68` `_extract_tool_calls()`：`name` 取 `function.model_name or function.name`，保证下一轮模型看到的历史工具名与当前绑定工具一致（同一 service 的两个端点在同名时仍可区分）。
- `_chat_history_to_langchain_messages` 的 tool 消息不写 name（`chat_history_assembly.py:507-516`），无需改动。

### 5. 审批（REQ-10）

- `services/agent/chat.py:2131` `_match_approval_item()`：命中集合由 `{tool_name, tool_code}` 扩为 `{tool_name, tool_code, metadata.get("original_name")}`，覆盖 `resource_type` 为 `tool` / `mcp_tool` / `mcp` 的所有分支。
- `services/agent/chat.py:2007` `_apply_tool_approval_settings()`：`target.display_name` 取 `metadata.get("display_name") or metadata.get("tool_name") or tool_name`，使审批单标题（`packages/interrupt_manager/approval.py:841` 的 `ticket_title`）显示原始名；`target.name` / `target.code` 仍是模型名，作为执行身份不参与展示。该方法复用 `build_tools()` 已算出的 `approval_items`（见决策 2），不再自行调用 `_normalize_tool_approval_bindings()`。
- 审批工具不参与同服务合并（见决策 1 第 2 条）：合并会丢弃端点实例，而被丢弃端点的审批绑定随之失效，调用会落到未绑定审批的同名工具上。
- 未改名的工具：`display_name == name`，审批 metadata 逐字不变。

### 6. 错误语义

不改任何异常的抛出点与包装：`MCPExceptionWrapper`（`base.py:711`）仍然包裹改名后的 `StructuredTool.coroutine`，调用失败仍走原有 MCP 异常路径；命名模块只在内存里做字符串处理，不新增异常分支。

## 接口与数据

| 对象 | 变化 | 兼容性 |
| --- | --- | --- |
| `StructuredTool.name` | 跨服务重名时变为 `<MCP_NAME>__<TOOL_NAME>` | 模型可见名，唯一 |
| `tool.metadata` | 新增 `original_name` / `display_name`（MCP 工具）；keeper 另加 `mcp_merged_servers` | 只增不改，非 MCP 工具不写 |
| `ExtendFunctionCall` | 新增可选 `model_name` | 仅改名时写入，未改名无该键 |
| DB `builtin_property.tool_calls[].function` | 同上 | 旧会话无该键，走 `or function.name` 回退 |
| `construct_mcp()` 返回 | 类型不变（`McpToolsResult`），工具数量可能减少（同服务合并） | `make_mcp_tools()` 自动一致 |
| `construct_mcp()` 签名 | 新增可选 `approval_matcher` | 缺省 `None` 时保持既有调用可用 |

执行路由契约：模型调用 `<MCP_NAME>__<TOOL_NAME>` → `StructuredTool.coroutine` → 闭包内 `session.call_tool(<原始 TOOL_NAME>)` → 返回原样透传。不需新代码，但需用例锁定（T-6）。

## 跨仓契约

无。`mcp_server_config` 下发格式不变，平台侧无联动发布；前端不改。

## 风险

1. **同服务判定依赖 `-prod-` 命名约定**：测试/预发环境若用 `-test-` / `-staging-` 命名，同服务端点的重名工具不会被合并，而是退化为改名保留。结果仍满足「不 400」，但工具数量比预期多一个，且模型看到的两个名字都可调用。缓解：日志记录判定结果，必要时后续把分隔词做成配置。
2. **展示名与模型名分离**：旧会话（本次上线前写入的 DB 记录）没有 `function.model_name`，历史装配按 `function.name` 回退；由于旧记录名字本就唯一，行为不变。上线后若回滚代码，新会话的 `function.name` 是原始名，可能出现两个同名历史 tool_call —— 功能不坏（按 `tool_call_id` 配对），但可读性下降。
3. **`_tools_mapping` 按名建索引**：同名工具在映射中已互相覆盖（存量行为，非本次引入）。改名后模型名唯一，映射反而更准确；但若同一 assistant 消息里同时出现两个原始名相同的调用，续流回填按名字查 `function_type` 可能取不到，仅影响 `function_type`（MCP 工具恒为 `None`），不影响审批与执行。
4. **hash 截断边界**：工具名 + Server 名合计超过 64 字符时依赖 `sha1` 前 8 位保证唯一，碰撞概率可忽略；真碰撞时按 T-1 的 `_2`/`_3` 兜底，仍保证确定性。
5. **审批豁免导致两条入口行为不同**：`ChatAgentBuilder` 路径带审批上下文，同服务重名组命中审批时不合并；`make_mcp_tools()` 路径无审批上下文，仍会合并。同一个配置经两条入口装配出的工具数量可能不同（差集恰为被审批保护的端点），但都不会 400、都不会绕过审批。
6. **未确认项**：`tools` 顺序稳定性对提示词缓存的影响未做量化，本方案只保证「重命名不改变相对位置」，不保证跨版本稳定。

## 测试策略

- 纯函数层：`tests/utils/test_tool_naming.py` 覆盖 `-prod-` 判定（含多 `prod`、无 `prod`、非对称名字）、卫生化、超长截断与确定性（同一输入重复调用结果相同）、审批豁免（`approval_matcher` 命中时不合并、改为改名）。
- 装配层：`tests/packages/langchain_core/tools/test_base.py` 增加 `construct_mcp` 多 Server 同名用例——同服务合并（保留配置顺序首个）、跨服务改名、审批工具不合并、`selected_tools` 先过滤、无重名时名字/顺序/metadata 逐项不变、日志条数与级别。
- 事件层：`tests/core/ag_ui/test_event_builders.py` 断言 `function.name` 为原始名、`function.model_name` 为模型名；`build_tool_result_event` 传 mapping 后返回原始名。
- 审批层：`tests/services/agent`（或现有审批用例）断言改名工具仍命中审批配置、`target.display_name` 为原始名。
- 历史层：`_extract_tool_calls` 断言优先取 `model_name`。
- 端到端（手工）：用 `ai-query-sops` 的同服务双端点配置跑一轮，确认请求不再 400、前端卡片显示 `msdk-console-prod-mcp / get_task_list`，审批工具仍弹卡。

验证命令（在 `src/agent` 下执行；该环境 `uv run` 会重建空 venv，故给出等价直跑方式）：

```bash
cd src/agent
uv run --no-sync pytest -c ./pyproject.toml tests/packages/resource_manager tests/core/ag_ui -q
# 本机等价：PYTHONPATH=$PWD /data/workspace/bk-aidev-agent/src/agent/.venv/bin/python -m pytest -c ./pyproject.toml tests/... -q
```
