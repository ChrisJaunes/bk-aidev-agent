# 工具的循环调用拒绝 技术方案

- 变更名: tool-loop-rejection
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/tool-loop-rejection/spec.md

## 背景与目标

同一 Trace 内「相同工具 + 相同入参」被反复调用，会让单次运行持续消耗 token 与时间预算直至超时（现网单 Trace 最多观测到 311 次重复调用）。根因链上还有一环：MCP 传输断裂后错误被包成正常内容并标记成功，模型因此认为「调用成功但没拿到结果」而继续重试。

本方案在 SDK 工具执行层建立连续重复检测：允许真实执行 `T` 次，之后拒绝并回传失败反馈，反馈轮次用尽而模型仍在重复时终止本次 run；同时把「工具未抛异常但结果正文以 `[ERROR]` 开头」的调用如实标记为失败。全部能力复用既有 `error.type` + span status 表达，不新增私有状态字段。

## 非目标

- 不做 MCP broken pipe 后的连接自动重建、读操作幂等与失败快速返回（Issue 已定为独立排期）。
- 不新增 `execution_status` 或任何等价私有属性；拒绝与失败一律走 `error.type` + span status。
- 不改平台后端接口与前端渲染，不改图拓扑、`Send` 分派方式与 `recursion_limit`（保持 1000）。
- 不新增工具级重试/退避/降级实现；模型依据失败信号自主决策。
- 不识别交替重复（A,B,A,B…）与近似重复（参数轻微变化），不比对返回内容是否相同。
- 不改 `knowledge_retrieval` 同批 single-flight 去重、工具结果脱敏、不可信内容清洗、结果限长等既有 wrapper 行为。

## 现状

执行链路（自外向内）：

1. `build_tool_node`（`src/agent/aidev_agent/core/nodes/tool/node.py:184`）装配 wrapper 链并返回 `KnowledgeToolNode`（`:302`）。外层到内层依次为：`tool_call` 代理解包（可选）、不可信净化、脱敏、ITSM 审批、参数修复、计时、结果限长、自定义 wrapper、single-flight 去重、工具本体。
2. 图把每个 pending tool_call 用 `Send` 单独派发（`core/graphs/react/graph.py:148-171`），每个 `tools` 任务携带**分派时刻的 state 快照**；单次 run 的循环上限只有 `recursion_limit=1000`（`graph.py:1350`），不存在重复次数上限。
3. `ToolNode._afunc` 逐个 call 调 `_arun_one` → wrapper 链 → `_execute_tool_async` → `tool.ainvoke`。
4. **工具 span 在 `tool.ainvoke` 内部产生**：`BaseTool.run` 派发 `on_tool_start`（`packages/opentelemetry/callback_handler.py:1203`）建 `tool.execution` span 并写 `tool.name` / `tool.input`；`on_tool_end`（`:1251`）无条件 `set_tool_response`（`:1263`，`gen_ai.tool.call.result` 唯一写入点）并置 OK（`:1278`）；异常走 `on_tool_error` → `_handle_error`（`:673`）写 `error.type=异常类名`（`:694`）并置 ERROR。
5. **wrapper 位于回调边界之外**：拒绝调用不进入 `tool.ainvoke`，`on_tool_start` 从未触发，因此不产生任何 `tool.execution` span；wrapper 抛出的异常会被 `ToolNode._arun_one` 的 `except Exception` 兜底转成 `status="error"` 的 ToolMessage，既无 span 也无法终止 run。本轮实测：wrapper 返回错误消息与 wrapper 抛异常两种形态，`tool.execution` span 数均为 0。
6. 既有同类机制：`knowledge_retrieval` 的 single-flight 去重只覆盖同一批次（`core/nodes/tool/deduplication.py:53`）；ITSM 审批拒绝同样以 wrapper 短路返回 error ToolMessage（`approval_wrapper.py:122`），不产生工具 span。

