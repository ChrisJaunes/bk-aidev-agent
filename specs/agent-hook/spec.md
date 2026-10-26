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

8. 第二期追补（本次追加）：`session_start` / `tool_pre` / `tool_post` 三个 hook 点、协议层同步 hook 往返、`ask` 裁决与 headless 中断恢复。详见文末「第二期追加：Agent Hook 点」；第一期条目 1–7 与上文说明不变。

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

---

# 第二期追加：Agent Hook 点

- 变更名: agent-hook（沿用）
- 工作分支: feature/agent-hook（沿用，不新开分支）
- 性质: 新增能力（在同一功能的既有一期之上追补 hook 点），非重构
- 本期覆盖档位: `tool_pre` / `tool_post` / `session_start` 三点 + 协议 hook 往返 + `ask` 裁决与 headless 中断恢复

## 范围

1. 在 headless 运行面上暴露三个 hook 点：`session_start`（会话建立与本轮驱动前，可注入上下文）、`tool_pre`（工具派发前，可放行 / 拒绝 / 转人工）、`tool_post`（工具本体返回后、既有净化之前，可接受 / 阻断 / 覆盖结果 / 追加上下文）。
2. 协议层新增同步往返：agent → host 的 hook 请求行，与 host → agent 的答复行；现有「请求 / 事件 / 响应」三行模型不改变语义，只在同一 NDJSON 通道上补两类行。
3. `ask` 裁决复用仓内既有中断机制，并为 headless 补上「中断恢复」能力（一期明示不支持），使「hook 要人拍板 → 宿主给人拍板 → 工具继续或放弃」闭环。
4. 宿主侧 hook 引擎（判定执行方）由仓外 A 档插件承担：插件读 `hooks.json`、跑命令 hook、合并裁决、写宿主审计；本仓只上报点位与载荷、接收裁决。
5. 接入文档补齐 hooks 一节：启用方式、两类协议行、三点载荷与裁决字段、`ask` 与中断恢复的宿主义务、降级语义。
6. 验收：本仓内以 fake 宿主（注入事件与裁决）覆盖三点与中断恢复；端到端以仓外插件 + 真实 dsh + `hooks.json` 作为验收证据。

## 非目标

- 不做 `pre_step`（含 `reject`）与 `turn_stopping`（含续跑上限）：二者依赖宿主侧 `steer` / `inject` 语义，仍属一期非目标。
- 不做 `system_prompt_assemble` 与 `subagent_start` / `subagent_stop`：前者已有 `append_system_prompt` 替代路径，后者可经 a2a 侧直接注入。
- 不做 B 档代发（把 hook 请求代发成宿主自身的事件，让宿主既有策略插件对 bkai 会话生效）。
- 不承诺 `updatedInput`（改工具参数）、`systemMessage`、`continue:false` 停整轮这三个字段的落地；本期承诺的阻断只有 `deny` / `block`。
- 本仓不解析 `hooks.json`、不执行 hook 命令、不实现 matcher（字面量 / 正则）、不定义 hook 的审计格式：执行归属在宿主。
- 不给 `hooks` 配置时不得有任何行为变化（零回归口径与一期 ACC-1～3 一致）。
- 不把 hook 的输入输出回写平台 DB；不新增 AG-UI 事件类型，不改平台 `agent_info` 配置键，不改 HTTP 接口契约。

## 跨仓边界

- 本仓切片：hook 点挂载、协议两类新行、headless 中断恢复与 `ask` 映射、接入文档、仓内测试。
- 宿主仓（dsh）：零改动；本仓不引 dsh 包、不提交插件、不改宿主源码。
- 仓外产物：A 档 dsh 插件（实现 hook 引擎：读 `hooks.json`、`matcher` 语义、最严格合并、`exit 2` 阻断、`hook/invoked` 与 `hook/result` 审计）+ 组合/profile 配置；仅作为本期端到端验收证据保留在工作区，不入本仓、不发布。
- 依赖：本仓零新增依赖（不引 dsh 包，也不引 hooks 配置解析库）；插件侧允许复用宿主既有 hooks 原语，但该面属半私有导出，必须钉版本并加 import 冒烟。
- 与一期切片的接口关系：hook 请求行只走 headless 通道，不进入 `AgentFactory` 的 `create` / `resume` 协议；`ask` 复用既有中断对象形状，不新增中断 reason 之外的审批机制。
- 执行顺序：无跨仓强序。本仓可先独立验收（fake 宿主），插件端到端证据随后补。

## 用户故事

