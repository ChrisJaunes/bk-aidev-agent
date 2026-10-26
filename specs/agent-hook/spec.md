# Agent Hook 系统：AgentFactory 创建/恢复注入点与 dsh 接入

- 变更名: agent-hook
- 工作分支: feature/agent-hook
- 类型: 重构
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 无

## 范围

本变更把「Agent 实例从哪来、由谁驱动」从写死的类方法收敛为一个可替换的注入点，并用真实的外部 harness（DeepSeek Harness，下称 dsh）接入一次，证明这个注入点确实可被外部实现接管。

1. 新增 `AgentFactory` 注入点：`create`（新建实例）与 `resume`（恢复既有会话/中断）两个方法，进程级注册中心持有，支持替换默认实现（沿用仓内 `utils/factory.py` 的 `SingletonFactory` / `replace_defaults` 约定）。
2. 默认实现 = 现状能力：`create` 复用 `AgentInstanceFactory.build_agent` 的装配逻辑，`resume` 复用现状续流逻辑；开关未开启时对外行为与今日逐项一致。
3. 创建入口收敛：仓内 5 处 `AgentInstanceFactory.build_agent` 调用方保持签名与用法不变，改由注入点产出实例；仓内不再存在绕过注入点的实例构造路径。
4. 恢复入口收敛：前端续流与后台审批续流改走 `resume`，覆盖「同一会话续轮」与「审批 / `ask_user_question` 中断恢复」两类恢复。
5. dsh 实现（可选）：以 `aidev-agent[dsh]` 可选 extras 引入 `deepseek-harness-sdk`，通过子进程 + stdio JSON-RPC 驱动真实 dsh，完成一轮纯文本对话；LLM 由本机 fake 提供者替代，验证「接得进去」而非模型效果。
6. 显式开关与依赖隔离：只有显式选择 dsh 实现时才加载 dsh 依赖；默认安装与默认路径不新增任何运行时依赖。

## 非目标

- 不做每轮驱动（turn 前后）、工具执行前后等瀑布式 hook；本期只有 `create` / `resume` 两个点，dsh 的其余 hook 不移植。
- dsh 实现不接工具调用、审批/中断恢复、`ask_user_question`、附件与 PV 图片、知识库检索、token 统计与 span 上报；这些能力在默认实现中保持现状，不因本变更降级。
- 不改动 dsh 仓：不 fork、不写插件、不提 MR；只消费其公开 Python SDK 与其公开配置项。
- 不改 HTTP 接口契约（入参、出参、字段名），不新增请求字段，不改 AG-UI 事件类型，不改平台 `agent_info` 配置键。
- 不做 FLOW / A2A 的 dsh 化；这两类继续走默认实现。
- 不做 dsh 进程池、常驻复用、自动重启、并发抢占与配额治理。
- 不做性能与成本优化，不做 dsh 侧历史与平台历史的双向同步。
- 不删除、不改写现有 `AgentInstanceFactory` 的装配细节（模型、工具管道、checkpointer、审批链路）。

## 跨仓边界

- 唯一业务仓：`bk-aidev-agent`（本仓切片即全部实现切片）。
- `deepseek-harness` 仓仅作只读参考与外部运行依赖，不作为业务仓纳入本次交付；dsh 侧零改动。
- 新增依赖（仅 extras）：`deepseek-harness-sdk`（内网 PyPI 实测可下载，0.2.1rc2）及其平台运行时 wheel；后者 `python/sdk-runtime/platforms.json` 覆盖 `linux-x64`（manylinux_2_28）。
- 与兄弟变更（`tool-lazy-loading` 等）无接口耦合：本变更只改「实例来源与恢复入口」，不动 graph 装配、工具管道与模型链路。
- 无跨仓执行顺序要求。

## 用户故事

- 作为平台研发，我希望「创建什么 agent、谁来恢复它」由一个可替换的实现入口决定，这样在不改平台接口、不改前端的前提下，可以把 agent 主循环换成 dsh 这类外部 harness。
- 作为接入方，我希望换实现是显式开关 + 可选依赖，未开启时行为与今日完全一致，随时可回滚。
- 作为验收方，我希望用本机 fake LLM 就能判定 dsh 是否真的接进来了，不依赖真实模型与真实 AK。

## 必须保持的外部行为与判定方式

