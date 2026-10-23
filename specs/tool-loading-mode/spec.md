# 工具加载模式（mode）需求规格

- 变更名: tool-loading-mode
- 工作分支: feature/tool-loading-mode
- 类型: 功能
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 无

## 范围

- 在 Agent 执行入口参数 `ExecuteKwargs` 中新增 `mode`，取值 `default`、`dontInterrupt`、`dontAsk`，缺省 `default`。
- 按 `mode` 在工具装配阶段控制进入 Agent 的工具类型：`default` 全量注入；`dontInterrupt` 剔除所有会中断执行流程的工具；`dontAsk` 只剔除 ask 类提问工具。
- 覆盖三类可中断来源：内置 ask 类工具（当前为 `ask_user_question`）、带工具级审批配置的工具、runtime `execute` 工具内部的命令级审批。
- A2A 子 Agent 调用继承父 Agent 的 `mode`。
- 内置插件模板 `template/builtin/{{cookiecutter.project_name}}/bk_plugin/versions/assistant.py` 的 `InputsForm` 增加 `mode` 选项并透传到 `execute_kwargs.mode`。

## 非目标

- 不新增第四种模式（例如「仅跳过审批」）。
- 不改变 `default` 下的工具集合、审批链路、事件协议与会话数据结构；Web、企微等现有调用方不传 `mode` 即行为不变。
- 不修改前端仓库与企微渠道代码，不在 UI 上新增 `mode` 展示或控制。
- 不为外部 MCP 工具、第三方 `StructuredTool` 推断「是否会中断」，也不按名称猜测除内置 ask 类之外的提问工具。
- 不新增 `mode` 相关的流式事件或链路追踪属性。
- 不改变 `FLOW` 类型 Agent 的行为（其执行路径不装配工具）。
- 不涉及命令黑名单、命令允许列表、命令语法规则等既有防护档位的策略变更。

## 跨仓边界

- 本任务只有 `bk-aidev-agent` 一个业务仓，无跨仓接口、无跨仓依赖、无跨仓执行顺序。
- `mode` 作为 `execute_kwargs` 的普通字段对外暴露，与既有 `stream`、`tool_allow`、`tool_deny` 等字段同层；不新增独立 HTTP 接口或路由。
- 对下游调用方（Web、企微、APIGW、内置插件）是新增可选字段，未传即 `default`，不构成破坏性接口变更。

## 用户故事

- 作为无人值守的 API 调用方，我希望声明「不要加载会中断的工具」，这样 Agent 不会停下来等用户回答或等审批，自动化任务不会挂起。
- 作为异步 API 调用方，我希望只跳过用户提问、保留审批，这样涉及人工审批的操作仍能正常发起并等待结果。
- 作为 Web 与企微渠道的使用方，我希望默认行为完全不变，这样升级 SDK 不需要修改任何现有请求。

## 需求

### REQ-1 mode 参数定义

`ExecuteKwargs` 新增可选字段 `mode`，合法取值 `default`、`dontInterrupt`、`dontAsk`，缺省 `default`。该字段随 `execute_kwargs` 从 ChatCompletion 入参、APIGW 调用、内置插件等现有链路原样传入，不需要新增接口。

### REQ-2 default 全量加载

`mode` 为 `default`（含未传）时不做任何工具过滤：ask 类工具、带审批工具、命令级审批均按现有逻辑注入与生效。

### REQ-3 dontInterrupt 跳过所有中断来源

`mode` 为 `dontInterrupt` 时，装配出的工具集合中不得包含 ask 类工具，也不得包含带工具级审批配置的工具；同时 runtime `execute` 工具内的命令级审批不得发起，灰名单命令按拒绝处理。`execute` 工具本身照常注入，其余无中断能力的工具不受影响。

### REQ-4 dontAsk 仅跳过 ask 类工具

`mode` 为 `dontAsk` 时，装配出的工具集合中不得包含 ask 类工具；带工具级审批配置的工具仍正常注入，审批中断与审批通过后的执行链路与 `default` 一致；命令级审批（属审批链路）同样保留。

### REQ-5 可中断工具的判定口径

ask 类工具指内置的 ask 类提问工具（当前为 `ask_user_question`）；带审批工具指工具 metadata 中存在生效的审批配置、且会走现有 ITSM 工具审批链路的工具。判定口径与现有工具级审批识别保持一致，不新增工具名单配置项。

### REQ-6 A2A 子 Agent 继承 mode

父 Agent 以非 `default` 的 `mode` 执行并调用 A2A 子 Agent 时，子 Agent 使用与父 Agent 相同的 `mode`；子 Agent 不得回落到 `default`，也不得擅自升级为更严格的模式。

