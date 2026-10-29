# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: 56e6865de8e40a64b1e3bed17f5b06546546f3c6
- 变更名: agent-hook（`specs/agent-hook/`）
- 轮次: 第 3 轮（第二期 hook 首次评审）

### 阻塞项

无。第二期 REQ-13 ~ REQ-20 / ACC-14 ~ ACC-22 逐条满足；ACC-23 按规格「测试约束」
记为未取证（缺仓外 A 档插件），不构成阻塞。一期已通过的 REQ-1 ~ REQ-12 未被本轮改动回归。

### 改进建议

以下均为一般改进建议，不构成返工理由。W-1 ~ W-7 为一期评审遗留（本轮复核仍存在），
W-8 ~ W-12 为本期新增。

- W-1 `DefaultAgentFactory.resume` 接受 `{"input": str}` 续轮形状后直接丢弃负载
  （`services/agent/factory.py:506-508`），headless 侧 `session.py:228` 也用 `payload` 只做校验、
  真正执行的输入取 `params.input`。建议统一语义，或在 `normalize_resume_payload` 文档写明
  「该形状仅供内部校验」。
- W-2 `HeadlessSession.prepare()`（`session.py:167-173`）装配出的实例存进 `_pending_agent` 后
  从未使用也从未 `stop()`；`_take_agent`（`session.py:442`）只把它置空。一期的死参数
  `allow_pending` 已在本轮删除，剩余这半边仍建议收敛（`prepare` 结束即 `stop()`，或去掉暂存）。
- W-3 未归类异常一律拼进对外可见文案：`f"{type(exc).__name__}: {exc}"`
  （`session.py:315-318`、`cli.py:155`、`cli.py:182`）。上游错误体片段会经 `RUN_ERROR` 与响应回显；
  REQ-11 / REQ-19 要求不输出凭据与完整请求体，建议对上游错误做一次归一化（只保留错误类与脱敏摘要）。
- W-4 `close` 第二次调用经 `_session_for` 的 `ensure_open`（`cli.py:250-253`）直接报 `session.closed`，
  使 `HeadlessSession.close()` 的 `already_closed`（`session.py:360-372`）在协议层不可达；
  `SessionRegistry.create`（`session.py:638-649`）对同 `session_code` 直接覆盖且不关闭旧会话。
- W-5 `cancel` 收敛超时走 `writer.finalize()`（`session.py:349-353`）补发取消终止事件后，
  仍在运行的轮次还可能继续写出事件，破坏「以终止事件收尾」。建议超时后标记本轮已终止并丢弃后续事件。
- W-6 REQ-4 / ACC-3（续流入口收敛）按一期技术方案「规格偏差与待修订」整体延后；`spec.md`
  仍保留 REQ-4 / ACC-3 原文，建议后续变更同步修订，避免规格与实现长期不一致。
- W-7 `tests/headless` 一期返工时出现过 1 例未捕获用例名的失败（`verification.md` 已如实记录）；
  本轮三组套件各跑一次全绿、未复现，暂不视为缺陷。
- W-8 `turn` 的 pending 中断前置检查在会话锁之外（`session.py:180-185`）：同会话并发两个 `turn`
  时，后到者可能在前者以中断收尾前通过检查，随后带着 pending 中断拉图。命中后仍以结构化错误
  收尾，属健壮性缺口；建议把该检查移到 `_drive_turn` 的会话锁内。
- W-9 中断恢复轮若在执行期失败（`status=error`），`_pending_interrupts` 会被清空
  （`session.py:287`），此后同一 pending 中断无法再用正确的 `interruptId` 恢复（返回
  `resume_interrupt_not_found`）。ACC-21 只覆盖「校验失败」路径，建议失败路径保留 pending 缓存。
- W-10 `keep_ask_memo` 对任意中断都置真（`session.py:277-280`），ITSM 审批中断也会保留
  ask memo，与 pending 生命周期不一致；建议仅对 `aidev:hook_ask` 保留。
