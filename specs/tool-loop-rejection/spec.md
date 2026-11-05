# 工具的循环调用拒绝

- 变更名: tool-loop-rejection
- 工作分支: feature/tool-loop-rejection
- 类型: 功能
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 无

## 范围

在 SDK 工具执行层建立「同一 Trace 内相同工具 + 相同参数连续重复调用」的检测、拒绝与终止机制，并补齐失败状态如实标记这一前置条件。具体包含六件事：

1. **计数与阈值**：同一判定键（工具名 + 参数指纹）在同一 Trace 内连续调用，默认允许真实执行 3 次，第 4 次起拒绝。
2. **拒绝反馈**：被拒绝的调用不执行工具本体，但必须向模型返回一条明确失败反馈，含已连续调用次数与最后一次真实返回内容摘要，引导模型换工具、换参数或终止任务。
3. **反馈轮次与终止**：拒绝后预留有限反馈轮次（默认 2 轮）；该判定键反馈轮次用尽而模型仍重复同一调用时，提前终止本次 run，终态失败并把原因归因为「工具循环调用拒绝」。
4. **失败状态如实标记**：工具底层失败（MCP 传输错误、超时、连接断裂、工具抛错）以及"未抛异常但结果内容带错误信号"的调用，都必须被标记为失败，不允许以 success 收尾。
5. **Trace 表达**：拒绝与失败一律复用既有 span 机制（`error.type` + span status）表达，不新增私有状态属性；拒绝量可按现网 `span_name='tool.execution'` + `error.type` 口径统计。
6. **配置**：阈值与反馈轮次只由 SDK 环境变量提供，平台侧不参与。

## 非目标

- 不做 MCP broken pipe 后的连接自动重建、读操作幂等与失败快速返回（Issue 已判定为独立排期）。
- 不新增 `execution_status` 或任何等价私有属性；状态一律走既有 `error.type` + span status。
- 不改平台后端（`bk-aidev`）的接口、配置项、会话/调度层字段与前端渲染。
- 不新增工具级重试、退避或降级实现；"失败重试 / 降级"仍由模型依据失败信号自主决策。
- 不改图拓扑、`Send` 分派方式与 `recursion_limit`（保持 1000）。
- 不做跨 Trace、跨 run、跨会话的重复计数，不把计数持久化到外部存储或 checkpoint 之外。
- 不识别交替重复（A,B,A,B…）与近似重复（参数轻微变化），这两类不视为"连续重复"。
- 不改既有 `knowledge_retrieval` 同批 single-flight 去重逻辑（`deduplication.py`）。
- 不改工具结果脱敏、不可信内容清洗、结果长度限制等既有 wrapper 行为。
- 不做参数指纹之外的内容级相似度判定（不比对返回内容是否"相同"）。

## 跨仓边界

- 业务仓只有 `bk-aidev-agent` 一个，无跨仓接口、无跨仓依赖、无跨仓执行顺序。
- 平台侧（`bk-aidev`）无需改造：终止通过既有 `RunErrorEvent` 链路呈现，会话侧已有 `handle_run_error` 把失败原因写成 `status="error"` 的 assistant 消息，不新增字段即可展示。
- 监控侧（BkMonitor APM）无需改造：沿用既有 span 名与属性（`tool.execution`、`tool.name`、`tool.input`、`agent.info.code`、`error.type`），拒绝与失败靠 `error.type` 取值区分，不新增白名单项。
- 与在途分支无强依赖：本变更基于 `develop` 切分；若 `feature/context-observability` 先合入，以本变更的重复检测与状态标记实现为准，不重复实现失败状态逻辑。

## 用户故事