### REQ-7 与 tool_allow / tool_deny 叠加

同时传入 `mode` 与 `tool_allow` / `tool_deny` 时，最终工具集合必须同时满足三者约束（取交集语义）；三者互不覆盖，任一条件不满足的工具都不得注入。

### REQ-8 内置插件模板透传

内置插件模板的 `InputsForm` 增加 `mode` 选项（`default`、`dontInterrupt`、`dontAsk`），执行时把所选值写入提交给主站的 `execute_kwargs.mode`；未选择时不写入该字段，等价于 `default`。

### REQ-9 非法取值被拒绝

`mode` 取合法集合以外的值时，请求在参数校验阶段被拒绝，Agent 不执行、不装配工具、不产生会话消息。

### REQ-10 生效范围限定在工具装配

`mode` 只影响进入 Agent 的工具集合与命令级审批的处置，不改变会话存储结构、不改变既有流式事件类型与顺序、不改变审批单的字段结构。

## 验收

- AC-1：当请求未携带 `mode` 时，系统必须装配与变更前完全一致的工具集合（含 `ask_user_question` 与带审批工具）。
- AC-2：当请求携带 `mode=default` 时，系统必须装配与未携带 `mode` 时相同的工具集合，且事件序列与变更前一致。
- AC-3：当请求携带 `mode=dontInterrupt` 时，系统必须不把 `ask_user_question` 注入工具集合。
- AC-4：当请求携带 `mode=dontInterrupt` 时，系统必须不把任何带生效审批配置的工具注入工具集合。
- AC-5：当请求携带 `mode=dontInterrupt`、`execute` 工具被注入且模型执行一条落在灰名单的命令时，系统必须直接拒绝该命令并把拒绝结果作为工具结果返回模型，不得暂停执行等待审批，也不得创建 ITSM 审批单。
- AC-6：当请求携带 `mode=dontInterrupt` 时，系统必须仍注入 `execute` 工具与其余无中断能力的工具，LLM 可见的工具清单只缺少被剔除的中断类工具。
- AC-7：当请求携带 `mode=dontAsk` 时，系统必须不把 `ask_user_question` 注入工具集合，且带审批工具仍在工具集合中。
- AC-8：当请求携带 `mode=dontAsk` 且模型调用带审批工具时，系统必须发起审批中断并在审批通过后继续执行，审批链路与 `default` 一致。
- AC-9：当请求携带 `mode=dontAsk` 且模型执行一条落在灰名单的命令时，系统必须按现有命令级审批流程处理（不因 `dontAsk` 而关闭审批）。
- AC-10：当请求携带合法集合以外的 `mode` 值时，系统必须返回参数校验错误，且不产生任何会话消息或工具装配结果。
- AC-11：当父 Agent 以 `mode=dontInterrupt` 或 `mode=dontAsk` 调用 A2A 子 Agent 时，子 Agent 必须以相同 `mode` 装配工具。
- AC-12：当同时传入 `tool_allow` 或 `tool_deny` 与 `mode` 时，系统必须只注入同时满足白名单、黑名单与 `mode` 约束的工具。
- AC-13：当内置插件模板执行且 `mode` 选择为 `dontInterrupt` 时，提交给主站的 `execute_kwargs` 必须包含 `mode=dontInterrupt`；未选择时该字段必须不出现。

## 边界与非功能

- 兼容性：`mode` 是新增可选字段，历史请求、历史会话续流、重生成（regenerate）路径均按 `default` 处理，行为不变。
- 流式与非流式一致：同一 `mode` 下，`stream=true`、`stream=false`、resume 续流三条路径装配出的工具集合必须一致。
- 无全局状态：`mode` 是请求级参数，不写会话配置、不落库、不影响同一 Agent 的其它并发请求。
- 安全取向：`dontInterrupt` 下命令级审批关闭必须 fail-closed，即灰名单命令被拒绝而不是被放行。
- 可观测性：不新增事件与追踪属性；生效的 `mode` 允许出现在服务端日志中用于排障。
- 可维护性：可中断工具的判定复用现有工具级审批识别与 ask 类工具识别，不新增需要人工维护的工具名单。

## 开放问题与假设

- 假设：非法 `mode` 的拒绝方式为请求参数校验失败并返回 4xx，而非静默回落 `default`；如期望回落，需要在本阶段结束前提出。
- 假设：`dontInterrupt` 下命令级审批关闭后的命令拒绝文案沿用现有命令拒绝语义，不新增专用错误码。
- 假设：外部 MCP 工具内部是否存在人工等待无法在装配阶段判定，故不在过滤范围；本期不承诺覆盖。
- 假设：内置插件模板只暴露 `mode` 选项，不在模板里固定填写某个模式，具体值由调用方选择。
