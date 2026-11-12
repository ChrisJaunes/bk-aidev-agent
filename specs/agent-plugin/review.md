# 代码评审

## 最新结论
- 结论: 需要返工
- 评审版本: 0258307a8c7e8e183ba6be807ad04c427202c62b

### 阻塞项

- **F-5（安全 / 正确性，REQ-4、AC8）「跳过提问并输入新消息」的续流会清空待执行批次的 `tool_pre` 裁决，未获批的工具照常执行。**
  `restore_run_state` 把「续流 + 带新输入」当作新一轮处理（`core/hook/run_state.py:230-231` 落到 `:240-253`），并在 `:252` 把 `tool_pre_decisions` 置空。生产链路在该场景下仍从被中断的工具任务继续（`services/agent/chat.py` 的 skip+input 分支用 `Command(resume=..., update={"messages": [...]})` 续流），而工具 wrapper 入口 `_tool_pre_decision` 先续接再取裁决（`core/nodes/tool/hook_wrapper.py:113-121`），裁决为空即返回 `None`，wrapper 直接 `execute(request)`；`_ask_approved` 里的 `interrupt()` 不会再被调用，人工「跳过 / 未批准」的答案被丢弃。
  实测（复用仓库 `tests/core/hook/test_resume.py` 的真实图夹具，MemorySaver）：`tool_pre=ask` 触发中断后，以 `Command(resume=[{"payload":{"answers":[]}}], update={"messages":[HumanMessage("新消息")]})` + `ExecuteKwargs(resume=..., input="新消息")` 续流，工具执行了 1 次（`alpha` 收到 `origin`，`ToolMessage` 为 `success`）。预期应为不执行并返回含原因的 `status=error` 结果。同一路径下同批次被 `deny` 或 `replace` 的调用也会丢失裁决（`deny` 被绕过、`replace` 参数不生效）。
  该问题由本轮 F-3 修复引入：上一版（`9c4d6614`）在 skip+input 分支只重置轮次标记、保留 `tool_pre_decisions`，裁决可续接。
  修复后可判定标准：新增真实 `interrupt()` + `Command(resume=..., update={"messages":[HumanMessage(...)]})`（`ExecuteKwargs` 带 `input`）用例——挂起中的 `ask` 在未选择「允许执行」时工具执行 0 次且模型收到含原因的 `status=error` 结果；同批次 `deny` 不执行、`replace` 的参数仍为实际执行参数；新一轮的 `model_pre` 仍按新轮触发。

### 改进建议