- 作为宿主使用者，我希望在工具执行前用自己已有的 hook 配置拦下危险命令，并且被拦下的调用真的不会执行、模型能看到拒绝原因。
- 作为宿主使用者，我希望在工具返回后改写或补充结果，而不必改动 bkai 的工具实现。
- 作为宿主使用者，我希望 hook 要求人工拍板时，会话能停在那个工具调用上等我决定，决定后从原处继续，而不是整轮重跑。
- 作为验收方，我希望用 fake 宿主与 fake 模型就能判定三个点与中断恢复都对，不依赖真实模型与真实 hook 命令。

## 需求

### REQ-13 hook 点的启用方式与执行归属

- `create` 必须接受可选的 `hooks` 配置对象；未提供该字段时 hook 层必须完全不启用（见 REQ-19）。
- `hooks` 配置必须能表达两件事：等待裁决的超时上限（毫秒，缺省 10000），以及启用哪些点（缺省三点全开）。
- 本仓对 hook 的职责只有两件：上报「哪个点发生了 + 载荷」，接收「裁决」。解析 `hooks.json`、执行命令、匹配 matcher、合并多份裁决、写审计，全部属宿主职责，本仓不得代为实现，也不得在无宿主时自行裁决。
- 每个 hook 载荷必须携带 `session_code`、`turn_id`、`point` 与该点的业务字段；载荷不得包含凭据、AK 或完整请求体。

### REQ-14 `session_start`

- 当会话建立后的首次驱动开始前、以及同一会话每次 `turn` / `resume` 的驱动开始前，必须触发一次 `session_start`；载荷必须含 `session_code`、`turn_id`、`first_turn`（是否该会话首轮）、`input`（本轮用户输入）。
- 当宿主返回非空 `context` 时，本轮模型请求必须包含该文本，且位于本轮用户输入之前。
- 注入只影响模型可见输入：不得改写会话账本中的 user 原文，不得改写平台落库内容，不得改变本轮 `turn_id`。
- 当宿主返回空 `context` 或裁决不可用时，本轮必须照常执行，不得中断、不得报错（fail-open）。

### REQ-15 `tool_pre`

- 当工具派发前，必须先于既有审批与结果脱敏触发 `tool_pre`；载荷必须含 `tool_name`、`tool_input`、`tool_use_id`。
- 裁决必须支持三种取值：
  - `allow`：继续既有链路（含审批与脱敏）；
  - `deny`：工具本体不得执行，模型必须收到含 `reason` 的工具结果，该工具调用记为失败结果；
  - `ask`：不得直接执行，必须按 REQ-17 转人工。
- `deny` 必须早于既有审批与脱敏生效：被拒的调用不得进入审批链路，也不得进入结果净化。
- 本期不承诺 `updatedInput`：`tool_input` 不会被 hook 改写。

### REQ-16 `tool_post`

- 当工具本体返回后、既有结果净化与截断之前，必须触发 `tool_post`；载荷必须含 `tool_name`、`tool_input`、`tool_use_id`、`tool_response`。
- 裁决必须支持四种能力：`accept`（默认，原样继续）、`block`（附 `reason`）、`content`（覆盖模型可见结果）、`context`（追加后续上下文）。
- 当裁决为 `block` 时，模型必须收到 `reason` 作为该工具结果，原结果不得进入模型上下文。
- 当裁决含 `content` 时，模型可见结果必须被替换为该值。
- 当裁决含 `context` 时，该文本必须在本轮后续模型请求中可见。
- 任何裁决都不得导致工具本体重复执行，也不得改变已有工具调用与结果的配对关系（`tool_use_id` 不变）。

### REQ-17 `ask` 裁决与 headless 中断恢复

- 当 `tool_pre` 返回 `ask` 时，必须以仓内既有人工审批中断机制暂停该工具调用：中断信息必须含 `toolCallId`（等于该次 `tool_use_id`）、`reason` 与可读说明；不得新造第二套审批通道，也不得跳过中断直接执行。
- 当一轮以中断结束时，headless 必须以可判别方式收尾：响应 `status` 必须为 `interrupt`，`RUN_FINISHED` 的 `outcome` 必须携带该中断（`id` / `reason` / `message` / `toolCallId` / `metadata`）；不得把中断轮判为 `finished` 或 `error`。
- `resume` 必须支持中断恢复负载（含 `interruptId` 与裁决）：裁决为放行时工具必须执行且结果回到模型；裁决为拒绝时工具本体不得执行，模型必须收到拒绝说明。
- headless 无平台 DB，恢复裁决的权威来源必须是宿主负载本身：必须校验负载 `interruptId`（携带 `toolCallId` 时一并校验）命中该会话当前 pending 中断；不命中或形状不合法时按 REQ-5 返回结构化错误，且不得改变图状态、不得误放行其它 pending。
- 中断与恢复全程不得创建平台单据、不得写平台 DB、不得导入 Django：REQ-8 的无平台依赖在中断路径上同样成立。
- 恢复完成后同一会话必须仍可继续 `turn`；恢复轮同样受 REQ-10 的串行与 `cancel` 约束。
- 本条取代一期 REQ-9 末句「headless 不驱动中断恢复」的约束范围：审批 / `ask_user_question` / hook `ask` 三类中断必须可恢复；其余中断类型仍按 REQ-5 显式失败。

