# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: 72b3d9a39c3b5c9e90c66b65e56d7b9d3d8f48c7
- 变更名: agent-hook（`specs/agent-hook/`）
- 轮次: 第 4 轮（rebase 到 develop + 切到 6 点位 hook 机制）

### 阻塞项

无。hook 机制已按需求方在 develop 的重写切换为 6 点位（`turn_start` / `model_pre` /
`model_post` / `tool_pre` / `tool_post` / `turn_end`，进程内 `HookSet`），headless 只保留
传输层；`tool_pre` 的 deny 短路、`tool_post` 的四类改写、`turn_start` 的 context 注入、
关闭态零回归与 fail-open 均有可复跑用例与真实 dsh 端到端证据。

### 改进建议

- W-13 **规格未随机制重写**：`spec.md` / `plan.md` / `tasks.md` 仍描述旧三点
  （`session_start` / `tool_pre` / `tool_post`）与 `ask` 中断；代码已按 develop 的 6 点位
  机制落地。建议在下一轮需求 / 方案节点重写 REQ-13 ~ REQ-20 与 ACC-14 ~ ACC-23，
  避免评审依据与实现长期分叉。
- W-14 `ask` 语义在新机制中不存在：`tool_pre` 只能返回 `ToolMessage` / `Command` 短路，
  宿主返回 `permissionDecision=ask` 时当前按 `deny` 处理并在 reason 中注明（`hook_bridge.py:299-312`）。
  若仍需「转人工」，需要产品明确走平台审批（ITSM）还是扩点位。
