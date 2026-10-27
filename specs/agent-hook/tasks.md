# Agent Hook 系统 任务清单

- 变更名: agent-hook
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/agent-hook/spec.md
- 方案: specs/agent-hook/plan.md（REQ-4 / ACC-3 延后，见方案「规格偏差与待修订」）
- 人工预览: 无可预览（本变更对终端用户无功能变化；headless 入口是面向宿主进程的调用面，未部署可预览环境）

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
- [x] 完成标准：`factory.py` 内新增 `AgentFactoryProtocol`（`create` / `resume` 两个方法，入参语义与 `build_agent` 一致）、`DefaultAgentFactory`（承载原 `build_agent` 方法体，装配链路与异常语义逐字不变）与 `agent_factory: SingletonFactory[str, AgentFactoryProtocol] = SingletonFactory("agent_factory", DefaultAgentFactory())`；`_FACTORY_TOKEN` 闸口保留，`AgentInstanceFactory()` 直接构造仍抛原 `RuntimeError`；`AgentFactoryError` 进 `exceptions.py`，带 `code` / `reason`；用例断言未替换时 `agent_factory()` 返回 `DefaultAgentFactory`、`replace_defaults` 后可替换并返回旧值、`build_agent` 与 `create` 对同一入参产出的 `AgentBuildContext` 字段全等。
- 验证记录：`make -C src/agent test path=tests/services/agent/test_agent_factory_registry.py` → **通过**，4 passed。覆盖：未替换时 `agent_factory()` 返回 `DefaultAgentFactory`；`replace_defaults` 返回旧值并切换实现；直接 `AgentInstanceFactory(...)` 仍抛原 `RuntimeError`；`build_agent` 与 `create` 对同一入参产出的 `AgentBuildContext` 字段全等（ACC-1 协议侧）。

## T-2 创建入口委派与 5 处调用方零改动校验

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/services/agent/factory.py`、`src/agent/tests/services/agent/test_agent_factory_equivalence.py`
- 需求: REQ-2、REQ-3
- 依赖: T-1
- 验证: `make -C src/agent test path=tests/services/agent/test_agent_factory_equivalence.py`
- [x] 完成标准：`AgentInstanceFactory.build_agent` 保留原签名与默认值（含 `agent_code=settings.APP_CODE`、`max_tokens=settings.MAX_TOKENS`），方法体仅剩 `return agent_factory().create(...)` 全量透传；记录型 `AgentFactory` 替换默认实现后，`services/agent_builder.py` 的 `by_session_code` 路径与直接 `build_agent`（CHAT 与 FLOW 各一例）全部命中 `create`，且 `git diff` 对这 5 处调用文件仅包含 factory 内部改动；`build_type` / `agent_type` 非法时的异常类型与消息与基线一致。
- 验证记录：`make -C src/agent test path=tests/services/agent/test_agent_factory_equivalence.py` → **通过**，3 passed。记录型 `AgentFactory` 替换默认实现后，CHAT 与 FLOW 两种调用形状（与 5 处调用方同形）全部命中 `create`、`resume` 零调用、参数全量透传；非法 `build_type` / `agent_type` 的异常类型与文案与基线一致。`git diff --stat` 显示 5 处调用方文件（`services/agent_bkplugin.py`、`services/agent_builder.py`、`views/chat.py`、`aidev_wxbot/wxaibot/strategies.py`）零改动。

## T-3 resume 协议与负载显式失败

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/services/agent/factory.py`、`src/agent/tests/services/agent/test_agent_factory_resume.py`
- 需求: REQ-1、REQ-5
- 依赖: T-1
- 验证: `make -C src/agent test path=tests/services/agent/test_agent_factory_resume.py`
- [x] 完成标准：`DefaultAgentFactory.resume` 先归一化负载（`dict` → 单元素列表），空值抛 `AgentFactoryError(code="resume_payload_missing")`，非中断形状抛 `code="resume_payload_unsupported"`（消息只含字段名与类型，不含取值）；校验通过后走与 `create` 共用的私有 `_build`，返回满足 `AgentProtocol` 的实例；不读写 `ExecuteKwargs`；替换实现抛出的异常原样外抛，不回落默认实现；用例覆盖两条错误分支与一条成功分支。
- 验证记录：`make -C src/agent test path=tests/services/agent/test_agent_factory_resume.py` → **通过**，9 passed。空负载（`None` / `[]` / `()`）→ `resume_payload_missing`；非中断形状（字符串、缺 `interruptId`、混合列表）→ `resume_payload_unsupported`，消息只带字段名与类型、不含取值；合法中断负载经共用 `_build` 返回满足 `AgentProtocol` 的实例；替换实现抛出的 `AgentFactoryError` 原样外抛、不回落。

