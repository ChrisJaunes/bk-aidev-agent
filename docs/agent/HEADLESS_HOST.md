# 以 aidev-agent 作为宿主核心 Agent（headless 接入指南）

本文档面向**外部宿主**（例如 DeepSeek Harness，下称 dsh）：说明宿主如何通过自己的插件机制，
把核心 Agent 换成 aidev-agent，并用本机 fake 模型离线验收一轮。

依赖方向是 **宿主 → 宿主插件 → aidev-agent**：`bk-aidev-agent` 不引入宿主的 SDK 依赖，
也不提交任何宿主插件代码。本仓提供的只是「可被外部进程驱动的运行面」。

---

## 一、组合 / profile 配置步骤

宿主的默认 Agent 实现由它自己的插件提供。多数宿主（含 dsh）的默认实现在**插件加载时无条件**
向宿主注册 Agent 工厂，且注册表拒绝二次注册（重复注册会直接抛 `an agent factory is already registered`
之类的错误）。因此接入方式是**替换**，不是**叠加**：

1. 在宿主的组合（composition）/ profile 配置里，**停用**默认 Agent 实现所在的那一行插件
   （dsh 中是 `@deepseek-ai/dsh-agent-loop`）。
2. 在同一位置**启用**仓外插件行（由接入方维护，不提交到本仓）。
3. 该插件在 `createAgent` 时拉起子进程 `python -m aidev_agent.headless`，用 `create` 建立会话；
   后续 `send` / `resume` / `cancel` / `handle.dispose` 分别映射到 `turn` / `resume` / `cancel` / `close`。
4. 不要改宿主源码：一旦两条插件行同时启用以至于重复注册，宿主会直接抛错；停用默认行即可。

> 本仓不感知、也不校验宿主的组合配置；上面对宿主行为的描述只是接入前提。
> 停用插件后宿主应回到自身默认行为（接入是可选能力）。

模型配置不取自宿主：`create.llm` 指向 **aidev-agent 侧**的模型端点（验收时指向本机 fake 端点）。
一个 headless 进程只有一个模型端点（进程级注入），会话之间可不同 `model`。

---

## 二、方法映射与报文格式

传输层是 **NDJSON over stdio**：stdin 一行一条请求，stdout 先出事件行、再出一条响应行，
每行都带请求 `id`；日志一律走 stderr。

### 2.1 请求 / 响应

```json
{"id": "req-1", "method": "create", "params": {"session_code": "s-1", "agent": {"agent_code": "my-agent"}, "llm": {"base_url": "http://127.0.0.1:8000/v1", "model": "fake-model"}}}
{"id": "req-1", "type": "event", "event": {"type": "RUN_STARTED", "threadId": "s-1", "runId": "..."}}
{"id": "req-1", "ok": true, "result": {"session_code": "s-1", "created": true}}
```

失败时响应为 `{"id": "...", "ok": false, "error": {"code": "...", "message": "...", "detail": {...}}}`，
并在该 `id` 下额外写出 `RUN_ERROR` 事件。

### 2.2 五方法

| method | params（粗体必填） | result |
| --- | --- | --- |
| `create` | **`session_code`**、**`agent`**（`{"agent_code": "..."}` 或字符串）、`llm`（`base_url` / `model` / `auth_headers` / `temperature` / `max_tokens`）、`username`、`hooks`（见第六节）、`tools`（仅进程内注入，非协议承诺） | `{"session_code", "created": true}` |
| `turn` | **`session_code`**、`input`、`messages`（非空时走无状态模式） | `{"session_code", "turn_id", "status": "finished\|interrupt\|cancelled\|error"}` |
| `resume` | **`session_code`**、`input`（续轮）、`resume`（中断恢复负载，见 6.4） | 同 `turn` |
| `cancel` | **`session_code`** | `{"session_code", "cancelled": true}` |
| `close` | **`session_code`** | `{"session_code", "closed": true}` |

宿主侧映射（dsh 语义）：

