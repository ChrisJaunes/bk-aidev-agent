# Agent Hook 系统 任务清单

- 变更名: agent-hook
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/agent-hook/spec.md
- 方案: specs/agent-hook/plan.md（REQ-4 / ACC-3 延后，见方案「规格偏差与待修订」）

## 需求覆盖

| 需求 | 任务 |
| --- | --- |
| REQ-1 注入点协议与注册中心 | T-1、T-3 |
| REQ-2 默认实现与现状等价 | T-1、T-2、T-13 |
| REQ-3 创建入口单一化 | T-1、T-2、T-6 |
| REQ-4 恢复入口单一化 | **延后**（方案「规格偏差与待修订」） |
| REQ-5 不支持能力必须显式失败 | T-3、T-9 |
| REQ-6 headless 调用形态 | T-5、T-6、T-7 |
| REQ-7 headless 事件契约 | T-6 |
| REQ-8 无平台依赖模式 | T-4、T-10 |
| REQ-9 会话语义 | T-6、T-7 |
| REQ-10 生命周期与并发 | T-8、T-9 |
| REQ-11 依赖与隔离 | T-4、T-10、T-13 |
| REQ-12 接入文档 | T-11、T-12 |

验收对应：ACC-1→T-2/T-13，ACC-2→T-2，ACC-3→**延后**，ACC-4→T-3/T-9，ACC-5→T-5/T-7，ACC-6→T-6，ACC-7→T-10，ACC-8→T-6/T-7，ACC-9→T-8，ACC-10→T-10/T-13，ACC-11→T-12，ACC-12→T-12，ACC-13→T-9。

## T-1 新增 AgentFactory 协议、默认实现与注册中心

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/services/agent/factory.py`、`src/agent/aidev_agent/exceptions.py`、`src/agent/tests/services/agent/test_agent_factory_registry.py`
- 需求: REQ-1、REQ-2、REQ-3
- 依赖: 无
- 验证: `make -C src/agent test path=tests/services/agent/test_agent_factory_registry.py`
- [ ] 完成标准：`factory.py` 内新增 `AgentFactoryProtocol`（`create` / `resume` 两个方法，入参语义与 `build_agent` 一致）、`DefaultAgentFactory`（承载原 `build_agent` 方法体，装配链路与异常语义逐字不变）与 `agent_factory: SingletonFactory[str, AgentFactoryProtocol] = SingletonFactory("agent_factory", DefaultAgentFactory())`；`_FACTORY_TOKEN` 闸口保留，`AgentInstanceFactory()` 直接构造仍抛原 `RuntimeError`；`AgentFactoryError` 进 `exceptions.py`，带 `code` / `reason`；用例断言未替换时 `agent_factory()` 返回 `DefaultAgentFactory`、`replace_defaults` 后可替换并返回旧值、`build_agent` 与 `create` 对同一入参产出的 `AgentBuildContext` 字段全等。

## T-2 创建入口委派与 5 处调用方零改动校验

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/services/agent/factory.py`、`src/agent/tests/services/agent/test_agent_factory_equivalence.py`
- 需求: REQ-2、REQ-3
- 依赖: T-1
- 验证: `make -C src/agent test path=tests/services/agent/test_agent_factory_equivalence.py`
- [ ] 完成标准：`AgentInstanceFactory.build_agent` 保留原签名与默认值（含 `agent_code=settings.APP_CODE`、`max_tokens=settings.MAX_TOKENS`），方法体仅剩 `return agent_factory().create(...)` 全量透传；记录型 `AgentFactory` 替换默认实现后，`services/agent_builder.py` 的 `by_session_code` 路径与直接 `build_agent`（CHAT 与 FLOW 各一例）全部命中 `create`，且 `git diff` 对这 5 处调用文件仅包含 factory 内部改动；`build_type` / `agent_type` 非法时的异常类型与消息与基线一致。