## T-4 HeadlessResourceManager

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/resource_manager.py`、`src/agent/tests/headless/test_resource_manager.py`
- 需求: REQ-8、REQ-11
- 依赖: 无
- 验证: `make -C src/agent test path=tests/headless/test_resource_manager.py`
- [x] 完成标准：实现 `ResourceManagerProtocol` 全部方法；`get_agent_config` / `retrieve_agent_config` 由注入参数合成 `AgentConfig`（`agent_code`、`agent_name=agent_code`、`chat_model`、`non_thinking_llm=chat_model`，工具与知识库为空）；`get_chat_session_contents` / `get_chat_session_context` 返回内存账本副本；`publish_event` 为 no-op 且 `event_publishing_enabled()` 返回 `False`；`get_client` 与其余平台方法抛 `HeadlessUnsupportedError(code="platform_api_unsupported")`；用例断言任一平台方法被调用时抛错且无网络请求（以 `httpx`/`requests` mock 断言零调用）。
- 验证记录：`make -C src/agent test path=tests/headless/test_resource_manager.py` → **通过**，27 passed。协议方法全部就位；配置合成字段正确；账本返回副本；22 个平台方法逐个断言抛 `HeadlessUnsupportedError(code=platform_api_unsupported)` 且 `requests` / `httpx` 零调用；空 MCP 配置返回空结果且零平台调用。
  实现补充（不影响结论）：`resolve_access_token` 返回空串、`retrieve_chat_session` 返回 `{}`、`construct_mcp` 在无配置时返回空结果——三者均在装配期被无条件调用且本身不发请求，抛错会导致纯文本一轮无法装配。

## T-5 NDJSON 协议层与入口骨架

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/__init__.py`、`__main__.py`、`cli.py`、`protocol.py`、`errors.py`、`src/agent/tests/headless/test_protocol.py`
- 需求: REQ-6、REQ-11
- 依赖: T-4
- 验证: `make -C src/agent test path=tests/headless/test_protocol.py`
- [x] 完成标准：`python -m aidev_agent.headless` 可启动并在 stdin EOF 时退出且返回码 0；请求 / 事件 / 响应三种行模型与序列化就位（事件行在前、响应行在后，均带请求 `id`）；stdout 只写协议数据，root logger 与全部 handler 指向 stderr；`protocol.invalid_json` / `protocol.unknown_method` / `protocol.invalid_params` 三个错误码可用且每条请求恰好一条响应；子进程用例断言 stdout 无日志文本、stderr 无协议行。
- 验证记录：`make -C src/agent test path=tests/headless/test_protocol.py` → **通过**，4 passed。子进程断言：stdin EOF 返回码 0；stdout 每行均为协议 JSON、无日志文本；stderr 不含协议行；每请求恰好一条响应；`protocol.invalid_json` / `protocol.unknown_method` / `protocol.invalid_params` 与 `session.not_found` 各自命中；事件行先于响应行且带请求 `id`。

