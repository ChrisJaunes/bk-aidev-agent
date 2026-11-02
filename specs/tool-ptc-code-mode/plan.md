# 工具 PTC（code mode）技术方案

- 变更名: tool-ptc-code-mode
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/tool-ptc-code-mode/spec.md
- 基线: `origin/develop` = `29487419`

## 背景与目标

在 Agent SDK 既有 ReAct 装配链路上新增 `tool_use_mode=code_mode`：模型只绑定一个代码执行工具 `code_execute`，把多步工具编排写成一段 Python 代码，代码在会话所选 runtime 的沙箱内执行，代码内以同步函数形式调用已装配工具。执行结果只把「代码 + 执行输出」回填模型，内层工具调用只发 AG-UI 事件、不进上下文。

目标是打通链路、对齐异常与事件语义、覆盖四类 runtime，并交付《code mode 收益测算方案》文档；不改默认路径、不改前端、不做与延迟加载的叠加。

## 非目标

沿用规格「非目标」一节，不重复展开。实现期额外约束：不新增 HTTP 路由与平台配置项；不改 `chat-x` 任何文件；不引入 JS/TS/shell 代码块；不做持久内核。

## 现状

| 能力 | 位置 | 现状 |
| :--- | :--- | :--- |
| 请求级开关 | `src/agent/aidev_agent/pydantic_models.py:13` | `ExecuteKwargs` 已有 `mode`、`tool_allow`、`tool_deny`；对象由调用方构造后经 `AgentExecutorKwargs` 注入 |
| 构建器入口 | `src/agent/aidev_agent/core/graphs/react/graph.py:594` | `set_bkai_options` 把 `options.execute_kwargs` 落到 `self._execute_kwargs`（`:630`） |
| 工具装配 | `graph.py:894` `_prepare_agent_tools` | 汇集 extra/middleware/ask/skill/runtime/task/a2a/knowledge 工具，去重后交给 `filter_tools_by_loading_mode`（`:971`） |
| 加载模式过滤 | `core/graphs/react/tool_loading_mode.py:41` | `filter_tools_by_loading_mode` + `command_approval_allowed`；该变更已合入 `develop`（`05840122`），可直接复用 |
| 模型绑定 | `core/nodes/model/model_chain.py:231` | 每轮 `context_assembly.get_choice_tools(ctx)` → `llm.bind_tools(tools)`；tool 管道可插中间件 |
| 提示注入 | `core/nodes/model/context_assembly.py:173` | 模板管道先于工具管道执行，`extra_template_middlewares` 是既有扩展点（`graph.py:871`/`:880`） |
| 工具执行链 | `core/nodes/tool/node.py:177` `build_tool_node` | 脱敏、不可信净化、ITSM 审批、JSON 修复、计时、结果截断、去重按序成链 |
| 事件 | `core/ag_ui/agent.py:857` | `CustomEventNames.ManuallyEmitToolCall` 已能产出 `TOOL_CALL_START/ARGS/END` 三元组，事件字段与类型不新增 |
| 运行时执行 | `core/tools/runtime_tools/provider.py:797` | `execute` 工具只做安全校验后调 `backend.execute`；四类 backend 均为「一次阻塞执行 + 文件读写」 |
| 运行时解析 | `provider.py:245` `RuntimeBackendResolver` | `default_runtime`、`_backends`（模型可见名 → 实例）、`resolve_backend` |
| 命令级审批开关 | `graph.py:783` | `command_approval_allowed(mode)` 为假时把 review 处置档降为 `block`，灰名单 fail-closed |
| A2A 子 Agent | `core/tools/a2a_tools/utils.py:108` | 子 `execute_kwargs` 由父对象 `model_copy()` 得到，新增字段自动继承 |
| 装配级测试范式 | `tests/core/graphs/react/test_tool_loading_mode.py` | 纯函数矩阵 + `_prepare_agent_tools` 集合断言 + 安全配置改写断言 |

关键结论：规格假设 5（兄弟变更未合入）已过期，code mode 可直接复用 `filter_tools_by_loading_mode`，不需要自带一份平行实现。

## 方案对比

