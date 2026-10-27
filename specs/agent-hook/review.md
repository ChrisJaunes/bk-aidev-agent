# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: 550fe122ace6ebe98a06f37bdf97a14198ec1c96
- 变更名: agent-hook（`specs/agent-hook/`）
- 轮次: 第 2 轮（第 1 轮 F-1 已修复）

### 阻塞项

无。第 1 轮 F-1 已解除：执行期只产出 `RUN_ERROR` 事件时，`turn` / `resume` 现在返回
`ok=false` + `error.code=agent.internal_error` + 该 `RUN_ERROR` 文案，事件序列仍为基线
（`RUN_ERROR` + 平台补发的 `RUN_FINISHED`），且同一条 `RUN_ERROR` 只写一次。

### 改进建议

以下均为一般改进建议，不构成返工理由（与第 1 轮相同，本轮未要求处理）。

- W-1 `DefaultAgentFactory.resume` 接受 `{"input": str}` 续轮形状后直接丢弃负载
  （`services/agent/factory.py:502-504`），headless 侧 `session.py:164` 也用 `payload` 只做校验、
  真正执行的输入取 `params.input`。当前文档口径是 `resume` 用 `input`，故不算缺陷；但同一负载两种写法
  语义不同，建议要么统一（负载 `input` 生效），要么在 `normalize_resume_payload` 文档里写明「仅供内部校验」。
- W-2 `HeadlessSession.prepare()`（`session.py:126-132`）装配出的实例存进 `_pending_agent` 后从未使用也从未
  `stop()`，`_take_agent` 的 `allow_pending` 参数（`session.py:308`）是死参数。建议 `prepare` 结束即 `stop()`，
  或删掉 `allow_pending`。
- W-3 未归类异常一律拼进对外可见文案：`f"{type(exc).__name__}: {exc}"`（`session.py:204-211`、`:305-306`、
  `cli.py:138-143`）。实测上游错误体片段（`upstream exploded`）会经 `RUN_ERROR` 与响应回显；REQ-11 要求
  不输出凭据与完整请求体，建议对上游错误做一次归一化（只保留错误类与脱敏摘要）。
- W-4 `close` 第二次调用经 `_session_for` 的 `ensure_open` 直接报 `session.closed`（`cli.py:231-238`），
  使 `HeadlessSession.close()` 的 `already_closed` 字段（`session.py:247-262`）在协议层不可达；
  `SessionRegistry.create`（`session.py:406-415`）对同 `session_code` 直接覆盖且不关闭旧会话。建议二选一收敛语义。
- W-5 `cancel` 收敛超时走 `writer.finalize()` 补发取消终止事件后（`session.py:239-243`），仍在运行的轮次
  还可能继续写出事件，破坏「以终止事件收尾」。建议超时后标记本轮为已终止并丢弃后续事件。
- W-6 REQ-4 / ACC-3（续流入口收敛）按技术方案「规格偏差与待修订」整体延后，本轮不作为返工项；
  但 `spec.md` 仍保留 REQ-4 / ACC-3 原文，建议后续变更同步修订规格，避免规格与实现长期不一致。
- W-7 `tests/headless` 在返工后首次运行出现过 1 例未捕获用例名的失败（`verification.md` 已如实记录）。
  本轮复跑 3 次全量（55 passed）与 5 次并发用例（5 passed）均未复现，暂不视为缺陷；建议后续若再现时保留
  `-p no:cacheprovider --tb=long` 完整输出，以便定位是否为时序相关用例。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "agent-hook",
  "iteration": 2,
  "reviewed_sha": "550fe122ace6ebe98a06f37bdf97a14198ec1c96",
  "blocking": [],
  "warnings": [
    {"id": "W-1", "evidence": "factory.py:502-504 与 session.py:164：resume 负载 {\"input\": str} 被校验后丢弃，执行输入取 params.input", "suggestion": "统一语义，或在 normalize_resume_payload 文档写明该形状仅供内部校验"},
    {"id": "W-2", "evidence": "session.py:126-132 prepare() 装配实例后不 stop；session.py:308 allow_pending 为死参数", "suggestion": "prepare 结束即 stop，或移除 allow_pending"},
    {"id": "W-3", "evidence": "session.py:204-211/305-306、cli.py:138-143 直接把上游异常串写入 RUN_ERROR 与响应，实测回显 upstream exploded", "suggestion": "对上游错误做归一化与脱敏，只保留错误类与摘要"},
    {"id": "W-4", "evidence": "cli.py:231-238 使 close 的 already_closed 不可达；session.py:406-415 同 session_code 覆盖旧会话不关闭", "suggestion": "收敛重复 close / 重复 create 的语义"},
    {"id": "W-5", "evidence": "session.py:239-243 cancel 超时补发终止事件后，运行中的轮次仍可继续写事件", "suggestion": "超时后标记终止并丢弃后续事件"},
    {"id": "W-6", "evidence": "spec.md 仍保留 REQ-4/ACC-3，plan.md 第 294-299 行登记为已批准偏差", "suggestion": "后续变更同步修订规格，避免长期不一致"},
    {"id": "W-7", "evidence": "verification.md:95-96 记录一次未捕获用例名的偶发失败；本轮 3 次全量 + 5 次并发用例未复现", "suggestion": "再现时保留完整输出以定位是否为时序用例"}
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
