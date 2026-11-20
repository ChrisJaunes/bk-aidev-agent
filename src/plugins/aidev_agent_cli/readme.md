# aidev-agent-cli

`aidev-agent` 的 CLI 运行面：在 stdio 上提供嵌入式会话协议与 TUI 版本化帧协议。

本包**单向依赖** `aidev-agent` SDK（CLI → SDK），SDK 不依赖本包。
要求 Python `>=3.11.9,<3.12`（与 SDK 自身 `>=3.11,<3.12` 的交集）。

三种运行面同处一个包：**嵌入式会话**不做任何平台调用，**TUI / AG-UI 进程**与 **ACP 服务端**
在平台支持下运行。它们都用 stdin/stdout 上的 NDJSON / JSON-RPC，诊断一律走 stderr；
TUI 不再需要单独的 Python `worker` 包。包内模块按**职责**分 4 层
（`runtime` / `adapters` / `resources` / `hooks` / `sdk`，另有叶子 `utils` 与端口 `infrastructure`，见「目录结构」）。

## 安装

```bash
pip install aidev-agent-cli
pip install "aidev-agent-cli[checkpoint]"
pip install "aidev-agent-cli[checkpoint,opentelemetry]"
pip install "aidev-agent-cli[checkpoint,acp]"       # 需要 ACP 服务端时
```

- `checkpoint`：TUI 持久化（SQLite saver）。SQLite saver 3.1 需要
  `langgraph-checkpoint>=4.1.0`（`DeltaChannelHistory` 接口）；该 extra 同时声明 LangGraph 1.2+，
  以匹配更新的 prebuilt / runtime 接口。
- `opentelemetry`：TUI 追踪，**复用** `aidev-agent[opentelemetry]>=3.0.0`，
  不在本包复制版本清单；追踪实现属 SDK，版本组合由 SDK 维护。
- `acp`：ACP 服务端（`agent-client-protocol>=1.0.0rc3`）。**可选**——不装它时
  agui 照常运行，`--protocol acp` / `acp-dsh` 会给出「请安装 aidev-agent-cli[acp]」而不是 ImportError 栈。
  版本下界刻意取 pre-release：镜像上尚无正式版，而该下界的候选集含 rc3，普通安装即可解析；
  上游出正式版时无需改声明。

除 SDK 外，本包还直接依赖 `ag-ui-protocol` / `langchain-core` / `langgraph` /
`xxhash`，版本下界取自 SDK 已解析的那一组（见 `pyproject.toml` 注释），不另猜版本。
SDK 为其原生 run-scoped 客户端声明 OpenAI 2.54+。

本包需与包含当前 hook 模块的 `aidev-agent` 一并安装。`pyproject.toml` 里的
`aidev-agent>=3.0.0` 是**待刷新的下界**：开发环境通过 `[tool.uv.sources]` 以 path 源装当前 SDK 源码，
独立安装验证使用本地构建的 SDK wheel。对外发布 CLI 前需先发布匹配的 SDK，并更新最低版本约束。

## 入口与支持矩阵

```bash
# acp-dsh（默认）：ACP + _aidev/* 扩展、注入式资源，供 dsh 等宿主插件驱动（见 docs/acp-dsh.md）
MESSAGE_HANDLER_TYPE=inmemory python -u -m aidev_agent_cli

# 平台支持的 TUI：版本化帧 + 原始 AG-UI 事件
python -u -m aidev_agent_cli --protocol tui

# ACP 服务端：JSON-RPC over stdio（见 docs/acp-integration.md）
python -u -m aidev_agent_cli --protocol acp --resources platform  # token 取自 bkai_tui.json
```

`--protocol` 取 `acp-dsh`（默认）/ `agui` / `tui` / `acp`；**`tui` 是 `agui` 的别名**（现网 TUI 注册命令在用）。
`--resources` 省略时由协议推导：`acp-dsh → injected`，`agui` / `acp → platform`；显式给出时按顺序叠加
（`resources/sources.py`）。`acp` 与 `acp-dsh` 都需要 `aidev-agent-cli[acp]`。

| `--protocol` | 默认资源 | 线上格式 | 典型对端 |
|---|---|---|---|
| `acp-dsh`（默认） | `injected` | ACP v1 + `_aidev/hook` / `_aidev/resume` 扩展 | dsh 宿主插件（替换核心 Agent） |
| `agui` / `tui` | `platform` | 版本化帧 + AG-UI 事件 | 平台 TUI |
| `acp` | `platform` | 标准 ACP v1（最小集） | ACP 客户端（编辑器等） |