| 方案 | 做法 | 优点 | 缺点 |
| :--- | :--- | :--- | :--- |
| A 单次执行 + 文件邮箱桥 | 代码在沙箱里跑一次；工具桩把请求写成文件，宿主轮询并回写结果 | 代码只执行一次，副作用语义与 function calling 一致；四类 runtime 共用一套协议 | 宿主需在 `execute` 阻塞期间并发读写沙箱文件；每次内层调用有一次轮询往返开销 |
| B 重放式续跑 | 代码在首次工具调用处中断退出，宿主执行工具后用结果缓存从头重跑 | 无并发、实现最简，只依赖 `execute` | 同一段代码跑 N 次；含 `random`/`time`/外部写入的代码会重放发散；单轮耗时随调用数平方增长 |
| C 宿主内受限进程 | 代码在宿主子进程执行，工具直接调宿主链路 | 实现成本最低，事件与回填最自然 | 代码未在所选 runtime 沙箱内执行，可直接读宿主文件系统，违反 REQ-9/REQ-10 |

选择 A。B 因为「重放发散」在本变更的核心场景（多步、含条件分支与聚合的编排代码）不可接受，降级为 A 在并发读写不可用时的兜底记录，不落地。C 直接排除。

配套决策（本轮已与需求方确认）：限额走 `settings` 环境变量；代码内工具函数保留 `target_runtime` 参数，由模型在代码中显式传。

## 决策

### 1 开关与默认路径

`ExecuteKwargs` 新增 `tool_use_mode: Literal["function_call", "code_mode"] = "function_call"`，位于 `mode` 附近。合法性由 pydantic 在对象构造期判定，非法值不可能进入装配；`ReActAgentBuilder.build()` 入口再做一次防御性断言（`_resolve_tool_use_mode`），保证即便对象被 `model_construct` 绕过也不会带着未知值装配工具。

`function_call` 路径一行不改：`_prepare_agent_tools` 只在 `code_mode` 时走新增分支，返回的工具列表、提示词、事件序列、审批链路保持原样。

理由：请求级字段不落库、不改会话，resume / regenerate 未携带即回落 `function_call`，符合 REQ-13。

### 2 装配：模型工具面收窄

在 `_prepare_agent_tools` 末尾（加载模式过滤之后）插入 code mode 分支：

1. 记 `assembled = filter_tools_by_loading_mode(tools, mode)`（既有逻辑不动）。
2. `inner = filter_tools_by_loading_mode(assembled, MODE_DONT_INTERRUPT)` —— 幂等，且无论调用方 `mode` 取什么值，ask 类与带审批工具都不进代码环境（REQ-5）。
3. 对 `inner` 套用 `tool_deny` → `tool_allow`，与 `ToolFilterMiddleware`（`team_middleware.py:94`）同口径。
4. 用 `inner` 构造 `code_execute`，返回 `[code_execute]`；随后对这一个元素同样套 deny/allow（REQ-3 字面语义：`code_execute` 只可能被 allow/deny/mode 裁掉）。
5. `code_execute` 不带 `metadata.approval`，天然不触发审批。

`ToolNode` 只拿到 `code_execute`；内层工具不经过图上的 ToolNode，而由「内层执行器」（决策 5）执行。

### 3 提示：精简工具清单

新增 `CodeModePromptMiddleware`，构建期通过 `node_options.extra_template_middlewares` 注册，把三段内容拼到 system prompt 尾部：

- 模式说明：只输出一段 Python；工具函数已注入；用 `print` 暴露结果；禁止 `import` 未列入清单的工具。
- 精简清单：每个工具一行，含名称、用途首行、参数要点（必填/类型）、`target_runtime` 的合法取值（由 `resolver.runtime_param_description()` 派生）。
- 约束：单次工具调用上限、超时、失败重试语义。

清单与代码桩都从同一个 `inner` 列表生成，保证「不多不少」（REQ-3）。完整 JSON Schema 不再注入模型。

### 4 执行：文件邮箱桥

`code_execute` 的执行流程：

