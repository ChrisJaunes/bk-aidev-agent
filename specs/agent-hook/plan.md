# Agent Hook 系统 技术方案

- 变更名: agent-hook
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/agent-hook/spec.md

## 背景与目标

规格要求把「Agent 实例从哪来、由谁恢复」从写死的类方法收敛为可替换的注入点，并补一个不依赖平台的最小运行面，使外部宿主（DeepSeek Harness，下称 dsh）能通过自己的插件机制把核心 Agent 换成 aidev-agent。依赖方向是 dsh → 插件 → aidev-agent：本仓零 dsh 依赖，插件与组合配置仓外维护。

本方案要达成的目标：

1. `AgentInstanceFactory.build_agent` 保持为对外签名，5 处调用方零改动，内部委派进程级注册中心的 `create`；仓内不再存在绕过注册中心构造 Chat / Flow 实例的业务路径。
2. 默认实现逐字复用现有装配链路，默认路径只多一次工厂查询与一次委派，外部行为（入参处理、返回值类型、异常语义）与基线一致。
3. 新增 `aidev_agent.headless` 子包，以 `python -m aidev_agent.headless` 形式提供 stdio NDJSON 入口，方法集覆盖 `create` / `turn` / `resume` / `cancel` / `close`，事件沿用 `ag_ui.core` 事件对象，可在无 Django、无平台凭证、无平台 DB 的环境完成一轮纯文本对话。
4. 交付接入文档，并以仓外插件在真实 dsh 上跑通一轮（本机 fake 模型）作为验收证据。

范围裁剪（需求方在方案确认时确定）：**续流入口收敛（REQ-4 / ACC-3）本期整体不做**，`views/chat.py` 与 `services/approval_resume.py` 等续流调用点保持现状。`AgentFactory.resume` 仍按 REQ-1 交付协议与默认实现（供 headless 与后续切换使用），但不接入平台续流路径。详见「规格偏差与待修订」。

## 非目标

- 不在本仓提交 dsh 插件、不 fork 或改造 dsh 源码、不引入任何 dsh 相关依赖（主依赖与 extras 均不变）。
- 不把平台续流（前端续流、后台审批续流、企微续流共 4 处隐式路径）切到 `resume()`；本期一处都不动。
- 不做 dsh `Agent` 全表面：`inbox` 多队列、`steer` / `inject` / `followup` 逐步语义、`runMaintenance`、compaction、子 Agent、多 Agent 因果链均不支持，调用必须显式报错。
- 不映射 dsh 的工具注册与审批体系；headless 不构造业务工具、不建审批工单，「跑通」口径为纯文本一轮。
- 不改 HTTP 接口契约、不改 AG-UI 事件类型、不新增平台 `agent_info` 配置键、不新增 `ExecuteKwargs` 字段。
- 不改 `AgentInstanceFactory` 的装配细节（模型链路、工具管道、checkpointer、审批链路、token 统计、span 上报）。
- 不做 FLOW / A2A 改造；FLOW 构建继续走默认实现。
- 不做性能与成本优化，不做进程池、常驻复用与自动重启。
- headless 不监听网络端口（只走 stdio），不引入新的必需依赖。

## 现状

| 关注点 | 位置 | 现状结论 |
| --- | --- | --- |
| 创建入口 | `services/agent/factory.py:117` `AgentInstanceFactory.build_agent` | 唯一创建入口；类方法，`_FACTORY_TOKEN` 闸口禁止外部直接 `__init__`；内部链路 `_validate_params` → `_build_from_session` / `_build_direct` → `_make_build_context` → `agent_registry.must_get(type)()` → `build(ctx)` |
| 创建调用方 | `services/agent_bkplugin.py:112`（CHAT/DIRECT）、`:449`（FLOW/DIRECT）、`services/agent_builder.py:209`（CHAT/SESSION）、`views/chat.py:446`（FLOW/DIRECT）、`aidev_wxbot/wxaibot/strategies.py:259`（FLOW/DIRECT） | 5 处，均只依赖 `build_agent` 关键字入参；其中 3 处为 FLOW 构建 |
| 实例契约 | `services/agent/registry.py:110` `AgentProtocol`、`ChatCompletionAgent` | `execute(execute_kwargs)` / `stop()`；事件经 `event_handler: Callable[[BaseEvent], None]`（平台侧实现为 `AGUISessionWriter`，`BaseSessionWriter` 子类）；调用点另读 `chat_history` / `event_handler` / `resource_manager` |
| 恢复现状 | `views/chat.py:174`、`services/approval_resume.py:156`、`wxaibot/approval_resume.py:165`、`wxaibot/question_resume.py:127` | 无独立 resume 入口：一律 `AgentBuilder.by_session_code(...)`（内部仍是 `build_agent`）+ 带 `resume` 的 `ExecuteKwargs` 调 `execute`。本期不改 |
| 恢复负载消费 | `services/agent/chat.py:327`、`:509`；`services/resume_events.py:131` | `ek.resume` 在 `ChatCompletionAgent.execute` 内被 `resolve_resumes` 消费并生成 `Command(resume=...)`；`AgentExecutor` 另用它判定 resume 事件流。负载属于 `ExecuteKwargs`，不在实例上 |
| 注册范式 | `utils/factory.py:31` `GenericFactory`、`:160` `SingletonFactory`、`:170` `SimpleFactory` | `replace_defaults(...)` / `register(...)` / `get()`（未注册回落默认）；仓内已有 `agent_registry`（类型→类）与 `common_agent_factory`、`resource_manager` 三处先例 |
| 可替换点 | `services/common_agent/registry.py`、`packages/resource_manager/registry.py` | 已覆盖 graph 构建器与配置来源；「实例来源」与「恢复入口」尚无替换点 |
| 无 Django 可行性 | `config.py:104` | 全包仅此一处函数内 `import django`（`update_django_settings`）；`aidev_agent` 其余模块无 Django / blueapps 模块级导入 |
| 平台客户端 | `packages/resource_manager/base.py:44`、`:46` | `BaseResourceManager` 模块级导入 `aidev_agent.api.paas_client` 与 `bkapi_client_core`（非 Django）；平台 API 调用统一经 `resource_manager.get_client()`，因此注入一个不触达平台的实现即可隔离 |
| 平台写入点 | `services/token_usage.py:44` | token 上报经 `resource_manager.get_client().api.create_chat_session_token_usage`；span 上报在 `packages/opentelemetry`（可选 extras，未安装即 no-op）。headless 用不触达平台的 RM 即可满足「零平台调用」 |
| 模型端点 | `config.py:124` `LLM_GW_ENDPOINT`、`config.py:71` `settings.set()`、`llm_gateway.py:241` `ApiGwMixin.get_setup_instance` | `build_chat_model`（`services/agent/chat.py:1801`）用 `settings.LLM_GW_ENDPOINT` 作 `base_url`；鉴权头取 `ChatBuildExtras.auth_headers`，缺省用 `settings.APP_CODE/SECRET_KEY` 组成 `X-Bkapi-Authorization`；`settings.set()` 可写 `_resolved` 覆盖已解析值 |
| 模型 token 计数 | `llm_gateway.py:295` | `get_num_tokens` 请求 `{base_url}/api/token_check`。**fake 端点必须同时实现该接口**，否则纯文本一轮也会在 token 统计处失败 |
| 消息与取消 | `services/messages_handler/factory.py:47`；`streaming_helper.py:752`；`core/ag_ui/agent.py:239` | 未配置 MQ 时默认 `InMemoryQueueMessageHandler`（单进程可用）；取消以 `RunErrorEvent(message=RunId.CANCELLED_MESSAGE)` + `RunFinishedEvent(run_id=RunId.CANCELLED)` 收尾，**不新增事件类型** |
| fake 模型 | `packages/langchain_core/models/mock.py` | 已有 `MockResponse` / mock chat model，可在无网络下构造模型，但走的是模型类替换而非 HTTP 端点；headless 验收仍需一个 `/v1` + `/v1/api/token_check` 的本机 HTTP fake |
| 测试 | `src/agent/tests/`（pytest，`asyncio_mode=auto`），`src/agent/Makefile:24` | 装配用例在 `tests/core/graphs/test_react.py`，执行链在 `tests/services/test_chat.py` 与 `tests/core/ag_ui/`，工厂版本用例在 `tests/services/test_agent_factory_version.py`；无 headless 用例 |