- 作为平台运营者，我希望模型陷入重复工具调用时被及时打断，避免单 Trace 上百次重复调用持续消耗 token 与时间预算。
- 作为 Agent 使用者，我希望任务因重复调用被中断时看到简明失败原因（「任务因工具重复调用被中断」），而不是长时间无响应后隐性超时。
- 作为排障者，我希望在 Trace 中区分「真实执行」「失败」「因循环被拒绝」三类调用，并能沿用现网重复调用统计 SQL 观测治理效果。
- 作为接入方，我希望默认阈值即可生效，需要调整时只改部署环境变量，不必等平台发版。

## 需求

### REQ-1 重复判定与连续计数

- 判定键 = 「工具名 + 参数指纹」。参数指纹按规范化 JSON 计算（对象键排序、紧凑分隔符、字符串原样、`null` 与缺失区分），仅用于比较，不写入 Trace、不写入 ToolMessage。
- 计数范围 = 同一 Trace（同一 run）内同一判定键的"连续"调用次数，按消息历史中工具调用出现的先后顺序累计。
- 连续语义：同一判定键的相邻调用之间，若出现不同工具名或不同参数指纹的工具调用，则该判定键的执行计数与拒绝计数一并归零，下次命中重新从第 1 次计。
- 计数与工具执行结果无关：成功、失败、被拒绝都计入连续次数，避免"失败—重试"链绕过计数。
- 并行批次：同一个 assistant 消息内一次性发出的多个 tool_call 由 `Send` 并行分派，各自看到分派时刻的状态快照；同一批次内出现的相同判定键不构成"连续重复"，本变更不保证批次内互斥。

### REQ-2 阈值与拒绝执行

- 阈值 `T` 表示"允许真实执行的次数"，默认 3，取值必须 ≥ 1。
- 同一判定键第 1 至第 `T` 次调用必须真实执行工具本体，行为与变更前一致。
- 同一判定键第 `T+1` 次及之后的调用必须拒绝执行：不进入工具本体，不产生任何工具副作用，不写 `gen_ai.tool.call.result`。
- 拒绝必须以失败反馈的形式返回，不允许静默丢弃或返回成功：向模型返回一条 `status="error"` 的 ToolMessage，内容必须包含工具名、该判定键已连续调用次数、本次调用被拒绝的结论、最后一次真实返回内容的摘要，以及"换用其他工具 / 调整参数 / 终止任务"的引导。
- 拒绝反馈中的"最后一次真实返回内容"必须取自该判定键最近一次真实执行的返回结果，长度沿用既有工具输出上限，超限从尾部截断并标注截断。
- 拒绝时 Trace 仍须照常上报 `tool.name` 与 `tool.input`，保证现网重复调用统计 SQL 的 `GROUP BY` 口径不变。

### REQ-3 反馈轮次与提前终止

- 反馈轮次 `F` 表示"拒绝后允许返回给模型的失败反馈次数"，默认 2，取值 ≥ 0。
- 同一判定键第 `T+1` 至第 `T+F` 次调用各返回一次拒绝反馈；`F=0` 时不存在拒绝反馈，首次重复即终止。
- 当该判定键的拒绝反馈次数已达 `F`，而模型再次发起同一判定键调用（第 `T+F+1` 次）时，SDK 必须立即终止本次 run：该次调用不执行工具本体，也不返回工具反馈。
- 因此单 Trace 内同一判定键的"工具调用往返"上限为 `T+F`（默认 5）：真实执行 ≤ `T` 次，拒绝反馈 ≤ `F` 次，第 `T+F+1` 次直接终止。
- 终止必须表现为 run 失败：终态为失败，失败原因含「任务因工具循环调用被中断」、工具名与该判定键已发生的调用/拒绝次数，供会话侧与调度层展示和统计。
- 终止不得丢弃中间结果：终止前已写入状态的消息（含 assistant 消息、已产生的 ToolMessage、任务列表等）必须保留，并可由既有 checkpoint 机制在 resume 时恢复。

### REQ-4 失败状态如实标记（配套前置）