| 外部行为 | 判定方式 |
| --- | --- |
| 5 处创建入口的入参、返回值类型与异常语义不变 | 基线测试全绿；调用方代码零改动（diff 只出现在工厂内部与其调用下一跳） |
| AG-UI 事件序列（start / message / tool / run 结束等）与现状一致 | 同一输入下的 AG-UI 事件录制的类型序列与关键字段逐项比对 |
| 会话落库结果一致（assistant 消息、状态、turn 归属） | 平台会话内容与状态的接口快照比对 |
| 审批 / `ask_user_question` 中断与恢复链路一致 | 现有中断恢复测试全绿；中断后前端续流与后台续流均能续上 |
| 模型、工具、checkpointer、知识库、token 统计、span 上报链路不变 | 相关模块 diff 为空；相关测试全绿 |
| 进程启动与插件加载不受新依赖影响 | 未安装 `[dsh]` extras 时的应用启动冒烟通过 |

## 需求

### REQ-1 注入点协议与注册中心

新增 `AgentFactory` 协议，必须包含两个方法：

- `create(...)`：入参语义与 `AgentInstanceFactory.build_agent` 现有参数一致（agent 身份、构建类型、会话码、构建上下文、事件处理器、资源管理器、checkpointer、额外参数），返回满足现有实例契约的对象（可 `execute(...)`、可 `stop()`、带 `event_handler`）。
- `resume(...)`：入参包含既有会话标识与恢复负载（审批结果 / `ask_user_question` 回答 / 续轮输入）与 `create` 相同的运行期依赖，返回同一实例契约。

协议必须可被进程级注册中心（单例）持有，并支持替换默认实现；注册中心必须与仓内既有工厂写法（`utils/factory.py` 的 `GenericFactory` / `SingletonFactory` / `replace_defaults`）一致，不引入新的注册范式。

### REQ-2 默认实现与现状等价

默认实现必须直接复用现状装配与续流逻辑：`create` 走 `AgentInstanceFactory` 既有 `_build_from_session` / `_build_direct` → `_make_build_context` → `agent_registry.must_get(...)` → `build(ctx)` 链路；`resume` 走现状 `Command(resume=...)` 续流链路。默认实现必须是注册中心的默认值，且不被本变更改写内部装配行为。

### REQ-3 创建入口单一化

`AgentInstanceFactory.build_agent` 保留为对外签名（现 5 处调用方零改动），内部改为委派注册中心的 `create`。仓内不得存在绕过注册中心直接构造 agent 实例的业务代码路径。

### REQ-4 恢复入口单一化

前端续流与后台审批续流必须改走注册中心的 `resume`，不得再以「重建实例 + 带恢复负载 `execute`」的隐式方式恢复。`resume` 必须覆盖两类恢复：同一会话续轮、审批 / `ask_user_question` 中断恢复。

### REQ-5 不支持能力必须显式失败

注入实现若对某类构建或恢复不支持，必须抛出 bkai 既有异常类型并产出 error 事件；禁止静默回落到默认实现，禁止返回空实现或空回复。

### REQ-6 开关与依赖隔离

新增一个显式开关选择生效实现，默认值指向默认实现。dsh 相关依赖必须置于可选 extras；未安装 extras、或开关未指向 dsh 时，默认路径的导入、启动与运行不得因 dsh 依赖缺失而失败；缺少依赖的错误只允许在选择 dsh 实现时出现，且必须可读。

### REQ-7 dsh 实现：创建与驱动

开关指向 dsh 后，一轮对话必须由真实 dsh 进程完成：以子进程 + stdio JSON-RPC 方式驱动 dsh（经其公开 Python SDK），把用户输入交给 dsh，取得 assistant 文本，并经现有 `event_handler`（`AGUISessionWriter`）与现有会话写回路径落库，前端可见。

### REQ-8 dsh 实现：会话标识与事实源

平台 DB 是唯一对话事实源。`session_code` 与 dsh 会话标识的映射必须持久化在平台侧并可重建；删除 dsh 的本地 home 后，必须能仅凭平台侧历史重建并再跑一轮。禁止把 dsh 侧历史作为唯一事实源。

### REQ-9 dsh 实现：恢复语义

dsh 实现的 `resume` 必须支持「同一 `session_code` 续轮」，并复用上一轮的 dsh 会话标识；审批 / 中断恢复在该实现下按 REQ-5 显式报不支持。

### REQ-10 dsh 实现：失败语义

dsh 启动失败、初始化超时、子进程异常退出、JSON-RPC 协议错误时，必须产出带原因的 error 事件并写入会话（沿用现状错误落库口径），不得挂起请求，不得返回空回复。

### REQ-11 dsh 实现：运行隔离

dsh 子进程必须使用按会话/请求隔离的 dsh home 与工作目录，不得读取或依赖 `~/.dsh`；fake LLM 提供者只允许监听本机回环地址。

### REQ-12 可观测与协议稳定

创建与恢复必须记录所选实现标识（默认实现 / dsh）到日志。不得新增对外 HTTP 字段，不得新增 AG-UI 事件类型。

## 验收

