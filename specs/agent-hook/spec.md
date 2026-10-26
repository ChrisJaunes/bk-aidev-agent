# Agent Hook 系统：AgentFactory 注入点与「dsh 以 aidev-agent 为核心 Agent」接入

- 变更名: agent-hook
- 工作分支: feature/agent-hook
- 类型: 重构
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 无

## 范围

本变更把「Agent 实例从哪来、由谁恢复」从写死的类方法收敛为可替换的注入点，并为本仓补一个不依赖平台的最小运行面，使外部宿主（DeepSeek Harness，下称 dsh）能够**通过自己的插件机制把核心 Agent 换成 aidev-agent**。依赖方向是 dsh → 插件 → aidev-agent：本仓不引入 dsh 依赖，也不承载插件代码。

1. 新增 `AgentFactory` 注入点：`create`（新建实例）与 `resume`（恢复既有会话/中断）两个方法，进程级注册中心持有，支持替换默认实现（沿用仓内 `utils/factory.py` 的 `SingletonFactory` / `replace_defaults` 约定）。
2. 默认实现 = 现状能力：`create` 复用 `AgentInstanceFactory` 既有装配逻辑，`resume` 复用现状续流逻辑；对外行为与今日逐项一致。
3. 创建入口收敛：仓内 5 处 `AgentInstanceFactory.build_agent` 调用方保持签名与用法不变，改由注入点产出实例；仓内不再存在绕过注入点的实例构造路径。
4. 恢复入口收敛：前端续流与后台审批续流改走 `resume`，覆盖「同一会话续轮」与「审批 / `ask_user_question` 中断恢复」两类恢复。
5. 新增 headless 运行面（对外、可选启用）：以可执行模块形式提供跨进程调用入口，接受逐行 JSON 请求、产出逐行 JSON 事件，方法覆盖 `create` / `turn` / `resume` / `cancel` / `close`；事件沿用现有 AG-UI 事件对象；默认不写平台 DB，模型访问与事件写出、checkpointer 均可由调用方注入。它是「外部宿主以 aidev-agent 为核心」的唯一驱动面。
6. 接入文档：说明 dsh 侧如何以插件与组合配置把核心 Agent 换成 aidev-agent、支持面与不支持面边界、如何用本机 fake 模型完成验收；文档不复制 dsh 源码。
7. 验收：以仓外插件在真实 dsh 中跑通一轮对话（LLM 用本机 fake）。

说明：本变更对终端用户无功能变化；新增的只是面向宿主进程的调用面（headless 入口）与接入文档。

## 非目标

- 不在本仓提交 dsh 插件、不 fork 或改造 dsh 源码；插件与 dsh 组合配置属接入方产物，仓外维护。
- 本仓不引入 dsh 或其 SDK 依赖：主依赖与可选 extras 均不新增 dsh 相关包。
- 不做 dsh Agent 全表面适配：`inbox`（next-turn / next-step 多队列）、`steer` / `inject` / `followup` 的逐步语义、`runMaintenance`、compaction、子 Agent、多 Agent 因果链均不在本期。
- 不映射 dsh 的工具注册与审批体系：本期「跑通」以纯文本一轮为准；aidev-agent 内部工具管道保持不变但不在验收范围。
- 不做 dsh session log 与平台会话存储的双向同步；本期事实源归宿主，平台侧默认不持久化。
- 不改 HTTP 接口契约（入参、出参、字段名），不新增请求字段，不改 AG-UI 事件类型，不改平台 `agent_info` 配置键。
- 不做 FLOW / A2A 的改造；这两类继续走默认实现。
- 不做性能与成本优化，不做进程池、常驻复用与自动重启。
- 不改写现有 `AgentInstanceFactory` 的装配细节（模型、工具管道、checkpointer、审批链路）。

## 跨仓边界

