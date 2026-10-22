# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: 81cad2b36de35044af4b599c2f2910aca97985f9
- 评审基线: `7a171a8af285c24733d75a16428ef31edfcbbd05`（`origin/develop`）
- 累计改动: 16 个文件，+1436 / -35（含 spec/plan/tasks 三份冻结以外的文档与 6 个测试文件）

### 阻塞项

无。

### 改进建议

- **W-1 端到端手工回归未执行**：`tasks.md` 的 T-6 仍为未勾选，「线上 `duplicate tool name` 400 是否消失」与「前端卡片显示 `<来源 MCP> / <原始工具名>`」只在单测层被间接覆盖（`test_renamed_mcp_tool_routes_to_original_tool_name` 断言下发给模型的工具名集合唯一、`ClientSession.call_tool` 收到原始名），没有真实预览环境的闭环证据。建议在可预览环境用 `ai-query-sops` 的同服务双端点配置补测一次；本轮不因此打回。
- **W-2 同服务判定依赖 `-prod-` 命名**：`is_same_service()` 要求两侧 Server 名恰好含一个 `-prod-` 且前缀相同，`msdk-console-test-mcp` / `msdk-console-test-mcp2` 这类测试/预发命名会退化为「改名保留」而不是「合并且不改名」。行为仍满足「不 400、不丢工具」，属 `plan.md` 已登记的风险 1，与 REQ-2 字面「互为前缀」的差异已在方案阶段收敛。若后续要覆盖非生产命名，建议把分隔词做成配置项。
- **W-3 审批续流回填路径不写 `function.model_name`**：`services/event_handlers/agui_writer.py:_fetch_tool_call_reconstruction()` 重建审批 tool_call 时只写 `function.name`（此时是展示名），未写 `model_name`。因此「改名 + 审批」的工具在**续流**场景写出的账本记录缺少模型名，下一轮历史装配按 `function.name` 回落原始名。原始名恒存在于工具集合（keeper 保留原名），不会出现未知工具名或 400，仅丢失「同服务两端点可区分」这一层信息，属 `plan.md` §3 既定范围。
- **W-4 审批命中集合比改动前略宽**：`_match_approval_item()` 的 `match_names` 由 `{tool_name, tool_code}` 扩为 `{tool_name, tool_code, original_name}`，且 `item["tool_code"]` / `item["code"]` 现在也会与 `tool_name` 比较。当平台工具（`resource_type=tool`）的 code 与某个 MCP 工具名相同时，去重阶段会误判为「该组有审批」而放弃合并。后果是多一次改名、不丢功能、不绕过审批，属保守方向；若希望收紧，可在 matcher 里只放开 `mcp_tool` / `mcp` 分支。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "mcp-duplicate-tool-name",
  "iteration": 1,
  "reviewed_sha": "81cad2b36de35044af4b599c2f2910aca97985f9",
  "blocking": [],
  "warnings": [
    {
      "id": "W-1",
      "evidence": "docs/superpowers/plans/mcp-duplicate-tool-name/tasks.md 中 T-6 未勾选，验证记录写明「手工按 ai-query-sops 同服务双端点配置跑一轮对话」未运行（节点无部署环境）；线上 400 消失与前端卡片渲染只有单测层间接证据",
      "suggestion": "在可预览环境用 ai-query-sops 的同服务双端点配置跑一轮对话，确认请求不再返回 duplicate tool name 400，且工具卡片显示「来源 MCP / get_task_list」"
    },
    {
      "id": "W-2",
      "evidence": "src/agent/aidev_agent/utils/tool_naming.py:56-73 is_same_service 要求 Server 名恰好含一个 -prod-；tests/utils/test_tool_naming.py:52 断言 (\"foo-mcp\",\"foo-mcp2\") 不同服务。测试/预发以 -test- 命名时同服务端点退化为改名保留",
      "suggestion": "属 plan.md 风险 1 已登记项；如需覆盖非生产命名，把 -prod- 分隔词做成可配置项"
    },
    {
      "id": "W-3",
      "evidence": "src/agent/aidev_agent/services/event_handlers/agui_writer.py:427-465 续流回填只写 function.name（展示名），不写 function.model_name；chat_history_assembly._extract_tool_calls 因此回落原始名",
      "suggestion": "若需保留同服务两端点在续流历史中的可分性，可在回填路径一并写入 model_name（原始工具名恒存在，故不影响功能正确性）"
    },
    {
      "id": "W-4",
      "evidence": "src/agent/aidev_agent/services/agent/chat.py:2143-2167 _match_approval_item 的 match_names 扩为 {tool_name, tool_code, original_name}，且 resource_type=tool 分支的 item[\"tool_code\"] / item[\"code\"] 也与 tool_name 比较；chat.py:2016 的 matcher 只传 mcp_name",
      "suggestion": "平台工具与 MCP 工具同名时会误判为审批组而放弃合并（保守方向，不绕过审批）；如要收紧，matcher 只放开 mcp_tool/mcp 分支"
    }
  ]
}
```

</details>

## 第 1 轮

### 本轮范围

- 仓库: `git@github.com:ChrisJaunes/bk-aidev-agent.git`（单仓，无跨仓切片）
- 工作分支: `fix/mcp-duplicate-tool-name`
- 基线: `origin/develop` = `7a171a8a`
- 评审对象: `7a171a8a..81cad2b3` 的累计改动（代码实现节点 `c6a34488` 交付）
- 冻结文档核对: 分支内 `docs/superpowers/specs/mcp-duplicate-tool-name/spec.md`、`docs/superpowers/plans/mcp-duplicate-tool-name/plan.md`、`tasks.md` 与已审批附件逐字一致（`diff` 无输出），实现阶段未改冻结文档；`tasks.md` 的勾选与验证记录属实现节点应写范围。

### 逐条需求核对

| 需求 | 结论 | 证据 |
| --- | --- | --- |
| REQ-1 名字唯一 | 满足 | `packages/resource_manager/base.py:796` 汇总后调用 `_dedupe_mcp_tool_names()`；`utils/tool_naming.py:132-220` 按 `tool.name` 分组，keeper 原名占位 + `_unique_name()` 冲突回退，输出集合唯一 |
| REQ-2 同服务合并、不改名 | 满足（按 plan 的 `-prod-` 口径） | `tool_naming.py:66-73` + `:176-183`；`tests/packages/langchain_core/tools/test_base.py::test_construct_mcp_same_service_duplicate_merged` 断言 `["get_task_list","msdk_a","msdk_b"]` 且 keeper 不变名 |
| REQ-3 跨服务改名 `<MCP_NAME>__<TOOL_NAME>` | 满足 | `compose_tool_name()` `tool_naming.py:85-102`；`test_construct_mcp_cross_service_duplicate_renamed` |
| REQ-4 改名后仍可正确执行 | 满足 | 只改 `StructuredTool.name`，执行走 `langchain_mcp_adapters` 闭包内 `session.call_tool(原始名)`；`test_renamed_mcp_tool_routes_to_original_tool_name` 用 mock `ClientSession.call_tool` 断言原始名与入参透传 |
| REQ-5 展示原始名 + 来源 MCP | 满足 | `core/ag_ui/utils.py:82-96 resolve_display_name()`；`event_builders.enhance_tool_call` / `build_tool_calls_with_approval_filter` / `build_tool_result_event` / `_ai_message_to_events` / `_tool_message_to_event` 全部经该函数；`tests/core/ag_ui/test_event_builders.py::TestRenamedToolDisplayName` 断言账本 `function.name` 无 `__` 前缀 |
| REQ-6 保留配置顺序首个、丢弃可追溯 | 满足 | `asyncio.gather` 保持输入顺序（`base.py:_load_all_tools`），`coro_results` 按配置顺序 `extend`；keeper = `members[0]`；丢弃 Server 写入 `metadata["mcp_merged_servers"]` 并记 INFO 日志 |
| REQ-7 INFO 日志且无重名不新增噪音 | 满足 | `base.py:_dedupe_mcp_tool_names()` 仅在 `result.changes` 非空时写 metadata 与日志；`test_construct_mcp_no_duplicate_keeps_metadata_and_silent` 断言 metadata 集合仍为 `{mcp_name, mcp_transport}` 且无新增日志 |
| REQ-8 未重名配置逐项不变 / `selected_tools` 先过滤 | 满足 | `dedupe_mcp_tools()` 在 `len(tools) < 2` 或组内无重复时直接返回、不改对象；`selected_tools` 过滤仍在 `_load_tool()` 内、汇总之前（`test_construct_mcp_selected_tools_filter_before_dedupe`） |
| REQ-9 命名约束与确定性 | 满足 | `sanitize()` + `compose_tool_name()` 截断 + `sha1[:8]`（冲突时 12 位、再退 `_2/_3`）；`TestComposeToolName` 断言正则、长度 64、同输入同结果 |
| REQ-10 改名后审批仍生效 | 满足 | 去重阶段 `approval_matcher` 命中即整组不合并（`tool_naming.py:168-170`）；`build_tools()` 复用 `_normalize_tool_approval_bindings()` 结果（`chat.py:1977/1986/2016`）；`_match_approval_item()` 增加 `original_name` 命中，`target.display_name` 回落原始名；`test_build_tools_same_service_approval_group_not_merged` 断言两个实例都在、名字唯一、`approval.target.display_name == get_task_list` 而 `target.name` 为模型名 |

### 正确性 / 安全 / 数据风险走查

- **并发顺序**：`_load_all_tools()` 用 `asyncio.gather` 且按 `new_server_config` 枚举顺序取结果，keeper 不依赖完成顺序，符合 REQ-6 与「确定性」约束。
- **执行路由**：改名仅作用于 `StructuredTool.name`；`ToolNode` 按模型名下发的 tool_call 查表命中改名后的工具对象，实际调用仍走该对象闭包捕获的原始 MCP 工具名，未新增路由分支，也未改变异常包装（`MCPExceptionWrapper` 未被绕过）。
- **审批绕过**：`ItsmApprovalTarget` 从 `request.tool`（实际执行对象）的 `metadata["approval"]` 构造，改名工具在 `_apply_tool_approval_settings()` 中仍被正确打上 approval metadata；去重阶段额外用「整组不合并」兜底，未发现绕过路径。
- **数据兼容**：账本新增 `function.model_name` 仅在展示名与模型名不同时写入（相等时显式 `pop`），未改名工具的 `builtin_property.tool_calls[].function` 逐字不变；旧记录无该键时 `_extract_tool_calls()` 回落 `function.name`，`chat_history_assembly.py:85` 已覆盖。
- **日志脱敏**：新增 INFO/WARNING 只含工具名、最终名、Server 名与合并列表，不含凭证、请求头、工具入参，符合规格「可观测」约束。
- **不改非目标**：未触碰 `_merge_state()`、`mcp_server_config` 契约、`inputSchema` 合并、平台工具与内置工具去重。

### 验证记录核对

- 记录与改动一致：新增用例可对上改动点（`test_tool_naming.py` 28 例、`test_base.py` 6 个 construct/mcp 用例 + 1 个执行路由用例、`TestRenamedToolDisplayName` 5 例、`TestExtractToolCallsModelName` 3 例、`test_chat_tool_approval_matching.py` 7 例）。
- 全量 `pytest tests` 的唯一失败 `test_otel_config_uses_central_attribute_limits_by_default` 已核为存量问题：`config.py:199-201` 默认 `500 * 1024` 与用例断言的 `80 * 1024` 不一致，两个文件均不在 `7a171a8a..81cad2b3` 的改动清单内，与本变更无关。
- 本节点无可用 Python 测试环境（无 venv / 依赖），按评审约定未重跑既有验证命令；改为静态核对改动与用例的对应关系，未发现「记录声称通过但代码不支持」的项。
- 剩余未覆盖项即 W-1 的端到端手工回归，作为建议记录，不作为打回理由。

### 结论

逐条需求均可在代码与用例中找到对应实现，未发现正确性、安全或数据层面的阻塞项；未解释的验证失败不存在。本轮判定 **通过，但有改进建议**（W-1 ~ W-4）。
