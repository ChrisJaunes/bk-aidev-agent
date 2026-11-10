# Agent Core 插件机制：hook 协议与注入 技术方案

- 变更名: agent-plugin
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/agent-plugin/spec.md
- 基线: `develop` = `952e2cb3`；工作分支 `feature/agent-plugin`

## 背景与目标

为 Agent 运行时补一层进程内 hook 协议：5 个点位（`session_start`、`model_pre`、`model_post`、`tool_pre`、
`tool_post`）各有明确的触发时机、上下文、裁决取值与执行语义；处理器集合在构建期由调用方注入
`AgentInstanceFactory.build_agent(...)`，随实例生效、按实例隔离；未注入时行为与基线逐项一致。

本方案只解决「协议 + 注入 + 5 个触点」，不含插件发现与加载（见规格「非目标」）。

## 非目标

- 不做插件发现、清单解析、entry point 导入、热加载。
- 不兼容 Workspace 中以本地文件描述的插件 / 工具 / 配置。
- 不做宿主侧 hook、不实现 headless NDJSON 上报、不改 `feature/agent-hook`。
- 不新增点位（`prompt_assemble`、`turn_stop`、`permission_request`、`subagent_*`、`pre_compact` 均不在本期）。
- 不改既有工具链判定（ITSM 审批、脱敏、不可信净化、截断、去重、计时、代理解包）。
- 不新增平台配置项、DB 字段、AG-UI 事件类型、前端改动。
- 不做 hook 的权限与信任模型。

## 现状

均为【代码确认】，路径相对 `src/agent/aidev_agent/`。

| 事实 | 证据 |
| --- | --- |
| 实例构建入口 `AgentInstanceFactory.build_agent(...)`，已用 `**extra` 承接可变参数 | `services/agent/factory.py:121`、`:143` |
| 构建期上下文 `AgentBuildContext` 由 `_make_build_context` 装配，Chat 专属字段在 `ChatBuildExtras` | `services/agent/factory.py:353`、`services/agent/registry.py:36` |
| `AgentExecutorKwargs` 为 `extra="allow"` 的 pydantic 模型，是「Chat 实例 → 执行器构建器」的参数协议 | `pydantic_models.py:976` |
| 图装配唯一入口 `ReActAgentBuilder`；模型 / 工具 / PV 节点分别由 `_prepare_agent_model_node`、`_prepare_agent_tool_node`、`_prepare_agent_pv_node` 构造 | `core/graphs/react/graph.py:838`、`:1159`、`:1116` |
| 图拓扑：`model → (条件) → pv_node → (Send) → tools → model`；`recursion_limit=1000` | `core/graphs/react/graph.py:1362-1376`、`:1395` |
| 模型节点内部有 recovery 重试链（LCEL `_assemble → 重试链 → 兜底`），一次节点调用只装配一次 | `core/nodes/model/node.py:209`、`core/nodes/model/model_chain.py:463-481` |
| 工具 wrapper 链首个为最外层；内置顺序：结果事件 → 循环守卫 → 代理解包 → 不可信净化 → 脱敏 → ITSM 审批 → JSON 修复 → 计时 → 结果截断 → 自定义 → 去重 → 原始错误识别 | `core/nodes/tool/node.py:306-381` |
| `ToolNode` 传给 wrapper 的 `execute()` 已把工具异常转成 `status="error"` 的 `ToolMessage` | langgraph 1.0.5 `prebuilt/tool_node.py:930-973`、`:1008-1019` |
| 代理解包按 `catalog.executable(name)` 判定真实工具，链首执行、返回时改名 | `core/nodes/tool/tool_call_proxy_wrapper.py:38-60` |
| 工具计时（`duration`）在计时 wrapper 内写入，位置在脱敏之外 | `core/nodes/tool/timer_wrapper.py:41-60` |
| `ask_user_question` 以 `interrupt(AskUserQuestionTarget.model_dump())` 暂停图，续流后用 `parse_resume_answers` 取答案 | `core/tools/ask_user_question.py:48-95`、`packages/interrupt_manager/ask_user_question.py:984` |
| 审批拒绝结果由 `_rejected_message` 构造（`status="error"` + 语义标记） | `core/nodes/tool/approval_wrapper.py:118-132` |
| 运行入口四条：`_stream`（AG-UI，构造 `AidevAGUIAgent`）、`_stream_with_legacy`、`_invoke`、`_invoke_resume_with_events`；`cfg` 在 `_execute` 统一构造并下传 | `services/agent/chat.py:1031`、`:1277`、`:1106`、`:1431` |
| `cfg["configurable"]` 已经承载 per-run 的 Python 对象（`execute_kwargs`），pv 节点直接读取 | `services/agent/chat.py:1041-1044`、`core/nodes/pv.py:180-183` |
| AG-UI 运行收尾有统一扩展点 `_emit_run_end_extras(state_values, thread_id, outcome)`，成功与中断出口都会走到 | `core/ag_ui/agent.py:338`、`core/ag_ui/aidev_agent.py:129` |
| 每次执行都会重建图：`_execute → _get_agent → get_agent_executor → builder.build()` | `services/agent/chat.py:1041`、`:1616`、`services/common_agent/agent.py:31` |