- W-11 宿主回 `{"result": {}}`（缺 `decision`）时按 allow / accept 放行且不写 `hook_degraded`
  （`core/hooks/bus.py:114-116`、`:130-135`）：与 REQ-18「`result` 形状不合法 → `invalid_shape`」
  的边界口径不够明确，建议在接入文档写明「缺 `decision` 视为最宽松裁决、不降级」。
- W-12 wrapper 顺序不变式只有同步链断言（`tests/core/nodes/tool/test_tool_node_hook_order.py`
  仅比对同步链成员与顺序），异步链（`_chain_async_tool_call_wrappers`）无对应用例；建议补一条
  异步链断言，防止后续重构把 `hook_pre` 移出链首而破坏 REQ-15 的「deny 早于审批」。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "agent-hook",
  "iteration": 3,
  "reviewed_sha": "56e6865de8e40a64b1e3bed17f5b06546546f3c6",
  "blocking": [],
  "warnings": [
    {"id": "W-1", "evidence": "factory.py:506-508 与 session.py:228：resume 负载 {\"input\": str} 被校验后丢弃，执行输入取 params.input", "suggestion": "统一语义，或在文档写明该形状仅供内部校验"},
    {"id": "W-2", "evidence": "session.py:167-173 prepare() 装配实例后不 stop；session.py:442 只置空 _pending_agent", "suggestion": "prepare 结束即 stop，或移除暂存"},
    {"id": "W-3", "evidence": "session.py:315-318、cli.py:155/182 把上游异常串写入 RUN_ERROR 与响应", "suggestion": "对上游错误做归一化与脱敏"},
    {"id": "W-4", "evidence": "cli.py:250-253 使 close 的 already_closed 不可达；session.py:638-649 同 session_code 覆盖旧会话不关闭", "suggestion": "收敛重复 close / 重复 create 语义"},
    {"id": "W-5", "evidence": "session.py:349-353 cancel 超时补发终止事件后，运行中的轮次仍可继续写事件", "suggestion": "超时后标记终止并丢弃后续事件"},
    {"id": "W-6", "evidence": "spec.md 仍保留 REQ-4/ACC-3，plan.md 第 294-302 行登记为已批准偏差", "suggestion": "后续变更同步修订规格"},
    {"id": "W-7", "evidence": "一期 verification.md 记录一次未捕获用例名的偶发失败；本轮三组套件未复现", "suggestion": "再现时保留完整输出以定位"},
    {"id": "W-8", "evidence": "session.py:180-185 pending 中断前置检查在会话锁外，并发 turn 存在 TOCTOU 窗口", "suggestion": "把检查移入 _drive_turn 的会话锁内"},
    {"id": "W-9", "evidence": "session.py:287 恢复轮执行期失败会清空 _pending_interrupts，同一 pending 中断此后无法恢复", "suggestion": "失败路径保留 pending 缓存"},
    {"id": "W-10", "evidence": "session.py:277-280 keep_ask_memo 对任意中断置真，ITSM 中断也保留 ask memo", "suggestion": "仅对 aidev:hook_ask 保留 memo"},
    {"id": "W-11", "evidence": "core/hooks/bus.py:114-116/130-135 缺 decision 时按最宽松放行且不写 hook_degraded", "suggestion": "在接入文档写明该边界口径"},
    {"id": "W-12", "evidence": "test_tool_node_hook_order.py 只断言同步 wrapper 链，异步链顺序无用例", "suggestion": "补一条异步链顺序断言"}
  ]
}
```

</details>

## 第 1 轮

### 评审范围

- 业务仓 `bk-aidev-agent`，工作分支 `feature/agent-hook`，评审起点
  `4c1d0381338ab4c5d80088ff9a6c5551c9935c0c`（与 `git ls-remote` 远端一致）。
- 任务基线 `develop` @ `7a171a8af285c24733d75a16428ef31edfcbbd05`；累计差异 28 文件 / +3842 / -17。
- 输入：`spec.md`、`plan.md`、`tasks.md`、`verification.md`、`acceptance/`、上游三个节点的 handoff。

### 规格与冻结文档一致性

- `specs/agent-hook/{spec,plan,tasks}.md` 与上游交付逐字一致（`diff -q` 无差异），实现阶段未改冻结定义。
- 变更名 / 分支 / 仓库 / 规格目录与 `spec.md` 头部一致；分支在远端存在。
- REQ-4 / ACC-3 的延后已在 `plan.md`「规格偏差与待修订」（第 294-299 行）登记，属技术方案阶段确认的偏差，
  本轮不据此拒收，仅记为 W-6。

### 逐需求核对

| 需求 | 结论 | 依据 |
| --- | --- | --- |
| REQ-1 注入点协议与注册中心 | 满足 | `AgentFactoryProtocol` + `SingletonFactory("agent_factory", DefaultAgentFactory())`，与仓内 `replace_defaults` 范式一致 |
| REQ-2 默认实现与现状等价 | 满足 | `DefaultAgentFactory._build` 逐字复刻原 `build_agent` 方法体；`build_agent` 仅剩一次委派 |
| REQ-3 创建入口单一化 | 满足 | 全仓仅 `factory.py:530` 构造 `AgentInstanceFactory`；5 处调用方 diff 为空；headless 走 `agent_factory()` |
| REQ-4 恢复入口单一化 | 延后（已登记偏差） | 平台续流调用点未改；`resume` 仅有 headless 与单测调用方 |
| REQ-5 不支持能力显式失败 | **不满足（F-1）** | 构建/恢复/未支持方法均为结构化错误；但执行期 in-band 错误返回空回复且 `ok=true` |
| REQ-6 headless 五方法 | 满足 | `create/turn/resume/cancel/close`；stdout 仅协议行，日志改道 stderr |
| REQ-7 事件契约与终止态 | **部分不满足（F-1）** | 事件复用 `ag_ui.core`、无新类型；但内部错误轮在响应侧不可判定，事件序列以 success 终止 |
| REQ-8 无平台依赖 | 满足 | 子进程断言 `django` / `blueapps` 均不在 `sys.modules`，平台调用计数为 0 |
| REQ-9 会话语义 | 满足 | `session_code` 原样回传；`messages` 无状态模式；`resume` 续轮；中断负载显式失败 |
| REQ-10 生命周期与并发 | 满足 | 同会话锁串行 / 跨会话并发 / cancel ≤10s / close 后拒绝 / 异常后进程可用 |
| REQ-11 依赖与隔离 | 满足（有 W-3 风险） | 依赖声明无 diff；注入凭据未出现在 stdout/stderr（实测 + 用例） |
| REQ-12 接入文档 | 满足 | `docs/agent/HEADLESS_HOST.md` 覆盖组合步骤、方法表、支持/不支持面、fake 验收 |

### 验证记录复核

- 未重跑全量回归；对记录做了抽样重跑，结果与 `verification.md` 一致：
  - `pytest tests/headless` → 54 passed（记录 54）。
  - `pytest tests/services/agent` → 30 passed（含 T-1/T-2/T-3 的 16 条）。
- 新增用例无 `skip` / `xfail`；事件类型集合断言为 `ag_ui.EventType` 子集，与「不新增事件类型」一致。
- 为验证 REQ-11 与错误路径，额外做了两次定向探测（不属于既有记录）：
  - 端点不可达 + 注入 `X-Bkapi-Authorization` 密钥：stdout/stderr 均无密钥泄漏；
  - 端点返回 500：暴露 F-1。

### 集成验收（ACC-11 / ACC-12）

- 证据目录 `acceptance/` 完整（fake 端点、仓外插件、overlay、两轮日志），日志内容与记录描述一致；
  `patched-run.stdout.log` 为 `你好，我是 aidev-agent`，`patched-run-json.stdout.log` 为
  `turn_start → step_start → text → step_end → turn_end(completed) → final`；默认组合日志为
  `MISSING_CREDENTIAL`。本轮未重跑 dsh（重跑需 pnpm 构建全仓，超出评审定向范围）。

### 结论

注入点与 5 处创建入口收敛、headless 五方法面、事件复用与无平台隔离均达标，改动范围与冻结文档一致。
唯一阻塞项是错误路径的对外语义：执行期错误只以事件出现时，headless 以 `ok=true` + 空回复结束本轮，
既违反 REQ-5 的「不得返回空回复」，也使 REQ-7 的三态在响应侧不可判定，并被仓外验收入口插件投影为成功。
按固定验收规则「任一项不满足即打回代码实现」，本轮判 `REWORK`，返回代码实现节点。

## 第 2 轮

### 评审范围

- 业务仓 `bk-aidev-agent`，工作分支 `feature/agent-hook`，评审起点
  `550fe122ace6ebe98a06f37bdf97a14198ec1c96`（与 `git ls-remote` 远端一致，工作区 clean）。
- 本轮性质：第 1 轮 F-1 返工复核；同时按累计差异重新核对全部已审批需求。
- 返工提交范围：`git diff 4c1d0381..550fe122` 共 6 文件（含第 1 轮 `review.md`），
  改动集中在 `headless/session.py`（+14/-1）、`docs/agent/HEADLESS_HOST.md`、
  `tests/headless/{_fake_llm,test_error_recovery}.py`、`specs/agent-hook/tasks.md`（仅勾选与返工记录）。

### F-1 复核

修复位置 `src/agent/aidev_agent/headless/session.py:190-204`：`_finish_turn()` 返回 `error` 时构造
`HeadlessError(AGENT_INTERNAL_ERROR, writer.error_message or 兜底文案)` 作为结果返回，`Dispatcher`
既有「`status=error` 且带 `error` 载荷」分支因此改写为 `ok=false`，且不补写事件。

本轮实测（独立探测，非沿用其记录）：

| 场景 | 事件序列 | 响应 |
| --- | --- | --- |
| 端点返回 500（`turn`） | `RUN_STARTED → STEP_STARTED → RUN_ERROR(模型调用异常: upstream exploded) → RUN_FINISHED(success)` | `ok=false`，`code=agent.internal_error`，message 含上游文案 |
| 端点返回 500（`resume`） | 同上，`RUN_ERROR` 仅 1 次 | `ok=false`，`code=agent.internal_error` |
| 端点不可达（`turn`） | `... RUN_ERROR(模型调用异常: Connection error.) → RUN_FINISHED(success)` | `ok=false`，`code=agent.internal_error` |

对照第 1 轮的三条验收标准：

1. 「该场景响应必须 `ok=false` 且带稳定 `error.code` / `error.message`」→ **满足**（`agent.internal_error`）。
2. 「事件序列保持基线、不重复写 `RUN_ERROR`，并以 fake 端点固定 500 的用例断言」→ **满足**；
   新增 `test_model_5xx_returns_structured_failure_without_duplicate_run_error` 同时覆盖 `turn` 与 `resume`，
   断言首事件 `RUN_STARTED`、末两条 `RUN_ERROR + RUN_FINISHED`、`RUN_ERROR` 计数为 1、`outcome.type=success`。
3. 「文档与实现一致，写明宿主以响应 `ok` 判定失败」→ **满足**；`HEADLESS_HOST.md` §2.3 增「响应」列并明确
   「判定本轮成败只看响应 `ok`」「同一条 `RUN_ERROR` 只出现一次」，§2.5 说明 `agent.internal_error` 的两类来源。

附带核对：取消路径未被误判为错误——`SessionEventWriter._observe` 先按 `RunId.CANCELLED_MESSAGE` 归入
`cancelled`（`events.py:172-179`），`_finish_turn` 再按 `_cancel_requested` 覆盖状态，`test_concurrency.py`
的取消用例 5 次复跑均以 `status=cancelled` + `RUN_ERROR(用户已取消) + RUN_FINISHED(runId=cancelled)` 收尾。

### 全量复核

- 冻结文档：`spec.md`、`plan.md` 与审批版本逐字一致；`tasks.md` 仅新增勾选与两条返工记录，定义未改。
- 5 处创建调用方（`services/agent_bkplugin.py` ×2、`services/agent_builder.py`、`views/chat.py`、
  `aidev_wxbot/wxaibot/strategies.py`）与依赖声明（`pyproject.toml`、`uv.lock`）累计 diff 仍为空。
- 验证记录复核（本轮抽样重跑，结果一致）：
  - `pytest tests/headless`：55 passed，连续 3 次全绿；
  - `pytest tests/headless/test_concurrency.py`：5 passed，连续 5 次全绿；
  - `pytest tests/services/agent`：30 passed。
- `verification.md` 记录的「首次运行 1 例未捕获用例名失败」本轮未复现（3 次全量 + 5 次并发用例），
  按记录如实保留为 W-7，不作为阻塞项。
- 仓外验收证据 `acceptance/` 与第 1 轮逐字一致（`diff -r` 无差异），本轮能力层未变，未重跑 dsh。

### 结论

F-1 已按第 1 轮给出的三条验收标准闭环，且未引入事件侧回归（事件序列与基线一致、无重复 `RUN_ERROR`、
取消态不受影响）。累计差异复核后，REQ-1/2/3/5/6/7/8/9/10/11/12 均满足，REQ-4/ACC-3 为技术方案已登记的
偏差延后。剩余问题均为一般改进建议（W-1 ~ W-7），不构成返工理由。

本轮判 `PASS_WITH_WARNING`，循环结束。

## 第 3 轮

### 评审范围

- 业务仓 `bk-aidev-agent`，工作分支 `feature/agent-hook`，评审起点
  `56e6865de8e40a64b1e3bed17f5b06546546f3c6`（`git ls-remote origin refs/heads/feature/agent-hook`
  与本地 `HEAD` 一致，工作区 clean）。
- 本轮性质：第二期 hook 增量（REQ-13 ~ REQ-20 / ACC-14 ~ ACC-23）首次评审；同时按累计差异回归一期已通过项。
- 评审基线：一期交付 HEAD `55e36306`（第二期增量 `git diff --stat 55e36306..56e6865d`：
  34 文件 / +3452 / -68）；任务基线 `develop` @ `7a171a8a`（累计 51 文件 / +8143 / -19）。
- 输入：`spec.md`（第二期追加章节）、`plan.md`（第二期技术方案与决策 D-10 ~ D-18）、`tasks.md`
  （T-14 ~ T-26 验证记录）、上游三节点 handoff、`acceptance/{regression,hooks}`。

### 规格与冻结文档一致性

- `spec.md`、`plan.md` 相对一期交付 `55e36306` 无 diff（`git diff --stat` 为空），实现阶段未改冻结定义。
- `tasks.md` 仅新增 `[x]` 勾选与逐条验证记录（以及 T-26 未取证说明），任务定义文字未改。
- 变更名 / 分支 / 仓库 / 规格目录与 `spec.md` 头部一致；远端分支存在且 SHA 一致。
- REQ-17 的「ITSM 权限审批中断不接线」已在 `plan.md`「规格偏差与待修订（第二期）」（第 639-645 行）
  登记，依据为需求方 2026-10-29 指示；与一期 W-6 同口径处理，本轮不据此拒收。
- ACC-23 由规格「测试约束」明示「缺插件时记为未取证，不影响 ACC-14 ~ ACC-22 的判定」。

### 逐需求核对

| 需求 | 结论 | 依据 |
| --- | --- | --- |
| REQ-13 启用方式与执行归属 | 满足 | `create.params.hooks` 只随会话下发，`HookConfig` 解析 `timeout_ms` / `points`（`headless/hooks.py:52-104`，非法形状 `hook.value_invalid`）；本仓不解析 `hooks.json`、不跑命令、不实现 matcher；载荷只含点位业务字段 |
| REQ-14 `session_start` | 满足 | `_drive_turn` 装配前触发一次，载荷四键；`context` 经 `HookContextMiddleware` 追加 system prompt（`core/hooks/middleware.py`），不改账本 / `turn_id`；空或降级 fail-open（`session.py:483-502`） |
| REQ-15 `tool_pre` | 满足 | 同步/异步 wrapper 均 `insert(0)` 到链首（`node.py:250-254`），`deny` 早退返回 `ToolMessage(status="error")` 且不调 `execute`，`ask` 调既有 `interrupt()`；顺序由 `test_tool_node_hook_order.py:100-148` 断言（含「`allow` 后审批入口仍被调用」） |
| REQ-16 `tool_post` | 满足 | wrapper `append` 在 `deduplicate` 之后（链尾，紧贴工具本体），`accept/block/content/context` 只改同一条 `ToolMessage`，`tool_call_id`/`name`/`additional_kwargs` 不变，`content`+`context` 先替换再追加（`core/hooks/wrappers.py:115-131`） |
| REQ-17 `ask` 与中断恢复 | 满足（ITSM 部分按已登记偏差延后） | `ask` 走 `interrupt()`，值含 `reason=aidev:hook_ask`/`message`/`toolCallId`；`status=interrupt` 与 `RUN_FINISHED(outcome=interrupt)` 由 `events.py:191-202` 判定；`HeadlessInterruptProcessor` 以 reason 白名单构造 `Command`，`dispatch_interrupts` 只归一不 prepare；`resume_interrupt_not_found` / `resume_interrupt_unsupported` / `session.pending_interrupt` 均已实现 |
| REQ-18 协议 hook 往返 | 满足 | 请求行 `id=<请求 id>#h<n>`（会话级自增），`is_hook_reply` 在 `handle_line` 前分流（`cli.py:101-106`），答复行不产生响应、不报未知方法；与事件行共用 `Dispatcher._write_lock`；超时 / `ok=false` / 形状不合法 / 未知决策四类 fail-open 各写恰好一条 `hook_degraded`（复用 `CUSTOM`）；等待期间 `cancel` 早于超时且不写降级 |
| REQ-19 关闭态零回归与凭据 | 满足 | 不传 `hooks` 时 `hook_bus is None`，不插 wrapper、不触发 `session_start`；`acceptance/regression/result.json` 归一后 15 行 sha256 双侧相同、`type=request` 0、`hook_degraded` 0、`EventType` 26 项一致；`pyproject.toml` / `uv.lock` 累计 diff 为空 |
| REQ-20 接入文档 hooks 一节 | 满足 | `docs/agent/HEADLESS_HOST.md` 新增第六节 6.1 ~ 6.6（配置与默认值、两类协议行与 `#h<n>`、三点载荷与裁决、`ask` 宿主义务、fail-open、A 档插件与钉版本），并同步 §2.2/§2.3/§2.5/§三；未复制宿主源码，半私有导出标注为不稳定 API；`docs/agent/readme.md` 索引已补 |