结论：需要新增两处能力——(1) 实例来源与恢复入口的注册中心（默认值逐字沿用现有装配）；(2) 不依赖平台的 headless 运行面（内存资源管理器 + stdio 协议 + 事件转发 + 会话线程模型）。既有 graph 装配、模型链路、工具管道、审批链路不改。

## 方案对比

### A. 注入点与注册中心形态

| 维度 | A1（选定）`SingletonFactory` + 默认实现 | A2 模块级全局变量 | A3 新写 `AgentFactoryRegistry` 类 |
| --- | --- | --- | --- |
| 与仓内约定 | 与 `agent_registry` / `common_agent_factory` / `resource_manager` 一致，支持 `replace_defaults` | 无统一入口，替换靠赋值，易漏 | 与既有 `GenericFactory` 体系并行，形成第二套注册范式 |
| 未注册语义 | `get()` 回落默认实现，`Teapot` 懒失败 | 需自行判空 | 需自行实现 |
| 结论 | **采用** | 排除：违反 REQ-1「不引入新的注册范式」且无默认值语义 | 排除：同上，且与 REQ-1 的「与仓内既有写法一致」直接冲突 |

### B. 创建入口收敛方式

| 维度 | B1（选定）`build_agent` 委派注册中心 | B2 改 5 处调用方直连注册中心 |
| --- | --- | --- |
| 调用方改动 | 零改动（ACC-2 直接满足） | 5 处签名与装配上下文外移，diff 大、回归面广 |
| 装配逻辑归属 | 默认实现持有 `AgentInstanceFactory` 的构造与 `_validate_params` / `_build_*` / `_make_build_context`，逐字保留 | 需把装配拆分给每个调用方，等价性风险高 |
| 结论 | **采用** | 排除：与 REQ-2「默认实现直接复现现状装配」和 ACC-1「调用方代码零改动」冲突 |

### C. `resume` 的落地面

| 维度 | C1（选定）轻量选路 | C2 绑定负载的代理实例 |
| --- | --- | --- |
| `resume` 职责 | 校验恢复负载形状与类型 → 走同一装配 → 返回实例 | 装配后返回薄代理，`execute` 时把负载注入 `ExecuteKwargs` |
| 负载传递 | 仍由 `ExecuteKwargs.resume` 承载（`resolve_resumes` / `Command(resume=)` / resume 事件判定都在 execute 链路里读它） | 需改 `AgentExecutor` 的 resume 事件判定与 4 处 `execute_kwargs` 构造，否则 `uses_resume_event_stream` 退化 |
| 平台零回归 | 是：平台路径字段与调用顺序不变 | 否：执行器与调用点均需改，回归面大 |
| 结论 | **采用** | 排除：本期不切换平台续流，改执行器付出的回归成本换不到收益；如后续要做「负载单一来源」，作为独立变更处理 |

### D. headless 进程与会话模型

| 维度 | D1（选定）单进程多会话 | D2 每会话一子进程 |
| --- | --- | --- |
| 进程数 | 1 个 headless 进程承载 N 个会话 | N 个会话 N 个进程 |
| 并发 | 会话锁串行 + 工作线程并发，直接对应 REQ-10「同会话串行、跨会话并发」 | 靠多进程实现，进程内天然串行 |
| `close` 语义 | 释放该会话（checkpointer、内存账本、线程、实例），进程继续服务 | 即进程退出 |
| 隔离性 | 单会话异常需在会话边界内捕获（REQ-10 要求进程保持可用，已覆盖） | 进程级隔离，崩溃不外溢 |
| 宿主成本 | 插件只维护 session_code → 会话的映射 | 插件维护进程池与生命周期 |
| 结论 | **采用**（需求方确认） | 排除：REQ-10 明确区分同会话串行与跨会话并发，D1 是其字面实现；D2 的隔离优势本期不构成收益 |

### E. headless 会话状态

| 维度 | E1（选定）内存账本 + 会话级 checkpointer | E2 纯无状态（每轮由宿主带全量历史） | E3 宿主侧持久化 |
| --- | --- | --- | --- |
| 历史来源 | `HeadlessResourceManager` 内存账本（`get_chat_session_contents`）+ `MemorySaver` 图状态 | 仅 `turn.messages`，无会话记忆 | 需宿主实现存储接口，超出本期 |
| 续轮 | 同一 `session_code` 直接续（REQ-9） | 不能续轮，与 REQ-9 冲突 | 可续，但引入新契约 |
| 无状态模式 | 同时支持（`turn.messages` 提供时走 DIRECT 构建、不写账本） | 仅此一种 | 同 E1 |
| 结论 | **采用** | 排除：不满足 REQ-9「同一会话标识续轮」 | 排除：规格明确本期事实源归宿主、平台侧默认不持久化，E3 属下一期 |

## 决策

### D-1 注册中心与协议（REQ-1）

在 `services/agent/factory.py` 内新增：

- `AgentFactoryProtocol`：`create(**build_params) -> AgentProtocol`、`resume(*, session_code: str, resume: Any, **build_params) -> AgentProtocol`。`build_params` 语义与 `build_agent` 现有关键字参数一致（agent 身份、构建类型、会话码、构建上下文、事件处理器、资源管理器、checkpointer、额外参数）。
- `DefaultAgentFactory`：默认实现，持有现有装配逻辑（见 D-2）。
- `agent_factory: SingletonFactory[str, AgentFactoryProtocol] = SingletonFactory("agent_factory", DefaultAgentFactory())`，进程级单例，替换走 `agent_factory.replace_defaults(impl)`，与仓内 `replace_defaults` 约定一致。

`_FACTORY_TOKEN` 闸口保留：凭证从「仅 `build_agent` 持有」改为「仅 `DefaultAgentFactory` 持有」，`AgentInstanceFactory.__init__` 的 `RuntimeError` 语义不变。

### D-2 默认实现与现状等价（REQ-2）

`DefaultAgentFactory.create` 承载现 `build_agent` 的方法体，逐字保留：`AgentInstanceFactory(...)` 构造 → `_validate_params()` → `_build_from_session()` / `_build_direct()` → `agent_registry.must_get(agent_type)()` → `_make_build_context()` → `build(ctx)`。异常类型、异常信息、日志行均不改。

`AgentInstanceFactory.build_agent` 保留原签名、默认值（含 `agent_code=settings.APP_CODE`、`max_tokens=settings.MAX_TOKENS` 的 import 期求值语义）与 docstring，方法体改为 `return agent_factory().create(...)`，原样透传全部关键字参数。

`create` 与 `resume` 共享私有 `_build(**build_params)`；`create` 直接调用它，`resume` 校验负载后调用它。这样替换为记录型实现时，能区分外部到底走了哪个入口。

### D-3 创建入口单一化（REQ-3）

5 处调用方不改一行。仓内不再存在「绕过注册中心构造 Chat / Flow 实例」的业务路径：`AgentInstanceFactory` 的直接构造仍被 `_FACTORY_TOKEN` 拦住，唯一构造点是 `DefaultAgentFactory`。headless 入口同样经 `agent_factory().create(...)` / `resume(...)`。

### D-4 `resume` 语义（REQ-1、REQ-5）

`DefaultAgentFactory.resume` 的顺序：

