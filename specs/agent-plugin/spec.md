# Agent Core 插件机制：hook 协议与注入

- 变更名: agent-plugin
- 工作分支: feature/agent-plugin
- 类型: 功能
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 无

## 范围

为 Agent 运行时引入 hook 协议与注入机制：定义 5 个点位（`session_start`、`tool_pre`、`tool_post`、`model_pre`、`model_post`）的触发时机、输入、可裁决取值与执行语义，并让 Agent 实例在构建期接受外部注入的处理器集合，在上述点位按协议执行。

本期覆盖的行为边界：

1. 协议：点位枚举、处理器签名（同步与 async）、上下文对象、裁决对象、合并与短路规则、非法返回值的兜底。
2. 触点：5 个点位各自的触发次数与相对顺序，含与既有工具 wrapper 链、模型 recovery 循环、ITSM 审批、结果脱敏与净化、去重的先后关系。
3. 注入：经 Agent 实例工厂（`AgentInstanceFactory.build_agent`）传入；按实例隔离；未注入时零回归。
4. 失败语义：fail-open（异常或超时按最宽松裁决继续）、结构化日志、单处理器超时上限。

不包含插件的发现与加载，见「非目标」。

## 非目标

- 不做插件发现与加载：不扫描工作区目录、不读插件清单或 manifest、不支持 entry point 与 dotted-path 自动导入、不做热加载与卸载。
- 不兼容「Workspace 中以本地文件描述的插件、工具或配置」；本期只做 hook 的注入点与协议设计，该兼容由后续变更承担。
- 不做宿主侧 hook：不实现 headless NDJSON 上报、不做 dsh 桥接、不修改 `feature/agent-hook` 的任何文件。
- 不落地本次未选中的点位：`prompt_assemble`、`turn_stop`、`permission_request`、`subagent_start/stop`、`pre_compact` 均不在本期（后续可按同一协议增量接入）。
- 不改既有工具链判定：不改 ITSM 审批、结果脱敏、不可信净化、结果截断、去重、计时、代理解包的逻辑与输出。
- 不新增平台侧配置项、数据库字段、AG-UI 事件类型与前端展示。
- 不做 hook 的权限与信任模型（谁能注入、可注入哪些点位），由调用方负责。
- 不定义跨进程或可序列化的 hook 协议；处理器只要求是进程内 Python 可调用对象。

## 跨仓边界

- 业务仓只有 `bk-aidev-agent` 一个，无跨仓接口、无跨仓依赖、无跨仓执行顺序。
- 宿主与平台后端不参与：处理器由调用方在进程内提供，本仓不新增对外接口或配置下发通道。
- 与未合入的 `feature/agent-hook` 无文件级依赖：本变更基线取 `develop`，不改该分支的 `specs/agent-hook/` 与相关实现；两侧点位名保持一致，但执行方、配置来源与传输方式各自独立。

## 用户故事

- 作为 Agent 调用方（平台后端、宿主或测试），我希望在构建 Agent 实例时注入一组 hook 处理器，从而在不改仓内代码的前提下观察与干预一次运行的模型与工具行为。
- 作为扩展开发者，我希望点位语义、裁决取值与失败语义有稳定契约，一次写好的处理器在不同调用方下行为一致。
- 作为平台维护者，我希望未注入 hook 时行为与现状逐项一致，从而可以先接协议、再接具体插件。

## 现状（已证实）

