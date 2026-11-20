# acp-dsh 协议（ACP v1 + `_aidev/*` 扩展）

`python -m aidev_agent_cli --resources injected`（默认协议即 `acp-dsh`）是供 dsh 一类宿主驱动的注入式运行面，取代了原先自定义的
headless NDJSON 协议（已删除）。线上是**标准 ACP v1**（JSON-RPC 2.0，stdio，一行一帧）；ACP 没有覆盖的能力
（下发模型 / hook / 中断恢复）放在规范允许的 `_meta` 与 `_` 前缀扩展方法里。

- 显式 `--resources injected` 时不触达平台、不需要 token；省略资源时使用统一 `default` 预设。需要 `aidev-agent-cli[acp]`。
- 一个进程承载多个会话；同会话的轮次排队串行，不同会话并发。
- stdout 只承载协议帧，日志走 stderr / 日志文件。
- 嵌入式接缝：入口脚本可调用 `run_acp_dsh(service, tool_factories=("module:factory",))` 注入 worker 内构造的工具（不传 live 工具）（不是线上协议的一部分）。

## 1. 方法总览

| 方向 | 方法 | 说明 |
| --- | --- | --- |
| client→agent | `initialize` | 标准；响应 `agentCapabilities.sessionCapabilities.close = {}`，`_meta.aidev = {protocol:"acp-dsh", revision:1, agentMethods, clientMethods}` |
| client→agent | `session/new` | 标准 + `_meta.aidev`（见 §2）；`sessionId` 即会话 code |
| client→agent | `session/prompt` | 标准；一轮。响应 `stopReason` + `_meta.aidev`（见 §3） |
| client→agent | `session/cancel` | 标准通知；只取消目标会话 |
| client→agent | `session/close` | 标准；幂等 |
| client→agent | `_aidev/resume` | 扩展请求：中断恢复（见 §4） |
| agent→client | `session/update` | 标准通知：`agent_message_chunk` / `agent_thought_chunk` / `tool_call` / `tool_call_update` |
| agent→client | `_aidev/hook` | 扩展请求：hook 裁决往返（见 §5） |
| agent→client | `_aidev/hook_degraded` | 扩展通知：hook 降级 `{sessionId, point, reason}` |

## 2. `session/new`

```json
{"cwd": "/abs/path", "mcpServers": [],
 "_meta": {"aidev": {
   "sessionCode": "s-1",
   "agent": {"agent_code": "my-agent"},
   "llm": {"base_url": "http://127.0.0.1:18080/v1", "model": "fake-model", "auth_headers": {}, "temperature": 0.2, "max_tokens": 4096},
   "username": "alice",
   "hooks": {"timeout_ms": 10000, "points": ["session_start", "tool_pre", "tool_post"]}
 }}}
```

- `agent` 为对象或字符串；省略时使用进程已配置的主 agent，二者都没有才报参数错误。`sessionCode` 省略时由 agent 生成；已存在的 code 报 `protocol.invalid_params`。
- `hooks` 省略即 hook 完全关闭（不会出现 `_aidev/hook` 请求）。
- `mcpServers` 暂被忽略。

## 3. `session/prompt`

请求：标准 `{sessionId, prompt:[ContentBlock]}`，只取文本块；`_meta.aidev.messages`（可选）走无状态模式。

响应：

| 结果 | `stopReason` | `_meta.aidev` |
| --- | --- | --- |
| 正常结束 | `end_turn` | `{status:"finished", turnId}` |
| 停在中断（hook ask / 用户提问） | `end_turn` | `{status:"interrupt", turnId, interrupts:[{id, reason, message, toolCallId, metadata}]}` |
| 被取消 | `cancelled` | `{status:"cancelled", turnId}` |
| 失败 | —— JSON-RPC error | 见 §6 |

**判定成败只看是不是 error 响应**，不要根据 `session/update` 推断。

## 4. `_aidev/resume`

```json
{"sessionId": "s-1", "resume": {"interruptId": "<interrupts[0].id>", "toolCallId": "<可选>", "decision": "allow"}, "input": "<可选>"}
```

`decision` 取 `allow` / `deny`，用户提问中断可传 `{"answers": [...]}`。响应体与 `session/prompt` 相同
（`{stopReason, _meta:{aidev:{...}}}`）。未命中 → `resume_interrupt_not_found`；形状不对 → `resume_payload_unsupported`；
ITSM 审批中断 → `resume_interrupt_unsupported`。有未恢复中断时再发 `session/prompt` → `session.pending_interrupt`。

## 5. `_aidev/hook`（agent → client）

```json
{"sessionId": "s-1", "requestId": "<轮次请求 id>#h1", "session_code": "s-1", "turn_id": "...",
 "point": "tool_pre", "payload": {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "call_1"}}
```

客户端返回的 `result` 即裁决：

| point | 载荷 | 裁决 |
| --- | --- | --- |
| `session_start` | `session_code` / `turn_id` / `first_turn` / `input` | `{context}` |
| `tool_pre` | `tool_name` / `tool_input` / `tool_use_id` | `{decision: allow\|deny\|ask, reason?}` |
| `tool_post` | 同上 + `tool_response` | `{decision: accept\|block\|content\|context, reason?, content?, context?}` |

fail-open：超时（`timeout_ms`）、客户端返回 error、结果不是对象、取值未知 → 按最宽松裁决继续，并发
`_aidev/hook_degraded`；等待期间被取消则直接按取消收尾。`ask` 会让本轮以 `status:"interrupt"` 结束，工具零执行。

## 6. 错误

失败一律是 JSON-RPC error：参数类错误 `-32602`，其余 `-32603`；`error.data = {code, message, detail?}`，`code` 沿用稳定错误码：
`protocol.invalid_params`、`session.not_found`、`session.closed`、`session.pending_interrupt`、`agent.build_failed`、
`agent.unsupported`、`agent.internal_error`、`resume_*`、`hook.value_invalid`、`platform_api_unsupported`。

## 7. 执行与生命周期

每轮事件解释器和 hook transport 独立。adapter 保存 hook 配置，在每次 start/resume 时显式传入；跨轮 Session 不持有连接、sink、编码器或待答复路由。

同会话通过 active + FIFO queue 串行。排队取消立即落定且不装配 SDK；活动取消协作等待后强杀进程并 join，再允许下一轮。强杀无法撤销已经提交的外部副作用。

本地 SQLite 保存历史、pending、hook memo；重新登记同一 sessionCode 可恢复。ContextInput（包括显式空 messages）不修改该会话的持久历史与 pending。

线上扩展允许省略 resume 负载或提供 {input: ...} 表示普通续轮；adapter 将其接到 start(ChatInput)，核心 resume 只接受含 interruptId 的 ResumeInterrupt。
