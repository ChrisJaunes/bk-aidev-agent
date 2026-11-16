# 现状核实与 web 流式输出链路分析（方案阶段输入）

- 关联变更: celery-agent-execution
- 基线: `develop@56816adb`
- 定位: 本文件只陈述**已核实的现状事实**、迁移对流式输出的**影响面**与**风险清单**，不含实现方案、不改任何代码，供方案阶段直接引用。

## 1. 结论摘要

1. **SSE 早已是「生产者/消费者分离」结构，不依赖请求线程直接读 Agent 生成器**：producer 把事件写入消息通道，consumer 读回后组成 SSE。迁移只需把 producer 从 web 进程搬到 celery worker，consumer 与对外协议完全不动。
2. **跨进程所需的共享状态已经在 broker 上**：producer 锁、跨进程取消信号、心跳都是通道级实现（Redis 的锁还带 TTL 与续期），因此 producer 与 consumer 分处两个进程是被现有代码结构支持的，不是新增能力。
3. **真正的风险不在「能不能流」，而在三个边界**：worker 未开始产出时的 attach 窗口、worker 异常死亡时缺失终态帧、以及生产通道是普通 RabbitMQ（非 stream）带来的抢占式消费语义。

## 2. 现状：web 会话 SSE 事件是怎么产生并送达的

### 2.1 入口与调用链（已核实）

| 环节 | 事实 | 证据 |
| --- | --- | --- |
| 视图 | `ChatCompletionViewSet` 在请求线程内 `AgentExecutor(...).execute_with_save(...)`，流式分支直接把返回的生成器交给 `streaming_response()` | `src/plugins/aidev_bkplugin/aidev_bkplugin/views/chat.py:188`、`:200`、`:277`、`:537` |
| 流式包装 | `ChatCompletionAgent._stream_with_queue()` 创建 `GeneratorStreamingHelper(thread_id=...)`，先 `prepare_run(run_id)`，再 `yield from helper.stream(producer, ...)`；`stream_mode == "attach"` 时传 `attach_only=True` | `src/agent/aidev_agent/services/agent/chat.py:1427`、`:1468-1492` |
| 生产者 | `_producer()` 消费业务生成器，逐条 `message_handler.put(self.thread_id, encoded_event)`；另有独立心跳线程按 `HEARTBEAT_INTERVAL` 写 `HEARTBEAT_CHUNK` | `services/messages_handler/streaming_helper.py:1235-1281` |
| 消费者 | `stream()` 先 `acquire_consumer(thread_id)`，再 `_consume_stream_messages()` 循环读通道并 yield，最终由 `StreamingHttpResponse` 输出 | `streaming_helper.py:1047-1124` |
| 生产者启动 | `_start_or_resume_stream()` 用 `copy_context()` 把 OTel 上下文带进 daemon 线程后启动 producer | `streaming_helper.py:625-704` |
| 通道选型 | `MESSAGE_HANDLER_TYPE`（模板为 `rabbitmq`）；配置了 `RABBITMQ_STREAM_PORT` 时工厂选 `RabbitMQStreamMessageHandler` | `services/messages_handler/factory.py:24-44`、`constants.py:136`、`template/builtin/{{cookiecutter.project_name}}/app_desc.yml:88-90` |

### 2.2 可跨进程复用的通道能力（已核实，均为 broker 侧状态）

| 能力 | 事实 | 证据 |
| --- | --- | --- |
| 生产者互斥 | `acquire_producer` / `has_active_producer` / `release_producer` 为通道方法；Redis 实现用 `SET NX PX` + TTL + 续期 | `messages_handler/base.py:268`、`:275`、`:278`；`rabbitmq.py:1567`、`:1586`、`:1607`；`redis.py:541-570` |
| 消费者登记 | `acquire_consumer` / `wait_for_previous_consumer` / `release_consumer` | `base.py:57`、`:61`、`:69` |
| 断点回放 | `get_messages_since(thread_id, offset)`；`restore_messages` 兼容旧通道的竞争消费语义 | `base.py:261`、`:114`；`rabbitmq.py:2083`；`rabbitmq_stream.py:437`、`:626`；`redis.py:446` |
| 跨进程取消 | `check_cancel_signal` / `clear_stopped` / `set_cancel_signal`；producer 每处理 10 个 chunk 检查一次 | `streaming_helper.py:412-428`、`:1259`、`:1297-1314`、`:1365`；`base.py:336`、`:350`、`:366` |
| 存活心跳 | producer 写 `HEARTBEAT_CHUNK`，consumer 侧有 `_emit_retryable_heartbeat_timeout` 兜底 | `streaming_helper.py:1279-1287`、`:767` |
| 断连保留 | consumer 收到 `GeneratorExit` 只释放消费者，缓存消息保留供重连 replay | `streaming_helper.py:1125-1128` |
| attach 语义 | `attach_only=True` 时不启动 producer，只接管已有流；无活跃 producer 且无缓存则抛 `StreamAttachUnavailableError` | `streaming_helper.py:641-653` |
| 模式入参 | 请求侧 `stream_mode` 支持 `start` / `attach` | `src/agent/aidev_agent/pydantic_models.py:15-18` |