结论：5 个点位都有确定落点；本期难点不在「插在哪」，而在「每轮一次」的计数口径与
「参数改写不得污染循环守卫判定」。

## 方案对比

### D-1 每轮状态（turn 计数、已触发标记）放在哪

| 方案 | 说明 | 取舍 |
| --- | --- | --- |
| **A（选定）运行态对象随 `config["configurable"]` 传递** | `_execute` 建 `HookRunState` 放进 `cfg`，各节点从 `config` 读 | 与既有 `configurable.execute_kwargs` 同构；不改图状态、不落库、不进快照；`_invoke` / `_stream` / legacy 四条路径都拿得到 |
| B 写入图状态（新增 state channel） | 计数器作为 `DefaultState` 扩展字段 | 会被 checkpointer 持久化、进快照，且 resume 语义与「每次运行一次」冲突 |
| C 模块级 ContextVar | 运行入口 set，节点 get | 四条执行路径里有线程/生成器边界（`async_to_sync_generator`、队列 producer），传播不保证 |

排除 B、C 的理由：B 把纯内存计数写进会话状态，违反「不改落库内容」的隐含约束；
C 依赖上下文传播，`_stream_with_queue` 的生产者线程不确定，且失败是静默的。

### D-2 `tool_pre` 在哪执行

| 方案 | 说明 | 取舍 |
| --- | --- | --- |
| **A（选定）pv 阶段串行执行，wrapper 应用裁决** | `pv_node` 每次工具批次只跑一次，按模型给出的顺序逐个调用处理器，裁决按 `tool_call_id` 存运行态 | 天然有序（并行 Send 不会乱序触发）；`deny` / `ask` / `replace` 的应用仍留在 wrapper，位置语义不变 |
| B 全部放进 tool wrapper | 每个调用的 wrapper 内直接调处理器 | Send 并行时触发顺序不确定，与 REQ-4 冲突 |
| C 放在模型节点出响应之后 | 拿到 tool_calls 立即执行 | 会对最终不会执行的调用触发（resume 已完成的、被循环守卫拒绝的），且离执行点太远 |

### D-3 `tool_post` 插在哪一层

选定：wrapper 链**最内层**（`_content_error` 之内），即 `deduplicate → _content_error → tool_post → 工具`。
理由：只有这一层能同时满足「看到未脱敏原始结果」「替换后仍按新内容重新识别错误」「继续流经去重、截断、脱敏、不可信净化」。
外层（净化/脱敏之后）看不到原文，内侧（工具内部）拿不到统一的 `ToolMessage` 形状。

### D-4 `model_pre.replace` 如何让该轮后续请求继承（用户选定 A）

| 方案 | 说明 | 取舍 |
| --- | --- | --- |
| **A（选定）运行态覆盖视图** | 覆盖结果存 `HookRunState`，该轮后续每次装配把 `覆盖消息 + 新增尾部消息` 作为发送内容 | 只改模型输入，不改图状态 / 落库 / 前端快照；AC3 的「与实际发送逐项相同」「后续继承」都可判定 |
| B 写回图状态 `messages` | 用 `RemoveMessage` + 新消息回写 | 会改写持久化会话与快照内容，超出本期「不改落库」的边界 |

### D-5 `tool_pre` 的 `ask` 走哪条中断（用户选定）

选定：复用 `ask_user_question` 通道——构造 `AskUserQuestionTarget`（单问题、两个选项）并 `interrupt()`，
续流后用 `parse_resume_answers` 取答案，答案不是「允许执行」即视为未批准、工具不执行。