1. 归一化负载：`dict` → 单元素列表；`None` / 空列表 → 抛 `AgentFactoryError(code="resume_payload_missing")`。
2. 类型校验：仅接受「中断恢复项」形状（元素为 `dict` 且含 `interruptId`，可选 `status` / `payload`）。形状不符 → `AgentFactoryError(code="resume_payload_unsupported")`，消息里只带字段名与收到的类型，不带取值内容。
3. 校验通过后走 `_build(...)` 返回实例；**不写 `ExecuteKwargs`，不缓存负载**。调用方仍把同一变量放进 `ek.resume`（`resolve_resumes` / `Command(resume=)` / resume 事件判定均读它）。

`AgentFactoryError` 继承仓内既有异常基类（`aidev_agent.exceptions`），带 `code` 与 `reason` 字段；不捕获后静默回落默认实现（REQ-5）。

本期平台侧无 `resume()` 调用方，该入口由 headless 续轮与单测驱动。

### D-5 headless 形态与协议（REQ-6、REQ-7）

新增子包 `src/agent/aidev_agent/headless/`，入口 `python -m aidev_agent.headless`。协议为 NDJSON over stdio：

- 请求（stdin，一行一条）：`{"id": "<宿主生成>", "method": "create|turn|resume|cancel|close", "params": {...}}`
- 事件（stdout）：`{"id": "...", "type": "event", "event": {...}}`，`event` 为 `ag_ui.core.events` 对象的 `model_dump(mode="json", by_alias=True)`，不新增类型、不裁字段
- 响应（stdout，每条请求恰好一条，位于该请求的事件之后）：`{"id": "...", "ok": true, "result": {...}}` 或 `{"id": "...", "ok": false, "error": {"code": "...", "message": "...", "detail": {...}}}`
- stdout 只写协议数据；`logging` 统一挂 `StreamHandler(sys.stderr)`，启动时把 root logger 指到 stderr

终止事件三态沿用现有编码，不新增事件类型：

| 结束原因 | 终止事件 |
| --- | --- |
| 正常结束 | `RunFinishedEvent(run_id=<run>)` |
| 用户取消 | `RunErrorEvent(message=RunId.CANCELLED_MESSAGE)` + `RunFinishedEvent(run_id=RunId.CANCELLED)`（与 `streaming_helper._build_terminal_cancel_events` 同构） |
| 内部错误 / 不支持 | `RunErrorEvent(message=<结构化原因>)`，响应 `ok=false` 带同一 `code` |

### D-6 会话模型与状态（REQ-9、REQ-10）

- `SessionRegistry` 持有 `session_code -> HeadlessSession`；`session_code` 由调用方提供并原样回传，入口不自造标识（未提供时按 `create` 参数生成 UUID 并回传，仅此一处例外）。
- 每个 `HeadlessSession` 持有：`threading.Lock`（同会话串行）、`MemorySaver` checkpointer（`thread_id = session_code`）、内存账本（`HeadlessResourceManager`）、当前执行实例与取消标志。
- `turn` 在会话锁内组装 `ExecuteKwargs(stream=True, session_code=..., thread_id=session_code, turn_id=<uuid>, input=...)`，经 `agent_factory().create(build_type=SESSION, ...)` 构建后调 `execute`，消费 SSE 生成器、事件经 writer 写 stdout；轮末由 writer 把 user 输入与 assistant 文本追加进内存账本。
- `turn.messages` 非空时走无状态模式：`build_type=DIRECT` + `session_context_data` 取该列表，不写账本、不复用图状态。
- `resume` 仅覆盖「同一会话续轮」（携带 `input`，同一 `thread_id`，不设 `ek.resume`）；审批 / `ask_user_question` 中断负载 → `resume_payload_unsupported`（REQ-5 显式失败）。
- 不同会话由独立工作线程执行 → 跨会话并发；同会话第二个 `turn` 阻塞在会话锁上 → 串行（ACC-9）。
- `cancel` 在会话锁外可达：置取消标志 → 对当前实例调 `stop()`（内部 `request_cancel(thread_id)`，走默认 `InMemoryQueueMessageHandler`）→ 等待取消终止事件，超时上限 10s 后由入口补发取消终止事件并释放锁。
- `close` 释放实例、checkpointer、账本与线程句柄并标记会话已关闭；之后该会话任何请求返回 `session_closed`（ACC-5）。

### D-7 无平台模式（REQ-8）

`HeadlessResourceManager` 实现 `ResourceManagerProtocol`：

- `get_agent_config` / `retrieve_agent_config`：由 `create` 的 `agent` 段合成 `AgentConfig`（`agent_code`、`agent_name=agent_code`、`chat_model`、`non_thinking_llm=chat_model`，`tool_codes` / `knowledgebase_ids` / `knowledge_ids` 为空），不触达平台。
- `get_chat_session_contents` / `get_chat_session_context`：返回内存账本副本。
- `publish_event`：no-op（事件只走 writer）；`event_publishing_enabled`：`False`。
- `get_client`：抛 `HeadlessUnsupportedError`（含方法名），使 `BKAidevTokenUsageSink` 等平台写入点在既有 `try/except` 内被日志吞掉，不产生任何网络调用。
- 其余平台方法（工具 / 知识库 / 审批 / 沙箱 / flow）：统一 `HeadlessUnsupportedError(code="platform_api_unsupported")`。

模型注入：

- `create.params.llm`：`{"base_url", "model", "auth_headers"?, "temperature"?, "max_tokens"?}`；首个 `create` 前执行 `settings.set("LLM_GW_ENDPOINT", base_url)`（进程级，与 REQ-8「由调用方注入」一致；单进程多会话共享同一端点，会话间可不同 `model`）。
- `auth_headers` 原样作为 `ChatBuildExtras.auth_headers` 传入，不落日志。
- checkpointer 默认 `MemorySaver`，可由 `create.params.checkpointer` 覆盖为调用方实现（需满足 `BaseCheckpointSaver`）。
- 事件写出：默认 `NdjsonEventWriter(stdout)`，可由 `create.params.event_sink` 覆盖（如测试用内存收集器）。

### D-8 错误码（REQ-5、REQ-10）

| code | 触发条件 |
| --- | --- |
| `protocol.invalid_json` | 请求行不是合法 JSON |
| `protocol.unknown_method` | `method` 不在五方法集合内 |
| `protocol.invalid_params` | 缺必填字段或类型不符（消息只带字段名） |
| `session.not_found` | `turn` / `resume` / `cancel` / `close` 指向未知会话 |
| `session.closed` | `close` 之后再次请求该会话 |
| `session.busy_cancelled` | 前一轮仍在取消收尾 |
| `agent.build_failed` | 装配抛异常（透传原始异常类型名与消息） |
| `agent.unsupported` | 注入实现不支持该构建 / 恢复类型 |
| `agent.internal_error` | 执行期未归类异常 |
| `resume_payload_missing` / `resume_payload_unsupported` | D-4 的两个校验分支 |
| `platform_api_unsupported` | headless 触达平台 API 的路径 |

任一请求失败后进程必须保持可用（ACC-13）：协议层 `try/except` 包住每条请求，异常一律转成响应，不退出。

### D-9 依赖与日志隔离（REQ-11）

- `headless` 子包只使用 `ag_ui`、`pydantic`、`langgraph.checkpoint.memory` 与标准库，均为现有依赖；`pyproject.toml` 不改。
- 日志字段新增 `agent_factory=<实现类名>`、`headless_session=<session_code>`；不记录 `llm` 段、`auth_headers`、完整请求体（只记 `method` + 字段名列表）。
- 不监听端口，fake 端点仅回环地址。

## 接口与数据

### Python 侧