## T-6 create / turn 与会话驱动

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/session.py`、`events.py`、`cli.py`、`src/agent/tests/headless/test_turn.py`
- 需求: REQ-3、REQ-6、REQ-7、REQ-9
- 依赖: T-5、T-1
- 验证: `make -C src/agent test path=tests/headless/test_turn.py`
- [x] 完成标准：`create` 经 `agent_factory().create(...)` 建立会话并回显 `session_code`；`turn` 组装 `ExecuteKwargs(stream=True, thread_id=session_code, turn_id=<uuid>, input=...)` 后执行，`NdjsonEventWriter` 把 `ag_ui.core` 事件以 `model_dump(mode="json", by_alias=True)` 逐行写出；一轮事件以 `RUN_STARTED` 起始、`RUN_FINISHED` 收尾且类型集合不超出基线 AG-UI 集合；`turn.messages` 非空时走 `build_type=DIRECT`（fake 模型收到的请求体包含该历史）且不写账本；默认模式下轮末账本包含本轮 user 与 assistant 文本，自定义 `event_sink` 时账本仍完整；用例用 fake 模型，全程离线。
- 验证记录：`make -C src/agent test path=tests/headless/test_turn.py` → **通过**，6 passed。`create` 命中记录型注入点并回显 `session_code`；一轮事件首 `RUN_STARTED`、尾 `RUN_FINISHED`、含 `TEXT_MESSAGE_CONTENT`，类型集合为 `ag_ui.core.EventType` 子集；默认模式账本含本轮 user + assistant；`turn.messages` 非空走 `build_type=DIRECT`、不写账本、fake 模型请求体含该历史；自定义 `event_sink` 时 stdout 无事件行但账本仍完整。全程离线。

## T-7 resume / close 与会话生命周期

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/session.py`、`cli.py`、`src/agent/tests/headless/test_lifecycle.py`
- 需求: REQ-6、REQ-9
- 依赖: T-6
- 验证: `make -C src/agent test path=tests/headless/test_lifecycle.py`
- [x] 完成标准：`resume` 经 `agent_factory().resume(...)` 在同一 `session_code` 与 `thread_id` 续轮并返回本轮响应；审批 / `ask_user_question` 形状的负载返回 `ok=false` + `resume_payload_unsupported`，并在事件流末尾补发 `RUN_ERROR`；`close` 释放实例、checkpointer、账本与线程句柄，之后同会话任意请求返回 `session_closed`，其他会话不受影响；`session_code` 在所有响应中原样回传。
- 验证记录：`make -C src/agent test path=tests/headless/test_lifecycle.py` → **通过**，6 passed。`resume` 经注入点在同一 `session_code` / `thread_id` 续轮，第二次模型请求体含第一轮问答；审批 / `ask_user_question` 形状负载返回 `ok=false` + `resume_payload_unsupported` 并在事件流末尾补发 `RUN_ERROR`；`close` 后该会话任意请求返回已关闭错误（错误码按方案 D-8 写作 `session.closed`，任务正文简称 `session_closed`），其他会话不受影响；`session_code` 在所有响应原样回传。