## T-3 resume 协议与负载显式失败

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/services/agent/factory.py`、`src/agent/tests/services/agent/test_agent_factory_resume.py`
- 需求: REQ-1、REQ-5
- 依赖: T-1
- 验证: `make -C src/agent test path=tests/services/agent/test_agent_factory_resume.py`
- [ ] 完成标准：`DefaultAgentFactory.resume` 先归一化负载（`dict` → 单元素列表），空值抛 `AgentFactoryError(code="resume_payload_missing")`，非中断形状抛 `code="resume_payload_unsupported"`（消息只含字段名与类型，不含取值）；校验通过后走与 `create` 共用的私有 `_build`，返回满足 `AgentProtocol` 的实例；不读写 `ExecuteKwargs`；替换实现抛出的异常原样外抛，不回落默认实现；用例覆盖两条错误分支与一条成功分支。

## T-4 HeadlessResourceManager

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/resource_manager.py`、`src/agent/tests/headless/test_resource_manager.py`
- 需求: REQ-8、REQ-11
- 依赖: 无
- 验证: `make -C src/agent test path=tests/headless/test_resource_manager.py`
- [ ] 完成标准：实现 `ResourceManagerProtocol` 全部方法；`get_agent_config` / `retrieve_agent_config` 由注入参数合成 `AgentConfig`（`agent_code`、`agent_name=agent_code`、`chat_model`、`non_thinking_llm=chat_model`，工具与知识库为空）；`get_chat_session_contents` / `get_chat_session_context` 返回内存账本副本；`publish_event` 为 no-op 且 `event_publishing_enabled()` 返回 `False`；`get_client` 与其余平台方法抛 `HeadlessUnsupportedError(code="platform_api_unsupported")`；用例断言任一平台方法被调用时抛错且无网络请求（以 `httpx`/`requests` mock 断言零调用）。

## T-5 NDJSON 协议层与入口骨架

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/__init__.py`、`__main__.py`、`cli.py`、`protocol.py`、`errors.py`、`src/agent/tests/headless/test_protocol.py`
- 需求: REQ-6、REQ-11
- 依赖: T-4
- 验证: `make -C src/agent test path=tests/headless/test_protocol.py`
- [ ] 完成标准：`python -m aidev_agent.headless` 可启动并在 stdin EOF 时退出且返回码 0；请求 / 事件 / 响应三种行模型与序列化就位（事件行在前、响应行在后，均带请求 `id`）；stdout 只写协议数据，root logger 与全部 handler 指向 stderr；`protocol.invalid_json` / `protocol.unknown_method` / `protocol.invalid_params` 三个错误码可用且每条请求恰好一条响应；子进程用例断言 stdout 无日志文本、stderr 无协议行。

## T-6 create / turn 与会话驱动

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/session.py`、`events.py`、`cli.py`、`src/agent/tests/headless/test_turn.py`
- 需求: REQ-3、REQ-6、REQ-7、REQ-9
- 依赖: T-5、T-1
- 验证: `make -C src/agent test path=tests/headless/test_turn.py`
- [ ] 完成标准：`create` 经 `agent_factory().create(...)` 建立会话并回显 `session_code`；`turn` 组装 `ExecuteKwargs(stream=True, thread_id=session_code, turn_id=<uuid>, input=...)` 后执行，`NdjsonEventWriter` 把 `ag_ui.core` 事件以 `model_dump(mode="json", by_alias=True)` 逐行写出；一轮事件以 `RUN_STARTED` 起始、`RUN_FINISHED` 收尾且类型集合不超出基线 AG-UI 集合；`turn.messages` 非空时走 `build_type=DIRECT`（fake 模型收到的请求体包含该历史）且不写账本；默认模式下轮末账本包含本轮 user 与 assistant 文本，自定义 `event_sink` 时账本仍完整；用例用 fake 模型，全程离线。

## T-7 resume / close 与会话生命周期

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/session.py`、`cli.py`、`src/agent/tests/headless/test_lifecycle.py`
- 需求: REQ-6、REQ-9
- 依赖: T-6
- 验证: `make -C src/agent test path=tests/headless/test_lifecycle.py`
- [ ] 完成标准：`resume` 经 `agent_factory().resume(...)` 在同一 `session_code` 与 `thread_id` 续轮并返回本轮响应；审批 / `ask_user_question` 形状的负载返回 `ok=false` + `resume_payload_unsupported`，并在事件流末尾补发 `RUN_ERROR`；`close` 释放实例、checkpointer、账本与线程句柄，之后同会话任意请求返回 `session_closed`，其他会话不受影响；`session_code` 在所有响应中原样回传。

## T-8 并发、串行与取消

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/session.py`、`cli.py`、`src/agent/tests/headless/test_concurrency.py`
- 需求: REQ-10
- 依赖: T-7
- 验证: `make -C src/agent test path=tests/headless/test_concurrency.py`
- [ ] 完成标准：同会话并发两个 `turn` 时第二个在第一个终止事件前不产出任何事件（会话锁串行）；两个不同 `session_code` 的 `turn` 可同时处于执行中（工作线程并发）；`cancel` 在 10s 内产出 `RunErrorEvent(message=RunId.CANCELLED_MESSAGE)` + `RunFinishedEvent(run_id=RunId.CANCELLED)` 并把响应 `status` 置为 `cancelled`；启动时若消息处理器不是内存实现则显式失败退出（错误信息含处理器类型）。