| 事实 | 证据 |
| --- | --- |
| 仓内没有 hook 实现：`develop` 上不存在 `core/hooks/` 目录，无任何 hook 注册或执行代码 | `git ls-files src/agent/aidev_agent/core` 无 hooks 项 |
| Agent 实例构建入口为 `AgentInstanceFactory.build_agent(...)`，已用 `**extra` 承接可变参数 | `services/agent/factory.py:121` |
| 图装配唯一入口为 `ReActAgentBuilder`，已支持注入 LangChain `AgentMiddleware` | `core/graphs/react/graph.py:178`、`:613` |
| `AgentMiddleware`（langchain 1.2.0）提供 6 个钩子：`before_agent`、`after_agent`、`before_model`、`after_model`、`wrap_model_call`、`wrap_tool_call` | 本机 `langchain 1.2.0` 的 `langchain.agents.middleware.AgentMiddleware` |
| 其中只有 `wrap_tool_call` / `awrap_tool_call` 被下发给工具节点 | `core/graphs/react/graph.py:1159`、`:1168` |
| 工具 wrapper 链按「列表首个为最外层」组合，请求流外→内、响应流内→外 | `core/nodes/tool/node.py:157` |
| 内置 wrapper 外→内顺序：结果事件派发 → 循环守卫（可选） → 代理解包（可选） → 不可信净化 → 结果脱敏 → ITSM 审批 → JSON 修复 → 计时 → 结果截断 → 自定义（含 middleware 的 `wrap_tool_call`） → 去重 → 原始错误内容识别 | `core/nodes/tool/node.py:306-381` |
| 模型节点自带 recovery 循环（空内容、纯思考、截断等异常的重试） | `core/nodes/model/node.py:86` |
| 运行入口为 `run()`，会话与事件由 `ChatCompletionAgent.execute()` 驱动 | `core/ag_ui/aidev_agent.py:163`、`services/agent/chat.py:528` |
| 提示词、工具、变量三条装配管道 | `core/nodes/model/context_assembly.py:29`、`:102` |
| Python 版本约束 3.11～3.12 | `src/agent/pyproject.toml:6` |

## 需求

### REQ-1 点位与处理器契约

- 点位集合固定为 5 个：`session_start`、`model_pre`、`model_post`、`tool_pre`、`tool_post`；名称与拼写不得变更。
- 处理器为进程内可调用对象；同步函数与 async 函数都必须支持，两条执行路径的裁决语义一致。
- 处理器入参必须包含：点位名、点位专属上下文（见 REQ-2 至 REQ-4）、只读标识（agent_code、session_code、run 标识、turn 序号）。
- 裁决取值固定为 5 种：`pass`（无干预，返回 `None` 等价于 `pass`）、`deny`（附非空原因字符串）、`ask`（仅 `tool_pre` 允许）、`replace`（附替换载荷）、`append`（附追加内容）。
- 同一实例内同一点位的处理器按注入顺序执行；协议不得要求处理器之间共享可变状态。
- 某点位未注册任何处理器时，该点位不得产生除日志以外的任何可观测变化。

### REQ-2 `session_start`

- 触发时机：一次运行在会话装载完成之后、该运行第一次模型请求之前；每次运行恰好触发一次。
- 上下文字段：agent_code、session_code、输入消息条数、最后一条用户输入、可用工具名列表。
- 可裁决：`pass`、`deny`、`append`。`append` 的内容必须出现在该次运行首轮的模型输入中，作为独立片段加入，不改写既有消息内容。
- `deny` 语义：本次运行不发起任何模型请求、不执行任何工具，以可读错误结束；会话与落库行为沿用既有失败路径。

### REQ-3 `model_pre` 与 `model_post`

- `model_pre` 触发时机：每次向模型发起请求之前，一次请求一次。模型节点的 recovery 重试每次重试都算一次新请求，均须触发。上下文包含待发送消息列表、模型名、工具名列表、turn 序号。
- `model_pre` 可裁决：`pass`、`replace`（替换消息列表，替换结果即实际发送内容）、`deny`（终止本次运行，不发出该请求，返回可读错误）。
- `model_post` 触发时机：每次模型响应返回之后、recovery 判定之前，一次响应一次。上下文包含原始响应（文本、工具调用、usage）与 turn 序号。
- `model_post` 可裁决：`pass`、`replace`（替换文本与工具调用的任意组合）、`deny`（终止本次运行）；替换后的内容必须参与后续 recovery 判定与路由，并成为该轮实际输出。
- 顺序约束：同一次请求内必须是 `model_pre` → 模型请求 → `model_post`；同一轮内多次请求之间不得交错。