### 验收项核对（ACC-14 ~ ACC-23）

| 验收 | 结论 | 依据 |
| --- | --- | --- |
| ACC-14 关闭态零回归 | 满足 | `result.json` 归一比对一致；`test_hook_degrade.py::test_close_state_has_no_request_lines_and_no_degraded` |
| ACC-15 `session_start` | 满足 | `test_hook_session_start.py`（5 例）：`context` 位于 user 输入之前、账本原文与 `turn_id` 不变、两轮 `first_turn` 为 `True`/`False` |
| ACC-16 `tool_pre=deny` | 满足 | `test_hook_points.py::test_tool_pre_deny_skips_body_and_reports_reason`（工具 0 次、模型收到 reason、`finished`） |
| ACC-17 `tool_post` 四态 | 满足 | `test_hook_points.py` 三例（block 不含原结果、content 等值替换、context 追加且保留原结果），工具调用次数均为 1 |
| ACC-18 `ask` → `resume` | 满足 | `test_hook_interrupt.py`：`status=interrupt`、`toolCallId=call_1`、工具零执行；放行后执行一次且结果回模型；拒绝后零执行并收到说明 |
| ACC-19 协议两类行 | 满足 | `test_hook_protocol.py`：`t1#h1` / `t2#h2`、答复行无响应行且不报未知方法、畸形答复不影响进程、每行均为完整 JSON |
| ACC-20 超时与取消 | 满足 | `test_hook_degrade.py::test_timeout_degrades_once_and_turn_finishes`（恰好一条 `hook_degraded(timeout)`）、`test_cancel_during_hook_wait_ends_before_timeout`（早于超时收尾且不写降级） |
| ACC-21 `interruptId` 校验 | 满足 | `test_hook_interrupt.py::test_resume_unknown_interrupt_id_then_correct_one`（先 `resume_interrupt_not_found`，随后正确 id 正常恢复） |
| ACC-22 无平台依赖（中断路径） | 满足 | `test_hook_no_platform.py`：子进程走完 `ask → 中断 → resume`，无 `django`/`blueapps`、`get_client` 与 `create_tool_approval` 计数为 0、`auth_headers` 不落 stdout/stderr |
| ACC-23 跨仓端到端 | 未取证（规格允许） | `acceptance/hooks/README.md` 如实记录：仓外插件无 hooks 引擎，未产出 `hook/invoked` / `hook/result`；未伪造日志，给出后续取证步骤 |