## T-9 错误处理与进程可用性

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/cli.py`、`protocol.py`、`src/agent/tests/headless/test_error_recovery.py`
- 需求: REQ-5、REQ-10
- 依赖: T-7
- 验证: `make -C src/agent test path=tests/headless/test_error_recovery.py`
- [ ] 完成标准：非法 JSON、未知方法、未知会话、`close` 后请求、装配异常（`agent.build_failed`）、执行期未归类异常（`agent.internal_error`）六类输入均返回结构化错误并写出 `RUN_ERROR`，进程在同一条连接上继续服务后续合法请求；错误响应不含完整请求体与凭据；用例以单进程连续发 6 类坏请求 + 1 条合法 `turn`，断言末尾合法请求正常完成。

## T-10 无平台依赖与隔离验证

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/headless/test_no_platform.py`
- 需求: REQ-8、REQ-11
- 依赖: T-7
- 验证: `make -C src/agent test path=tests/headless/test_no_platform.py`
- [ ] 完成标准：子进程完成一轮纯文本对话后断言 `sys.modules` 中不存在 `django` / `blueapps`；以计数桩替换 `HeadlessResourceManager.get_client` 断言平台调用为 0；token 上报与 span 上报路径未产出任何对外请求；注入的 `auth_headers` 值不出现在 stdout / stderr；`git diff --stat` 对 `src/agent/pyproject.toml` 与 `uv.lock` 为空，且依赖清单中无 dsh 相关包。

## T-11 接入文档

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `docs/agent/HEADLESS_HOST.md`、`docs/agent/readme.md`（新增索引条目）
- 需求: REQ-12
- 依赖: T-7
- 验证: 手工评审依据：文档章节自检清单（组合配置步骤 / 方法映射 / 不支持面 / fake 模型验收步骤 / 日志与安全 五节齐备），且全文无 dsh 源码片段
- [ ] 完成标准：说明宿主如何以组合 / profile 配置停用 `@deepseek-ai/dsh-agent-loop` 并启用仓外插件（含「重复注册会抛 `an agent factory is already registered`」的原因）、五方法映射与 NDJSON 报文样例、支持面（`create` / `turn` / `resume` / `cancel` / `close`）与不支持面（`inbox` 多队列、`steer` / `inject` / `followup` 逐步语义、`runMaintenance`、compaction、子 Agent，且必须抛错不得降级）、用本机 fake 模型（含 `/v1/api/token_check`）完成验收的步骤、日志与凭据约束；索引条目指向新文档。

## T-12 仓外集成验收证据（dsh 插件 + fake LLM）

- 仓库: 本项目（证据留在节点工作区 `acceptance/`，不提交本仓、不发布）
- 文件: `acceptance/fake_llm_server.py`、`acceptance/dsh-plugin/`、`acceptance/README.md`
- 需求: REQ-7、REQ-12
- 依赖: T-10、T-11
- 验证: 手工依据：`acceptance/README.md` 的复现步骤逐条执行，命令与期望输出记录在案
- [ ] 完成标准：fake 服务同时实现 `/v1/chat/completions` 与 `/v1/api/token_check` 并在回环地址启动；仓外插件按 T-11 文档接入真实 dsh 后能完成一轮「用户输入 → assistant 回复」，dsh 侧日志证明默认 Agent 实现未参与；停用插件、以 dsh 默认组合启动时 dsh 行为不变；记录两轮运行的命令、关键日志与结论，作为 ACC-11 / ACC-12 证据。

## T-13 回归与依赖封板

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: 无新增文件（回归执行与结论记录）
- 需求: REQ-2、REQ-11
- 依赖: T-2、T-10、T-12
- 验证: `make -C src/agent test path=tests/services`、`make -C src/agent test path=tests/core`、`make -C src/agent test path=tests/headless`
- [ ] 完成标准：`tests/services`、`tests/core`、`tests/headless` 三组全绿且无新增 skip；`src/agent/pyproject.toml` 与 `uv.lock` 无 diff；AG-UI 事件类型集合未新增（以基线事件名清单比对）；会话落库相关用例（`tests/core/interrupt/`、`tests/services/test_resume_events.py`）保持通过，证明未启用 headless 时进程行为不变。
