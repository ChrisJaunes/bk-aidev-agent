# Gunicorn 退出导致 Agent 执行报错：Agent 任务执行迁移至 Celery worker

- 变更名: celery-agent-execution
- 工作分支: feature/celery-agent-execution
- 类型: 缺陷
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 执行顺序: 无

## 范围

把本仓在 gunicorn web 进程内构建并执行 **Chat 类智能体**任务的入口，改为投递 Celery 任务到 `bkai_agent_task` 队列、由 celery worker 执行；web 进程只负责投递、转发事件与返回结果，请求线程内不再执行 Agent。本期只覆盖 `AgentType.CHAT`，`AgentType.FLOW`（Flow 类智能体）整体不在范围内。覆盖三个入口：

1. **web 会话 chat（流式与非流式）**：`ChatCompletionViewSet` 当前在请求线程内直接 `AgentExecutor(...).execute_with_save(...)`（`src/plugins/aidev_bkplugin/aidev_bkplugin/views/chat.py:188`、`:200`、`:277`）。
2. **标准运维插件同步 invoke（Chat 类）**：`BkpluginAgentRunner.execute()` → `BkpluginChat._do_execute()`（`services/agent_bkplugin.py:340`、`:351`），当前在请求线程内同步跑完再返回字符串。
3. **已迁移的异步 invoke**：`dispatch_async()` → `_enqueue_background()` → `tasks.py:63` 的 `run_bkplugin_background_agent_task`（`services/agent_bkplugin.py:306`、`:363`、`:421`）。该入口已走 Celery，本期只把它并入统一的 Task 执行入口与统一超时口径，不改变其对外行为。

在同一变更内交付：任务级超时（执行 2 小时 / 排队 10 分钟）、任务观测数据、迁移前后报错量对比口径，以及「现状证据」表逐条对应的迁移点分析（分析结论作为方案阶段的交付物，本规格提供已核实的基线事实）。

## 非目标

- 不支持 **Flow 类智能体**（`AgentType.FLOW`）：`views/flow_agent.py` 的启动与节点操作后恢复轮询（`views/flow_agent.py:293`）、以及插件 invoke 的 FLOW 分支（`build_bkplugin_runner` 返回 `BkpluginFlow`，`services/agent_bkplugin.py:409`、`:513`）都保持现有进程内执行方式，本期不改。
- 不改 `aidev_wxbot` 常驻长连接进程的 Agent 执行链路（`src/plugins/aidev_wxbot/aidev_wxbot/wxaibot/views.py:389`、`services/execution.py:30` 的有界守护线程池）：它不在 gunicorn web 进程内，独立进程部署，本期不迁移。
- 不做 Celery worker 侧的优雅退出改造：不实现 warm shutdown 等待在途任务、不配置 `acks_late` 任务重投、不改 `celery_runtime.py` 现有的「broker 断连即退出」策略（已确认不纳入本期）。
- 不改 Agent 内部执行逻辑：模型调用、工具运行时、上下文压缩、checkpoint、AG-UI 事件 schema 与会话落库格式均不动。
- 不改前端与主站的接口契约：不新增/修改对外 HTTP 接口，不改 SSE 事件字段与内容编码。
- 不调整部署容量：不改 `resQuotaPlan` 与 `replicas`；worker 并发沿用模板现有 `-c 4` 单副本（允许按需调整 `-c`/`-Q`/`--max-tasks-per-child` 参数，见「边界与非功能」）。
- 不做排队积压的限流、优先级队列、多队列拆分等调度策略设计。
- 不处理 `cannot schedule new futures after shutdown` 之外的其他失败类型（如模型侧 5xx、工具超时）。

## 跨仓边界