### 验证记录复核

本轮重跑三组套件与定向用例，结果与 `tasks.md` T-14 ~ T-25 记录逐项一致：

| 命令 | 记录 | 本轮实测 |
| --- | --- | --- |
| `pytest tests/headless` | 98 passed / 0 skipped | 98 passed |
| `pytest tests/core` | 2176 passed / 9 skipped / 39 deselected / 4 xfailed | 同左（101.97s） |
| `pytest tests/services` | 754 passed / 40 skipped / 5 deselected | 同左（34.93s） |
| `tests/core/nodes/tool/test_hook_{wrappers,tool_node_hook_order}.py` + `tests/services/agent/test_agent_factory_hooks_passthrough.py` | 14 + 7 + 8 passed | 29 passed |
| `tests/services/agent` + `tests/core/nodes/tool` + `tests/core/interrupt` | — | 207 passed |

补充核对：

- 一期敏感面累计 diff 仍为空：5 处创建调用方、`views/`、`aidev_wxbot/`、`pyproject.toml`、`uv.lock`。
- 新增用例无 `skip` / `xfail`；headless 用例全部走真实 `Dispatcher.handle_line` 分流（`_fake_host.py`
  以 `send_raw` 回答复行），不是绕过协议的分支调用。
- 一期行为未被本期改动破坏：`resume` 的「纯输入续轮」仍走 `payload=None` + `interrupt_processor=None`
  （与一期一致），仅中断恢复轮注入 `HeadlessInterruptProcessor`；`turn` 恒注入去平台化编排，属 ACC-22 的收紧。