| 方案 | 取舍 |
| --- | --- |
| ITSM 审批通道 | 建单依赖 `approval.approvers`，工具没配审批人就无法成单；且会把 hook 裁决混进审批语义 |
| **ask_user_question 通道（选定）** | 纯聊天卡片，不要求工具带审批配置；沿用既有问答恢复链路，不新增 reason、不改 `InterruptProcessor` 的 handler 表 |
| 新增独立 reason | 要动中断分发与恢复解析，收益不足 |

### D-6 模块落位（用户选定）

选定 `src/agent/aidev_agent/core/hook/`（与 Issue 描述「添加 core/hook」一致）。
`feature/agent-hook` 的同名目录已知有问题、后续废弃，本期不与其做词汇统一，也不引用其代码；
若该分支先合入，冲突只在 `core/hooks/__init__.py`（不同目录名，实际为零冲突）。

### D-7 `session_start.append` 注入位置

选定：把追加内容作为**独立 `SystemMessage` 插到装配后消息列表的 system 段末尾**（首个非 system 消息之前），
不合并进任何既有消息。补充：该片段在本次运行后续请求中保持存在（存于运行态，按次应用，不重复追加）。

排除：追加到消息列表尾部（部分网关对「system 出现在 user 之后」处理不一致）；
写入 system prompt 模板槽位（要改 `ContextAssembly` 中间件顺序，侵入面更大且与「独立片段」的判定口径不一致）。

## 决策

### 1. 模块结构

新增 `src/agent/aidev_agent/core/hook/`：

| 文件 | 职责 |
| --- | --- |
| `points.py` | `HookPoint` 五点位枚举、各点位允许的裁决集合、点位名常量 |
| `decisions.py` | `HookDecisionKind`（pass/deny/ask/replace/append）、`HookDecision` |
| `contexts.py` | `HookIdentity` 与五个点位专属 payload、`HookContext` |
| `registry.py` | `HookSet`：点位 → 有序处理器元组；构建期校验点位名与可调用性 |
| `runner.py` | `HookRunner`：按序执行、合并、短路、fail-open、5 秒超时、结构化日志 |
| `run_state.py` | `HookRunState`、`bind_run_state` / `get_run_state` / `finalize_run` |
| `errors.py` | `HookDeniedError`（继承 `exceptions.AgentException`，可读错误） |
| `stages.py` | `run_tool_pre_stage`（pv 阶段用）、`apply_session_and_model_pre`、`run_model_post` 三个纯函数，供节点调用 |
| `__init__.py` | 对外导出协议与注入类型 |

`packages/` 红线不变：`core/hook/` 只依赖标准库、pydantic、langchain 消息类型与 `core/nodes/tool` 的既有原语，
不 import `services/`；`services/` 与 `core/nodes/` 反向依赖它是允许方向。

### 2. 协议

- 点位：`session_start`、`model_pre`、`model_post`、`tool_pre`、`tool_post`，取值即点位名，不得改名。
- 处理器：`Callable[[HookContext], HookDecision | None]`，同步与 `async def` 都支持；两条路径裁决语义一致
  （`runner.run_sync` / `runner.run_async` 共用同一套合并与降级实现，只有执行方式不同）。
- 上下文：`HookContext(point, identity, payload)`；`identity` 只读，含 `agent_code`、`session_code`、`run_id`、
  `turn`；payload 按点位区分（见「接口与数据」）。
- 裁决：`pass`（`None` 等价）、`deny`（必带 `reason`）、`ask`（仅 `tool_pre`）、`replace`（必带 `payload`）、
  `append`（必带 `payload`）。
- 合并：同点位按注入顺序执行；遇到 `deny` / `ask` 立即短路；`replace` / `append` 后写覆盖先写。
- 未注册处理器：`HookRunner.has(point)` 为 `False` 时节点直接跳过，除日志外无任何可观测变化。

### 3. 注入链路

`build_agent(hooks=HookSet(...))`
→ `AgentInstanceFactory.hooks`
→ `ChatBuildExtras.hooks`（`AgentBuildContext.chat`）
→ `ChatCompletionAgent.hooks`
→ `_get_agent()` 传入 `agent_cls.get_agent_executor(hooks=...)`
→ `AgentExecutorKwargs.hooks`
→ `ReActAgentBuilder._hooks`（`set_bkai_options` 读取，另提供 `set_hooks()` 供 fluent 用法）
→ 三个节点构造参数。

按实例隔离：`HookSet` 只在实例字段上，构建期固化；`HookRunner` 由 builder 为每次 `build()` 新建，
不写模块级可变全局。未注入（`None`）时：`build_tool_node` 不加任何 wrapper，模型链不插任何步骤，
`_execute` 不写运行态键。