## 3. 迁移对流式输出的影响面（逐环节）

| 环节 | 迁移后由谁负责 | 语义是否变化 | 备注 |
| --- | --- | --- | --- |
| 增量事件 | worker 内的 producer 写通道；web 只读 | 不变 | 事件编码、事件类型、顺序、`thread_id` key 均沿用现状 |
| 首帧 | worker 开始执行后才产出 | 排队期无事件 | 排队期间 web 不得合成 Agent 事件，前端维持既有 loading 态 |
| 结束事件 | producer 写 `RUN_FINISHED` + EOD | 不变 | consumer 读到即收尾，`data: [DONE]` 由现有代码产出 |
| 错误事件 | producer 写受控错误帧；**worker 被强杀时无帧** | 需要补 | 需由任务级兜底写失败终态，配合心跳超时让 consumer 退出（见风险 R2） |
| 取消 | web 侧写跨进程取消信号，worker 的 producer 轮询感知 | 不变 | 现有 `check_cancel_signal` 路径可直接复用 |
| 客户端断连 | 释放消费者，producer 继续 | 不变 | 重连 `attach` 接管；web 重启同理 |
| 单进程开发 | `in_memory` 通道无法跨进程 | 需要开关 | 需显式回退进程内执行 |

## 4. 风险清单与待决策

- **R1 attach 窗口**：任务已投递但 worker 尚未 `acquire_producer` 时，`attach_only` 会直接失败（`streaming_helper.py:641-644`）。需要在投递后等待「有活跃 producer 或有缓存」再接管，等待上限受排队超时约束。
- **R2 worker 异常死亡**：consumer 现在用 `producer_thread.join()` 判断生产端存活，跨进程后该判据失效。若 worker 被 OOM/强杀，会话会缺 `RUN_FINISHED`，必须依赖心跳超时 + 任务级兜底写终态，否则会话可能停在 running。
- **R3 通道语义**：模板只配了 `MESSAGE_HANDLER_TYPE=rabbitmq`，未配 `RABBITMQ_STREAM_PORT`，因此走的是竞争消费语义（新 consumer 抢占旧 consumer），而非「多消费者各自从起点回放」。单 web 实例下无影响；gunicorn `-w 2` 多 worker / 多副本场景下，同一 `thread_id` 只能有一个 consumer 持有，重连会抢占。**是否切 stream 模式属方案阶段决策，本规格不预设。**
- **R4 长连接占用**：迁移不减少 web 侧 SSE 连接的线程占用（`-w 2 --threads 32`），2 小时长流会长期占住 1 个线程。容量是否足够由上线后指标判定。
- **R5 回放窗口**：回放数据随通道保留策略过期，超窗口后 attach 只能拿到会话已落库内容。窗口内接管、窗口外降级为读会话内容的边界需在方案阶段明确。

## 5. 对需求的约束（已写入 spec.md）

- REQ-6 / AC-2：事件必须由 worker 产生、web 以 consumer / attach 身份按 `thread_id` 读出转发，队列等待期间不得合成事件。
- REQ-7 / AC-3：断线后 attach 的行为与改造前一致。
- REQ-9 / AC-7：worker 异常必须落失败终态且不覆盖既有终态（对应 R2）。
- AC-9：单进程开发模式必须可用（对应 `in_memory` 通道无法跨进程）。
- 边界与非功能：复用既有 `messages_handler` 四种通道实现，不新建传输层。

## 6. 核实方式

- 代码基线 `develop@56816adb`，逐个文件阅读而非全文检索推断：`views/chat.py`、`services/agent/chat.py`、`services/messages_handler/{streaming_helper,base,rabbitmq,rabbitmq_stream,redis,factory,constants}.py`、`pydantic_models.py`、`app_desc.yml`。
- 本文件未运行任何服务或测试，结论均来自源码事实；标注「需补/需决策」的条目为尚未在代码中存在的部分，不是既有行为。