### REQ-4 `tool_pre` 与 `tool_post`

- `tool_pre` 触发时机：模型产出的每个工具调用在真正执行之前，一次调用一次；并行多个调用时按模型给出的顺序逐个触发。上下文包含真实工具名、tool_call_id、参数、turn 序号。
- `tool_pre` 可裁决：`pass`、`deny`（工具不执行，模型收到含拒绝原因的失败结果）、`ask`（走既有中断机制请求人工裁决，未获批准不得执行）、`replace`（改写参数后按改写结果执行）。
- `tool_post` 触发时机：工具执行返回后（工具抛错时同样触发，上下文 `is_error` 为 true）、结果进入模型上下文之前，一次调用一次。上下文包含真实工具名、tool_call_id、原始结果内容、`is_error`、耗时。
- `tool_post` 可裁决：`pass`、`replace`（替换结果内容）、`append`（不改原结果，追加说明）、`deny`（结果替换为含原因的拒绝说明；工具已执行的事实不回滚）。
- 位置硬约束：
  - `tool_pre` 必须晚于工具代理解包（处理器看到真实工具名，而非代理名），且早于 ITSM 审批与工具执行。
  - 循环守卫按模型给出的原始参数判定：`tool_pre` 对参数的改写不得改变循环守卫的判定输入，避免 hook 掩盖模型自身的重复调用。
  - `tool_post` 必须位于既有 wrapper 链最内层（`deduplicate` 与 `_content_error` 之内）：处理器看到未脱敏的原始结果，其输出继续流经 `_content_error`（按替换后的内容重新识别错误）、去重、结果截断、脱敏与不可信净化，并按最终结果派发工具结果事件。

### REQ-5 注入与隔离

- Agent 实例工厂必须接受 hook 注入：注入物为一组处理器集合，随构建出的实例生效，同一实例的所有点位共用该集合。
- 注入按实例隔离：并发构建的两个实例注入不同集合时互不串扰；不得用模块级可变全局持有实例级 hook。
- 未注入、注入空集合、或注入集合中缺少某点位处理器时，系统行为与基线完全一致（见 REQ-7）。
- 注入入口必须与既有 `build_agent` 参数风格一致（显式关键字参数或既有 `**extra` 通道其一），调用方不需要修改图装配代码。

### REQ-6 执行语义

- 合并规则：同一点位多个处理器按注入顺序执行；任一处理器返回 `deny` 或 `ask` 时短路，后续处理器不再执行；否则后续 `replace` / `append` 覆盖先前的写操作，最后一次写生效。
- fail-open：处理器抛异常，或单次执行超过 5 秒时，该处理器按 `pass` 处理，记 warning 日志，同点位其余处理器与业务链路继续。
- 非法返回：处理器返回未知裁决取值或缺少必需载荷时，按 `pass` 处理并记 warning，不得让非法返回值进入业务链路。
- 可观测：每次处理器执行记一条结构化日志，含点位、处理器标识、耗时、裁决；不得新增 AG-UI 事件类型。

### REQ-7 零回归

- 未注入 hook 时：模型请求序列、工具执行序列、工具 wrapper 顺序与判定、最终输出、AG-UI 事件序列、落库内容必须与变更前逐项一致。
- 未注入 hook 时不得新增模型调用、工具调用、属性写入与事件；新增判断只允许 `O(1)` 的空集合检查。

## 验收