- 异常路径（工具抛错、MCP `isError`、连接断裂、超时）必须产出 `tool.execution` span 且以 ERROR 结束、`error.type` 为异常类名，同时返回 `status="error"` 的 ToolMessage。该路径当前已成立，本变更不得回退。
- 内容路径：工具正常返回但结果正文以错误信号（`[ERROR]`）开头时，该次调用必须被判为失败——ToolMessage `status="error"`，对应 span 以 ERROR 结束且带可辨识的 `error.type`，不得以 success 结束。
- 失败判定只认前缀：仅当结果正文（去前导空白后）以 `[ERROR]` 开头才判失败；正文中部或尾部出现该字样不得判失败，避免把正常文档内容误判。
- 判定只作用于字符串结果；结构化 / 多模态结果不做内容匹配，沿用既有结果处理路径。

### REQ-5 拒绝与终止的 Trace 表达

- 每一次被拒绝的调用必须产生一条 `tool.execution` span，且该 span 必须以 ERROR 结束并带专属 `error.type` 取值，使"循环拒绝"可与普通失败区分统计。
- 被拒绝的调用不得写 `gen_ai.tool.call.result`（没有真实结果），也不得被计为成功调用。
- 终止不产生额外的工具 span；终止以顶层 agent span 失败（ERROR + 失败原因）表达。
- 不新增 `execution_status` 等属性；既有属性名、层次与语义保持不变。

### REQ-6 配置项

- 新增两项环境变量：`BKAI_TOOL_LOOP_REPEAT_LIMIT`（阈值 `T`，默认 3，必须 ≥ 1）、`BKAI_TOOL_LOOP_FEEDBACK_ROUNDS`（反馈轮次 `F`，默认 2，必须 ≥ 0）。
- 非法值（非整数、阈值 < 1、反馈轮次 < 0）必须在进程启动期报错并指明变量名，不静默回落到默认值。校验风格沿用既有 `BKAI_TOOL_LAZY_LOADING_*` 的实现。
- 两项配置只从部署环境读取，不接受平台下发、请求参数或智能体配置覆盖。
- 未设置时取默认值，行为与"默认阈值 3 / 反馈 2"一致。

### REQ-7 非重复调用零影响

- 不构成连续重复的调用，其执行结果、ToolMessage 内容与 `status`、span 名与属性必须与变更前逐项一致。
- 判定逻辑中的任何异常不得阻断工具调用主链路：判定失败时按"不拒绝"放行，不影响工具执行、返回与 Trace 上报。
- 新增开销限于参数指纹计算与当前连续段的历史扫描，不引入网络调用、模型调用或额外持久化。

## 验收