| 宿主动作 | headless 方法 |
| --- | --- |
| `AgentFactory.createAgent(...)` | 拉起子进程 + `create` |
| `agent.send(...)` | `turn` |
| `AgentFactory.resume(...)` | `resume`（同一会话续轮） |
| `agent.cancel()` | `cancel` |
| `AgentHandle.dispose()` | `close` |

### 2.3 事件契约

事件是 `ag_ui.core.events` 的原对象（`model_dump(mode="json", by_alias=True)`），不新增类型、
不裁剪字段。每轮必须收尾于终止类事件，三态可区分：

| 结束原因 | 终止事件 | 响应 |
| --- | --- | --- |
| 正常结束 | `RUN_FINISHED`（`runId` 为本轮 run，`outcome.type=success`） | `ok=true`，`result.status=finished` |
| 本轮停在中断 | `RUN_FINISHED`（`outcome.type=interrupt`，`outcome.interrupts[0]` 含 `id` / `reason` / `message` / `toolCallId` / `metadata`） | `ok=true`，`result.status=interrupt`，另带 `result.interrupts` |
| 用户取消 | `RUN_ERROR`（`message` = 用户已取消）+ `RUN_FINISHED`（`runId` = `cancelled`） | `ok=true`，`result.status=cancelled` |
| 执行期错误（模型 5xx / 端点不可达等） | `RUN_ERROR`（`message` 为错误原因）+ 平台补发的 `RUN_FINISHED`（`outcome.type=success`） | **`ok=false`**，`error.code=agent.internal_error`，`error.message` 含该 `RUN_ERROR` 文案 |
| 不支持 / 协议或会话错误 | `RUN_ERROR`（`message` 为结构化原因） | `ok=false`，`error.code` 为对应错误码 |

> **判定本轮成败只看响应 `ok`**。执行期错误路径上，平台会在 `RUN_ERROR` 之后补发一条
> `RUN_FINISHED(outcome.type=success)`（与平台基线 SSE 序列一致，headless 不裁事件、
> 也不重复补 `RUN_ERROR`）。因此**不能**用「收到 `RUN_FINISHED`」判定成功：该轮响应必为
> `ok=false`，宿主应据此把本轮投影为失败（例如 dsh 的 `turn_end` 用 `error` 而非 `completed`）。
> 同一条 `RUN_ERROR` 只出现一次。

### 2.4 会话语义与并发

- `session_code` 由宿主提供并在响应中原样回传；headless 不自造标识。
- 同一会话的 `turn` 串行；不同会话可并发；`cancel` 在有界时间（≤10s）内让当前轮以取消终止事件收尾。
- `close` 之后该会话的任何请求返回 `session.closed`，其他会话不受影响。
- `turn.messages` 非空时走无状态模式：以调用方传入的历史为一轮输入，不写会话账本。

### 2.5 错误码

`protocol.invalid_json`、`protocol.unknown_method`、`protocol.invalid_params`、`session.not_found`、
`session.closed`、`session.busy_cancelled`、`session.pending_interrupt`、`agent.build_failed`、
`agent.unsupported`、`agent.internal_error`、`resume_payload_missing`、`resume_payload_unsupported`、
`resume_interrupt_not_found`、`resume_interrupt_unsupported`、`hook.value_invalid`、
`platform_api_unsupported`。

`resume_payload_unsupported` 语义已收窄为「负载不是中断恢复形状」；`resume_interrupt_not_found`
表示 `interruptId` 未命中该会话 pending 中断；`resume_interrupt_unsupported` 表示命中的是
ITSM 权限审批中断（本期 headless 不接线）；`session.pending_interrupt` 表示还有未恢复的中断
又来了一轮 `turn`。

`agent.internal_error` 覆盖两类执行期失败：headless 捕获到的未归类异常，以及执行链只发
`RUN_ERROR` 事件、由平台补发 `RUN_FINISHED` 的错误轮（模型 5xx、端点不可达）。后者的
`error.message` 直接取该 `RUN_ERROR` 的文案，便于宿主定位上游原因。

