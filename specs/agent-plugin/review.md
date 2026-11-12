# 代码评审

## 最新结论
- 结论: 通过，但有改进建议
- 评审版本: 897ddc74ff19741c347bfdba1bcfbb22cc2a214c

### 阻塞项
无。

### 改进建议

- **W-1** 快照体积与陈旧数据：`HookRunState.to_mapping`（`core/hook/run_state.py:110-128`）每次模型节点返回都把完整 `model_pre_override` 与挂起的 `tool_pre_decisions` 序列化进图状态，且不裁剪；长会话 / 长工具循环会按轮累积。建议只在本轮确有 hook 写入时落快照，并让覆盖视图短生命周期化。
- **W-2** `restore_run_state` / `merge_mapping` 无同步：并行工具 wrapper 会并发进入，`persisted_merged` 与 `tool_pre_decisions` 无锁保护。建议加锁或改为一次性不可变快照。
- **W-3** 续接快照写入图状态偏离已审批方案 D-1（「运行态不写图状态」）。规格未禁止、仅在注入 hooks 时存在，本项不据此打回；建议在方案/评审记录中保留该取舍说明。
- **W-4** 快照通道会进入 AG-UI 的 STATE_SNAPSHOT：`HookState` 并入 state schema 后，编译图的 output schema 为 `['messages', 'hook_run_state']`，`get_state_snapshot`（`core/ag_ui/agent.py:550-554`）按 output keys 过滤，因此每次 STATE_SNAPSHOT 都会携带该通道；当 `model_pre` 返回 `replace` 时，`model_pre_override` 是消息正文（可能含 system 提示）。建议在 `get_state_snapshot` 中显式剔除该通道或让它不进入 output schema，并补一条快照断言；同时确认平台是否持久化 state 快照。当前仅在注入 hooks 且使用 `model_pre.replace` 时出现，不影响已审批 AC，故列为改进建议。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "agent-plugin",
  "iteration": 4,
  "reviewed_sha": "897ddc74ff19741c347bfdba1bcfbb22cc2a214c",
  "blocking": [],
  "warnings": [
    {"id": "W-1", "evidence": "to_mapping 每次模型调用序列化完整 model_pre_override 与挂起 tool_pre_decisions，且不裁剪。", "suggestion": "仅在有 hook 写入时落快照，覆盖视图短生命周期化。"},
    {"id": "W-2", "evidence": "restore_run_state/merge_mapping 无同步，并行 wrapper 会并发合并。", "suggestion": "加锁或一次性不可变快照。"},
    {"id": "W-3", "evidence": "快照写入图状态偏离方案 D-1。", "suggestion": "在方案/评审记录保留取舍说明。"},
    {"id": "W-4", "evidence": "编译图 output schema 含 hook_run_state，get_state_snapshot 按 output keys 过滤后 STATE_SNAPSHOT 会携带该通道；model_pre.replace 时该通道含消息正文。", "suggestion": "在 get_state_snapshot 剔除该通道或移出 output schema，并补快照断言。"}
  ]
}
```

</details>

## 第 4 轮

### 评审范围

- 仓库：`git@github.com:ChrisJaunes/bk-aidev-agent.git`，分支 `feature/agent-plugin`，基线 `develop@952e2cb3`。
- 本轮范围：`b3307155..897ddc74`（1 个提交、3 个文件、+171/-12）：`core/hook/run_state.py`、`tests/core/hook/test_resume.py`、`tasks.md`（勾选与验证记录）。
- 评审开始时远端与本地 HEAD 均为 `897ddc74ff19741c347bfdba1bcfbb22cc2a214c`，工作区 clean；`spec.md`、`plan.md` 未改动。

### 上轮阻塞项复核

- F-5（skip+input 续流丢裁决、未获批工具执行）：已修复。`restore_run_state` 换轮时只重置本轮级标记，`_pending_decisions` 按 `tool_call_id` 只保留仍挂起（尚无 `ToolMessage`）的裁决。独立复跑仓库真实图夹具：`ask` 中断后用 `Command(resume=[{"payload":{"answers":[]}}], update={"messages":[HumanMessage("新消息")]})` + `ExecuteKwargs(input=...)` 续流，工具执行 0 次，模型收到 `status=error`、内容 `hook ask not approved at tool_pre: need approval`；同批次 replace/deny/ask 混合用例中 replace 参数生效、deny 与 ask 均不执行。
- 用例有效性：验证记录说明已用「把 `_pending_decisions` 临时替换为 `{}`」反证两条新用例会失败，覆盖真实。

### 前几轮修复回归

- F-3（快照跨轮泄漏）：不回退。独立复跑同 thread 连续两轮非续流：`model_pre` = `[1, 2]`、`model_post` = `[(1,"one"), (2,"two")]`，第二轮请求为 `["hello"]`，不含第一轮覆盖视图。
- F-4（续流最终答复不触发 `model_post`）：不回退。`test_model_post_replace_applies_to_final_answer_after_resume` 通过，续流最终答复的 `replace` 生效。
- F-1/F-2：真实中断续流用例保持通过（拒绝 0 次、允许 1 次、同批次裁决续流后生效）。

### 逐条需求核对（累计）

- REQ-1 点位与裁决：5 个点位、5 种裁决、同步/异步一致、非法返回值降级，`tests/core/hook/test_contracts.py` 覆盖。
- REQ-2 `session_start`：每次运行一次、`append` 注入 system 段、`deny` 不发请求，覆盖 AC1/AC2。
- REQ-3 `model_pre` / `model_post`：每轮 `model_pre` 1 次；`model_post` 普通轮 1 次（AC4，`replace` 生效）；无最终答复的终止补触发 1 次（AC6，`replace` 忽略）；中断后续流产生最终答复共 2 次，第二次为最终答复且 `replace` 生效——口径已在验证记录第 5 节与用例 docstring 写明。
- REQ-4 `tool_pre` / `tool_post`：pv 阶段按模型顺序串行触发、代理解包后取真实工具名、wrapper 位置正确、循环守卫不受 `replace` 影响；`tool_post` 在 `_content_error` 内层看到原文，结果继续经净化/截断/脱敏。`ask` 的拒绝、允许、跳过提问+新输入三条路径均不执行未获批工具（AC7/AC8）。
- REQ-5 注入与隔离：`build_agent(hooks=...)` 全链路贯通，`HookSet` 实例隔离，未注入时零回归。
- REQ-6 执行语义：合并、短路、5 秒超时 fail-open、结构化日志、contextvars 透传（W-3 已采纳）、异常收窄为 `Exception`（W-4 已采纳）。
- REQ-7 零回归：`hook_run_state` 仅在注入 hooks 时进入 schema；`test_zero_regression.py` 全绿。

### 验证

- `tests/core/hook`：63 passed（`test_resume.py` 12 条），与记录一致。
- `tests/core/nodes/test_tool.py tests/core/graphs tests/core/ag_ui tests/core/interrupt`：698 passed / 1 skipped。
- `tests/services`：1179 passed / 47 skipped。
- 复跑均无失败；独立复现脚本不进入仓库。

### 结论

四个历史阻塞项（F-1～F-5）均已修复并有真实中断/续流的回归用例；AC1–AC15 均有对应用例，定向与回归验证通过。剩余 W-1～W-4 为快照体积、并发、方案偏离与 AG-UI 快照暴露面的改进建议，不构成正确性阻塞。本轮判定为通过（有改进建议），流程继续。

## 第 3 轮

- 结论：需要返工（`reviewed_sha` = `0258307a8c7e8e183ba6be807ad04c427202c62b`）。
- 阻塞：F-5 skip+input 续流清空挂起 `tool_pre` 裁决，未获批的 `ask` 工具仍执行。

## 第 2 轮

- 结论：需要返工（`reviewed_sha` = `9c4d6614175fc876f2f1322f87ff9be0b67443e4`）。
- 阻塞：F-3 续接快照跨轮泄漏；F-4 纯续流后的最终答复不再触发 `model_post`。

## 第 1 轮

- 结论：需要返工（`reviewed_sha` = `070471e54c54f12ae45e17822ab197268683ccb6`）。
- 阻塞：F-1 续流后 `tool_pre` 裁决丢失；F-2 续流后同轮 `model_pre` / `model_post` 重复触发。