参数错误不静默降级：进程在协议握手之前以非零码退出，并在 **stderr** 说明原因
（stdout 是协议通道，绝不写入启动错误）。

协议在**导入 SDK 服务之前**选定：stdout guard、进程配置与可选的评测出网限制都在导入平台客户端之前安装。
`import aidev_agent_cli` 不加载任一 runtime，也不改进程环境（不配置 logging、不读 `~/.bkai`、不设环境变量）。

原 headless NDJSON 协议（`create` / `turn` / `resume` / `cancel` / `close`）已由 `acp-dsh` 取代并删除，
对照表见 `docs/acp-dsh.md` §7。
旧的 `python -m aidev_agent.headless` 与 `python -m worker acp` 入口都已移除，**不提供兼容 shim**。

## 进程隔离运行时

三个协议均通过 `RuntimeService` 的异步入口登记、启动、恢复、取消和关闭。父进程的
`SessionRegistry` 保存跨轮会话；每个 `Run` 固定输入、身份、配置、sink 和 hook 快照，
SDK 构建与同步生成器执行均在单独的 spawn `ProcessWorker` 中。

同会话按协议拒绝 busy 或 FIFO 排队，排队取消不构建。关闭先封禁入口，再等待 worker
合作取消、terminate/kill 与 join；成功/失败/取消终态只在进程回收后发布。强杀无法撤销外部副作用。

worker 自行打开 checkpoint 与本地历史 SQLite。`BKAI_TUI_CHECKPOINT_FILE` 和
`BKAI_TUI_HISTORY_FILE` 可覆盖数据库路径；默认历史位于 `~/.bkai/history.sqlite`。
本地历史同时持久化 pending interrupt 和 hook memo；重启后显式登记相同 session code
再 resume，不依赖上一进程的内存。历史与 checkpoint 不宣称跨库原子提交。

工具只接受 `module:factory` 字符串，在 worker 内构造；不接受 live 工具、客户端或 saver。

## 目录结构

```text
aidev_agent_cli/
├── __init__.py            # 轻量：__version__ / __author__ / main() / serve()（全部延迟导入）
├── __main__.py            # 唯一命令入口 + composition root（argv → adapter × resources × runtime）
├── runtime/               # 三协议共用的进程隔离运行时；**跨轮状态的唯一所有者**
│   ├── service.py         # RuntimeService：admission（含 pending 中断校验）、快照、session→run 索引
│   ├── session.py         # 父进程 Session（持有已提交的 SessionState）+ SessionRegistry
│   ├── runs.py            # RunQueue + execute_turn：spawn、join、按结局提交或整体回滚会话状态
│   ├── worker.py          # ProcessWorker + SDK_ENTRY：显式 IPC、取消/构建超时、启动纪律
│   ├── storage.py         # history / checkpoint 文件位置（默认 ~/.bkai，可经环境变量覆盖）
│   ├── process_config.py  # 进程级配置聚合（本地配置 / 智能体 / 启动选项 / AuthManager）
│   ├── settings.py        # bkai_tui.json
│   └── models.py          # 快照值对象（无 SDK）
├── bootstrap/             # 进程启动纪律：装载 SDK 之前完成（startup / network_guard / open_telemetry），入口与 worker 共用
├── sdk/                   # worker 侧：**单轮**组装 aidev_agent 并合并本轮产物（不持久化、不跨轮）
│   ├── executor.py        # AgentRun（单轮组装、执行与状态合并）+ import target：快照 → AgentSpec，返回 {outcome, state}
│   ├── spec.py            # AgentSpec（组装配置，全部由上层传入）与 TurnPorts（取消 / hook / 构建阶段）
│   ├── factory.py         # SDK agent 工厂接线
│   ├── checkpoints.py     # 按给定路径打开的 SQLite saver
│   ├── events.py          # AG-UI 事件观察与账本
│   ├── interrupts.py      # 中断归一与恢复输入
│   ├── hooks.py           # 宿主 hook bus / 关联 / 超时 / ask 重放
│   └── workspace.py       # 工作区 agent 注入
├── adapters/              # 线上协议（只做协议翻译，不持第二份运行状态）
│   ├── agui.py            # 版本化 NDJSON 12 方法路由 + started/event/completed/result/error 信封
│   └── acp.py             # ACP 服务端 + AG-UI→ACP 解释器；acp / acp-dsh 以 AcpProfile 区分
├── resources/             # 资源策略（平台 / 注入式，两者的执行实现刻意不合并）
│   ├── platform.py        # 平台工厂、会话、配置/知识/审批/MCP 接线
│   ├── injected.py        # 注入 LLM/工具 + 内存账本
│   ├── local_config.py    # per-agent 声明 + 本地 MCP 声明与 mtime 缓存（叶子）
│   ├── loaders.py         # loader 链（平台/本地/审批/知识/runtime 覆盖）
│   └── workspace.py       # 工作区目录投影 + 本地文件 runtime 叠加
├── infrastructure/        # adapter 所见的运行时端口
│   └── ports.py           # RuntimePort（实现是 runtime.service.RuntimeService）
└── utils/                 # 叶子：通用工具与跨层值契约，只依赖标准库与自身
    ├── logging.py         # BKAI 状态路径与文件/stderr 日志
    ├── process.py         # 信号处理与守护线程执行器
    ├── stdio.py           # stdout guard、协议流、sys.path 加固
    ├── errors.py          # 错误码与结构化异常
    ├── inputs.py / runs.py# typed 输入、请求 / 会话选项 / 终态值对象
    ├── terminal.py        # 纯 stdlib AG-UI 终止事件 builder（runtime.runs 与 sdk.events 共用）
    └── history.py         # SQLite 运行事实

（另：adapters/streams.py 为宿主协议通道；runtime/settings.py 为 bkai_tui.json；
bootstrap/{startup,network_guard,open_telemetry}.py 为装载 SDK 之前的启动纪律。）
```