1. 每次执行分配独立远端目录 `scratch_dir(run_id)`，上传 `_aidev_bridge.py`（prelude）、`user_code.py`、`run.py`。
2. 子线程调用 `backend.execute(f"python3 {dir}/run.py", timeout=CODE_MODE_EXECUTE_TIMEOUT, max_output_size=CODE_MODE_OUTPUT_LIMIT)`。
3. 主线程按 `CODE_MODE_BRIDGE_POLL_INTERVAL` 轮询 `dir/calls/`：读到未应答的 `req_<seq>.json` 就执行工具、写 `resp_<seq>.json`；超过 `CODE_MODE_MAX_TOOL_CALLS` 时回写一条拒绝响应。
4. 子线程结束后读 `status.json` 与执行输出，合成结果。

协议细节见「接口与数据」。宿主只做文件轮询，不需要沙箱回连宿主，四类 runtime 的能力面一致。

### 5 内层调用复用执行链

构建期用 `build_tool_node(inner, node_options=self._tool_node_options, security_settings=self._security_settings)` 生成一个不出现在图里的内层 ToolNode，挂在 code mode 会话对象上。

每次内层调用：构造 `AIMessage(content="", tool_calls=[call])`，以 `{"messages": [...]}` 调 `ainvoke(text, config=child_config)`，从返回的 `ToolMessage` 取内容与状态。`child_config` 从当前 `RunnableConfig` 复制、清空 `callbacks`，避免内层工具的 on_tool_* 事件被 AG-UI 适配器再解释一遍（重复三元组），同时保留 `configurable` 中的 `thread_id` / `execute_kwargs`。

这样脱敏、不可信净化、结果截断、去重、计时与审批包装全部走既有实现，不另起旁路（REQ-4）。审批类工具已被排除，审批 wrapper 在内层为空操作。

### 6 事件

每次内层调用前 `dispatch_custom_event(CustomEventNames.ManuallyEmitToolCall.value, data={"id": ..., "name": ..., "args": ...}, config=config)`，由 `core/ag_ui/agent.py:857` 转成独立的三元组。`id` 用 `cm-<run_id>-<seq>`，全局唯一且与外层 `code_execute` 的 id 不同。事件只用于展示与审计（REQ-7）。

### 7 上下文回填

`code_execute` 返回的 ToolMessage 内容只有执行输出（含截断标记与状态行）。模型产出的代码已经在 `AIMessage.tool_calls` 的入参里，属于「代码」一段。内层调用的入参与结果既不写 state、也不生成 ToolMessage，因此不会出现在下一轮请求体中（REQ-6）。

### 8 异常、超时与重试

执行结果状态：`ok`、`syntax_error`、`runtime_error`、`tool_error`（工具异常未被代码捕获）、`timeout`、`budget_exceeded`、`infra_error`。

- 前六类都回传模型（状态行 + 截断后的 stderr），模型可改写代码再试。
- `infra_error`（后端不可用、上传失败、轮询异常）同样回传，但计入失败次数。
- 重试计数在请求级会话对象上累加：首次执行不计入重试，连续失败达到 `CODE_MODE_MAX_RETRIES`（默认 3）即重试 3 次，第 4 次执行仍失败时抛 `CodeModeRetryExhausted`。
- `CodeModeRetryExhausted` 继承 `langgraph.errors.GraphBubbleUp`，被 `default_tool_call_handler`（`core/nodes/tool/node.py:68`）原样重抛，图运行失败，AG-UI 输出 RUN_ERROR，不会静默返回空结果。
- 每次重试重新建目录、重新上传、重新执行，不复用上次部分结果（REQ-8）。
- 超时由 `backend.execute` 的 `timeout` 兜底，返回超时状态而不是空输出成功。

### 9 运行时覆盖

- 代码执行环境取 `resolver.default_runtime`；未注册时回退到唯一已注册的 runtime 名；无可用 runtime 或存在多个且无默认值时返回明确错误，不伪造本地执行（REQ-9）。
- 内层工具函数保留 `target_runtime` 必填参数，由模型在代码里显式传，合法取值写在精简清单里。
- `RuntimeBackend` 基类补齐 code mode 依赖的契约：`ls_info` / `upload_files` / `download_files` 提升为基类方法声明，新增 `scratch_dir(name)`（默认 `<cwd>/.aidev_code_mode/<name>`，`paas` 覆写为 `$STORAGE_PATH/.aidev_code_mode/<name>`）。
- 后端缺少任一能力时抛 `CodeModeUnsupported`，不降级到 local。