```python
# services/agent/factory.py
class AgentFactoryError(AIDevException):
    code: str
    reason: str

class AgentFactoryProtocol(Protocol):
    def create(self, **build_params) -> AgentProtocol: ...
    def resume(self, *, session_code: str, resume: Any, **build_params) -> AgentProtocol: ...

class DefaultAgentFactory:
    def create(self, *, agent_code=..., agent_type=..., build_type=..., session_code=None,
               session_context_data=None, agent_cls=None, callbacks=None, auth_headers=None,
               default_headers=None, temperature=None, max_tokens=..., retry_strategy=None,
               switch_agent_by_scene=False, resource_manager=None, is_temporary=False,
               event_handler=None, checkpointer=None, username=None, version=None, **extra) -> AgentProtocol
    def resume(self, *, session_code: str, resume: Any, **build_params) -> AgentProtocol

agent_factory: SingletonFactory[str, AgentFactoryProtocol]
```

`AgentInstanceFactory.build_agent(...)` 签名与默认值不变，方法体委派 `agent_factory().create(...)`。

### headless 请求/响应

| method | params（必填粗体） | result |
| --- | --- | --- |
| `create` | **`session_code`**、**`agent`**（`agent_code`）、`llm`、`username`、`checkpointer`、`event_sink` | `{"session_code", "created": true}` |
| `turn` | **`session_code`**、`input`、`messages` | `{"session_code", "turn_id", "status": "finished|cancelled|error"}` |
| `resume` | **`session_code`**、`input`、`resume` | 同 `turn` |
| `cancel` | **`session_code`** | `{"session_code", "cancelled": true}` |
| `close` | **`session_code`** | `{"session_code", "closed": true}` |

事件行与响应行都带请求 `id`，宿主按 `id` 聚合；事件行先于响应行写出。

### 目录结构

```
src/agent/aidev_agent/headless/
├── __init__.py        # serve()/main() 导出
├── __main__.py        # python -m 入口
├── cli.py             # stdin 循环、请求分发、线程与锁
├── protocol.py        # 请求/响应/事件行模型与序列化
├── errors.py          # 错误码与 HeadlessError
├── session.py         # HeadlessSession / SessionRegistry
├── resource_manager.py# HeadlessResourceManager
└── events.py          # NdjsonEventWriter
```

## 跨仓契约

- dsh 仓：只读参考，零改动。已核对的两条硬约束仍成立——`packages/core/agent-loop/src/index.ts:336` 在插件加载时无条件 `ctx.agents.setFactory(this)`；`packages/core/agent/src/index.ts:383-385` 的 `setFactory` 在已有 factory 时抛 `an agent factory is already registered`。
- 仓外插件（接入方维护，不提交本仓）需满足：
  - 在 dsh 的组合 / profile 层停用 `@deepseek-ai/dsh-agent-loop` 行并启用插件行（不是叠加注册，不改 dsh 源码）；
  - 实现 dsh `AgentFactory.createAgent` / `resume`，在 `createAgent` 时拉起 `python -m aidev_agent.headless` 子进程并用 `create` 建立会话；
  - `send` → `turn`，`resume` → `resume`，`cancel` → `cancel`，`handle.dispose` → `close`；
  - 不支持面（`inbox` 多队列、`steer` / `inject` / `followup` 逐步语义、`runMaintenance`、compaction、子 Agent）必须抛错，不得静默降级；
  - 模型配置不取自 dsh：`create.llm` 指向本机 fake 端点。
- 本仓对外保证：五方法语义、事件为 AG-UI 原对象、错误码表、stdout 只承载协议数据。协议传输层替换（如改 Unix socket）不影响方法语义与事件契约。

## 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| `settings.set("LLM_GW_ENDPOINT", ...)` 是进程级覆盖，多会话共享同一端点 | 单进程内无法给不同会话配不同端点 | 单进程多会话的既定取舍；文档写明「一端点/进程」，端点差异留待后续按会话拆分 |
| `ChatModel.get_num_tokens` 要求 `{base_url}/api/token_check` | fake 端点漏实现该接口时纯文本一轮也会失败，且报错点远离根因 | 验收脚本的 fake 服务必须实现 `/v1/chat/completions` 与 `/v1/api/token_check`；T-12 的完成标准包含该项 |
| `AgentInstanceFactory.build_agent` 由类方法改为委派后，默认值求值时机变化（如 `agent_code=settings.APP_CODE` 在 import 期） | 若 `create` 重新声明默认值，可能与 `build_agent` 的 import 期快照不一致 | `create` 的默认值与 `build_agent` 逐项对齐，并在单测中断言两者对同一入参的 `AgentBuildContext` 全等（ACC-1） |
| 取消依赖默认 `InMemoryQueueMessageHandler` 与 `agent.stop()` | 若宿主环境显式配置了 MQ，取消路径会打到外部；取消也可能迟迟不收敛 | headless 启动时断言消息处理器为内存实现（否则显式失败），`cancel` 设 10s 上限并兜底补发取消终止事件 |
| headless 轮末写账本依赖事件流（`TEXT_MESSAGE_*`） | 事件被 `event_sink` 覆盖后账本可能不完整 | 账本记录挂在 `HeadlessSession` 内部监听器上，与 `event_sink` 解耦；T-6 覆盖自定义 sink 场景 |
| 规格 REQ-4 / ACC-3 本期不做 | 续流仍走隐式路径，`resume()` 在平台侧无调用方（仅 headless 与单测使用），存在「接口先建、切换延后」的空窗 | 已在「规格偏差与待修订」登记，后续变更按 D-4 的 C1 口径接入，无需改协议 |
| dsh 侧工具注册与审批体系未映射 | 「跑通」只覆盖纯文本一轮，真实业务对话仍不可用 | 规格已限定口径；文档的「不支持面」一节明确列出并说明必须抛错 |

## 测试策略

- **等价性（ACC-1、ACC-2）**：现有测试全绿；新增用例断言 `build_agent` 与 `create` 对同一入参产出的 `AgentBuildContext` 字段全等、异常类型与消息一致；以记录型 `AgentFactory` 替换默认实现后，5 处调用入口（用 `AgentBuilder.by_session_code` 与直接 `build_agent` 两条路径代表）全部命中 `create`，且调用方代码 diff 为空。
- **恢复入口（ACC-4 的协议侧）**：`resume` 对空负载、非中断形状负载抛 `AgentFactoryError` 及对应 `code`；替换实现不支持时抛出的异常不被吞掉、不回落默认实现。
- **headless 协议（ACC-5、ACC-13）**：以 `subprocess` 起 `python -m aidev_agent.headless`，逐行喂请求；断言 stdout 只有协议行、每请求恰一条响应、非法 JSON / 未知方法 / 内部异常后进程仍可服务后续请求、`close` 后同会话请求被拒。
- **事件契约（ACC-6）**：fake 模型完成一轮，断言事件类型集合是基线 AG-UI 集合的子集、首尾为 `RUN_STARTED` / `RUN_FINISHED`、含 assistant 文本事件；字段口径与 `EventEncoder` 输出的 SSE payload 逐项比对。
- **无平台（ACC-7、ACC-10）**：子进程内断言 `sys.modules` 无 `django` / `blueapps`；以计数 mock 替换 `HeadlessResourceManager.get_client` 断言零调用；`pyproject.toml` 与 `uv.lock` diff 为空；注入的凭据串不出现在 stderr。
- **会话语义（ACC-8）**：`session_code` 原样回传；`turn.messages` 模式下 fake 模型收到的请求体包含该历史。
- **并发与取消（ACC-9）**：同会话并发两个 `turn` 串行（第二个在第一个结束前不产生事件）；跨会话并发完成；`cancel` 在有界时间内收到取消终止事件。
- **接入验收（ACC-11、ACC-12）**：仓外插件 + fake LLM 在真实 dsh 跑通一轮；停用插件以 dsh 默认组合启动，行为不变。证据留在节点工作区，不入库。
- 定向命令：`make -C src/agent test path=src/agent/tests/<目标文件>`；单个用例追加 `args="-k <expr>"`。