## T-8 并发、串行与取消

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/session.py`、`cli.py`、`src/agent/tests/headless/test_concurrency.py`
- 需求: REQ-10
- 依赖: T-7
- 验证: `make -C src/agent test path=tests/headless/test_concurrency.py`
- [x] 完成标准：同会话并发两个 `turn` 时第二个在第一个终止事件前不产出任何事件（会话锁串行）；两个不同 `session_code` 的 `turn` 可同时处于执行中（工作线程并发）；`cancel` 在 10s 内产出 `RunErrorEvent(message=RunId.CANCELLED_MESSAGE)` + `RunFinishedEvent(run_id=RunId.CANCELLED)` 并把响应 `status` 置为 `cancelled`；启动时若消息处理器不是内存实现则显式失败退出（错误信息含处理器类型）。
- 验证记录：`make -C src/agent test path=tests/headless/test_concurrency.py` → **通过**，5 passed。同会话两个 `turn` 串行（第二个的首个事件晚于第一个 `RUN_FINISHED`，fake 端点观测最大并发 1）；跨会话并发（观测到并发 2）；`cancel` 在 10s 内收敛，`turn` 响应 `status=cancelled`，事件尾部为 `RUN_ERROR(message=用户已取消)` + `RUN_FINISHED(runId=cancelled)`；消息处理器非内存实现时启动即 `SystemExit` 且错误信息含处理器类型。

## T-9 错误处理与进程可用性

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/headless/cli.py`、`protocol.py`、`src/agent/tests/headless/test_error_recovery.py`
- 需求: REQ-5、REQ-10
- 依赖: T-7
- 验证: `make -C src/agent test path=tests/headless/test_error_recovery.py`
- [x] 完成标准：非法 JSON、未知方法、未知会话、`close` 后请求、装配异常（`agent.build_failed`）、执行期未归类异常（`agent.internal_error`）六类输入均返回结构化错误并写出 `RUN_ERROR`，进程在同一条连接上继续服务后续合法请求；错误响应不含完整请求体与凭据；用例以单进程连续发 6 类坏请求 + 1 条合法 `turn`，断言末尾合法请求正常完成。
- 验证记录：`make -C src/agent test path=tests/headless/test_error_recovery.py` → **通过**，3 passed。单条连接连发 6 类坏请求（非法 JSON、未知方法、未知会话、`close` 后请求、装配异常、执行期未归类异常）+ 一条合法 `turn`：各自返回结构化错误码（`protocol.invalid_json` / `protocol.unknown_method` / `session.not_found` / `session.closed` / `agent.build_failed` / `agent.internal_error`）并写出 `RUN_ERROR`，末尾合法请求正常 `finished`；注入的 `auth_headers` 值不出现在 stdout / stderr。