### 10 安全

- 模型代码只在沙箱内执行，宿主从不 exec 模型产出的代码。
- 工具名白名单双保险：prelude 只为可调用集合生成函数；宿主收到请求后再校验一次名称，不在集合内直接拒绝且不执行工具本体（REQ-4/REQ-10）。
- 不新增环境变量注入，沿用 backend 构造时的 envs 清洗口径。
- 执行输出回填前走 runtime 现有的 `_runtime_redactor` 脱敏出口（`provider.py:127`），与 `execute` 工具一致；内层工具结果另经 ToolNode 的脱敏 wrapper，两道都不动既有阈值。
- 工作目录在沙箱 cwd 之下，隔离级别与文件可见范围沿用各 runtime 现状。

### 11 A2A 继承

无需改代码：子 Agent 的 `execute_kwargs` 由 `model_copy()` 得到，`tool_use_mode` 自动继承（REQ-11）。以测试兜住，防未来有人改成逐字段构造。

### 12 收益测算方案

新增文档 `src/agent/docs/code-mode-benefit-measurement.md`，覆盖任务集构成、四项指标口径、对照方法、采样与统计口径、判据五部分（REQ-12）。本期不写评测代码、不产出实测数据。

## 接口与数据

### `ExecuteKwargs`

| 字段 | 类型 | 默认 | 说明 |
| :--- | :--- | :--- | :--- |
| `tool_use_mode` | `Literal["function_call", "code_mode"]` | `function_call` | 工具使用模式，请求级，不落库 |

### 新增配置（`config.py`）

| 名称 | 默认 | 用途 |
| :--- | :--- | :--- |
| `CODE_MODE_EXECUTE_TIMEOUT` | 120 | 单次代码执行超时（秒），透传 `backend.execute` |
| `CODE_MODE_MAX_TOOL_CALLS` | 50 | 单段代码内工具调用上限 |
| `CODE_MODE_OUTPUT_LIMIT` | 100000 | 执行输出截断字符数（与 `execute` 同口径） |
| `CODE_MODE_MAX_RETRIES` | 3 | 连续失败后的重试上限 |
| `CODE_MODE_BRIDGE_POLL_INTERVAL` | 0.1 | 宿主轮询间隔（秒），仅调优用 |

### `code_execute` 工具

| 项 | 值 |
| :--- | :--- |
| 名称 | `code_execute` |
| 入参 | `code: str`（纯 Python 或带 ```python 围栏，围栏会被剥离） |
| 返回 | 字符串：执行输出；失败时首行 `[code_mode] status=<status> tool_calls=<n>` 后接截断后的错误输出 |
| 审批 | 无 `metadata.approval`，不触发中断 |

### 沙箱内目录与协议

```text
<scratch_dir>/.aidev_code_mode/<run_id>/
  _aidev_bridge.py    # prelude：工具桩 + 桥客户端
  user_code.py        # 模型产出的代码
  run.py              # 入口：加载 prelude 后 exec user_code
  calls/
    req_0001.json     # 沙箱 → 宿主
    resp_0001.json    # 宿主 → 沙箱
  status.json         # 沙箱 → 宿主：执行结论