## 规格偏差与待修订

| 规格条目 | 偏差 | 依据 | 处理 |
| --- | --- | --- | --- |
| REQ-4 恢复入口单一化 | 本期不切换任何续流调用点（前端 `views/chat.py:174`、后台 `services/approval_resume.py:156`、企微 `wxaibot/approval_resume.py:165` 与 `wxaibot/question_resume.py:127` 全部保持现状） | 需求方在技术方案确认时指示「续流整块本期不做，作规格偏差延后」 | `resume()` 协议与默认实现按 D-4 交付，供 headless 与后续切换使用；切换动作留到后续变更，届时不需要改协议 |
| ACC-3 后台续流必须调用 `resume` | 本期无对应测试与实现 | 同上 | 移出本期验收；在 tasks.md 的覆盖表中标注「延后」 |
| ACC-4 中「注入实现的 `resume` 抛错不得回落」 | 保留但收敛为协议侧单测（不经平台续流链路） | 同上 | 由 T-3 / T-9 覆盖 |

除上表外，`spec.md` 不做任何修改；实现以本方案为准，验收口径以本方案与 `tasks.md` 为准。

---

# 第二期追加：Agent Hook 点 技术方案

- 变更名: agent-hook（沿用）
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/agent-hook/spec.md（「第二期追加」章节，REQ-13 ~ REQ-20）
- 工作分支: feature/agent-hook（沿用，不新开分支）
- 验收范围: ACC-14 ~ ACC-23

## 背景与目标

规格要求在已交付的 headless 运行面上追补三个 hook 点与一次同步裁决往返，使宿主（dsh 及其 hooks 生态）能在 aidev-agent 驱动的会话里插话：

1. `session_start`：每轮驱动前把宿主给的 `context` 注入本轮模型可见输入（不改账本）。
2. `tool_pre`：工具派发前同步裁决 `allow` / `deny` / `ask`，`deny` 必须早于既有审批与脱敏。
3. `tool_post`：工具本体返回后、净化前裁决 `accept` / `block` / `content` / `context`。
4. 协议补两类行（agent→host 的 hook 请求、host→agent 的答复），复用同一 NDJSON 通道与写锁。
5. `ask` 复用仓内既有 `interrupt()` 原语，并补上 headless 的中断恢复（一期 REQ-9 末句由 REQ-17 取代）。
6. 关闭态（`create` 不带 `hooks`）stdout 行集合逐字节一致，零额外延迟（REQ-19）。

本方案要达成：hook 层全部落在既有接缝上（不改图拓扑、不改 `ExecuteKwargs`、不新增 AG-UI `EventType`、不引新依赖）；hook 的判定执行方留在宿主，本仓只上报点位与载荷、接收裁决。

## 非目标

- 不做 `pre_step` / `turn_stopping` / `system_prompt_assemble` / `subagent_start|stop`（规格「第二期追加 · 非目标」）。
- 不在本仓解析 `hooks.json`、不跑 hook 命令、不实现 matcher 与多份裁决合并、不定义宿主审计格式；本仓不得在无宿主时自行裁决。
- 不承诺 `updatedInput`（改工具参数）、`systemMessage`、`continue:false` 停整轮；本期硬阻断只有 `deny` / `block`。
- 不把 hook 的输入输出回写平台 DB / 会话账本；不新增 AG-UI `EventType`（降级复用 `CUSTOM`）。
- 不改 HTTP 契约、不改平台 `agent_info` 配置键、不改一期已交付的五方法语义。
- 不改造审批链路：`ask` 只复用 `interrupt()` 与中断对象形状，不新造第二套审批通道、不建单。
- 不做 B 档代发（把 hook 请求代发成宿主自身事件）。
- 不给 `hooks` 配置时的行为变化为零（含事件序列、日志、耗时）。

## 现状

一期交付后新核对到的接缝与硬缺口：

| 关注点 | 位置 | 现状结论 |
| --- | --- | --- |
| 工具 wrapper 链 | `core/nodes/tool/node.py:246-284` | 顺序即「外层→内层」：不可信净化 → 结果脱敏 → ITSM 审批 → json_repair → timer → result_limit → 外部 wrappers → `deduplicate`（最内层）。`_chain_tool_call_wrappers` 约定「第一个为最外层」（`:99-138`） |
| 工具节点构建 | `core/graphs/react/graph.py:1063-1104` | `_prepare_agent_tool_node` 把 `langchain_middleware` 的 wrap 方法作为外部 wrappers 传入；自定义 middleware 只能落在内置 wrapper **内侧** |
| 中断原语 | `core/nodes/tool/approval_wrapper.py:162` | `interrupt(value)` 已用于工具审批，`value` 含 `toolCallId` / `reason` / `message`；`default_tool_call_handler` 对 `GraphBubbleUp` 透传，wrapper 抛中断不会被转成错误结果（`node.py:66-70`） |
| 平台续流裁决 | `services/agent/chat.py:914-935`、`packages/interrupt_manager/processor.py:271-356` | `ek.resume` → `get_resume_input` 走 DB 权威：`_aggregate_resume_status` 查记录、未完成分支 `dispatch_interrupts` 建 ITSM 单。headless 走这条必然触达平台，违反 REQ-17 / ACC-22 |
| 中断编排注入点 | `services/agent/chat.py:522-545` | `interrupt_processor` 是惰性字段；`build` 期已有值时不重建 → 可由外层注入替代实现 |
| 中断收尾 | `core/ag_ui/agent.py:405-455` | 流结束由 `_resolve_exit` 调 `dispatch_interrupts` 做 per-reason prepare，再以 `RUN_FINISHED(outcome.type="interrupt", interrupts=[首个活跃])` 收尾 |
| headless 终态判定 | `headless/events.py:142-184` | 只区分 `finished` / `cancelled` / `error`；`RUN_FINISHED` 不看 `outcome`，中断轮会被判成 `finished` |
| headless 恢复现状 | `headless/session.py:153-167`、`:451-457` | `_is_interrupt_payload` 直接抛 `resume_payload_unsupported`（一期明示不支持） |
| 协议分发 | `headless/cli.py:93-99`、`:242-245` | 所有 stdin 行都进 `parse_request`；答复行会被判「method 缺失」。写锁 `self._write_lock` 已由事件 writer 共用 |
| 模型节点模板中间件 | `core/graphs/react/graph.py:840-851` | `ModelNodeSettings.extra_template_middlewares` 已存在，可在 system prompt 装配后追加文本，无需改 `ContextAssembly` |
| 构建期透传 | `services/agent/factory.py:346-417` | `_make_build_context` 按 `agent_type` 组装 `ctx.chat`（`ChatBuildExtras`），`extra` 里的剩余键进 `ctx.extra`；headless 已在用 `channel_type` 走 extra |
| 工具来源 | `services/agent/chat.py:1973-2005`、`headless/resource_manager.py:164-165` | 工具来自 `resource_manager.construct_tool(tool_code)`；headless 实现直接抛 `platform_api_unsupported` → 当前 headless 无任何工具 |

结论：三点都能挂在既有接缝上（两个薄 wrapper + 模型节点模板中间件 + `_drive_turn` 轮次边界），协议改动集中在 `cli.py` 的行分流；真正的技术难点只有两处——(1) 中断恢复的裁决来源必须换成宿主负载，不能再走 DB 门禁；(2) 工具节点的重放语义（LangGraph 恢复会从头重跑节点），`tool_pre` 不能重复上报。

## 方案对比

### A. hook 层挂在哪条链路