## 方案对比

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| A（选定） | 新增 `LoopGuardToolNode` 覆写 `_run_one` / `_arun_one`：判定前置，拒绝时手工派发一对工具回调，终止时直接抛异常 | 正常路径逐行不动，工具身份不变；代价是依赖 langgraph 私有方法，且拒绝 span 需手工复刻 `BaseTool` 的回调参数 |
| B | 用工具代理包裹每个 `BaseTool`，在 `_run` / `_arun` 内判定并抛异常 | span 由既有机制天然产出，不需手工派发；但所有调用都要过代理层，必须原样转发 `name/description/args_schema/metadata` 与延迟加载用的 `catalog`，而审批、`tool_call` 解包、去重都按 `request.tool` 判定，改动面与回归面更大 |
| C | 最外层 wrapper 判定后返回错误 ToolMessage，再手工补 span | 表面改动最小，但 `tool.input` 需另行复刻，且终止无法实现（wrapper 异常被 `_arun_one` 吞掉），最终仍要叠加 A 的节点改动，排除 |

内容路径（`[ERROR]` 前缀）的两种落点：

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| A（选定） | 节点层把 ToolMessage 置 `status="error"`；回调层按同一前缀独立判 span | 不依赖是否开启 trace，失败标记始终生效；同一判定规则出现在两层，需共享同一函数 |
| B | 只在 `on_tool_end` 里就地改状态并置 span | 改动最少，但关闭 trace 的部署不会标记失败，失去「让模型感知真实失败」的前置作用，排除 |

终止的实现候选：返回 `Command(goto=END)`（改变节点返回语义，且不构成 run 失败）、抛 `GraphBubbleUp` 子类（会被 `_arun_one` 的 `except Exception` 一并吞掉，除非从工具体内部抛出）、抛普通异常越过 ToolNode（选定）。实测普通异常能让整图失败，顶层 `invoke_agent` / `chain.task` span 置 ERROR 且不产生多余工具 span。

## 决策

### D-1 落点与类结构

- 新增 `core/nodes/tool/loop_guard.py`：`LoopGuardPolicy`（`repeat_limit` / `feedback_rounds` / `result_limit`）、`LoopDecision`、`ToolLoopRejectionError`、`ToolLoopTerminationError`、`LoopGuardToolNode(ToolNode)`。
- `LoopGuardToolNode.__init__` 接收可选的 `loop_guard: LoopGuardPolicy`，其余参数原样交给 `ToolNode`；`KnowledgeToolNode` 改为继承它，`build_tool_node` 的装配顺序与 wrapper 链一行不改，只把 `node_options.result_limit_thrd` 作为反馈摘要上限传入。
- 覆写 `_run_one` / `_arun_one`（`langgraph/prebuilt/tool_node.py`，仓库锁定 `langgraph==1.0.7`）：

```text
判定（任何异常 → 放行）
  allow     → super()._run_one / _arun_one（与变更前完全一致）
  reject    → 派发一条工具 span（结束为 ERROR）→ 返回 status="error" 的 ToolMessage
  terminate → 抛 ToolLoopTerminationError（不进入 super，越过 ToolNode 兜底）
```

判定逻辑整体包在 `try/except Exception` 内，判定失败一律按「不拒绝」放行（REQ-7 / AC14）。

### D-2 判定键与连续计数

- 判定键 = 工具名 + 参数指纹。指纹为 `json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`；序列化失败（不可 JSON 化）时本次不判定。指纹只在内存比较，不写 Trace、不写 ToolMessage。
- 计数窗口 = 最后一条 `HumanMessage` 之后的消息；窗口内没有用户消息时退化为全量历史（见风险 R-4）。
- 批次折叠：同一个 `AIMessage` 内同一判定键只算一次（取首次出现）。这样同一 `Send` 批次里的相同调用既不互相加成，也不互相抑制。
- 当前 call 用 `tool_call_id` 在序列中定位；定位不到时按追加在末尾处理。
- `n` = 当前位置之前、同一判定键的连续出现次数；遇不同工具名或不同指纹即停止累计。默认 `T=3`、`F=2`：

| 条件 | 结果 |
| --- | --- |
| `n < T` | 放行，真实执行（第 1～T 次） |
| `T <= n < T+F` | 拒绝，返回失败反馈（第 T+1～T+F 次） |
| `n >= T+F` | 终止 run（第 T+F+1 次起） |

### D-3 拒绝反馈

内容固定包含：工具名、该判定键已连续调用次数（含本次）、被拒绝结论、最后一次真实返回内容摘要、后续动作引导。摘要取自该判定键连续段内最近一条有对应 ToolMessage 的真实结果，按 `result_limit_thrd` 从尾部截断并标注截断；该结果在写入 state 前已经过脱敏与不可信内容清洗，因此反馈不会引入未处理内容。