- AC1 当同一 Trace 内同一工具名与同一参数指纹连续调用第 1 至第 3 次时（默认阈值），系统必须真实执行该工具，返回与变更前一致的 ToolMessage 与 OK 的 `tool.execution` span。
- AC2 当同一判定键第 4 次调用时，系统必须不执行工具本体，返回 `status="error"` 的 ToolMessage，且该次调用不得写出 `gen_ai.tool.call.result`。
- AC3 当拒绝发生时，该次调用的 `tool.execution` span 必须以 ERROR 结束，`error.type` 为循环拒绝的专属取值；同一 Trace 内成功调用的 span 必须仍为 OK 且不带 `error.type`。
- AC4 当拒绝反馈内容被检查时，必须包含工具名、已连续调用次数、被拒绝结论、最后一次真实返回内容摘要与后续动作引导。
- AC5 当同一判定键第 5 次调用时（`T+F`，默认），系统必须仍返回拒绝反馈；当第 6 次调用时（`T+F+1`），系统必须终止 run 且不返回工具反馈。
- AC6 当 run 被终止时，终态必须为失败，失败原因必须含「工具循环调用被中断」与工具名；终止前已产生的 assistant 消息与 ToolMessage 必须仍存在于状态中，并可经 resume 恢复。
- AC7 当工具抛异常或 MCP 返回 `isError` 时，该次调用必须产生 ERROR 的 `tool.execution` span、`error.type` 等于异常类名，且返回 `status="error"` 的 ToolMessage。
- AC8 当工具正常返回且结果正文以 `[ERROR]` 开头时，系统必须判该次调用失败：ToolMessage `status="error"`、span 以 ERROR 结束且 `error.type` 非空。
- AC9 当工具返回值正文中部或尾部包含 `[ERROR]` 字样、但并非开头时，系统必须仍判为成功（span OK、ToolMessage `status` 非 error）。
- AC10 当同一判定键的连续序列中间穿插了不同工具或不同参数调用时，该判定键的计数必须归零，其后再次调用同组合必须重新从第 1 次计。
- AC11 当设置 `BKAI_TOOL_LOOP_REPEAT_LIMIT=5` 时，第 6 次调用起必须被拒绝；当设置 `BKAI_TOOL_LOOP_FEEDBACK_ROUNDS=0` 时，首次重复调用必须直接终止 run。
- AC12 当环境变量取非法值（非整数、阈值 0 或负数、反馈轮次负数）时，进程启动必须失败并报出变量名。
- AC13 当构造一次不含重复的普通工具调用时，其 ToolMessage、span 名、span 属性与状态必须与变更前逐项一致。
- AC14 当判定逻辑内部抛出异常时，工具调用必须照常执行并返回，不出现新增异常，也不产生拒绝。

## 边界与非功能

- **默认值即生效**：不提供独立功能开关；需要等效关闭时可把阈值调到足够大的值，此用法列入假设。
- **并行批次**：同一 assistant 消息内并行发出的相同判定键调用不保证互斥（见 REQ-1），单批次内至多按各自首次出现计一次连续。
- **交替与近似重复**：A/B 交替、参数轻微变化的重复不在拦截范围，可能仍出现长循环，属已知限制。
- **性能**：每次工具调用增加一次指纹计算与一次连续段扫描，复杂度与该连续段长度线性相关；不引入 IO、RPC 或锁竞争，不改变单次调用耗时量级。
- **兼容**：默认阈值下，偶发重复（≤3 次）行为完全不变；仅连续第 4 次起才有可见差异。
- **安全**：拒绝反馈只回传该判定键最近一次真实返回内容，沿用既有输出上限截断，不新增外发通道，不改变脱敏与不可信内容清洗 wrapper 的既有行为。
- **可测性**：全部验收可在本仓用 fake 工具 + 内存 span exporter + 直接驱动 ToolNode 完成，不依赖真实模型、MCP 服务或监控平台。
- **观测口径**：拒绝与终止均可由 `span_name='tool.execution'` + `error.type` 与 run 终态失败统计；不新增属性，现网 SQL 无需改写。

## 现状证据（已证实）