- 业务仓只有 `bk-aidev-agent` 一个，无跨仓接口变更、无跨仓依赖、无跨仓执行顺序。
- 标准运维插件宿主（bksops / `bk_plugin_framework`）通过既有 `invoke` / `poll` 契约调用本仓：契约与入参出参不变，宿主侧无需改造，也不需要感知 Celery。
- 主站后端（`bk-aidev`）只消费会话状态与 AG-UI 事件，本变更不新增接口、不改数据格式，无需主站配合改造。
- 部署模板 `template/builtin/{{cookiecutter.project_name}}/app_desc.yml` 属本仓：web/celery 的 `procCommand` 调整在本仓内完成。
- 跨进程流式通道复用既有 `services/messages_handler`（`in_memory` / `redis` / `rabbitmq` / `rabbitmq stream`），不新建消息中间件、不改通道协议。

## 复现、期望与实际、影响及回归范围

### 复现

部署形态为 `gunicorn bk_plugin_runtime.wsgi -k gthread -w 2 --threads 32 --timeout 300 --max-requests 512 --max-requests-jitter 50`（`template/builtin/{{cookiecutter.project_name}}/app_desc.yml:96`）。触发条件：worker 因发布重启、优雅退出或达到 `--max-requests` 被回收时，进程内线程池已 shutdown（`asyncio` 默认执行器关闭路径见 `src/agent/aidev_agent/utils/loop.py:191`，提交点见 `core/ag_ui/aidev_agent.py:156`、`services/agent/artifacts.py:133`、`core/tools/runtime_tools/{bubblewrap,e2b,paas}_backend.py`），仍在执行的 Agent 任务继续提交工作项即抛 `RuntimeError: cannot schedule new futures after shutdown`。

线上口径：该报错日均出现 1500~2000 次，占全部任务量的 0.5%~1%。

### 期望

Agent 任务执行与 gunicorn worker 进程生命周期解耦：web 进程退出、重启、按 `--max-requests` 回收时，既不影响在途任务继续执行，也不影响新到任务被受理。

### 实际

两类入口（web 会话 chat、插件同步 invoke）都在 web 请求线程内构建并运行 Chat 类智能体（见下方「现状证据」），进程退出即中断任务；新到的任务在已关闭的线程池上提交失败，直接以该 `RuntimeError` 结束。

### 影响

- 约每 100~200 个 Agent 任务就有 1 个因该报错失败，是当前链路占比最高的单类失败原因。
- 用户侧表现为会话直接报错、无回复；插件侧表现为 invoke 调用失败。
- 发布与 `--max-requests` 回收越频繁，报错越集中，属于常态化的稳定性损耗而非偶发故障。

### 回归范围

- web 会话 chat：流式 SSE（事件序列、事件类型、`data: [DONE]`、attach/回放接管）、非流式返回。
- Flow 类智能体：本期不改其进程内执行链路，但需确认 `views/flow_agent.py` 与插件 invoke 的 FLOW 分支行为与改造前一致（不受本次改动影响）。
- 标准运维插件：同步 invoke 返回值、异步 invoke + poll 状态流转。
- 会话终态与消息落库：成功/失败/取消三态写入、turn 幂等。
- `aidev_wxbot` 共用 Agent SDK 与 `messages_handler`，虽不改其执行链路，仍需回归长连接会话不受本次改动影响。

### 现状证据（已证实）