### 4. 五个点位的接入

| 点位 | 落点 | 关键实现 |
| --- | --- | --- |
| `session_start` | 模型节点装配后、首次请求前（一次运行一次） | 运行态 `session_started` 去重；`append` 存运行态并按次插到 system 段末尾；`deny` 在发起请求前抛 `HookDeniedError` |
| `model_pre` | 同上，紧随 `session_start`（每轮一次） | 运行态 `model_pre_done` 去重，recovery 重试与工具回灌不再触发；`replace` 存覆盖视图；`deny` 抛错，请求不发 |
| `model_post` | 模型节点产出响应后、返回状态前（每轮一次） | 仅当响应无 `tool_calls` 时触发并置 `model_post_done`；`replace` 改 `response.content` 后返回（随状态落库）；`deny` 抛错 |
| `tool_pre` | `pv_node` 内、Send 分派之前（一次工具调用一次） | 枚举 `state.messages[-1].tool_calls` 中未产出 `ToolMessage` 的调用，按模型顺序逐个调处理器；代理解包名通过既有 `_unwrap` 的判定逻辑取真实工具名；裁决按 `tool_call_id` 存运行态 |
| `tool_post` | 工具 wrapper 链最内层（一次工具调用一次） | `execute()` 前后自计耗时；`is_error` 取 `status == "error"` 或 `is_error_content`；`replace` / `append` 改同一条 `ToolMessage`（`tool_call_id` / `name` / `additional_kwargs` 不变）；`deny` 换成含原因的失败说明并置 `status="error"` |

`tool_pre` 的裁决应用（`core/nodes/tool/hook_wrapper.py`）：

- `pass`：原样继续。
- `deny`：直接返回 `ToolMessage(status="error", content=reason)`，不调 `execute`，不进审批。
- `ask`：先 `interrupt()`，未获批返回拒绝 `ToolMessage`；获批继续（若工具本身还配了 ITSM 审批，继续走既有审批）。
- `replace`：`request.override(tool_call={**call, "args": payload})` 后继续。

位置约束的满足方式：`tool_pre` 应用 wrapper 插在**代理解包之后、不可信净化 / 脱敏 / ITSM 审批之前**；
循环守卫在最外层，先于本次改写判定，因此 `replace` 不影响守卫输入。

### 5. 失败与错误语义

- 处理器抛异常 / 超过 5 秒 / 返回未知裁决 / 缺少必需载荷：该处理器按 `pass` 处理，记 warning，
  同点位其余处理器与业务链路继续（fail-open）。
- `deny` 的可读错误：`HookDeniedError` 消息形如 `hook deny at <point>: <reason>`；
  `session_start` / `model_pre` 的 `deny` 在发请求前抛出，本次运行不产生任何模型请求与工具执行。
- `tool_post` 在工具抛错时同样触发（`execute()` 已返回错误 `ToolMessage`，见现状表）；
  `interrupt()` 冒泡路径（审批、`ask`）不进入 `tool_post`。
- `model_post` 在无最终答复的终止路径（中断、续跑上限、取消）由运行收尾补触发一次，
  此时只执行观察语义；`replace` / `deny` 记 warning 后忽略（运行已终止，无答复可写、无请求可拦）。

### 6. 可观测

每次处理器执行记一条结构化日志：

```
[Hook] point=tool_pre handler=<id(handler)> decision=deny duration_ms=3 agent=<agent_code> session=<session_code> run=<run_id> turn=1 tool=<tool_name>
```

降级另记 `logger.warning("[Hook] degraded point=%s handler=%s reason=%s")`（`exception` / `timeout` /
`invalid_decision` / `invalid_payload`）。不新增 AG-UI 事件。

## 接口与数据

### 协议类型（`core/hook/`）