- W-1 ~ W-7 为一期 headless 遗留（resume 负载语义、prepare 暂存实例、上游异常回显、
  close/create 重复语义、cancel 超时、REQ-4/ACC-3 偏差、偶发用例失败），本轮复核仍存在，
  均不构成返工理由。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "agent-hook",
  "iteration": 4,
  "reviewed_sha": "72b3d9a39c3b5c9e90c66b65e56d7b9d3d8f48c7",
  "blocking": [],
  "warnings": [
    {"id": "W-13", "evidence": "spec.md / plan.md / tasks.md 仍为旧三点 + ask，代码已切 6 点位", "suggestion": "下一轮需求/方案节点重写 REQ-13~20 与 ACC-14~23"},
    {"id": "W-14", "evidence": "hook_bridge.py:299-312 把宿主 ask 落成 deny", "suggestion": "产品确认是否走 ITSM 审批或扩点位"},
    {"id": "W-1", "evidence": "resume 负载 {"input": str} 校验后丢弃", "suggestion": "统一语义或写明仅供内部校验"},
    {"id": "W-2", "evidence": "prepare() 装配实例不 stop", "suggestion": "prepare 结束即 stop"},
    {"id": "W-3", "evidence": "上游异常串直接对外回显", "suggestion": "归一化与脱敏"},
    {"id": "W-4", "evidence": "重复 close / 重复 create 语义未收敛", "suggestion": "二选一收敛"},
    {"id": "W-5", "evidence": "cancel 超时后仍可能继续写事件", "suggestion": "超时后标记终止"},
    {"id": "W-6", "evidence": "REQ-4/ACC-3 偏差仍在规格原文", "suggestion": "后续变更同步修订"},
    {"id": "W-7", "evidence": "一期偶发用例失败未复现", "suggestion": "再现时保留完整输出"}
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

## 第 4 轮

### 评审范围

- 上游把分支重写过：远端 `feature/agent-hook` 从本轮评审的 `95c67dae` 变为
  `f68edb35`（整合 TUI headless 运行时），随后需求方在 `develop` 重写了 hook 机制
  （`88ee4b06` feat(agent-plugin): 新增 6 点位 hook 协议与运行时注入机制）。
- 本轮按需求方要求把工作分支 rebase 到 `origin/develop`（`88ee4b06`），并把 headless
  与仓外 dsh 桥接件切到新机制。评审版本 `72b3d9a3`；rebase 后提交：
  `c838d107`（原 feature 主体）、`cd9ba639`（TUI 运行时）、`72b3d9a3`（切 6 点位）。
- 冲突处理：`graph.py` / `node.py` / `pydantic_models.py` / `chat.py` / `factory.py` /
  `registry.py` 一律采用 develop 侧（新 hook 机制），再补回 headless 需要的非 hook 透传
  （`interrupt_processor` / `injected_tools`）。

### 机制切换

| 维度 | 旧（一期/二期自建） | 新（develop） |
| --- | --- | --- |
| 点位 | 3 个：`session_start` / `tool_pre` / `tool_post` | 6 个：`turn_start` / `model_pre` / `model_post` / `tool_pre` / `tool_post` / `turn_end` |
| 形态 | `HookBusProtocol` 鸭子类型 + headless 同步往返 | 进程内 `HookSet` + `HookRunner`（原生对象，5s 超时，fail-open） |
| 短路 | `deny` → `ToolMessage`；`ask` → `interrupt()` | `tool_pre` 返回 `ToolMessage` / `Command` 短路；无 `ask` |
| headless 角色 | 自己实现总线、ask 中断与恢复 | 只保留传输层 `headless/hook_bridge.py` |

`headless/hook_bridge.py` 把 6 个点位接成宿主 NDJSON 往返：`HookSet` 处理器 → 请求行 →
等待答复 → 翻译回 `ToolMessage` / `SystemMessage` / `HookRetry`；超时 / 宿主失败 /
形状非法 fail-open 并写 `hook_degraded`。等待上限 3s（上限 4s），低于 `HookRunner` 的 5s。

### 逐需求核对（对照旧规格）

| 需求 | 结论 | 依据 |
| --- | --- | --- |
| REQ-13 启用方式与执行归属 | 部分满足（规格待重写） | `create.params.hooks` 仍随会话下发，宿主仍是判定执行方；点位名与载荷改由新机制定义（W-13） |
| REQ-14 `session_start` | 由 `turn_start` 承接 | 处理器在最前面插一条 system，`tests/headless/test_hook_bridge.py::test_turn_start_context_reaches_system_prompt` |
| REQ-15 `tool_pre` | 满足 | `deny` 返回 `ToolMessage(status="error")` 短路，工具零执行；载荷 `tool_name` / `tool_input` / `tool_use_id` |
| REQ-16 `tool_post` | 满足 | `accept` / `block` / `content` / `context` 四类改写（`hook_bridge.py:314-350`） |
| REQ-17 `ask` 与中断恢复 | **语义变化** | 新机制无 `ask`；`aidev:user_question` 恢复仍在，ITSM 中断仍显式失败（W-14） |
| REQ-18 协议往返 | 满足 | 两类行、`<请求 id>#h<n>`、答复行分流、fail-open、`hook_degraded` |
| REQ-19 关闭态零回归 | 满足 | 不传 `hooks` 时不注册处理器、无 `type=request`（`test_hooks_disabled_emits_no_request_lines`） |
| REQ-20 文档 | 满足 | `docs/agent/HEADLESS_HOST.md` 第六节改写为 6 点位 + 传输层口径 |

### 验证记录

- `tests/headless` 65 passed（含新增 `test_hook_bridge.py` 10 条：关闭态、context 注入、
  deny/allow、tool_post context、超时与宿主失败降级、协议形状、非法配置）。
- `tests/services/agent` 445 passed；`tests/core/hook` 52 passed；`tests/core/nodes/tool` 147 passed。
- 仓外 dsh 端到端（`probe/probe-hook-e2e.ts`，rebased 分支）：
  `hook-deny` 工具 0 执行且 `hook/result(PreToolUse:deny)`、模型收到 stderr；
  `hook-post` 工具执行 1 次并追加 `[hook context]`；`hook-allow` 正常执行；
  `hook-ask` 落为 deny；`hook-context` 的 `turn_start` 注入在 system prompt 命中。
- `hooks-smoke.ts`（宿主协议冒烟）：`@deepseek-ai/dsh-hook-protocol@0.2.1-rc.3` 导出可用，
  matcher / exit 2 / additionalContext / 审计事件全部通过。

### 结论

rebase 后分支与 develop 的 hook 机制一致，headless 只保留传输层，仓外桥接件已按新点位重写；
关闭态零回归、fail-open、审计事件与真实 dsh 链路均有证据。规格文档与实现已分叉（W-13）、
`ask` 语义缺失需产品确认（W-14），二者属交付口径问题，不构成本轮代码阻塞。

本轮判 `PASS_WITH_WARNING`。