- **W-1**（沿用）快照体积与陈旧数据：`HookRunState.to_mapping`（`core/hook/run_state.py:107-125`）每次模型节点返回都把完整 `model_pre_override` 与全部 `tool_pre_decisions` 序列化进图状态且不裁剪，长会话 / 长工具循环会按轮累积。建议只在本轮确有 hook 写入时落快照，并让覆盖视图短生命周期化。
- **W-2**（沿用）`restore_run_state` / `merge_mapping` 无同步，并行工具 wrapper 会并发进入，`persisted_merged` 与 `tool_pre_decisions` 无锁保护。建议加锁或改为一次性不可变快照。
- **W-3**（沿用）续接快照写入图状态偏离已审批方案 D-1（「运行态不写图状态」）。规格未禁止，本次不据此打回；建议在方案/评审记录中显式保留该取舍说明。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "REWORK",
  "change_name": "agent-plugin",
  "iteration": 3,
  "reviewed_sha": "0258307a8c7e8e183ba6be807ad04c427202c62b",
  "blocking": [
    {
      "id": "F-5",
      "severity": "security",
      "requirement_ids": ["REQ-4"],
      "evidence": "run_state.py:231 将续流+input 归入新一轮分支，:252 清空 tool_pre_decisions；hook_wrapper.py:113-121 取不到裁决即直接 execute。实测 ask 中断后以 Command(resume=[answers:[]], update=messages) 续流：工具执行 1 次且返回 success。上一版 9c4d6614 保留裁决、无此问题。",
      "acceptance": "新增真实 interrupt + Command(resume, update=messages) 用例：未批准时工具执行 0 次并返回 status=error；同批次 deny 不执行、replace 参数生效；新轮 model_pre 正常。"
    }
  ],
  "warnings": [
    {"id": "W-1", "evidence": "to_mapping 每次模型调用序列化完整 model_pre_override 与全部 tool_pre_decisions，且不裁剪。", "suggestion": "仅在有 hook 写入时落快照，覆盖视图短生命周期化。"},
    {"id": "W-2", "evidence": "restore_run_state/merge_mapping 无同步，并行 wrapper 会并发合并。", "suggestion": "加锁或一次性不可变快照。"},
    {"id": "W-3", "evidence": "快照写入图状态偏离方案 D-1。", "suggestion": "在方案/评审记录保留取舍说明。"}
  ]
}
```

</details>

## 第 3 轮

### 评审范围

- 仓库：`git@github.com:ChrisJaunes/bk-aidev-agent.git`，分支 `feature/agent-plugin`，基线 `develop@952e2cb3`。
- 本轮范围：`4992b453..0258307a`（1 个提交、3 个文件、+184/-37）：`core/hook/run_state.py`、`tests/core/hook/test_resume.py`、`tasks.md`（勾选与验证记录）。
- 评审开始时远端与本地 HEAD 均为 `0258307a8c7e8e183ba6be807ad04c427202c62b`，工作区 clean；`spec.md`、`plan.md` 未改动。

### 上轮阻塞项复核

- F-3（快照跨轮泄漏）：已修复。`restore_run_state` 对非续流运行只承接 `turn+1`，其余标记、覆盖视图与 `tool_pre` 裁决全部重置。实测同 thread 连续两轮非续流：`model_pre` = `[1, 2]`、`model_post` = `[(1,"one"), (2,"two")]`；第二轮请求为 `["hello"]`，不再携带第一轮的覆盖视图。
- F-4（续流最终答复不触发 `model_post`）：已修复。`terminal_post_done` 与 `model_post_done` 拆分；实测 `ask` 中断 → 允许 → 最终答复：`model_post` 依次收到 `""`、`"final"`，`replace` 后会话最后一条消息为 `REPLACED-FINAL`。计数口径已在验证记录与用例 docstring 中写明（普通轮 1 次；中断后纯续流共 2 次，第二次为最终答复且 `replace` 生效）。

### 本轮发现

- F-5 见上：skip+input 续流把待执行批次的 `tool_pre` 裁决清空，`ask` 未获批时工具仍执行，属本轮 F-3 修复引入的安全 / 正确性阻塞。复现基于仓库现有真实中断/续流夹具，临时脚本不进入仓库。
- 上轮建议 W-1/W-2 未处理（验证记录已注明），继续作为改进建议，不据此打回。

### 验证

- `tests/core/hook`：61 passed，与记录一致。
- `tests/core/nodes/test_tool.py tests/core/graphs tests/core/ag_ui tests/core/interrupt`：698 passed / 1 skipped，无失败。
- 未重跑 `tests/services`（本轮改动不涉 services 注入链路）。

### 结论

F-3、F-4 已按要求修复并可验证；但 skip+input 续流下 `tool_pre` 裁决被清空（F-5），未获批的工具会执行，违反 AC8，需返工。本轮判定为需要返工，目标节点为代码实现。

## 第 2 轮

- 结论：需要返工（`reviewed_sha` = `9c4d6614175fc876f2f1322f87ff9be0b67443e4`）。
- 阻塞：F-3 续接快照跨轮泄漏；F-4 纯续流后的最终答复不再触发 `model_post`。

## 第 1 轮

- 结论：需要返工（`reviewed_sha` = `070471e54c54f12ae45e17822ab197268683ccb6`）。
- 阻塞：F-1 续流后 `tool_pre` 裁决丢失；F-2 续流后同轮 `model_pre` / `model_post` 重复触发。