| 事实 | 证据 |
| --- | --- |
| web 进程为 gunicorn gthread，`--timeout 300 -w 2 --threads 32 --max-requests 512` | `template/builtin/{{cookiecutter.project_name}}/app_desc.yml:96` |
| celery worker 已存在：`-P threads -Q plugin_schedule,bkai_agent_task,bkai_agent_metric -c 4 --max-tasks-per-child 256`，单副本 4C4G | 同上 `:106-109` |
| 已有一个 Agent 执行任务 `run_bkplugin_background_agent`，投递到 `bkai_agent_task` | `src/plugins/aidev_bkplugin/aidev_bkplugin/tasks.py:63-82` |
| bkplugin 异步 invoke 已走 Celery | `services/agent_bkplugin.py:306-330`（`_enqueue_background`） |
| web 会话 chat 在请求线程内执行 Agent（流式与非流式） | `views/chat.py:188`、`:200`、`:277` |
| 标准运维插件同步 invoke 在请求线程内执行 Agent | `services/agent_bkplugin.py:351`（`BkpluginChat._do_execute`） |
| Flow agent 启动/恢复在请求线程内跑轮询生成器（本期不迁移） | `views/flow_agent.py:293` |
| 插件 invoke 按 `AgentType` 分发：CHAT → `BkpluginChat`，FLOW → `BkpluginFlow`（本期只迁移 CHAT 分支） | `services/agent_bkplugin.py:333`、`:409`、`:513` |
| 流式事件由 producer 写入消息通道、由请求侧 consumer 读出组 SSE，以 `thread_id` 为 key；producer 锁、跨进程取消信号、心跳都在 broker 上而非进程内存 | `services/agent/chat.py:1468-1492`、`streaming_helper.py:1047-1124`、`:1235-1281`、`rabbitmq.py:1567/1586/1607`、`redis.py:541-570`（锁带 TTL 与续期） |
| 插件 SSE 2.0 已具备轮询取状态的通道 | `services/agent_bkplugin.py:545`（`poll_bkplugin_agent`） |
| 现有 Celery 任务未配置任何 task 级超时或排队超时 | `tasks.py:63-67` 仅设 `queue` + `ignore_result`；全仓无 `CELERY_TASK_TIME_LIMIT` / `task_time_limit` / `visibility_timeout` / `acks_late` |
| broker 断连退出策略已实现，与超时/重投无关 | `celery_runtime.py:44-76`、`settings.py:40-47` |
| 进程内有界守护线程池仅被 wxbot 使用，不产生 `futures` 语义的 shutdown 报错 | `services/execution.py:30`、`src/plugins/aidev_wxbot/aidev_wxbot/wxaibot/views.py:389` |
| 跨进程流式通道已存在，模板配置为 `MESSAGE_HANDLER_TYPE=rabbitmq` | `services/messages_handler/factory.py:24-44`、`app_desc.yml:88-90` |
| 流式支持 start/attach 两种模式（断线接管） | `src/agent/aidev_agent/pydantic_models.py:15-18`（`stream_mode`） |
| `asyncio` 默认执行器存在明确关闭路径，是报错的落地机制之一 | `src/agent/aidev_agent/utils/loop.py:191` |

## 需求