- ACC-1（REQ-2）当开关未指向 dsh 时，`AgentInstanceFactory.build_agent` 的入参处理、返回值类型与异常行为必须与基线逐项一致，现有测试必须全绿。
- ACC-2（REQ-3）当注册中心被替换为一个记录调用的实现后，`views/chat.py`、`services/agent_bkplugin.py`（2 处）、`services/agent_builder.py`、`aidev_wxbot/wxaibot/strategies.py` 共 5 处创建调用必须全部命中该实现，且调用方代码无改动。
- ACC-3（REQ-4）当审批中断后由后台工作线程续流时，系统必须调用 `resume` 而不得调用 `create`；默认实现下的续流输出与基线一致。
- ACC-4（REQ-5）当注入实现的 `resume` 对某恢复类型抛出既有 bkai 异常时，系统必须写入一条 error 状态的 assistant 消息，且不得回落到默认实现。
- ACC-5（REQ-6）当未安装 `[dsh]` extras 且开关指向 dsh 时，系统必须在创建阶段抛出可读错误，且进程启动、默认实现路径与其他插件不受影响。
- ACC-6（REQ-7）当安装 extras、开启开关并以本机 fake LLM 运行时，一轮「用户输入 → assistant 回复」必须端到端完成，会话中存在 assistant 文本，且事件序列包含 run 开始与消息类基础事件。
- ACC-7（REQ-8）当删除 dsh 本地 home 后在同一 `session_code` 再跑一轮时，系统必须仅凭平台侧历史重建会话并成功完成。
- ACC-8（REQ-9）当同一 `session_code` 发起续轮时，系统必须命中 `resume`，且复用上一轮的 dsh 会话标识。
- ACC-9（REQ-10）当 dsh 启动失败或初始化超时时，系统必须在配置的超时上限内产出 error 事件并把错误写入会话，不得挂起请求。
- ACC-10（REQ-11）当检查运行环境时，dsh 子进程不得创建或读取 `~/.dsh`，fake LLM 提供者只监听 `127.0.0.1`。
- ACC-11（REQ-12）当完成一次默认实现与一次 dsh 实现的对话后，日志中必须能区分所选实现标识，且对外 HTTP 响应字段与 AG-UI 事件类型集合与基线一致。

## 边界与非功能

- 运行时：Python 3.11–3.12；验收在 Linux x64（manylinux）容器内完成。
- 兼容性：主依赖不变；`[dsh]` 为新增可选 extras；开关默认关闭，生产行为不受影响。
- 并发：同一 `session_code` 的并发语义保持现状（既有抢占机制）；dsh 路径不新增抢占实现，也不得影响默认路径。
- 性能：默认路径不得引入可测量延迟（仅增加一次工厂查询与一次委派）；dsh 路径不设性能目标。
- 安全：不新增对外网络依赖（fake LLM 仅回环）；不读写用户凭证；日志不得输出 AK、密钥或完整请求体；dsh 工作目录限定在可配置的会话目录内。
- 数据处理：dsh 本地 home 落在可配置目录并按会话生命周期清理，不作为事实源。
- 可观测性：新增工厂实现标识的日志字段；不新增 span 属性。
- 测试约束：默认路径的等价性由现有测试保证；dsh 路径的验收测试必须可离线运行（不依赖真实模型与外部网络）。

## 开放问题与假设

- 假设 1：`deepseek-harness-sdk` 可从内网 PyPI 取得（已实测下载 0.2.1rc2），其平台运行时 wheel 覆盖 linux-x64。若目标机器无法安装运行时 wheel，SDK 支持以 `dsh_bin` / `profile` 指向宿主预装的 dsh，验收口径不变。
- 假设 2：对话事实源为平台 DB（沿用现状会话存储），dsh 侧只作执行上下文（已固化进 REQ-8）。
- 假设 3：`specs/<变更名>/` 目录沿用同仓兄弟变更（`tool-lazy-loading`）的 SDD 布局；基线 `develop` 尚无该目录，本变更新建。
- 假设 4：开关采用环境变量式配置，与 `src/agent/aidev_agent/config.py` 既有 `env.bool` 先例一致；本期不向平台下发 `agent_info` 配置键。
- 假设 5：dsh 实现本期只覆盖 CHAT 类型；FLOW 与 A2A 继续走默认实现。
- 假设 6：fake LLM 以本机 OpenAI 兼容 SSE stub 形式提供，经 dsh 的 `base_url` / `api_key` 覆盖注入，不修改 dsh 源码。
- 开放问题：无。原 3 个待定项（落点与替换方向、hook 粒度、验收深度）已由需求方确认为：仅改本仓、只做 `create` / `resume` 两个点、必须真接 dsh 跑通一轮且 LLM 允许 fake。