```

请求体：`{"seq": 1, "tool": "read_file", "args": {...}}`
响应体：`{"seq": 1, "ok": true, "content": "..."}` 或 `{"seq": 1, "ok": false, "error": "...", "error_type": "tool_error"}`
状态体：`{"status": "ok", "tool_calls": 3, "error_type": null, "message": ""}`

写入原子性：先写 `*.json.tmp` 再 `os.replace`；宿主只读「无对应 resp 且非 .tmp」的 req，避免读到半截文件。

### prelude 对外形态

- 为 `inner` 中每个工具生成同名同步函数，签名按工具 schema 派生，含 `target_runtime`。
- 内部 `_aidev_call(name, kwargs)`：写请求 → 轮询响应 → 命中则返回 `content`，`ok=false` 时抛 `CodeModeToolError`（`Exception` 子类，可被模型代码 `except` 捕获）。
- 超过调用上限时抛 `CodeModeBudgetExceeded` 并在 `status.json` 记 `budget_exceeded`。

### 事件 payload

```json
{"id": "cm-3f2a-0001", "name": "read_file", "args": "{\"file_path\": \"/tmp/a.txt\", \"target_runtime\": \"local\"}"}
```

## 跨仓契约

- 本任务只有 `bk-aidev-agent` 一个业务仓，无跨仓依赖与执行顺序。
- 平台侧只依赖 `execute_kwargs` 原样透传：新增字段对未升级调用方是未知字段，不影响既有请求。
- `chat-x` 零 diff：内层事件只使用既有 `EventType` 与字段，渲染路径不变。
- 与 `tool-loading-mode` 的关系：同源复用 `filter_tools_by_loading_mode` 与 `command_approval_allowed`，叠加取交集，合入顺序互不阻塞。

## 风险

| 风险 | 影响 | 处置 |
| :--- | :--- | :--- |
| 宿主在 `execute` 阻塞期间并发读写同一后端 | e2b/paas 的客户端是否线程安全未经取证 | 桥以「执行线程 + 轮询线程」实现，轮询失败按 `infra_error` 回传；ACC-15 用可注入后端断言往返契约，真实环境另补证据 |
| 轮询间隔带来的内层调用延迟 | 远端后端每次调用多 0.1–0.2s | 间隔可配；`local`/`bubblewrap` 为宿主直读，开销可忽略 |
| prelude 依赖沙箱内 `python3` | 镜像缺解释器时整条链路不可用 | 启动失败归为 `infra_error` 并回传明确文案；不静默降级 |
| 保留 `target_runtime` 增加模型负担 | 模型可能传错或漏传 | 精简清单列出合法取值；漏传是 `TypeError`，可被代码捕获，属模型可修正错误 |
| `code_execute` 可被 `tool_allow`/`tool_deny` 裁掉 | 极端配置下模型没有任何工具 | 按 REQ-3 字面语义实现，装配期记日志；裁剪后图退化为无工具直出 |
| 重试语义的口径 | 「重试上限 3 次」有两种读法 | 采用「首次 + 3 次重试」；任务与测试固定该口径，评审如要求改为总次数只改一处常量语义 |
| 多 runtime 注册且无默认值 | 代码环境无法确定 | 抛 `CodeModeUnsupported` 并提示显式配置默认 runtime，不猜测 |
| 4xx 校验在平台侧 | 本仓只保证对象构造即校验 | ACC-3 在本仓以 `ValidationError` + 未装配判定；HTTP 4xx 由调用方既有 schema 校验映射，本仓不新增路由 |

## 测试策略

| 层 | 覆盖 | 方式 |
| :--- | :--- | :--- |
| 纯函数 | 开关取值、清单生成、桩生成、请求体校验 | 参数化单测，无 IO |
| 协议 | 桥的请求-响应往返、超时、次数上限、原子写 | `FakeCodeModeBackend`（内存目录 + 可编程延迟/失败） |
| 执行链 | 脱敏、结果截断、去重、错误状态 | fake 工具 + 内层 ToolNode |
| 装配 | 模型工具面、清单集合、mode 叠加、allow/deny、A2A 继承、三路径一致 | `ReActAgentBuilder._prepare_agent_tools` + fake model 捕获 `bind_tools` 入参 |
| 事件 | 三元组数量、id 唯一、事件类型集合不越界 | 直接向 AG-UI 适配器喂 `on_custom_event`；前端目录 diff 断言为空 |
| 端到端 | `local`、`bubblewrap` 真实跑通含工具调用的代码 | 真实 `FilesystemBackend` + 本机 `python3`；缺 `bwrap` 时跳过 |
| 未取证 | `agent_run`（E2B）、`paas` | 以 fake 后端断言往返契约，真实环境补证据 |

全部命令走 `make test path=<dir-or-file>`（仓内约定，不直接调 `pytest`）。