- **REQ-1（入口收敛）**：`ChatCompletionViewSet`（流式与非流式）与标准运维插件同步 invoke（`AgentType.CHAT`）这两个入口，改为把 Chat 类智能体的执行投递到 Celery `bkai_agent_task` 队列，由 celery worker 构建并运行 Agent；gunicorn web 进程只负责投递任务、转发事件、返回结果，请求线程内不构建、不运行 Agent 实例。
- **REQ-2（统一任务入口）**：所有 Chat 类智能体执行经由单一的 Celery Task 执行函数完成；已存在的异步 invoke 任务与新增入口共用同一执行函数与同一套失败写回逻辑，不允许每个入口各实现一套执行代码。
- **REQ-3（执行超时）**：任务开始执行后最长运行 7200 秒，超过即中止该任务；该时长必须可配置（默认 7200 秒），不得硬编码在调用点。
- **REQ-4（排队等待超时）**：任务从投递到开始执行的等待时长超过 600 秒时，不得再开始执行；必须在该 turn 落失败终态并给出「排队超时」可读信息。该时长必须可配置（默认 600 秒）。
- **REQ-5（超时后的收尾动作）**：执行超时或排队超时必须把该 turn 的会话状态写为失败终态、写入可读超时原因，并释放占用的 worker 线程，使后续排队任务能被继续消费。
- **REQ-6（流式对外语义不变）**：web 端 SSE 的事件序列、事件类型（`think` / `text` / `error` / `reference_doc`）、`data: [DONE]` 结束标记、错误事件文案与会话内容编码格式保持与改造前一致；事件由 worker 产生、web 进程原样转发。web 请求必须以既有消息通道（`messages_handler`）的 consumer / attach 身份、按 `thread_id` 读取事件后转发，不得在 web 进程内启动 producer 执行 Agent；worker 未开始产出事件期间，web 不得下发任何合成的 Agent 事件。
- **REQ-7（断线接管不变）**：客户端断线后重新 attach 的行为保持不变，仍能在既有回放窗口内接管后续事件。
- **REQ-8（同步 invoke 契约不变）**：标准运维插件同步 invoke 成功时必须返回与改造前一致的输出字符串；失败时 session 落失败终态并保留既有错误信息前缀，插件侧不感知 Celery。
- **REQ-9（失败终态唯一）**：投递失败、worker 执行异常、执行超时、排队超时四种情况都必须把该 turn 落到失败终态，且不得覆盖已确定的 `FINISHED` / `CANCELLED` / `FAILED` 终态。
- **REQ-10（可观测）**：每次 Agent 任务必须记录队列等待时长、执行时长、结果（成功/失败/执行超时/排队超时）、入口标识（web SSE / plugin sync / plugin async）与执行进程标识（celery worker），用于前后对比与容量评估。
- **REQ-11（效果可度量）**：以发布日为界，给出 `cannot schedule new futures after shutdown` 在发布前 7 天与发布后 7 天的日均次数对比口径，取数来源为本仓错误日志/APM。
- **REQ-12（迁移点可追溯）**：方案与实施阶段的「bkplugin 使用 celery_worker 现状分析」必须逐条对应本规格「现状证据」表，并对每条给出对应的改造点；实测与表内事实不符时以代码为准，并回写本规格。

## 验收

- **AC-1** 当用户发起 web 会话 chat 请求（流式或非流式）时，系统必须把 Agent 执行投递到 celery worker，且该请求的 Agent 构建与执行只发生在 worker 进程内。
- **AC-2** 当流式 chat 任务在 worker 中执行完成时，系统必须产出与改造前一致的 SSE 事件序列、事件类型、`data: [DONE]` 结束标记与一致的最终会话内容，且该 SSE 内容必须由 web 进程以 consumer 身份从消息通道读出后转发（web 进程内不启动 producer）。
- **AC-3** 当客户端在流式过程中断线并在回放窗口内重新 attach 时，系统必须能继续下发后续事件，行为与改造前一致。
- **AC-4** 当 Agent 任务执行时长达到 7200 秒时，系统必须中止该任务、把该 turn 写为失败终态并写入超时原因，且该 worker 线程必须可用于执行后续任务。
- **AC-5** 当任务在队列中等待达到 600 秒仍未开始执行时，系统必须不再开始执行该任务，并把该 turn 写为失败终态且错误信息包含「排队超时」。
- **AC-6** 当 Celery 投递失败时，系统必须把该 turn 写为失败终态并返回/下发可读错误信息，不得让请求静默挂起。
- **AC-7** 当 worker 执行过程中抛出异常时，系统必须把该 turn 写为失败终态且只写一次；turn 已处于 `FINISHED` / `CANCELLED` / `FAILED` 时不得被覆盖。
- **AC-8** 当标准运维插件同步 invoke 成功时，系统必须返回与改造前一致的输出字符串；当失败时，session 必须为失败终态且错误信息保留既有前缀。
- **AC-9** 当以单进程开发模式（`MESSAGE_HANDLER_TYPE` 为内存通道、未启动 celery worker）通过显式开关启动时，系统必须仍能完成一次完整 Chat 类智能体执行。
- **AC-10** 当任一 Agent 任务结束时，系统必须记录含队列等待时长、执行时长、结果与入口标识的观测数据。
- **AC-11** 当发布后运行满 7 天时，监控中 `cannot schedule new futures after shutdown` 的日均出现次数必须较发布前 7 天基线下降 95% 以上，且 gunicorn web 进程中该报错的日均出现次数必须为 0。
- **AC-12** 当任一入口（web SSE / plugin sync / plugin async）投递 Chat 类任务时，系统必须经由同一个 Celery Task 执行函数进入 Agent 执行，三个入口不得各自实现一套执行与失败写回逻辑。
- **AC-13** 当方案阶段提交「bkplugin 使用 celery_worker 现状分析」时，该分析必须逐条覆盖「现状证据」表的全部行，并对每一行给出对应的改造点。
- **AC-14** 当用户通过 `views/flow_agent.py` 发起 Flow 类智能体执行、或插件 invoke 传入 Flow 类智能体时，系统必须仍走现有进程内执行链路，行为与改造前一致，且不投递到 Celery。