| 维度 | A1（选定）core 薄 wrapper + 会话级 `HookBus` | A2 复用既有 `AgentMiddleware` 通道 | A3 headless 侧包住 `execute` 拦截 |
| --- | --- | --- | --- |
| `tool_pre` 位置 | `insert(0)`，在净化 / 审批 / 脱敏之外，满足 REQ-15「deny 早于审批与脱敏」 | 只能落在内置 wrappers 内侧（`graph.py:1073-1078`），`deny` 晚于审批与脱敏，直接违反 REQ-15 | 拿不到工具级时机，只能看事件流，事后无法阻断 |
| `tool_post` 位置 | 最内层（紧贴工具本体），满足「返回后、净化前」 | 同样被内置净化包在外侧，看到的是已被改写的结果 | 同上，不可行 |
| 复用与代价 | 复用既有 wrapper 链与 `interrupt()`；新增两个薄 wrapper（各 ~30 行） | 零新增 wrapper，但语义不达标 | 需要新造一层事件拦截，收益为负 |
| 结论 | **采用** | 排除：位置决定语义，A2 无法满足 REQ-15 / REQ-16 的时序 | 排除 |

### B. `session_start` 的 context 怎么进模型请求

| 维度 | B1（选定）模型节点 system prompt 追加 | B2 本轮 `input` 前缀拼接 | B3 账本插一条 system 记录 |
| --- | --- | --- | --- |
| 模型可见 | 在 user 输入之前（system 段），满足 REQ-14 / ACC-15 | 与 user 原文混在同一条消息里，「位于用户输入之前」只能靠字符串前缀保证 | 在下一条历史里，位置对但会留痕 |
| 账本与落库 | 不触碰账本与 DB（中间件只在装配期生效） | 需要把原文与注入拆开传参，容易污染账本与 `turn_id` 语义 | **污染后续轮次**：该记录会进入之后每一轮的历史 |
| 实现代价 | 复用 `extra_template_middlewares`（`graph.py:840-848`）+ 一个 append 中间件 | 需在 headless 侧拼串并把原文另行落账，且模型请求里 user 文本被改写 | 需给 `HeadlessLedger` 加一次性前缀语义，跨轮误用风险高 |
| 结论 | **采用** | 排除：违背「不改写会话账本中的 user 原文」的精神，且 `input` 同时是账本来源 | 排除：跨轮泄漏 |

### C. `tool_post` 的 `content` / `context` 怎么落地

| 维度 | C1（选定）只改同一个 `ToolMessage` | C2 另加一条独立 system 消息 |
| --- | --- | --- |
| 配对关系 | `tool_call_id` / `tool_use_id` 不变，模型侧仍是「一条调用对一条结果」，满足 REQ-16 末条 | 需要 `Command(update={"messages": [...]})` 追加消息，改变消息序列 |
| 与 Send 分派 / 错误处理的交互 | 只是改写返回的 `ToolMessage`，与 `deduplicate` / `json_repair` / 结果截断无冲突 | 与 `Send` 分派的任务边界、`handle_tool_errors` 的转换路径都要额外核对 |
| 裁决表达 | `block` → `reason` 作为结果；`content` → 整体替换；`context` → 追加在同一结果尾部 | `context` 语义更「干净」，但 `block` / `content` 仍需改原结果 |
| 结论 | **采用** | 排除：收益（消息隔离）换不到风险成本；`context` 只需模型可见，追加在同一结果即可 |

### D. `ask` 中断被 resume 重放时，同一个 `tool_pre` 是否重复上报

| 维度 | D1（选定）会话级 memo 复用裁决 | D2 每次重放都重新上报 |
| --- | --- | --- |
| 宿主观感 | 一个 `tool_use_id` 只收到一次 `tool_pre` 请求 | 恢复轮会再收到一次同样的请求，宿主需自行去重 |
| 语义风险 | memo 命中直接进 `interrupt()` 取恢复值，不存在「第二次 ask 覆盖第一次」 | 若宿主第二次给不同裁决，与 pending 中断不一致 |
| 实现 | `HookBus` 按 `tool_use_id` 记 `ask` 裁决，轮末清理 | 无状态，但把一致性推给宿主 |
| 结论 | **采用** | 排除：REQ-18 要求「同一会话内不重复」的派生 id 可路由，重复上报会让宿主 pending 表出现歧义 |

### E. headless 中断恢复的裁决来源

| 维度 | E1（选定）注入 `HeadlessInterruptProcessor`，裁决来自宿主负载 | E2 复用平台 `get_resume_input` | E3 给 `ExecuteKwargs` 加 headless 专用字段直传 `Command` |
| --- | --- | --- | --- |
| 裁决权威 | 负载本身（`interruptId` + 裁决），校验命中 pending | DB 记录（需 `resource_manager.get_client()`，headless 会抛错） | 负载本身，但绕开既有编排 |
| 平台调用 | 零：`dispatch_interrupts` 与 `get_resume_input` 双入口都不建单、不查 DB | 触达 DB / 建单，违反 REQ-17 与 ACC-22 | 零 |
| 契约影响 | 只注入实现，不改 `ExecuteKwargs` / 协议字段 | 无契约改动但不可用 | 新增 `ExecuteKwargs` 字段，一期方案 D-5 明确不做 |
| 与既有代码的关系 | 继承 `InterruptProcessor`，复用 `build_command_resume` / `_build_resume_map` | 复用最多 | 需在 `chat._stream` 里加分支，回归面最大 |
| 结论 | **采用** | 排除：无平台环境下必然失败 | 排除：破坏「不改 `ExecuteKwargs`」且绕开中断编排 |

## 决策

### D-10 hook 层形态与挂载（REQ-13、REQ-15、REQ-16）

新增 `src/agent/aidev_agent/core/hooks/`（core 层只定义协议与 wrapper，不依赖 headless）：

- `bus.py`：`HookPoint`（`session_start` / `tool_pre` / `tool_post`）、`HookBusProtocol`（`enabled` / `points` / `emit(point, payload) -> dict`），鸭子类型，不做 `isinstance` 校验。
- `wrappers.py`：四个工厂 —— `build_hook_pre_sync_wrapper` / `build_hook_pre_async_wrapper` / `build_hook_post_sync_wrapper` / `build_hook_post_async_wrapper`。

`build_tool_node` 新增可选参数 `hooks`（默认 `None`）：

- `hooks is None` 或该点未启用 → 一个 wrapper 都不插，链与现状逐字一致（REQ-19）。
- `tool_pre`：`sync_wrapper_list.insert(0, ...)` / `async_wrapper_list.insert(0, ...)` —— 位于不可信净化与脱敏之外，先于 ITSM 审批（`node.py:246-284`）。
- `tool_post`：`append` 在 `deduplicate` 之后（最内层，紧贴工具本体）。已知取舍：`knowledge_retrieval` 的批内去重命中时工具本体未执行，`tool_post` 不触发（与「工具本体返回后」的定义一致）。

裁决语义：

| 点 | 裁决 | 落地 |
| --- | --- | --- |
| `tool_pre` | `allow` | `execute(request)` 继续既有链路（审批 + 脱敏不变） |
| `tool_pre` | `deny` | 直接返回 `ToolMessage(status="error", content=reason)`，工具本体零调用，不进入审批与净化 |
| `tool_pre` | `ask` | 调 `interrupt({...})`（见 D-14），工具本体零调用 |
| `tool_post` | `accept` | 原样返回 |
| `tool_post` | `block` | 返回 `ToolMessage(status="error", content=reason)`，原结果不进模型 |
| `tool_post` | `content` | 用该值替换 `content`（保留 `tool_call_id` / `name` / `additional_kwargs`） |
| `tool_post` | `context` | 追加到同一 `ToolMessage.content` 尾部（分隔块，标注为 hook 追加）；若同时给 `content`，先替换再追加 |

异步 wrapper 里的阻塞等待必须下放到线程池（`asyncio.get_running_loop().run_in_executor(None, bus.emit, ...)`），不得在事件循环线程上阻塞。