### REQ-18 协议 hook 往返

- 必须新增两类协议行，与现有三行共用同一 NDJSON 通道：
  - agent → host 请求行：`{"id": ..., "type": "request", "method": "hook", "params": {"session_code": ..., "turn_id": ..., "point": ..., "payload": {...}}}`；
  - host → agent 答复行：`{"id": ..., "ok": true, "result": {...}}` 或 `{"id": ..., "ok": false, "error": {"code": ..., "message": ...}}`。
- 请求行 `id` 必须由发起该轮的请求 `id` 派生为 `<该轮请求 id>#h<n>`（`n` 自 1 递增，同一会话内不重复），使宿主无需额外状态即可路由答复。
- 入口必须能区分答复行与请求行：答复行不得进入请求解析流程，不得产生响应行，不得被报成未知方法。
- hook 请求行与事件行必须共用同一写锁，任何并发场景下都不得出现两行交错。
- 等待裁决期间收到 `cancel` 时，必须立即放弃等待并按取消语义收尾，不得等满超时。
- 以下情形必须 fail-open，即按「该点最宽松裁决」继续（`tool_pre` → allow、`tool_post` → accept、`session_start` → 不注入）：超过 `timeout_ms`、宿主回 `ok=false`、`result` 形状不合法、宿主返回未知 `decision`。
- 降级必须可观测且不得新增事件类型：必须写一条既有 AG-UI `CUSTOM` 事件（名称 `hook_degraded`，含 `point` 与降级原因）；不得新增 `EventType`。

### REQ-19 关闭态零回归与凭据约束

- 当 `create` 未提供 `hooks` 时，headless 的 stdout 行集合必须与一期逐字节一致：不得出现 `type=request` 行，不得出现 `hook_degraded` 事件，不得引入可测量延迟。
- hook 相关的事件、日志与错误信息不得输出完整载荷、凭据或 AK；只允许输出点位、裁决取值、耗时与降级原因。

### REQ-20 接入文档的 hooks 一节

- 必须在仓内接入文档中补齐：`hooks` 配置字段与默认值；两类协议新行与 `id` 派生规则；三点各自载荷与裁决字段；`ask` 与中断恢复的宿主义务（收到中断后如何组 `resume` 负载）；fail-open 与 `hook_degraded` 的语义；A 档插件的接入方式与钉版本注意。
- 文档不得复制宿主源码，不得把半私有导出写成稳定 API。

## 验收

- ACC-14（REQ-13、REQ-19）当 `create` 未提供 `hooks` 时，headless 的 stdout 行集合必须与基线逐字节一致，且不出现任何 `type=request` 行与 `hook_degraded` 事件。
- ACC-15（REQ-14）当 `session_start` 返回 `{"context": "X"}` 时，fake 模型收到的本轮请求体必须包含 `X` 且位于用户输入之前；会话账本中的 user 原文必须不变；同一会话第二轮必须仍触发 `session_start` 且 `first_turn=false`。
- ACC-16（REQ-15）当 `tool_pre` 返回 `deny` 时，工具本体调用次数必须为 0，模型必须收到含该 `reason` 的工具结果，且该轮必须以成功终止事件收尾。
- ACC-17（REQ-16）当 `tool_post` 返回 `block` 时，模型可见的工具结果必须为 `reason` 文本且不含原结果；含 `content` 时模型可见结果必须等于该值；含 `context` 时该文本必须出现在紧接的模型请求中。三种情形下工具本体调用次数必须均为 1。
- ACC-18（REQ-15、REQ-17）当 `tool_pre` 返回 `ask` 时，该轮响应 `status` 必须为 `interrupt`，`RUN_FINISHED.outcome.interrupts[0].toolCallId` 必须等于该次 `tool_use_id`，工具本体调用次数必须为 0；随后 `resume` 放行时工具必须执行、结果回到模型且该轮以 `finished` 收尾；`resume` 拒绝时工具本体调用次数必须仍为 0 且模型必须收到拒绝说明。
- ACC-19（REQ-18）当宿主对一条 hook 请求行答复后，该请求行 `id` 必须等于 `<本轮请求 id>#h1`，事件流必须继续；答复行不得产生响应行，也不得被报成未知方法。
- ACC-20（REQ-18、REQ-10）当 hook 等待期间发出 `cancel` 时，必须在该点超时上限之前以取消终止事件收尾；当宿主永不答复时，必须在 `timeout_ms` 之后放行并出现恰好一条 `hook_degraded` 事件。
- ACC-21（REQ-17）当 `resume` 携带的 `interruptId` 不命中该会话 pending 中断时，必须返回 `ok=false` 的结构化错误，且随后用命中的 `interruptId` 重新 `resume` 仍必须正常恢复。
- ACC-22（REQ-8、REQ-17）当完整走一次「`ask` → 中断 → `resume`」时，进程内不得导入 Django，且平台 DB 写入与单据创建调用次数必须为 0（以 mock 断言零平台调用）。
- ACC-23（跨仓，REQ-13、REQ-18、REQ-20）当按文档接入仓外 A 档插件并提供 `hooks.json`（`PreToolUse` 配 `exit 2`、`PostToolUse` 输出 `additionalContext`）时，真实 dsh 中该工具必须不执行、其 stderr 文本必须进入模型输入、宿主会话中必须出现 `hook/invoked` 与 `hook/result(decision=deny)`。