## 边界与非功能

- **幂等**：同一 turn 被重复投递时不得并发执行出两份结果，沿用现有 turn 幂等与心跳保护机制判定与处置。
- **消息通道兼容**：`in_memory`（单进程）、`redis`、`rabbitmq`、`rabbitmq stream` 四种通道实现都必须可用；多进程模式复用既有 `messages_handler` 跨进程流式通道，不新增传输层。
- **滚动升级兼容**：`aidev_bkplugin` 以 SDK 形式被宿主模板依赖，web 与 celery 是两个独立进程：发布时必须保证新旧代码能在升级窗口内共存（先升级 worker 再升级 web），期间不得出现任务无人消费或重复执行。
- **容量**：worker 并发沿用模板现有 `-c 4` 单副本，不改 `resQuotaPlan` 与 `replicas`；允许调整 `template/builtin/{{cookiecutter.project_name}}/app_desc.yml` 中 celery 的 `-c` / `-Q` / `--max-tasks-per-child` 参数。上线后以排队超时率作为是否需要扩容的判定依据。
- **鉴权与上下文**：不改现有用户身份与租户上下文传递方式（`executor` / `username` 透传沿用现有实现），worker 侧必须拿到与改造前等价的身份上下文。
- **数据**：不改会话与消息落库 schema，不改 checkpoint 存储格式。
- **性能**：投递路径不得在请求线程内做同步阻塞等待（同步 invoke 的高层等待必须走轮询/事件通知，不得占住 gunicorn 线程直至任务结束）。

## 开放问题与假设

- **假设（已确认口径）**：超时后的收尾动作沿用确认选项的表述——执行超时与排队等待超时都写会话失败终态并释放 worker；用户只调整了时长（执行 2 小时、排队 10 分钟）。
- **假设（已确认口径）**：本期只覆盖 Chat 类智能体（`AgentType.CHAT`）；Flow 类智能体（`views/flow_agent.py`、插件 invoke 的 FLOW 分支）保持现有进程内执行，不投递 Celery。
- **假设**：排队等待期间 web 侧不下发任何合成的 Agent 事件（前端维持既有 loading 态），首帧 SSE 由 worker 开始执行后经消息通道产出；不新增「排队中」事件类型。
- **假设（已确认口径）**：`aidev_wxbot` 常驻长连接进程不在「web 进程只转发不执行」的范围内，作为非目标处理。
- **假设（已确认口径）**：Celery worker 的优雅退出与在途任务重投不纳入本期，`celery_runtime.py` 现有断连退出策略保持不变。
- **假设**：迁移后 worker 并发沿用现有 `-c 4` 单副本，容量是否不足由上线后排队超时率决定，不在本期预先扩容。
- **假设**：`cannot schedule new futures after shutdown` 的取数以本仓错误日志与 APM 为准，不引入新的监控平台改造。
- 无待用户回答的开放问题。