### 独立探测

- 关闭态：`test_close_state_has_no_request_lines_and_no_degraded` 与 `acceptance/regression` 双侧一致，
  确认「不传 `hooks` 即零请求行、零降级事件」。
- 中断链路：`ask` 轮的工具调用计数为 0、`resume` 放行后恒为 1，重放不再二次上报 `tool_pre`
  （`test_hook_interrupt.py:92`、`:125`），与方案 D1 的 memo 去重一致。
- 确认 `hook_degraded` 复用 `CUSTOM`（`EventType.CUSTOM` + `name="hook_degraded"`），未新增 `EventType`；
  `EventType` 取值集合与一期同为 26 项。
- 确认 wrapper 顺序在同步与异步两条链上都以「第一个为最外层」组合（`node.py:99-183`），
  `tool_pre` 的 `insert(0)` 在异步链同样位于审批之前。

### 结论

第二期增量逐条满足 REQ-13 ~ REQ-20，ACC-14 ~ ACC-22 均有可复跑的仓内证据且本轮实测与记录一致；
ACC-23 按规格的测试约束记为未取证。冻结文档未被实现阶段改写，改动范围与已审批方案一致，
未发现正确性、安全或数据方面的阻塞项，约定验证无失败。

本轮判 `PASS_WITH_WARNING`：阻塞项为空，遗留 W-1 ~ W-12 为一般改进建议，循环结束。
