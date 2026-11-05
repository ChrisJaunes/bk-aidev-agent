# 工具的循环调用拒绝 任务清单

- 变更名: tool-loop-rejection
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/tool-loop-rejection/spec.md
- 方案: specs/tool-loop-rejection/plan.md

## T-1 新增阈值与反馈轮次配置项

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/config.py`
- 需求: REQ-6
- 依赖: 无
- 验证: `cd src/agent && python -c "from aidev_agent import config; print(config.BKAI_TOOL_LOOP_REPEAT_LIMIT, config.BKAI_TOOL_LOOP_FEEDBACK_ROUNDS)"`；分别以 `BKAI_TOOL_LOOP_REPEAT_LIMIT=0`、`BKAI_TOOL_LOOP_FEEDBACK_ROUNDS=-1` 启动应报错并指出变量名
- [ ] 完成标准：两项环境变量默认 3 / 2，非整数、`T<1`、`F<0` 均在 import 期抛错并带变量名；不新增功能开关

## T-2 判定键、连续计数与错误内容前缀的纯函数

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/nodes/tool/loop_guard.py`、`src/agent/aidev_agent/utils/tool_result.py`
- 需求: REQ-1, REQ-4, REQ-7
- 依赖: 无
- 验证: `cd src/agent && make test path=tests/core/nodes/tool/test_loop_guard.py -k fingerprint`
- [ ] 完成标准：`fingerprint(args)` 对键序、空白不敏感，区分 `null` 与缺失及 `1` / `"1"`，不可序列化时返回 `None`；`count_contiguous(messages, call)` 按「最后一条 HumanMessage 之后 + 同批次同键折叠 + 遇不同键归零」返回 `n`；`is_error_content` 只对字符串且去前导空白后以 `[ERROR]` 开头返回真

## T-3 守卫节点：放行、拒绝与终止

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/nodes/tool/loop_guard.py`
- 需求: REQ-2, REQ-3, REQ-7
- 依赖: 阻塞于 T-2
- 验证: `cd src/agent && make test path=tests/core/nodes/tool/test_loop_guard.py -k "reject or terminate"`
- [ ] 完成标准：`LoopGuardToolNode._run_one` / `_arun_one` 在 `n<T` 时原样调用 `super()`；`T<=n<T+F` 返回 `status="error"` 的 ToolMessage，正文含工具名、次数、拒绝结论、末次真实返回摘要、引导，且不执行工具本体；`n>=T+F` 抛 `ToolLoopTerminationError`，消息含「任务因工具循环调用被中断」、工具名与调用/拒绝次数；判定过程抛异常时按放行处理，不产生新增异常

## T-4 拒绝调用的工具 span 派发

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/nodes/tool/loop_guard.py`
- 需求: REQ-5
- 依赖: 阻塞于 T-3
- 验证: `cd src/agent && make test path=tests/packages/opentelemetry/test_tool_result_status.py -k rejection`
- [ ] 完成标准：拒绝时派发一条 `tool.execution` span 并以 ERROR 结束、`error.type=ToolLoopRejection`、携带与真实调用同格式的 `tool.name` / `tool.input`、不写 `gen_ai.tool.call.result`、父 span 为 `tools` 节点；`callbacks` 缺失时跳过派发且拒绝结果不受影响；同步与异步路径行为一致

## T-5 内容路径失败标记

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/nodes/tool/loop_guard.py`、`src/agent/aidev_agent/packages/opentelemetry/callback_handler.py`、`src/agent/aidev_agent/utils/tool_result.py`
- 需求: REQ-4
- 依赖: 阻塞于 T-2
- 验证: `cd src/agent && make test path=tests/packages/opentelemetry/test_tool_result_status.py -k error_prefix`
- [ ] 完成标准：工具正常返回且正文（去前导空白）以 `[ERROR]` 开头时，ToolMessage `status="error"`、span 以 ERROR 结束且 `error.type=ToolResultError`、`gen_ai.tool.call.result` 仍写入；正文中部或尾部含该字样仍为成功；非字符串结果不参与判定

## T-6 接入装配链

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/nodes/tool/node.py`、`src/agent/aidev_agent/core/nodes/tool/deduplication.py`
- 需求: REQ-1, REQ-2, REQ-3, REQ-5, REQ-6
- 依赖: 阻塞于 T-3、T-4
- 验证: `cd src/agent && make test path=tests/core/nodes/test_tool.py`
- [ ] 完成标准：`KnowledgeToolNode` 继承 `LoopGuardToolNode`；`build_tool_node` 装配 wrapper 链顺序与返回值形态不变，仅把环境变量阈值与 `node_options.result_limit_thrd` 传入守卫；既有 `tests/core/nodes/test_tool.py` 全绿

## T-7 单测：判定、拒绝、终止、配置

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/nodes/tool/test_loop_guard.py`
- 需求: REQ-1, REQ-2, REQ-3, REQ-6, REQ-7
- 依赖: 阻塞于 T-6
- 验证: `cd src/agent && make test path=tests/core/nodes/tool/test_loop_guard.py`
- [ ] 完成标准：覆盖 AC1、AC2、AC4、AC5、AC6、AC10、AC11、AC12、AC14；用 fake 工具 + `StateGraph(tools → END)` 驱动，不接真实模型与 MCP；单测函数不超过 30 行，优先参数化

## T-8 单测：Trace 表达与零影响

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/packages/opentelemetry/test_tool_result_status.py`
- 需求: REQ-4, REQ-5, REQ-7
- 依赖: 阻塞于 T-6
- 验证: `cd src/agent && make test path=tests/packages/opentelemetry/test_tool_result_status.py`
- [ ] 完成标准：用 `InMemorySpanExporter` 断言 AC3、AC8、AC9、AC13；拒绝 span 与真实 span 的 `tool.name` / `tool.input` 完全相等；普通调用的 span 名、属性集合与状态与变更前逐项一致

## T-9 回归与静态检查

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: 无（仅执行）
- 需求: REQ-7
- 依赖: 阻塞于 T-7、T-8
- 验证: `cd src/agent && make test path=tests/core/nodes`；`cd src/agent && make test path=tests/packages/opentelemetry`；`make lint`
- [ ] 完成标准：工具节点与可观测性两个目录的测试全绿，`make lint` 无新增告警，diff 只包含方案列出的文件