### D-11 构建期透传（REQ-13）

`hooks` 从 `create` 一路到工具节点，全部走可选字段，缺省零行为变化：

1. `DefaultAgentFactory.create(**build_params)` 已接受 `**extra`；新增三个可选键 `hooks` / `hook_context` / `interrupt_processor`。
2. `_make_build_context` 在 CHAT 分支把它们从 `remaining_extra` 弹出，写入 `ChatBuildExtras` 的同名字段（`registry.py:35-45`）。
3. `ChatCompletionAgent.build` 写入同名实例字段（`hooks` / `hook_context` / `interrupt_processor`），并把 `hooks`、`hook_context` 加进 `agent_cls.get_agent_executor(...)` 的调用（`chat.py:1605-1625`）。
4. `AgentExecutorKwargs` 声明两个可选字段 `hooks` / `hook_context`（`extra="allow"` 已能透传，声明只为可发现性）。
5. `CommonQAAgent.get_agent_executor` 把二者交给 `ReActAgentBuilder.set_hooks(...)` / `set_hook_context(...)`；builder 在 `_prepare_agent_tool_node` 传 `hooks=`，在 `_prepare_agent_model_node` 追加模板中间件（D-12）。

`interrupt_processor` 在 `ChatCompletionAgent.build` 里赋值即可：`execute()` 只在字段为 `None` 时惰性构造（`chat.py:522`），已有值时不覆盖。

### D-12 `session_start`（REQ-14）

- 触发点：`HeadlessSession._drive_turn` 的会话锁内、装配之前（`session.py:171-190`），`turn` 与 `resume` 都触发一次；`session_code` / `turn_id` 此时已就绪。
- 载荷：`{"session_code", "turn_id", "first_turn", "input"}`；`first_turn = 本会话此前没有任何已驱动轮次`（`self._turn_count == 0`，计数在轮次结束时自增）。
- 注入：`context` 非空时写入 `hook_context`，随本轮 `create(...)` 下发；builder 追加一个模板中间件 `ctx.prompt_slots.system += "\n\n" + context`（复用 `ModelNodeSettings.extra_template_middlewares`，`graph.py:840-848`）。headless 每轮重建实例（`session.py:320-324`），因此注入天然只作用于本轮。
- 边界：不改账本、不改 `turn_id`、不改落库内容（headless 本就无落库）；空 `context` / 降级时本轮照常执行（fail-open）。

### D-13 `tool_pre`（REQ-15）

载荷固定三字段：`tool_name`（`request.tool_call["name"]`）、`tool_input`（`args`）、`tool_use_id`（`id`）。不携带 `state` / 工具对象等内部结构。

`deny` 走早退返回，不调 `execute`，因此不会进入审批与脱敏；`ask` 见 D-14。`updatedInput` 本期不承诺，`tool_input` 原样透传。

### D-14 `ask` 与 headless 中断恢复（REQ-17）

`ask` 在 wrapper 内直接调 `interrupt()`（与审批同一原语），中断值形状：

```json
{"reason": "hook_ask", "message": "<宿主 reason>", "toolCallId": "<tool_use_id>", "id": "<可选>"}
```

`interrupt_id` 由 LangGraph 生成，经 `_normalize_interrupt_value`（`core/ag_ui/agent.py:354-402`）归一为 AG-UI `Interrupt`，`toolCallId` 落在 `RUN_FINISHED.outcome.interrupts[0]`，满足 ACC-18。

重放去重（D1）：`HookBus` 维护 `ask_memo: dict[tool_use_id, decision]`；重放时同一 `tool_use_id` 直接复用上次 `ask` 裁决、不再发请求，随即进入 `interrupt()` 取恢复值。memo 在轮次结束（含中断、取消、异常）时清空。

恢复链路：

1. `resume` 负载形状 `{"interruptId": "...", "toolCallId": "可选", "decision": "allow|deny", "reason": "可选"}`，允许单元素列表。
2. `HeadlessSession` 用上一轮 `RUN_FINISHED(outcome=interrupt)` 缓存下来的 pending 中断校验负载：`interruptId` 必须命中；携带 `toolCallId` 时一并校验。不命中 / 形状不合法 → `HeadlessError(RESUME_INTERRUPT_NOT_FOUND / RESUME_PAYLOAD_UNSUPPORTED)`，不改图状态、不放行其它 pending（ACC-21）。
3. 校验通过后 `create/resume` 注入 `HeadlessInterruptProcessor`，并把负载放进 `ExecuteKwargs.resume`；`chat._stream` 的 `forwarded_props.command.resume` → `get_resume_input` 走我们的实现。
4. `HeadlessInterruptProcessor` 两个入口都去平台化：
   - `get_resume_input(tasks, ...)`：只读 `tasks` 的 pending 中断；命中校验通过后构造 `Command(resume=<裁决>)`（单 pending 直接传取值，多 pending 复用 `_build_resume_map` 只指向命中的那个），返回 `ResumeInputResult(ready=True, command=...)`；不查 DB、不调 `dispatch_interrupts`。
   - `dispatch_interrupts(tasks, ctx)`：只把 pending 归一为 `InterruptOutcome(status="built")`，**不 prepare、不建单、不 enrich**。
5. 恢复轮以 `finished` 收尾；裁决为 `deny` 时工具本体零调用，模型收到拒绝说明；裁决不合法（缺 `decision` 或取值未知）按 `deny` 处理并记 warning（宁可不执行，也不误放行）。
6. 恢复后 `ask_memo` 与 pending 缓存清空；同一会话可继续 `turn`。若存在未恢复的 pending 中断时收到 `turn`，返回结构化错误 `session.pending_interrupt`（不拉图，避免 LangGraph 以 pending 状态启动失败）。

### D-15 协议两类新行与分发（REQ-18）

- agent→host 请求行：`{"id": "<本轮请求 id>#h<n>", "type": "request", "method": "hook", "params": {"session_code", "turn_id", "point", "payload"}}`；`n` 为会话级自 1 递增计数，不随请求重置。
- host→agent 答复行：`{"id": "...", "ok": true, "result": {...}}` 或 `{"id": "...", "ok": false, "error": {"code", "message"}}`。
- 分流：`Dispatcher.handle_line` 先判 `"ok" in payload and "method" not in payload` → 投递 `HookPendingRouter`（`threading.Event` 唤醒等待方）；其余仍走 `parse_request`。答复行不产出响应行、不报未知方法（ACC-19）。
- 写锁：hook 请求行与事件行共用 `Dispatcher._write_lock`（`cli.py:242-245`），不得交错。
- 取消：等待期间以 50 ms 粒度巡检会话取消标志；命中即放弃等待并返回 `allow` / `accept`，由既有取消收尾产出取消终止事件（早于 `timeout_ms`，ACC-20）。

### D-16 降级与可观测（REQ-18、REQ-19）

fail-open 规则与降级原因（`hook_degraded` 的 `value.reason`）：

| 情形 | 该点取值 | 降级原因 |
| --- | --- | --- |
| 超过 `timeout_ms` | 最宽松裁决 | `timeout` |
| 宿主 `ok=false` | 最宽松裁决 | `host_error` |
| `result` 形状不合法 | 最宽松裁决 | `invalid_shape` |
| `decision` 取值未知 | 最宽松裁决 | `unknown_decision` |
| 等待期间被 `cancel` | 最宽松裁决（不写 `hook_degraded`） | — |

降级事件写一条既有 `CUSTOM` 事件（`name="hook_degraded"`，`value={"point", "reason"}`），经 `SessionEventWriter.emit` 走同一通道与写锁；不新增 `EventType`。日志只记点位、裁决取值、耗时与降级原因，不记载荷全文。

### D-17 错误码扩充（REQ-5、REQ-17）