- 本仓切片：注入点 + 注册中心 + 入口收敛 + headless 运行面 + 接入文档 + 等价性测试。
- dsh 仓：仅作外部宿主与只读参考，零改动。已核对的两条硬约束：`packages/core/agent-loop/src/index.ts:336` 在插件加载时无条件 `ctx.agents.setFactory(this)`；`packages/core/agent/src/index.ts:383-385` 的 `setFactory` 在已有 factory 时抛 `an agent factory is already registered`。因此接入必须在 dsh 的组合/profile 层停用 `@deepseek-ai/dsh-agent-loop` 行并启用仓外插件行，而不是叠加注册。
- 仓外产物：dsh 插件（Node/TS）与组合配置由接入方维护；本次仅作为验收证据放在节点工作区，不入本仓、不发布。
- 依赖：本仓零新增依赖；`ag-ui-protocol` 已在主依赖中，headless 入口的事件对象直接复用。
- 与兄弟变更（`tool-lazy-loading` 等）无接口耦合：本变更只改「实例来源、恢复入口与宿主驱动面」，不动 graph 装配、工具管道与模型链路。
- 无跨仓执行顺序要求。

## 用户故事

- 作为平台研发，我希望「创建什么 Agent、谁来恢复它」由一个可替换的实现入口决定，这样外部宿主可以在不改平台接口、不改前端的前提下接管 Agent 的创建与恢复。
- 作为宿主任（dsh 使用者），我希望把 dsh 的核心 Agent 换成 aidev-agent 时，不需要给 aidev-agent 加 dsh 依赖、也不需要把插件提交进 aidev-agent 仓，只要一个稳定的跨进程调用面。
- 作为验收方，我希望用本机 fake 模型就能判定「dsh 真的用上了 aidev-agent」，不依赖真实模型与真实 AK。

## 必须保持的外部行为与判定方式

| 外部行为 | 判定方式 |
| --- | --- |
| 5 处创建入口的入参、返回值类型与异常语义不变 | 基线测试全绿；调用方代码零改动（diff 只出现在工厂内部与其调用下一跳） |
| AG-UI 事件序列（start / message / tool / run 结束等）与现状一致 | 同一输入下 AG-UI 事件录制的类型序列与关键字段逐项比对 |
| 会话落库结果一致（assistant 消息、状态、turn 归属） | 平台会话内容与状态的接口快照比对 |
| 审批 / `ask_user_question` 中断与恢复链路一致 | 现有中断恢复测试全绿；中断后前端续流与后台续流均能续上 |
| 模型、工具、checkpointer、知识库、token 统计、span 上报链路不变 | 相关模块 diff 为空；相关测试全绿 |
| 未启用 headless 入口时进程行为不变 | 未设置任一开关时的应用启动冒烟与基线一致；不新增常驻进程与监听端口 |

## 需求

### REQ-1 注入点协议与注册中心

新增 `AgentFactory` 协议，必须包含两个方法：

- `create(...)`：入参语义与 `AgentInstanceFactory.build_agent` 现有参数一致（agent 身份、构建类型、会话码、构建上下文、事件处理器、资源管理器、checkpointer、额外参数），返回满足现有实例契约的对象（可 `execute(...)`、可 `stop()`、带 `event_handler`）。
- `resume(...)`：入参包含既有会话标识、恢复负载（审批结果 / `ask_user_question` 回答 / 续轮输入）与 `create` 相同的运行期依赖，返回同一实例契约。

协议必须可被进程级注册中心（单例）持有并支持替换默认实现；注册中心必须与仓内既有写法（`GenericFactory` / `SingletonFactory` / `replace_defaults`）一致，不引入新的注册范式。

### REQ-2 默认实现与现状等价

默认实现必须直接复用现状装配与续流逻辑：`create` 走 `AgentInstanceFactory` 既有 `_build_from_session` / `_build_direct` → `_make_build_context` → `agent_registry.must_get(...)` → `build(ctx)` 链路；`resume` 走现状 `Command(resume=...)` 续流链路。默认实现是注册中心的默认值，内部装配行为不得被本变更改写。

### REQ-3 创建入口单一化

`AgentInstanceFactory.build_agent` 保留为对外签名（现 5 处调用方零改动），内部改为委派注册中心的 `create`。仓内不得存在绕过注册中心直接构造 Agent 实例的业务代码路径；headless 入口同样必须经注册中心。

### REQ-4 恢复入口单一化

前端续流与后台审批续流必须改走注册中心的 `resume`，不得再以「重建实例 + 带恢复负载 `execute`」的隐式方式恢复。`resume` 必须覆盖两类恢复：同一会话续轮、审批 / `ask_user_question` 中断恢复。