- AC1 当一次运行开始时，`session_start` 必须在首轮 `model_pre` 之前触发且恰好一次；其 `append` 的内容必须出现在该轮实际发送给模型的消息中。
- AC2 当 `session_start` 处理器返回 `deny` 时，系统必须不发起任何模型请求、不执行任何工具，并以可读错误结束本次运行。
- AC3 当模型节点即将发起请求时，`model_pre` 必须在请求发出前触发；其 `replace` 后的消息必须与实际发送给模型的消息逐项相同。
- AC4 当模型响应返回时，`model_post` 必须在 recovery 判定之前触发；其 `replace` 的文本与工具调用必须成为该轮实际输出，并参与后续路由。
- AC5 当模型节点发生 recovery 重试时，每次重试必须各自触发一次 `model_pre`，触发次数等于请求次数。
- AC6 当模型产出工具调用时，`tool_pre` 必须在工具执行前触发；其 `deny` 必须导致工具不被执行，且模型收到包含拒绝原因的失败结果。
- AC7 当 `tool_pre` 返回 `ask` 时，系统必须走既有中断机制请求人工裁决，未获批准时该工具不得执行。
- AC8 当工具返回结果或抛出异常时，`tool_post` 必须触发一次且 `is_error` 与实际一致；其 `replace` 的内容必须成为模型看到的结果，并继续经过既有结果截断、脱敏与不可信净化。
- AC9 当 `tool_pre` 与 `tool_post` 触发时，处理器收到的工具名必须是模型请求的真实工具名而非代理名，`tool_post` 收到的结果必须是未脱敏原文。
- AC10 当未注入任何 hook 时，同一输入下模型请求序列、工具执行序列、最终输出与 AG-UI 事件序列必须与基线逐项一致。
- AC11 当某处理器抛异常或执行超过 5 秒、且同点位还有其他处理器时，系统必须跳过该处理器、记 warning，并继续执行其余处理器与业务。
- AC12 当同一点位多个处理器给出不同裁决时，系统必须按注入顺序执行并在首个 `deny` 或 `ask` 处短路，最终裁决为最严格者。
- AC13 当同进程内两个 Agent 实例注入不同集合并发运行时，各自必须只执行自己的处理器，日志中不得出现对方的处理器标识。
- AC14 当处理器返回未知裁决取值或缺少必需载荷时，系统必须按 `pass` 处理并记 warning，业务结果不受影响。

## 边界与非功能

- **性能**：未注入 hook 时仅一次空集合判断；注入后额外开销只来自处理器自身执行时间，不引入网络调用，不新增模型或工具调用。
- **安全**：`tool_pre` 能看到工具参数，`tool_post` 能看到未脱敏原始结果，hook 属可信内部扩展点；本期不为其增设权限校验，注入方须自行保证不外发敏感内容。该风险须在实现与评审中被显式接受。
- **并发**：hook 集合按实例隔离；同一实例内的处理器不得假设存在超出串行调用的额外一致性保护。
- **兼容**：点位名与 `feature/agent-hook`（宿主侧）一致以便后续统一词汇，但两侧执行方、配置来源与传输方式不同，本期不做互通。
- **可测性**：全部 AC 必须能在本仓测试内用 fake 模型、fake 工具与 fake 处理器完成，不依赖真实模型、外部平台或宿主进程。

## 开放问题与假设

已确认决策（本轮澄清）：

1. 与 `feature/agent-hook` 为独立变更，基线 `develop`，工作分支 `feature/agent-plugin`。
2. 本期点位取 5 个：`session_start`、`tool_pre`、`tool_post`、`model_pre`、`model_post`。
3. 本期只做 hook 方案，并允许 Agent 实例工厂接受 hook 注入；Workspace 本地文件描述的插件、工具与配置的兼容不在本期范围。

默认假设（如需调整请在审批意见中指出）：

- 处理器以进程内 Python 可调用对象提供，不定义跨进程协议与序列化格式。
- 合并规则取「按序执行、首个拒绝短路、最后写生效」；后续若需表决或全量执行，按新变更处理。
- 单处理器超时上限固定为 5 秒，不新增配置项。
- 规格落位 `specs/agent-plugin/spec.md`：`develop` 尚无 `specs/` 目录，本变更首次引入，与本项目既有 SDD 约定一致。
- 提交信息按项目惯例携带 `--story=1070093903138045802`。
