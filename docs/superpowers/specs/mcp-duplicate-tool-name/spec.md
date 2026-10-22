# MCP 重名工具导致 LLM 请求 400

- 变更名: mcp-duplicate-tool-name
- 工作分支: fix/mcp-duplicate-tool-name
- 类型: 缺陷
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 1

## 范围

本仓（Agent 运行时）内，从 MCP 装配到模型请求之间的工具名唯一性。

- `BaseResourceManager.construct_mcp()` 汇总多个 MCP Server 工具后，返回值中的工具名必须唯一。
- `ChatAgentBuilder.build_tools()` 汇总后的工具列表（平台工具 + MCP 工具）在送入 `bind_tools` 前，MCP 工具之间不得存在重名。
- MCP 工具的用户可见展示（ag-ui 事件中的工具名与来源 MCP）。

## 复现

1. 某智能体（调用方 `ai-query-sops`）的 `mcp_server_config.mcpServers` 同时注册 `msdk-console-prod-mcp` 与 `msdk-console-prod-mcp2` 两个端点，二者同属 `msdk-console` 服务，且都暴露 `get_task_list`。
2. `construct_mcp()` 并发拉取各 Server 工具后直接拼接（`packages/resource_manager/base.py:736-749`），未做名字去重。
3. `bind_tools()`（`core/nodes/model/model_chain.py:157`）把两个同名 `get_task_list` 一并序列化进 `POST /api/aidev/gateway/llm/v1/chat/completions`。
4. 上游返回 `400 invalid_request_error: duplicate tool name get_task_list`。

Trace `3de139fd248427e6ad209c5561f3578d`（2026-09-29 11:21:27 CST，模型 qwen3-5-397B）在 11:21:06、11:21:21、11:21:27 连续失败 3 次；近 6 小时同类错误 112 条。

现有 `_merge_state()`（`services/agent/chat.py:794`）按工具名去重，但只作用于 ag-ui state（前端展示），与模型请求是两条链路，因此前端看起来正常、模型侧仍然 400。该函数无法作为本缺陷的修复点。

## 期望与实际

- 期望：装配阶段保证模型请求中的工具名唯一；同服务的同名工具只出现一次；不同服务的同名工具各自可区分、可调用。
- 实际：同名工具被原样重复下发，模型请求被上游拒绝，整轮对话失败。

## 影响与回归范围

- 影响：所有配置了「同服务多端点」或「跨服务同名工具」的智能体；表现为整轮请求 400、用户无法得到回复，且重试无效（同一配置必然复现）。
- 回归范围：单 Server 配置、无重名配置下的工具名、工具顺序、`selected_tools` 过滤结果、工具审批绑定（按工具名匹配）、ag-ui 工具事件、MCP 调用链路。

## 非目标

- 不修改平台侧 `mcp_server_config` 的下发格式，不修改 MCP Server 本身的工具定义。
- 不对平台工具（`tool_codes` 构造的工具）与内置工具（`knowledge_retrieval`、`read_image` 等）做去重或改名；它们与 MCP 工具、彼此之间的重名不在本次范围。
- 不合并同名工具的 `inputSchema`、不改变工具描述与参数定义以适配不同 Server。
- 不改动 LLM Gateway 服务本身的入参校验。
- 不做工具名国际化、展示文案改版或前端卡片样式调整。

## 跨仓边界

单仓变更，无跨仓依赖，无跨仓接口变更。下游技术方案与实现只作用于本仓；`mcp_server_config` 契约保持现状，平台侧无需联动发布。

## 需求

- REQ-1 `construct_mcp()` 返回的 MCP 工具列表中，`tool.name` 必须唯一，同一 Server 内重复与跨 Server 重复均需消除。
- REQ-2 同名工具若来自「同一服务」的不同 MCP 端点（端点 Server 名互为前缀，如 `msdk-console-prod-mcp` 与 `msdk-console-prod-mcp2`），视为同一工具，只保留一个实例，不重命名。
- REQ-3 同名工具若来自不同服务，则全部保留，并按 `<MCP_NAME>__<TOOL_NAME>` 重命名（双下划线分隔），其中 `MCP_NAME` 为工具所属的 Server 名。
- REQ-4 重命名后的工具必须仍能正确执行：模型调用 `<MCP_NAME>__<TOOL_NAME>` 时，实际调用路由到原 Server 的原始 `<TOOL_NAME>`，入参、返回值、异常包装与重试行为与原工具一致。
- REQ-5 重命名与合并仅影响模型的工具名与执行路由；用户可见的工具展示必须仍呈现原始工具名（如 `get_task_list`）并附带来源 MCP 标识（`mcp_name`），不得露出 `<MCP_NAME>__` 前缀。
- REQ-6 同一服务多端点合并时，保留 `mcp_server_config` 配置顺序中最先出现的 Server 的实例，不得依赖并发完成顺序；被丢弃的实例必须可追溯。
- REQ-7 发生合并或重命名时，必须记录 INFO 级别日志，包含原始工具名、最终工具名、来源 Server 名；被合并丢弃的 Server 名一并记录。未发生重名时不得新增噪音日志。
- REQ-8 未发生重名的配置，其工具名、工具数量、工具顺序、工具 metadata 必须与本次改动前完全一致；`selected_tools` 过滤在去重判定之前生效，过滤后不再重名的不产生重命名。
- REQ-9 重命名后工具名必须满足模型侧命名约束：仅含 `[A-Za-z0-9_-]`、长度不超过 64 字符；Server 名中的其他字符替换为 `_`，超长时截断 Server 前缀并追加稳定短哈希以保证唯一且可复现。
- REQ-10 工具审批按工具名匹配的逻辑必须对重命名后的工具继续生效，不得因改名导致已配置审批的工具绕过审批。