---

## 三、支持面与不支持面

**支持**：`create` / `turn` / `resume` / `cancel` / `close`，纯文本一轮对话、无状态历史、
同一会话续轮、会话级关闭与取消、三个 hook 点（`session_start` / `tool_pre` / `tool_post`）、
hook `ask` 与 `ask_user_question` 两类中断的恢复。

**不支持**（宿主调用必须显式失败，禁止静默降级）：`inbox` 多队列（next-turn / next-step）、
`steer` / `inject` / `followup` 的逐步语义、`runMaintenance`、compaction、子 Agent 与多 Agent
因果链、宿主工具注册与审批体系映射、会话落库双向同步。

插件实现这些能力时应**直接抛错**并把错误透传给宿主，不得回落到「当成普通 `turn` 处理」。
headless 侧的中断恢复只接线 ask 类（`aidev:hook_ask` / `aidev:user_question`）；ITSM 权限审批
中断（`aidev:tool_approval`）的 `resume` 返回 `resume_interrupt_unsupported`，不改图状态。

---

## 四、用本机 fake 模型完成验收

1. 启动 fake 端点（仅回环地址），**两个接口都要实现**：
   - `POST {base}/chat/completions`（OpenAI 兼容，支持 `stream=true` 的 SSE 与 `data: [DONE]`）
   - `POST {base}/api/token_check`（返回 `{"prompts": [{"tokenCount": N}]}`）

   漏实现 `/api/token_check` 时，纯文本一轮也会在 token 统计处失败，且报错点远离根因。

2. 起 headless 子进程（`MESSAGE_HANDLER_TYPE=inmemory`，取消依赖内存消息处理器）：

   ```bash
   python -m aidev_agent.headless
   ```

3. 依次发 `create` → `turn`，期望：`RUN_STARTED` → `TEXT_MESSAGE_*` → `RUN_FINISHED`，
   且 `turn` 响应 `status=finished`。

4. 接入宿主后重跑：宿主日志应显示该轮由 aidev-agent 产出，且默认 Agent 实现未参与
   （默认插件行已停用）。

5. 反向验证：停用仓外插件、以宿主默认组合启动，宿主行为不变。

---

## 五、日志与凭据约束

- stdout **只承载协议数据**，日志全部走 stderr；启动时会把 root logger 与全部 handler 指向 stderr。
- 不监听网络端口，只走 stdio；fake 端点仅回环地址。
- 日志字段最多出现 `agent_factory=<实现类名>` 与 `headless_session=<session_code>`；
  不记录 `llm` 段、`auth_headers`、完整请求体（协议错误只报字段名与类型）。
- 错误响应只含 `code` / `message` / `detail`（字段名、类型名），不含凭据与请求体。
- hook 相关日志只记点位、裁决取值、耗时与降级原因，不记载荷全文；hook 事件（`hook_degraded`）
  只含 `point` 与 `reason`。
- 未启用 headless 时不产生任何常驻进程、端口或后台线程。

---

## 六、hooks 一节（6 点位，进程内处理器 + headless 传输）

hook 机制在 SDK 里是**进程内**的（`core/hook`）：`HookSet` 持有 6 个点位的处理器，
`HookRunner` 按注入顺序分发，处理器直接读写 LangChain / LangGraph 原生对象。
headless 侧只做一层**传输**：把点位变成一次 NDJSON 往返，让宿主（dsh 插件）能插话。
本仓不解析 `hooks.json`、不跑命令、不实现 matcher —— 那些是宿主职责。

### 6.1 六个点位