## 边界与非功能

- 超时：单点默认 10000 ms，可由 `hooks` 配置收紧或放宽；超时只降级不阻断。
- 并发：同一会话的 hook 等待占住该会话的轮次锁（串行语义不变）；不同会话的 hook 等待互不影响。
- 性能：关闭态零额外开销；启用态每个点最多一次阻塞往返，不在 hook 内部做重试。
- 安全：hook 载荷只含该点必要字段；不因 hook 引入网络监听、不新增对外网络依赖；日志与事件不输出载荷全文与凭据。
- 数据处理：hook 的输入输出不落平台 DB、不落会话账本；宿主审计由其自身负责。
- 可观测性：新增 `hook_degraded` 事件与点位耗时日志；不新增 span 属性、不新增 HTTP 字段。
- 测试约束：三点与中断恢复必须能在仓内以 fake 宿主 + fake 模型离线验收；端到端证据依赖仓外插件，缺插件时 ACC-23 记为未取证，不影响 ACC-14～ACC-22 的判定。

## 对既有需求的修订

| 既有条目 | 本期修订 |
| --- | --- |
| REQ-7（每轮以终止类事件收尾，不新增事件类型） | 不变；headless 响应的 `status` 取值集合新增 `interrupt`，属协议字段取值扩展，不新增 AG-UI `EventType` |
| REQ-8（无平台依赖） | 覆盖面扩展到中断路径：中断与恢复不得建单、不得写平台 DB、不得导入 Django（由 REQ-17 承接） |
| REQ-9 末句（headless 不驱动中断恢复） | 由 REQ-17 取代：审批 / `ask_user_question` / hook `ask` 三类中断必须可恢复，其余类型仍按 REQ-5 显式失败；ACC-8 不变 |
| 非目标「不做 steer / inject 逐步语义」 | 保持不变；这正是本期不做 `pre_step` / `turn_stopping` 的理由 |
| 非目标「不改写审批链路」 | 保持不变；`ask` 复用既有中断对象形状，不新增审批机制 |

## 开放问题与假设

- 假设 8：`session_start` 按「每轮触发」实现而非「仅首轮触发」。依据：参考文档把时机写为「会话建立 / 每轮开始前（resume 同样）」，且 headless 每轮重建实例；首轮性由 `first_turn` 区分，宿主可自行决定是否只用首轮。
- 假设 9：`hooks` 经 headless `create` 的字段下发，而非进程级环境变量。理由：只有随会话下发才能保证「不传即关闭」的零回归口径，也不让平台路径被动继承 hooks。
- 假设 10：`ask` 复用既有人工审批中断语义，headless 侧不建单，因此裁决权威在宿主负载；由此产生的信任边界由 REQ-17 的 `interruptId` 校验约束。
- 假设 11：`pre_step` / `turn_stopping` 依赖宿主 `steer` / `inject` 就绪，本期不做，待宿主侧能力落地后另立变更；`system_prompt_assemble` 与子 Agent 两点同理。
- 假设 12：A 档插件复用的宿主 hooks 原语属半私有导出，需钉版本 + import 冒烟；插件不入本仓，其失败不改变本仓验收结论（见「边界与非功能」测试约束）。
- 假设 13：一期评审遗留的 REQ-4 / ACC-3 规格偏差（续流入口收敛的口径）仍按 `plan.md` 的「规格偏差与待修订」登记，本期不顺势改写该条，避免与 hook 交付混在同一次审批里。
- 开放问题：无。