### D-4 拒绝调用的 Trace 表达

每条被拒绝的调用手工派发一条 `tool.execution` span 并以 ERROR 结束：

```python
run_id = uuid4()
rm = await callbacks.on_tool_start(
    {"name": call["name"], "description": tool.description or ""},
    str(call.get("args") or {}),      # 必须与 BaseTool 一致：str(args)，不是 json.dumps
    run_id=run_id,
    inputs=call.get("args"),
    tool_call_id=call.get("id"),
    name=call["name"],
)
await rm.on_tool_error(ToolLoopRejectionError(feedback))   # 同步路径为 rm.on_tool_error(...)
```

- `serialized`、`input_str`、`tool_call_id` 逐项复刻 `BaseTool.run`，保证 `tool.input` / `gen_ai.tool.call.arguments` 与真实调用同格式，现网 `GROUP BY tool.input` 口径不变（AC3/AC4 的前提）。
- 不调用 `on_tool_end`，因此不写 `gen_ai.tool.call.result`；`error.type` 由 `_handle_error` 取异常类名，得到 `ToolLoopRejection`。
- `config["callbacks"]` 缺失（未启用 trace）时跳过派发，拒绝本身照常返回。
- 终止不产生工具 span（REQ-5）。

### D-5 终止

`ToolLoopTerminationError(RuntimeError)`，消息含「任务因工具循环调用被中断」、工具名、已发生调用次数与被拒绝次数。异常从节点直接抛出，经 `RunErrorEvent` 链路（`services/event_handlers/base.py:937` 的 `handle_run_error`）落成 `status="error"` 的 assistant 消息；终止前各超步的写入已在 checkpoint 中，不因本次失败丢失（AC6）。

### D-6 内容路径失败标记

- 判定函数 `is_error_content(content)`：`content` 为字符串且 `content.lstrip().startswith("[ERROR]")`；结构化/多模态结果不参与匹配（AC8/AC9）。
- 节点层：`super()` 返回后命中则就地 `result.status = "error"`，ToolMessage 内容保持不变。就地修改沿用 `_execute_tool_async` 里 `response.content = ...` 的既有做法。
- 回调层：`on_tool_end` 命中则照常写 `gen_ai.tool.call.result`，随后置 `error.type = "ToolResultError"` 并以 ERROR 结束，不再置 OK。
- 两处判定共用 `aidev_agent/utils/tool_result.py` 中的同一函数，避免口径漂移；该模块只放常量与一个纯函数，回调包按既有方式从 `aidev_agent.utils.*` 引用。

### D-7 配置

`config.py` 新增两项，import 期解析并在越界时报错，风格对齐 `BKAI_TOOL_LAZY_LOADING_*`（`config.py:189-192`）：

| 变量 | 含义 | 默认 | 约束 |
| --- | --- | --- | --- |
| `BKAI_TOOL_LOOP_REPEAT_LIMIT` | 允许真实执行次数 `T` | 3 | ≥ 1 |
| `BKAI_TOOL_LOOP_FEEDBACK_ROUNDS` | 拒绝反馈轮次 `F` | 2 | ≥ 0 |

只从部署环境读取，不接受平台下发、请求参数或智能体配置覆盖；不提供独立功能开关。

### D-8 与其他机制的关系

- 被拒绝的调用不经过任何 wrapper：不产生工具副作用、不进入审批、不计时、不做结果限长；拒绝反馈由守卫自己生成。
- 不影响 `knowledge_retrieval` 同批去重：去重作用于单批次，守卫作用于跨轮次，二者不交叉。
- 放行路径不改变 wrapper 链顺序、ToolMessage 形态与 span 属性（AC13）。

## 接口与数据

新增与修改：

| 文件 | 变更 |
| --- | --- |
| `core/nodes/tool/loop_guard.py` | 新增：策略、判定、异常、`LoopGuardToolNode` |
| `core/nodes/tool/deduplication.py` | `KnowledgeToolNode` 基类改为 `LoopGuardToolNode` |
| `core/nodes/tool/node.py` | `build_tool_node` 传入策略（阈值来自 `config`，摘要上限来自 `node_options.result_limit_thrd`） |
| `config.py` | 新增两个环境变量与启动期校验 |
| `packages/opentelemetry/callback_handler.py` | `on_tool_end` 增加内容路径失败判定 |
| `utils/tool_result.py` | 新增：`ERROR_RESULT_MARKER` 与 `is_error_content` |