依赖方向由 **AST 脚本强制**（`tests/test_layer_graph.py`），不是口头约定：

| 方向 | 规则 |
|---|---|
| `__init__` / `__main__` → 任意层 | 允许，但**只能在函数体内**（保住启动延迟导入） |
| `adapters/*` → `runtime/*` + `resources/*` + `infrastructure/*` | 允许（adapter 必须能把「哪个策略」交给 `RuntimeService`，那是 composition 的一半） |
| `runtime/*` → `resources/*`（只经 contracts 的窄接口）+ `infrastructure/*` | 允许；**禁止** `runtime → adapters` |
| `resources/*` → `infrastructure/*` + `runtime/contracts`（仅类型） | 允许；**禁止** `resources → runtime.{service,session,runner}`（防环） |
| `infrastructure/*` → 其他层（`utils` 除外） | **禁止** |
| `sdk/*` → `runtime/*` | **禁止**（共用契约在 `utils`，worker 端点按结构协议注入，启动纪律只在 `runtime.worker`） |
| `utils/*` → 任何其他层 | **禁止** |
| 跨轮状态 / 持久化路径 | 只在 `runtime`：sdk 收快照里的 `state` 与 `checkpoint_path`，传回合并后的 `state`；失败 / 取消 / 强杀的轮次整体不提交 |
| `runtime.models` | 叶子：只允许 stdlib + `contracts` / `inputs`；**不得**到达 `runtime.sdk` |
| `utils.terminal` | 纯 stdlib 的 AG-UI 终止事件 builder，只允许 `runtime.runs` 与 `sdk.events` 引用 |

新增两条更硬的断言：`aidev_agent_cli` 下**函数内 import 为零**（白名单只有 import-target
边界、stdout guard 之前、可选依赖单点装载、脚本冷路径、re-export），以及核心层
fresh import 逐个不得拉起 SDK。

两条细节（同样由 AST 断言）：`runtime` 不得 import `adapters`（runtime 不认识线上格式）；
`resources` 对 `runtime` **执行实现**（service / session / runner）的引用
必须在**函数体内**——模块级会成环，`runtime.contracts` 是唯一允许的模块级 runtime 引用。

四个子包的 `__init__` 只写职责 docstring，**不 re-export 任何名字**：任何 re-export 都会把
`import aidev_agent_cli.runtime` 变成拉起 SDK 的入口，破坏启动延迟契约。

## 共享 runtime 生命周期

`RuntimeService` 是**唯一**的会话与运行状态所有者：

```text
create(options) -> SessionInfo
start(request, sink) -> RunHandle        # await handle.result() 得到 RunOutcome
resume(request, sink) -> RunHandle
cancel(session_code, run_id=None) -> CancelOutcome
close(session_code) -> CloseOutcome
close_all(reason) -> None                # 有界等待 → 取消 → 关 owned saver → flush，幂等
```

- **运行登记只有一份**：`session_code -> {run_id: RunHandle}`。协议 adapter **不持有**第二份
  运行状态，也没有可变 sink 字段——每条 adapter 只做协议翻译。
- **取消两步**：先翻转 `CancelToken`（协作，backend 轮询），再在 `CANCEL_GRACE_SECONDS` 后
  由 service 统一做 backend 相关硬停。这是唯一一份后备，不写在 adapter 里。