```python
class HookPoint(str, Enum):
    SESSION_START = "session_start"
    MODEL_PRE = "model_pre"
    MODEL_POST = "model_post"
    TOOL_PRE = "tool_pre"
    TOOL_POST = "tool_post"


class HookDecisionKind(str, Enum):
    PASS = "pass"
    DENY = "deny"
    ASK = "ask"
    REPLACE = "replace"
    APPEND = "append"


@dataclass(frozen=True)
class HookDecision:
    kind: HookDecisionKind
    reason: str = ""      # deny / ask 必填非空
    payload: Any = None   # replace / append 必填


@dataclass(frozen=True)
class HookIdentity:
    agent_code: str
    session_code: str
    run_id: str
    turn: int


@dataclass(frozen=True)
class HookContext:
    point: HookPoint
    identity: HookIdentity
    payload: SessionStartPayload | ModelPrePayload | ModelPostPayload | ToolPrePayload | ToolPostPayload


class HookSet:
    def __init__(self, handlers: Mapping[HookPoint | str, Sequence[HookHandler]]) -> None: ...
    def handlers(self, point: HookPoint) -> tuple[HookHandler, ...]: ...


class HookRunner:
    TIMEOUT_SECONDS = 5.0
    def has(self, point: HookPoint) -> bool: ...
    def run_sync(self, point: HookPoint, identity: HookIdentity, payload: Any) -> HookDecision: ...
    async def run_async(self, point: HookPoint, identity: HookIdentity, payload: Any) -> HookDecision: ...
```

### 五个 payload

| 点位 | 字段 | 允许裁决 |
| --- | --- | --- |
| `session_start` | `message_count: int`、`last_user_input: str`、`tool_names: tuple[str, ...]` | pass / deny / append（`str`） |
| `model_pre` | `messages: list[BaseMessage]`、`model_name: str`、`tool_names: tuple[str, ...]`、`turn: int` | pass / deny / replace（`list[BaseMessage]`） |
| `model_post` | `response: BaseMessage`、`model_request_count: int`、`turn: int` | pass / deny / replace（`str`） |
| `tool_pre` | `tool_name: str`、`tool_call_id: str`、`arguments: dict`、`turn: int` | pass / deny / ask / replace（`dict`） |
| `tool_post` | `tool_name: str`、`tool_call_id: str`、`content: str \| list`、`is_error: bool`、`duration_ms: int`、`turn: int` | pass / deny / replace / append（`str`） |

### 运行态（`core/hook/run_state.py`）

```python
HOOK_RUN_STATE_KEY = "aidev_hook_run_state"

@dataclass
class HookRunState:
    agent_code: str
    session_code: str
    run_id: str
    turn: int = 1
    session_started: bool = False
    session_append: str | None = None
    model_pre_done: bool = False
    model_pre_override: list[AnyMessage] | None = None
    model_pre_anchor_ids: frozenset[str] = frozenset()
    model_post_done: bool = False
    model_request_count: int = 0
    tool_pre_decisions: dict[str, HookDecision] = field(default_factory=dict)


def bind_run_state(
    cfg: RunnableConfig, hooks: HookSet, *, agent_code: str, session_code: str, run_id: str
) -> HookRunState | None: ...


def get_run_state(config: RunnableConfig) -> HookRunState | None: ...


def finalize_run(state_values: Any, config: RunnableConfig) -> None: ...
```

`bind_run_state` 在 `ChatCompletionAgent._execute` 里调用（`hooks is None` 时直接返回 `None`，不写任何键）；
`run_id` 取 `execute_kwargs.turn_id`，为空回退本轮最后一条消息 id；`turn` 起始 1（一次运行一轮）。

### 既有模块的签名变化（均为向后兼容的新增可选参数）

| 模块 | 变化 |
| --- | --- |
| `services/agent/factory.py` | `build_agent(..., hooks: HookSet \| None = None)`；`__init__` 增 `hooks`；`_make_build_context` 填入 `ChatBuildExtras.hooks` |
| `services/agent/registry.py` | `ChatBuildExtras` 增 `hooks: Any = None` |
| `pydantic_models.py` | `AgentExecutorKwargs` 增 `hooks: Optional[Any] = None` |
| `services/agent/chat.py` | `ChatCompletionAgent` 增 `hooks: Any = None`；`build()` 承接；`_get_agent()` 下传；`_execute()` 绑定运行态；流 / 非流出口调用 `finalize_run` |
| `core/graphs/react/graph.py` | `ReActAgentBuilder` 增 `_hooks` 与 `set_hooks()`；`set_bkai_options` 读取 `options.hooks`；三个 `_prepare_*` 透传 |
| `core/nodes/model/node.py` | `build_model_node(..., hooks: HookRunner \| None = None)` |
| `core/nodes/model/model_chain.py` | `_build_model_chain(..., hooks=None)`；装配后插 hook 步骤，链尾插 `model_post` 步骤 |
| `core/nodes/tool/node.py` | `build_tool_node(..., hooks: HookRunner \| None = None)`；新增两个 wrapper 的装配位 |
| `core/nodes/pv.py` | `make_pv_node(..., before_dispatch: Callable[[dict, RunnableConfig], None] \| None = None)` |