### REQ-5 不支持能力必须显式失败

注入实现或 headless 入口对某类构建、恢复或请求不支持时，必须返回/抛出结构化错误（含错误码与原因）并产出 error 事件；禁止静默回落到默认实现，禁止返回空实现或空回复。

### REQ-6 headless 运行面的调用形态

必须提供一个可被非 Python 宿主以子进程方式驱动的入口（安装后以模块调用形式可执行），传输契约为逐行 JSON：一行请求 → 零到多行事件 → 一条响应。方法集必须覆盖：

- `create`：给定会话标识、Agent 身份、构建上下文与运行期依赖，返回可驱动的会话句柄；
- `turn`：给定一轮用户输入（以及可选的历史消息），驱动一轮并流式返回事件；
- `resume`：给定会话标识与恢复负载，继续既定会话或中断；
- `cancel`：取消当前一轮；
- `close`：释放会话与子进程资源。

入口的 stdout 必须只承载协议数据，日志必须走 stderr。

### REQ-7 headless 运行面的事件契约

事件必须复用现有 AG-UI 事件对象（`ag_ui.core.events`），不新增事件类型、不裁剪既有字段；每轮必须以终止类事件收尾（成功或错误），且终止事件必须能区分「正常结束」「用户取消」「内部错误」。宿主据此投影为自身会话事件，本仓不定义宿主的投影规则。

### REQ-8 headless 运行面的无平台依赖模式

入口必须能在无平台凭证、无 Django 应用、无平台数据库的环境中完成一轮：

- 不得导入 Django 或平台插件层；
- 默认不写平台 DB，不向平台上报 token / span；
- 模型访问配置（端点、模型名、凭据）必须可由调用方注入，指向本机 fake 端点时必须可用；
- 事件写出与 checkpointer 必须可由调用方注入（默认内存实现）。

### REQ-9 headless 运行面的会话语义

会话标识由调用方提供并在响应中原样回传；`turn` 必须支持调用方每轮携带历史消息的无状态模式；`resume` 必须支持同一会话标识续轮；不支持的中断恢复类型按 REQ-5 显式失败。入口不得自造与调用方无关的会话标识。

### REQ-10 生命周期与并发

同一会话内的 `turn` 必须串行；不同会话可并发。`cancel` 必须在有界时间内让当前轮以取消终止事件收尾。`close` 之后该会话不得再接受请求。进程在任一请求出现协议错误或内部异常后必须保持可用。

### REQ-11 依赖与隔离

本仓主依赖与可选 extras 均不得出现 dsh 相关包；headless 入口不得引入新的必需依赖；入口的日志与错误信息不得输出凭据、AK 或完整请求体。

### REQ-12 接入文档

必须在仓内文档中说明：宿主如何以组合/profile 配置停用其默认 Agent 实现并接入本仓 headless 入口；支持面（`create` / `turn` / `resume` / `cancel` / `close`）与不支持面（`inbox` 多队列、`steer` / `inject`、`runMaintenance`、compaction、子 Agent）；以及用本机 fake 模型完成验收的步骤。文档不得复制 dsh 源码。

## 验收