- **终态唯一**：`RunHandle` 落定一次 `finished` / `cancelled` / `interrupted` / `failed`；
  重复落定只记日志、不覆盖。
- **busy 判定在 service**：同会话第二个活动 run 被拒（`rejected`），由各 adapter 决定怎么
  回给对端（AG-UI 回 error 帧 / acp-dsh / ACP 回 JSON-RPC error）。

## 执行路径

三个协议共用同一个执行策略 `SessionStrategy` 与同一个会话类型 `Session`：一轮在工作线程里
同步执行（会话锁串行、session_start hook、装配上限、限流重试、中断收集、终止事件补齐），
事件经会话 writer 交给协议的 sink。资源来源决定差异：含平台来源时 SDK 事件处理器是
「平台回写 + 转发」组合 writer、历史由平台承载；无平台来源时历史写入会话账本。

## ACP 能力（诚实声明）

已实现：`initialize`、`session/new`、`session/prompt`、`session/update`（通知）、`session/cancel`。

**不支持**（也不宣告，`agentCapabilities` 里全为 false）：

- `loadSession`（会话恢复面）
- 权限请求 / 响应（审批在 ACP 侧**降级为一段人可读文本**，不发起 `session/request_permission`）
- image / audio / embedded 内容块（只取文本块，其余忽略）
- 客户端提供的 MCP 服务器（忽略，只用自己的 MCP 面）
- ACP 文件读写服务（`fs/read_text_file` / `fs/write_text_file`）
- 模型列表（`models: 0` 是正常的）

## ACP token 与工作区隔离

所有协议同一条组装路径：身份只来自 `bkai_tui.json` 的 `auth.access_token`（三层合并后的结果），
不读环境变量。

- token **不进日志、不进响应、不进异常**。
- **不接受 argv 传入**（`--token` 不存在）：进程列表对同机用户可读。
- 缺 token 不在启动期拦截，由首次平台调用报错（作为该轮的 JSON-RPC error 返回）。

工作区隔离：`--resources platform` 下 `cwd` 是 **per-session** 的会话选项，ACP 的 `session/new`
**不再写** `os.environ["AGENT_TUI_CWD"]`（那是全进程的，第二个会话会污染第一个）。
进程级 `AGENT_TUI_CWD` 只作**默认值来源**。

## TUI 线上契约

信封与方法名保持不变。方法清单：

`session.start`、`session.cancel`、`session.create`、`session.get`、`session.list`、
`auth.login`、`auth.logout`、`agent.info`、`config.info`、`config.set`、`agent.llms`、
`process.shutdown`。

响应类型为 `ready`、`started`、`event`、`completed`、`result`、`error`。
交错到达的帧按 `id` 与 `runId` 关联，**不按顺序**。

```bash
printf '{"id":"shutdown","method":"process.shutdown","params":{}}\n' \
  | python -u -m aidev_agent_cli --protocol tui
```

期望：`ready` → `result`，退出码 0。stdin EOF 会取消活跃 run 并关闭 writer 与
checkpoint saver；SIGTERM 走同一关闭路径。进程**从不监听端口，也不是守护进程**。

TUI 配置 `bkai_tui.json` 三层深度合并（低 → 高）：`~/.bkai/` → `<AGENT_TUI_CWD>/.bkai/` →
`$AGENT_TUI_TUI_CONFIG_FILE`；写入落到存在的最高一层（都不存在时为用户层）。
per-agent 声明与 MCP 合并语义不变。`AGENT_TUI_CWD` 仍是用户工作区，不是 Python 模块目录。
认证持久化由 `BkAiConfig` 决定：`auth.login` 写入 `auth.access_token`，`auth.logout` 清除它；
协议响应**从不回显凭据**（token 只进不出）。

## 运行期模型重试

限流处理在 `aidev_agent.packages.langchain_core.models.llm_retry`。
`ChatModel` 使用 SDK 自有的同步与异步 OpenAI 客户端；不修改全局 OpenAI 类，
也没有启动期补丁安装。调用方显式传入的 completion 客户端仍由调用方控制。

两个环境变量控制这一行为：`AGENT_TUI_LLM_RATE_LIMIT_RETRIES`（重试次数）与
`AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET`（等待预算，秒）。