### 数据变化

无表结构、无配置项、无事件类型。运行态对象只活在 `config["configurable"]` 与处理器闭包里，
不写入图状态、不落库、不进 AG-UI 消息快照。

## 跨仓契约

- 本仓是唯一业务仓，无跨仓接口、无跨仓依赖、无跨仓执行顺序。
- 处理器由调用方在进程内提供，不定义跨进程/可序列化协议；本仓不新增对外 HTTP 接口与配置下发通道。
- 与 `feature/agent-hook` 无文件级依赖：不引用其 `core/hooks/`，不改其 `specs/agent-hook/`；
  点位名保持同名，便于后续统一词汇。

## 风险

1. **并行工具调用的触发顺序**：`tool_pre` 在 pv 阶段串行执行，顺序稳定；`tool_post` 仍随 Send 并行，
   处理器会并发进入（REQ-4 未要求 post 顺序，规格「并发」一节已允许串行假设的放宽）。
2. **`ask` 的卡片归属**：hook 触发的提问卡片挂在真实工具调用上（`toolCallId` 为该工具调用 id），
   与既有 `ask_user_question` 工具卡片同源；前端是否对非 `ask_user_question` 名的工具渲染问答卡片需联调确认。
3. **超时不可抢占**：同步处理器在独立线程执行，超时后按 `pass` 返回，但线程无法强杀，
   处理器若永久阻塞仍会占一个线程（fail-open 语义保证业务不受影响，线程泄漏风险须在实现与评审中显式接受）。
4. **`session_start` 与 resume**：中断续流是新的运行，`session_start` 会再触发一次，但 `turn` 序号延续、
   `model_pre` 不会重复触发（该轮已打开）。若与既有审批恢复链路出现双触发，以「该轮只开放一次」为准并在实现时逐路径核对。
5. **`model_post` 终止路径**：中断 / 续跑上限 / 取消三种终止靠运行收尾补触发，
   须逐一核对 `_emit_run_end_extras`、`_stream_with_legacy`、`_invoke` 三条出口，避免漏触发或重复触发。
6. **`model_pre` 覆盖视图的锚点**：靠消息 id 判定「覆盖后新增的尾部消息」，历史消息缺 id 时需在装配末尾兜底补 id，
   否则可能出现重复拼接；该兜底只在注入 hooks 时生效。
7. **会话状态零回归**：所有判断都收敛到 `hooks is None` 的 `O(1)` 短路；`build_tool_node` 在无 hooks 时不改变 wrapper 列表形状。

## 测试策略

全部 AC 在本仓用 fake 模型、fake 工具、fake 处理器完成，不依赖真实模型 / 平台 / 宿主。

| 测试文件 | 覆盖 |
| --- | --- |
| `tests/core/hook/test_contracts.py` | 点位与裁决取值、未知裁决/缺载荷降级、注入顺序、短路与覆盖、异常与超时降级、同步与异步一致（AC12、AC13、AC15） |
| `tests/core/hook/test_model_hooks.py` | `session_start` 一次且 append 生效、deny 不发请求（AC1、AC2）；`model_pre` 每轮一次 + 恢复重试与工具回灌不重复触发（AC3、AC5）；`model_post` 每轮一次、replace 成为输出、中间响应不触发、无最终答复时补触发（AC4、AC6） |
| `tests/core/hook/test_tool_hooks.py` | `tool_pre` deny / replace / ask（AC7、AC8）；`tool_post` 原文与 `is_error`、replace 后仍经截断/脱敏（AC9、AC10） |
| `tests/core/hook/test_isolation.py` | 两实例不同集合并发互不串扰、日志不含对方标识（AC14） |
| `tests/core/hook/test_zero_regression.py` | 未注入时模型请求序列、工具执行序列、最终输出、AG-UI 事件序列与基线逐项一致（AC11） |

验证命令（均需在本仓可执行）：

```bash
cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook -q
cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/nodes/test_tool.py tests/core/nodes/tool tests/core/graphs/test_react.py -q
cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/ag_ui tests/core/interrupt -q
```

回归面：`tests/core/nodes/**`、`tests/core/graphs/**`、`tests/services/**`（`ChatCompletionAgent` 装配路径）、
`tests/core/interrupt/**`（中断与恢复）。lint 由仓内 pre-commit（ruff v0.14.10，`line-length=120`）在提交时强制。