| 事实 | 证据 |
| --- | --- |
| 工具执行统一入口为 `build_tool_node`，自定义 wrapper 以 `(ToolCallRequest, execute)` 形态串联 | `core/nodes/tool/node.py:184`、`:228` |
| 默认异常处理 `default_tool_call_handler` 作为 `handle_tool_errors` 传入 ToolNode | `core/nodes/tool/node.py:55` |
| 图拓扑为 model → pv_node →（Send 逐 call 分派）→ tools → model，循环无重复检测 | `core/graphs/react/graph.py:148`、`:1330-1331` |
| 单次 run 的循环上限只有 `recursion_limit=1000`，不存在重复调用次数上限 | `core/graphs/react/graph.py:1350` |
| 工具 span 名为 `tool.execution`，并上报 `tool.name` / `tool.input` | `packages/opentelemetry/callback_handler.py:1224`、`packages/opentelemetry/span_utils.py:303` |
| `on_tool_end` 无条件把 span 置为 OK，并写入 `gen_ai.tool.call.result` | `packages/opentelemetry/callback_handler.py:1250`、`:1261`、`:1278` |
| 异常逃逸时 `_handle_error` 写 `error.type`（异常类名）并把 span 置 ERROR | `packages/opentelemetry/callback_handler.py:673`、`:694`、`:695` |
| 实测：工具抛 `ToolException` 时，span 为 `tool.execution` + `status=ERROR` + `error.type=ToolException`，且 `tool.input` 正常上报 | 本轮临时探针脚本（内存 exporter）实测输出 |
| MCP `isError` 结果在适配层被转成 `ToolException` 抛出，因此走异常路径 | `langchain_mcp_adapters/tools.py:79` |
| 仓库中不存在 `execution_status`（或等价字段），全仓 Python 源码检索为空 | `grep -rn "execution_status" src/agent` 空结果 |
| `[ERROR]` 前缀仅由异常路径生成（MCP 异常包装为 `ToolException`），无"返回错误文本但标成功"的显式实现 | `packages/langchain_core/tools/base.py:611`、`:624-633`、`:815-824` |
| 内置 wrapper 链（外→内）：不可信净化 → 脱敏 → ITSM 审批 → 参数修复 → 计时 → 结果限长 → 自定义 wrapper → single-flight 去重 → 工具本体，插入点已存在 | `core/nodes/tool/node.py:241-300` |
| 已有同批 single-flight 去重仅作用于 `knowledge_retrieval`，与跨轮次重复检测不冲突 | `core/nodes/tool/deduplication.py:53`、`:78` |
| 配置项以 `environs` 在 import 期解析，越界值可在启动期报错 | `config.py:190-192` |
| run 失败经 `RunErrorEvent` 落成 `status="error"` 的 assistant 消息，可承载失败原因 | `services/event_handlers/base.py:937`、`:992-1004` |
| 图默认用 `MemorySaver` 做 checkpoint，终止前的状态可恢复 | `core/graphs/react/graph.py:1180-1183` |

## 开放问题与假设

已确认决策（本轮澄清）：

1. 工具调用的成功 / 失败 / 拒绝状态不新增字段，复用既有机制：异常时 span 自动写 `error.type` 与 span status。拒绝因此以专属异常类型表达，落点仍是 `error.type` + span status。
2. 阈值语义为「允许执行次数」：默认 3 表示前 3 次放行，第 4 次起拒绝。
3. 阈值与反馈轮次只从 SDK 环境变量读取，平台侧不改。

默认假设（如需调整请在审批意见中指出）：

- 反馈轮次 `F` 计为"拒绝反馈次数"：`T+F` 次调用往返后，第 `T+F+1` 次直接终止（默认 3 次执行 + 2 次反馈 = 5 次，第 6 次终止），以此满足 AC3"调用次数不超过阈值 + 有限反馈轮次"。
- "连续"只按同一判定键的相邻调用判定；穿插其他工具或参数变化即归零。交替重复（A,B,A,B…）视为非连续，不拦截。
- 拦截范围为全部工具（MCP、HTTP 接口、内置工具、写操作与审批类工具一视同仁），不设白名单；审批类工具被拒绝时同样不进入审批流程。
- 参数指纹采用规范化 JSON（键排序 + 紧凑分隔），不做类型宽松比较（`1` 与 `"1"` 视为不同参数）。
- 场景 3 的缺口定位为"未抛异常但结果内容带错误信号"的路径：异常路径经实测已正确标记 ERROR，因此 REQ-4 只补齐内容路径，不回改异常路径。
- 不新增独立功能开关；等效关闭通过把阈值调到极大值实现。
- 拒绝反馈中"最后一次真实返回内容"沿用既有工具输出上限截断，不新增专用预算。
- 规格落位 `specs/tool-loop-rejection/spec.md`：`develop` 引入 `specs/` 目录后按 `specs/<变更名>/spec.md` 约定存放。
- 提交信息按项目惯例携带 `--story=1070093903139672317`。