- ACC-1（REQ-2）当未启用 headless 运行面时，`AgentInstanceFactory.build_agent` 的入参处理、返回值类型与异常行为必须与基线逐项一致，现有测试必须全绿。
- ACC-2（REQ-1、REQ-3）当注册中心被替换为记录调用的实现后，`views/chat.py`、`services/agent_bkplugin.py`（2 处）、`services/agent_builder.py`、`aidev_wxbot/wxaibot/strategies.py` 共 5 处创建调用必须全部命中该实现，且调用方代码无改动。
- ACC-3（REQ-1、REQ-4）当审批中断后由后台工作线程续流时，系统必须调用 `resume` 而不得调用 `create`；默认实现下的续流输出与基线一致。
- ACC-4（REQ-5）当注入实现的 `resume` 对某恢复类型抛出既有 bkai 异常、或 headless 入口收到未支持方法时，系统必须返回结构化错误并写入 error 状态事件，且不得回落到默认实现。
- ACC-5（REQ-6）当以 headless 入口 `create` 一个会话并发出 `turn` 时，stdout 必须只出现协议数据，且依次收到事件行与一条响应；`close` 后同一会话的请求必须被拒绝。
- ACC-6（REQ-7）当完成一轮纯文本对话（本机 fake 模型）时，事件序列必须以终止事件收尾，其中包含 run 开始、assistant 文本与 run 结束类事件，且事件类型集合不超出基线 AG-UI 集合。
- ACC-7（REQ-8）当未注入平台 writer 时完成一轮，必须不产生平台 DB 写入、不上报 token / span（以 mock 断言零平台调用），且进程内未导入 Django。
- ACC-8（REQ-9）当同一会话标识再次发起 `turn` 时，会话标识必须原样回传；当携带历史消息发起一轮时，模型必须收到该历史（以 fake 模型收到的请求体断言）。
- ACC-9（REQ-10）当发出 `cancel` 时，必须在有界时间内收到取消类终止事件；当同一会话并发发起两个 `turn` 时，必须串行化而非交错。
- ACC-10（REQ-11）当检查依赖声明与运行日志时，本仓不得出现 dsh 相关包，日志中不得出现注入的凭据值。
- ACC-11（REQ-6、REQ-7、REQ-12）当按文档步骤接入一个仓外插件后，真实 dsh 必须能完成一轮「用户输入 → assistant 回复」，该轮由 aidev-agent 产出；且 dsh 侧日志能证明其默认 Agent 实现未参与。
- ACC-12（REQ-6）当停用仓外插件、以 dsh 默认组合启动时，dsh 行为不受影响（接入为可选）。
- ACC-13（REQ-10）当发送非法 JSON、未知方法与内部异常触发请求时，headless 进程必须返回结构化错误并继续服务后续请求。

## 边界与非功能

- 运行时：Python 3.11–3.12；验收在 Linux x64（manylinux）容器内完成。
- 兼容性：主依赖与 extras 不变；headless 运行面默认不启用，未启用时进程行为与基线一致（不新增常驻进程与端口）。
- 并发：同一 `session_code` 的并发语义保持现状（既有抢占机制）；headless 入口按会话串行，不新增跨会话抢占。
- 性能：默认路径不得引入可测量延迟（仅增加一次工厂查询与一次委派）；headless 入口不设性能目标。
- 安全：headless 入口不监听网络端口（走 stdio）；不新增对外网络依赖，fake 模型端点仅回环地址；日志不得输出 AK、密钥或完整请求体。
- 数据处理：本期会话事实源归宿主；平台侧默认不持久化，内存 checkpointer 随 `close` 释放。
- 可观测性：新增工厂实现标识与入口会话标识的日志字段；不新增 span 属性、不新增对外 HTTP 字段。
- 测试约束：等价性由现有测试保证；headless 与接入验收必须可离线运行（本机 fake 模型，不依赖外部网络与真实 AK）。

## 开放问题与假设

- 假设 1：跨进程契约采用逐行 JSON（NDJSON over stdio）。理由：宿主侧为 Node，stdio 是无额外依赖的最小通道；若下游改用其它传输，方法语义与事件契约不变，替换点限定在传输层。
- 假设 2：dsh 侧接入通过在组合/profile 层停用 `@deepseek-ai/dsh-agent-loop` 并启用仓外插件实现（依据：该行加载时无条件注册 factory，`setFactory` 重复注册即抛错）；不改 dsh 源码。
- 假设 3：本期事实源归宿主（dsh session log）；aidev-agent 默认不持久化，可注入。
- 假设 4：本期模型调用仍由 aidev-agent 现有模型链路完成，dsh 的模型配置与工具注册不参与；因此 fake 模型在 aidev-agent 侧注入。
- 假设 5：一期不映射 dsh 的工具与审批体系，纯文本一轮为「跑通」口径。
- 假设 6：`specs/<变更名>/` 沿用同仓兄弟变更（`tool-lazy-loading`）的 SDD 布局；基线 `develop` 尚无该目录，本变更新建。
- 假设 7：仓外插件与组合配置不计入本仓交付物，只作为验收证据保留在工作区。
- 开放问题：无。方向已由需求方在本次澄清中修正：dsh 通过插件机制引入 aidev-agent，本仓不引入 dsh 依赖、不提交插件，只提供可被宿主驱动的运行面。