## T-10 无平台依赖与隔离验证

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/headless/test_no_platform.py`
- 需求: REQ-8、REQ-11
- 依赖: T-7
- 验证: `make -C src/agent test path=tests/headless/test_no_platform.py`
- [x] 完成标准：子进程完成一轮纯文本对话后断言 `sys.modules` 中不存在 `django` / `blueapps`；以计数桩替换 `HeadlessResourceManager.get_client` 断言平台调用为 0；token 上报与 span 上报路径未产出任何对外请求；注入的 `auth_headers` 值不出现在 stdout / stderr；`git diff --stat` 对 `src/agent/pyproject.toml` 与 `uv.lock` 为空，且依赖清单中无 dsh 相关包。
- 验证记录：`make -C src/agent test path=tests/headless/test_no_platform.py` → **通过**，3 passed。子进程完成一轮后 `sys.modules` 无 `django` / `blueapps`；`HeadlessResourceManager.get_client` 计数桩为 0；注入凭据不出现在输出；headless 子包源码无 django / blueapps 导入；`pyproject.toml` 无 dsh 相关包。
  说明：`src/agent/pyproject.toml` 与 `src/agent/uv.lock` 在 `git status` 中确认为无改动。

## T-11 接入文档

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `docs/agent/HEADLESS_HOST.md`、`docs/agent/readme.md`（新增索引条目）
- 需求: REQ-12
- 依赖: T-7
- 验证: 手工评审依据：文档章节自检清单（组合配置步骤 / 方法映射 / 不支持面 / fake 模型验收步骤 / 日志与安全 五节齐备），且全文无 dsh 源码片段
- [x] 完成标准：说明宿主如何以组合 / profile 配置停用 `@deepseek-ai/dsh-agent-loop` 并启用仓外插件（含「重复注册会抛 `an agent factory is already registered`」的原因）、五方法映射与 NDJSON 报文样例、支持面（`create` / `turn` / `resume` / `cancel` / `close`）与不支持面（`inbox` 多队列、`steer` / `inject` / `followup` 逐步语义、`runMaintenance`、compaction、子 Agent，且必须抛错不得降级）、用本机 fake 模型（含 `/v1/api/token_check`）完成验收的步骤、日志与凭据约束；索引条目指向新文档。
- 验证记录：手工评审（依据任务定义的五节自检清单）→ **通过**。`docs/agent/HEADLESS_HOST.md`：组合/profile 配置步骤（含「重复注册会抛 `an agent factory is already registered`」的原因）、五方法映射与 NDJSON 报文样例、支持面与不支持面（明确必须抛错、不得降级）、本机 fake 模型验收步骤（含 `/v1/api/token_check`）、日志与凭据约束；`docs/agent/readme.md` 已新增索引条目；全文无 dsh 源码片段。

## T-12 仓外集成验收证据（dsh 插件 + fake LLM）

- 仓库: 本项目（证据留在节点工作区 `acceptance/`，不提交本仓、不发布）
- 文件: `acceptance/fake_llm_server.py`、`acceptance/dsh-plugin/`、`acceptance/README.md`
- 需求: REQ-7、REQ-12
- 依赖: T-10、T-11
- 验证: 手工依据：`acceptance/README.md` 的复现步骤逐条执行，命令与期望输出记录在案
- [x] 完成标准：fake 服务同时实现 `/v1/chat/completions` 与 `/v1/api/token_check` 并在回环地址启动；仓外插件按 T-11 文档接入真实 dsh 后能完成一轮「用户输入 → assistant 回复」，dsh 侧日志证明默认 Agent 实现未参与；停用插件、以 dsh 默认组合启动时 dsh 行为不变；记录两轮运行的命令、关键日志与结论，作为 ACC-11 / ACC-12 证据。
- 验证记录：真机 dsh 验收 → **通过**。dsh `master` @ `22ddee3792402c3e70ec5f86a63c3e3e895a2f60`（只读参考、零改动），Node v24.9.0 + pnpm 11.28.5，`pnpm install --frozen-lockfile` 与 `pnpm build --scope artifacts` 均成功。
  - ACC-11：`DSH_HOME=<tmp> pnpm dsh --profile headless --patch <abs>/acceptance/dsh-plugin/agent-hook.patch.yml "hello aidev-agent"` → stdout `你好，我是 aidev-agent`，退出码 0；**全程未设置任何 DeepSeek 凭证**；`--json` 事件为 `turn_start → step_start → text → step_end → turn_end(completed) → final`；`--dump-config` 显示 `agent-loop` 行 `disabled: true` 且插件行已插入；插件加载失败的中间轮次报 `dsh: no agent factory registered (load an agent-loop plugin)`，反证默认工厂已停用而非叠加注册。
  - ACC-12：不传 `--patch` 时 `pnpm dsh --profile headless "hello"` 按默认路径报 `MISSING_CREDENTIAL: llm-deepseek ...`（退出码 1），与接入前一致。
  - 证据：`acceptance/fake_llm_server.py`、`acceptance/dsh-plugin/`、`acceptance/logs/`、`acceptance/README.md`。

## T-13 回归与依赖封板

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: 无新增文件（回归执行与结论记录）
- 需求: REQ-2、REQ-11
- 依赖: T-2、T-10、T-12
- 验证: `make -C src/agent test path=tests/services`、`make -C src/agent test path=tests/core`、`make -C src/agent test path=tests/headless`
- [x] 完成标准：`tests/services`、`tests/core`、`tests/headless` 三组全绿且无新增 skip；`src/agent/pyproject.toml` 与 `uv.lock` 无 diff；AG-UI 事件类型集合未新增（以基线事件名清单比对）；会话落库相关用例（`tests/core/interrupt/`、`tests/services/test_resume_events.py`）保持通过，证明未启用 headless 时进程行为不变。
- 验证记录：`make -C src/agent test path=tests/services` → **通过**，746 passed / 40 skipped / 5 deselected；`make -C src/agent test path=tests/core` → **通过**，2155 passed / 9 skipped / 39 deselected / 4 xfailed；`make -C src/agent test path=tests/headless` → **通过**，54 passed / 0 skipped。本变更新增用例未使用 `skip` / `xfail`；`src/agent/pyproject.toml` 与 `src/agent/uv.lock` 无 diff；未新增 AG-UI 事件类型（headless 复用 `ag_ui.core` 原对象）；`tests/core/interrupt/` 与 `tests/services/test_resume_events.py` 全部保持通过。