对外可观测契约：

| 观测点 | 取值 |
| --- | --- |
| 拒绝调用 `tool.execution` | `status=ERROR`、`error.type=ToolLoopRejection`、带 `tool.name` / `tool.input`、无 `gen_ai.tool.call.result` |
| 内容路径失败 `tool.execution` | `status=ERROR`、`error.type=ToolResultError`、保留 `gen_ai.tool.call.result` |
| 正常调用 | 与变更前逐项一致（OK、无 `error.type`） |
| 拒绝反馈 ToolMessage | `status="error"`、`tool_call_id` 保留、正文含工具名/次数/结论/摘要/引导 |
| 终止 | run 失败，失败原因含「任务因工具循环调用被中断」与工具名 |

## 跨仓契约

业务仓只有本仓，无跨仓接口与执行顺序。平台侧沿用既有 `RunErrorEvent` 链路展示失败原因，无需改动；监控侧沿用 `tool.execution` + `error.type`，仅新增两个 `error.type` 取值，无新增属性、无需改现网统计 SQL。

## 风险

- R-1 依赖 langgraph 私有方法 `_run_one` / `_arun_one`。`requirements.txt` 锁定 1.0.7，但 `src/agent/pyproject.toml` 只写了 `langgraph>=1.0.0`，升级后若签名变化需回归；测试会锁定拒绝、反馈、终止与 span 四项行为。
- R-2 拒绝 span 为手工派发，参数与 `BaseTool.run` 存在漂移风险；用「拒绝 span 与真实 span 的 `tool.name` / `tool.input` 完全相等」的用例兜住。
- R-3 终止会丢弃当前超步（第 `T+F+1` 次调用不产生 ToolMessage），已完成的超步不受影响；这是规格允许的语义。
- R-4 计数窗口依赖最后一条 `HumanMessage`。无用户消息的调度（如子智能体直接驱动工具节点）会退化为全量历史，多轮场景下可能把历史相同调用计入。
- R-5 同一批次内的同键调用不互斥：两个并行调用看到同一快照，可能同时被拒。
- R-6 交替重复与近似重复不拦截，长循环仍可能出现。
- R-7 关闭 trace 时不会产生拒绝 span，REQ-5 的观测能力不成立，但拒绝、计数与终止不受影响。
- R-8 `[ERROR]` 前缀判定会把确实以该字样开头的正常文本误判为失败；规格明确接受，只排除正文中部与尾部。

## 测试策略

全部用 fake 工具 + `StateGraph(tools → END)` 直接驱动工具节点，不依赖真实模型、MCP 服务与监控平台；trace 用 `InMemorySpanExporter` 断言 span 属性。

| 用例组 | 覆盖 |
| --- | --- |
| 指纹与计数 | 键序/紧凑/类型区分/`null` 与缺失区分；连续 3 次放行、第 4/5 次拒绝、第 6 次终止；穿插其他工具或参数归零；同批次折叠；跨用户消息不累计（AC1、AC5、AC10、AC11） |
| 拒绝反馈 | 五个要素齐全、摘要截断标注、ToolMessage `status="error"`、不写 `gen_ai.tool.call.result`（AC2、AC4） |
| Trace 表达 | 拒绝 span ERROR + `error.type=ToolLoopRejection` + `tool.input` 与真实调用一致；成功 span 仍 OK 且无 `error.type`（AC3） |
| 终止 | 抛异常类型、消息关键字、run 失败、已有消息保留（AC6） |
| 内容路径 | 前缀命中判失败、中部/尾部命中判成功、ToolMessage 状态与 span 同步（AC8、AC9） |
| 配置 | 非法值启动期报错并带变量名；`T=5` / `F=0` 行为（AC11、AC12） |
| 零影响 | 普通调用 ToolMessage、span 名与属性逐项与变更前一致；判定内部异常时放行（AC13、AC14） |

验证命令：

```bash
cd src/agent && make test path=tests/core/nodes/tool/test_loop_guard.py
cd src/agent && make test path=tests/packages/opentelemetry/test_tool_result_status.py
cd src/agent && make test path=tests/core/nodes/test_tool.py
cd src/agent && make test path=tests/packages/opentelemetry
make lint
```