## 验收

- 当某智能体配置了属于不同服务的两个 MCP Server 且二者都暴露 `get_task_list` 时，系统必须使模型请求中同时存在 `get_task_list` 与 `<MCP_NAME>__get_task_list` 各一个，且该请求不再返回 `duplicate tool name` 的 400。
- 当两个 MCP Server 属同一服务（Server 名互为前缀）且都暴露 `get_task_list` 时，系统必须只保留一个名为 `get_task_list` 的工具实例，且不产生任何重命名。
- 当模型发起对 `<MCP_NAME>__<TOOL_NAME>` 的调用时，系统必须把该调用转发到 `MCP_NAME` 对应 Server 的原始 `<TOOL_NAME>`，并原样返回其结果。
- 当模型发起对保留下来的 `get_task_list` 的调用时，系统必须把该调用转发到配置顺序中最先出现的那个 Server。
- 当配置中不存在重名工具时，系统产出的工具名列表必须与改动前逐项相同，顺序与 metadata 亦相同。
- 当 `selected_tools` 过滤后某 Server 不再暴露同名工具时，系统不得对该工具重命名。
- 当发生合并或重命名时，系统必须输出一条 INFO 日志，包含原始工具名、最终工具名与来源 Server 名。
- 当重命名后的工具被调用时，前端工具卡片必须展示原始工具名 `get_task_list` 与来源 MCP 名，不得展示 `<MCP_NAME>__` 前缀。
- 当某工具在审批配置中已绑定审批、且因跨服务重名被重命名时，系统必须对该工具继续触发审批。
- 当 `<MCP_NAME>__<TOOL_NAME>` 长度超过 64 字符时，系统必须产出符合 `[A-Za-z0-9_-]` 且长度不超过 64 的唯一工具名，且同一输入重复装配得到相同名字。

## 边界与非功能

- 命名约束：工具名 `^[A-Za-z0-9_-]{1,64}$`；分隔符固定为 `__`；不引入空格、点号、斜杠等字符。
- 确定性：同一份 `mcp_server_config`（含顺序）重复装配，必须得到相同的工具名集合与顺序；合并取舍以配置顺序为准。
- 顺序稳定：重命名不改变工具在列表中的相对位置，避免影响提示词缓存与既有工具选择倾向。
- 性能：仅做内存后处理，不新增 MCP 网络请求，复杂度与工具总数线性相关；不得改变现有并发拉取方式与超时/重试语义。
- 可观测：不改变现有 MCP span 属性（`mcp.server.name`、`mcp.tool.count` 等）语义；新增日志不得输出凭证、请求头或工具入参。
- 兼容：`make_mcp_tools()` 作为 `construct_mcp()` 的兼容层，行为必须与主链路一致，不得出现两套去重规则。

## 开放问题与假设

- 假设：跨服务判定以 Server 名互为前缀为准（方案示例 `msdk-console-prod-mcp` / `msdk-console-prod-mcp2`）。该判定可能把不同服务误判为同一服务（如 `foo-mcp` 与 `foo-mcp-admin`）；本次按方案执行，误判风险由日志留痕，后续如需精确判定需平台下发服务标识。
- 假设：同一服务的同名工具若 `inputSchema` 不一致，仍按 REQ-6 保留配置顺序第一个，不做 schema 合并，仅记 WARNING 日志提示差异。
- 假设：工具名超长时采用「截断 Server 前缀 + 稳定短哈希」方案；短哈希算法与长度在技术方案阶段确定，规格只约束结果必须唯一、可复现且符合命名约束。
- 已确认决策（用户答复）：重名分隔符用双下划线；去重范围仅限 MCP 工具之间；前端展示保留原始工具名并附来源 MCP 标识。