| code | 触发条件 |
| --- | --- |
| `resume_interrupt_not_found` | `resume` 负载的 `interruptId` 未命中该会话 pending 中断 |
| `session.pending_interrupt` | 存在未恢复 pending 中断时收到 `turn` |
| `hook.value_invalid` | `create.params.hooks` 形状不合法（非对象 / 超时非正整数 / 未知点位） |

既有 `resume_payload_unsupported` 语义收窄为「负载不是中断恢复形状」，一期文档中「headless 不支持中断恢复」的表述由本方案取代。

### D-18 关闭态零回归（REQ-19）

`create` 不带 `hooks` 时：`ctx.chat.hooks is None` → builder 不设 `_hooks` → `build_tool_node(hooks=None)` 不插 wrapper → 不设 `_hook_context` → 不追加模板中间件 → `_drive_turn` 不发 `session_start`。stdout 行集合与一期逐字节一致，无新增等待与日志。

## 接口与数据

### `create.params.hooks`

```json
{"timeout_ms": 10000, "points": ["session_start", "tool_pre", "tool_post"]}
```

- `timeout_ms`：单点等待上限，缺省 10000，正整数，超上限按 1 小时钳制。
- `points`：缺省三点全开；未知点位 → `hook.value_invalid`。
- 该字段只在 headless `create` 上生效；平台路径不传即不启用。

### 三个点的载荷与裁决

| 点 | 载荷 | 裁决 |
| --- | --- | --- |
| `session_start` | `session_code`、`turn_id`、`first_turn`、`input` | `{"context": "<str>"}`（空或缺省 = 不注入） |
| `tool_pre` | `tool_name`、`tool_input`、`tool_use_id` | `{"decision": "allow"|"deny"|"ask", "reason": "..."}` |
| `tool_post` | `tool_name`、`tool_input`、`tool_use_id`、`tool_response` | `{"decision": "accept"|"block", "reason": "...", "content": "...", "context": "..."}` |

### headless 响应

- `turn` / `resume` 的 `result.status` 取值集合由 `finished|cancelled|error` 扩展为 `finished|interrupt|cancelled|error`。
- 中断轮：`ok=true`，`result = {"session_code", "turn_id", "status": "interrupt", "interrupts": [<AG-UI Interrupt>]}`；事件侧仍以 `RUN_FINISHED(outcome=interrupt)` 收尾，不新增事件类型。
- 中断轮同样落账（保留已产出的 assistant 文本），避免下一轮丢上下文；`user` 原文不动。

### 目录结构

```text
src/agent/aidev_agent/core/hooks/
├── __init__.py        # HookPoint / HookBusProtocol / wrapper 工厂导出
├── bus.py             # 点枚举与总线协议（无 headless 依赖）
└── wrappers.py        # tool_pre / tool_post 的同步与异步 wrapper

src/agent/aidev_agent/headless/
├── hooks.py           # HookConfig / HookChannel / HookPendingRouter / HeadlessHookBus
└── interrupts.py      # HeadlessInterruptProcessor（负载裁决，不触达平台）
```

## 跨仓契约

- 宿主（dsh）零改动；仓外 A 档插件（读 `hooks.json`、跑命令、合并裁决、写审计）继续由接入方维护，仅作 ACC-23 证据。
- 本仓对宿主的承诺：两类新行与 `id` 派生规则；三个点的载荷字段与裁决取值；`ask` 后必须回 `resume` 负载（`interruptId` 取 `RUN_FINISHED.outcome.interrupts[0].id`）；降级语义（`hook_degraded`）与 `timeout_ms` 生效方式。
- 本仓不承诺：`updatedInput`、`systemMessage`、`continue:false`、B 档代发。

## 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| LangGraph 恢复会重放整个工具节点 | 同一 `tool_pre` 可能被二次上报，或 `ask` 二次裁决 | D1 的会话级 memo + 轮末清理；用例覆盖「重放不重复上报」 |
| 多 pending 中断（一轮多个 `ask`） | 一次 `resume` 只能恢复一个，其余仍是 pending | 逐次恢复；未恢复时 `turn` 报 `session.pending_interrupt`；用例覆盖两次串行恢复 |
| 异步 wrapper 内阻塞等待可能卡住事件循环 | 模型流停摆、`cancel` 不收敛 | 等待下放线程池 + 50 ms 粒度巡检取消标志；ACC-20 用例断言早于超时收尾 |
| headless 无工具 | ACC-16 / ACC-17 无法端到端取证 | 新增 `create.params.tools`（进程内注入，stdio 下不可用）作为测试/嵌入式接缝，用例注入 fake 工具；文档写明该字段不入协议承诺 |
| `tool_post` 在 `knowledge_retrieval` 批内去重命中时不触发 | 与「每次工具返回都触发」的朴素预期不同 | 属「工具本体未执行」的合理语义，文档写明；用例以普通工具判定 |
| system prompt 追加会占用上下文预算 | 超长 context 可能挤压历史 | 由宿主负责上下文预算；本仓只做追加，不截断，文档写明 |
| 宿主不答复时依赖 `timeout_ms` 放行 | 单点最长阻塞 10 s，重工具链上放大 | 超时只降级不阻断；`timeout_ms` 可由宿主收紧 |
| ACC-23 依赖仓外插件 | 本仓无法独立取证 | 按规格「测试约束」记录为未取证，不影响 ACC-14 ~ ACC-22 判定 |

## 测试策略

- **关闭态零回归（ACC-14）**：同一脚本分别跑「不带 hooks」与一期基线（手工比对的 stdout 落盘文件），断言行集合逐字节一致、无 `type=request`、无 `hook_degraded`。
- **wrapper 顺序（ACC-16、ACC-17）**：直接对 `build_tool_node` 构造图，断言 `tool_pre` 的 `deny` 在审批 wrapper 之前（用带 `approval` metadata 的 fake 工具，断言审批未被调用）、`tool_post` 看到的是工具原值（断言脱敏 wrapper 之后才生效）。
- **三点语义（ACC-15 ~ ACC-17）**：fake 宿主固定答复，fake 模型断言收到的请求体：`context` 位于 user 之前、`deny` 后工具调用次数为 0、`block` / `content` / `context` 三种裁定的模型可见结果。
- **协议与降级（ACC-19、ACC-20）**：子进程级用例断言 `#h1` 派生、答复行不产出响应行、不报未知方法、超时后恰好一条 `hook_degraded`、等待期间 `cancel` 早于超时收尾。
- **中断恢复（ACC-18、ACC-21）**：`ask` → `status=interrupt` → `resume` 放行（工具执行一次、结果回模型）/ 拒绝（零调用、模型收到拒绝说明）；错误 `interruptId` 返回 `ok=false` 且随后用正确 id 仍可恢复。
- **无平台（ACC-22）**：完整走一次 `ask → 中断 → resume`，断言进程内无 `django`、`HeadlessResourceManager.get_client` 调用为 0、无单据创建调用。
- **接入证据（ACC-23）**：仓外 A 档插件 + `hooks.json` 在真实 dsh 跑一轮，记录命令与日志；缺插件时记录「未取证」。
- 定向命令：`make -C src/agent test path=tests/<目标文件>`，单用例追加 `args="-k <expr>"`。

## 需求覆盖

| 需求 | 决策 |
| --- | --- |
| REQ-13 启用方式与执行归属 | D-10、D-11、D-17 |
| REQ-14 `session_start` | D-12 |
| REQ-15 `tool_pre` | D-10、D-13 |
| REQ-16 `tool_post` | D-10 |
| REQ-17 `ask` 与 headless 中断恢复 | D-14、D-17 |
| REQ-18 协议 hook 往返 | D-15、D-16 |
| REQ-19 关闭态零回归与凭据约束 | D-16、D-18 |
| REQ-20 接入文档 hooks 一节 | 见 tasks.md T-22 |