嵌入式模式仅在 `AGENT_TUI_LLM_RATE_LIMIT_RETRIES` 为正数时绑定 `RetryState`，
并以 `AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET` 作为等待预算（秒）。未绑定 state 时，
既有 OpenAI 与 SDK 重试行为不变。绑定后的 state 在模型调用与 fallback 之间**共享预算**、
遵守 `Retry-After`、支持取消，并发出 `llm_rate_limit_retry` 状态事件。
预算耗尽即该 state 的终态；**已经开始的成功流永不重放**。

SDK 调用方可以直接绑定 `RetryState`，无需 import 本包。

## 迁移说明

本包由 SDK 的 `aidev_agent/headless/` 迁出，SDK 侧不再保留运行面与 `headless`
extra（其 checkpoint 依赖随迁入本包的 `checkpoint` extra）：

- 导入路径为 `aidev_agent_cli`，`python -m aidev_agent.headless` 已移除，**不提供兼容 shim**；
- 依赖方向单向：CLI 依赖 SDK，SDK 不反向依赖 CLI；
- 线上协议不变——同一 NDJSON 信封、同一方法集，`--protocol tui` 仍以 `ready` → `result` 收尾。

进一步把「按 UI 场景分层」（`embedded` / `protocols` / `tui`）重排为「按职责分层」
（`runtime` / `adapters` / `resources` / `infrastructure`），**不提供任何旧路径 shim**：
`tui/`、`embedded/`、`protocols/` 三个目录已删除，在那三个名字上再放转发模块会让
「一个概念对应一个模块」重新退化成两套入口。迁移映射：

| 旧位置 | 新位置 |
|---|---|
| `embedded/cli.py` + `protocols/protocol.py` | `adapters/headless.py` |
| `tui/dispatch.py` | `adapters/agui.py` |
| `protocols/frame_protocol.py` | `adapters/agui.py` |
| `protocols/errors.py` | `infrastructure/errors.py` |
| `embedded/events.py` | `runtime/events.py` |
| `embedded/session.py` | `runtime/session.py` |
| `tui/agent_runner.py` | `runtime/runner.py` |
| `embedded/{hooks,interrupts}.py` | `runtime/{hooks,interrupts}.py` |
| `tui/runtime.py` | `adapters/agui.py`（stdout guard / 信号 / OTel 接线与协议分派同处：它们是 TUI 进程的启动序列，与帧协议不可分割） |
| `tui/local_resources.py` | `resources/{local_config,loaders,platform,workspace}.py` |
| `embedded/resource_manager.py` | `resources/manager.py` |
| `infrastructure/utils.py` | `infrastructure/logging.py`（逐字改名） |

入口签名、三种协议与事件/帧结构逐字不变；`checkpointer.py` 与 `network_guard.py` 未改动。

## 开发与验证

全部命令使用**本插件自己的 uv 环境**（`./.venv`）：`make init` 建它，`make test` 用它。
SDK 通过 `[tool.uv.sources]` 以可编辑 path 源装进同一环境（`../../agent`），
因此跑的是当前 SDK 源码；SDK 仍然不反向依赖本包。

```bash
make init              # uv sync：dev 组含 checkpoint + 追踪 + ACP 依赖
make test              # 全套（实测 641 例，不含 slow）
make test markers=     # 含 slow
make test path=tests/runtime/test_tui_checkpointer.py
bash tests/smoke_wheel.sh   # 装 wheel 后做真握手 / EOF 的端到端 smoke
make build             # 本地构建 wheel，不发布
```

`dev` 组已带 `aidev-agent-cli[checkpoint,opentelemetry,acp]`，SQLite saver、追踪实现与
真实 `agent-client-protocol` 都已就位（ACP 用例测的是真 SDK，不是 fake）。
子进程测试仍以 `src/agent` 为 cwd（`.env`、`AGENT_TUI_CWD`、`~/.bkai` 语义不变）。
插件 `tests/conftest.py` 不再在缺 sqlite saver 时静默跳过 checkpoint 相关用例，而是直接失败——
「少收集却显示全绿」比报错更危险。

测试目录**按职责镜像源码分层**：`tests/runtime` / `tests/adapters` / `tests/resources` /
`tests/utils` 放各层单元与契约用例，`tests/integration` 放跨层 E2E（T 系列 hook /
生命周期 / 真子进程 JSON-RPC）及其共享驱动 helpers；`tests/test_layer_graph.py`、
`test_entries.py`、`test_package.py` 与 `test_*_entry.py` 是横切的包 / 架构 / 入口 smoke。
历史上按 UI 场景命名的 `test_tui_*` / `test_acp_*` 文件名保留不改，避免改动测试节点 ID。

`test_tui_*` 是迁入的 worker 回归套件；其余用例继续验证嵌入式会话、无平台保证与 hook。

## License

MIT License