| 点位 | 时机 | 载荷 | 处理器可返回 |
| --- | --- | --- | --- |
| `turn_start` | 图入口，一轮开始前 | `messages`（整表） | 新的消息列表（整表替换；常用注入一条 system） |
| `model_pre` | 每次模型调用前 | `messages` | 新的消息列表 |
| `model_post` | 每次模型调用成功后 | 助手消息（`content` / `tool_calls`） | `{"retry": true}` → `HookRetry`，其余忽略 |
| `tool_pre` | 工具派发前 | `tool_name` / `tool_input` / `tool_use_id` | `{"decision":"allow"\|"deny","reason"}`；`deny` 早退不执行 |
| `tool_post` | 工具本体返回后、净化前 | `tool_name` / `tool_use_id` / `tool_response` / `status` | `accept` / `block` / `content` / `context` |
| `turn_end` | 本轮走到终点 | `turn_id` | 不等答复（通知型） |

### 6.2 启用方式

`create` 请求体的 `params.hooks`：

```json
{"timeout_ms": 3000, "points": ["turn_start", "tool_pre", "tool_post"]}
```

| 字段 | 默认值 | 约束 |
| --- | --- | --- |
| `timeout_ms` | `3000` | 单点等待上限；必须为正整数，上限 4000（`HookRunner` 对处理器有 5s 上限） |
| `points` | 六点全开 | 只接受上表六个点位名；未知点位 → `hook.value_invalid` |

不传 `hooks` 即完全关闭：不注册任何处理器、不产生 `type=request` 行、图拓扑与基线一致。

### 6.3 两类协议行与 `id` 派生

agent → host 请求行：

```json
{"id": "req-1#h1", "type": "request", "method": "hook",
 "params": {"session_code": "s-1", "turn_id": "...", "point": "tool_pre",
            "payload": {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "call_1"}}}
```

host → agent 答复行：`{"id": "req-1#h1", "ok": true, "result": {"decision": "allow"}}`
或 `{"id": "req-1#h1", "ok": false, "error": {"code": "...", "message": "..."}}`。

- `id` 为 `<本轮请求 id>#h<n>`，`n` 在会话内自 1 递增；
- 答复行不产生响应行、不进入请求解析、不会被报成未知方法；
- 请求行与事件行共用同一把写锁，不会交错。

### 6.4 裁决语义

| 点位 | result | 落地 |
| --- | --- | --- |
| `turn_start` / `model_pre` | `{"context": "..."}` | 在消息表最前面插一条 system（不改账本、不改 `turn_id`） |
| `tool_pre` | `{"decision": "deny", "reason": "..."}` | 返回 `ToolMessage(status="error")`，工具零执行；`ask` 无对应机制，按 `deny` 处理并在 reason 里说明 |
| `tool_post` | `{"decision": "block", "reason": "..."}` | 模型只看到 reason，原结果不进上下文 |
| `tool_post` | `{"decision": "content", "content": "..."}` | 替换模型可见结果 |
| `tool_post` | `{"decision": "context", "context": "..."}` | 追加到同一结果尾部（`[hook context]` 分隔） |
| `model_post` | `{"retry": true}` | 抛 `HookRetry`，模型链按既有重试策略重来 |

任何裁决都不会让工具本体重复执行，也不改变 `tool_call_id` 配对。

### 6.5 fail-open 与 `hook_degraded`

超时、宿主 `ok=false`、`result` 不是对象、未给 `decision`：一律按「不干预」继续
（返回 `None`），并写一条既有 `CUSTOM` 事件（`name="hook_degraded"`，`value={"point","reason"}`），
不新增 `EventType`。降级只影响该点位，不阻断本轮。

### 6.6 宿主侧（dsh 插件）职责

- 读 `hooks.json`、按 matcher 选组、跑命令、合并多份裁决（最严格优先）、写 `hook/invoked` 与 `hook/result` 审计；
- 把 Claude 方言的事件名映射到本仓点位：`SessionStart → turn_start`、`UserPromptSubmit → model_pre`、
  `PreToolUse → tool_pre`、`PostToolUse → tool_post`、`Stop → turn_end`；
- 复用宿主 hooks 原语属**半私有导出**：钉版本 + import 冒烟，升级后先跑冒烟；
- 插件加载失败不影响本仓 headless 行为（关闭态零回归）。
