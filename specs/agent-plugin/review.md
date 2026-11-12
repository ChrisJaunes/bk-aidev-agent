# 代码评审

## 最新结论
- 结论: 需要返工
- 评审版本: 9c4d6614175fc876f2f1322f87ff9be0b67443e4

### 阻塞项

- **F-3（正确性 / 数据，REQ-3、REQ-5、REQ-7）续接快照跨轮泄漏：同一 thread 的第二个普通用户轮次起，`model_pre` / `model_post` 被静默跳过，`model_pre.replace` 的覆盖视图会串到新轮。**
  `hook_run_state` 已成为图状态通道（`core/graphs/react/graph.py:149-157`、`:1290-1293`），按 thread 随 checkpoint 持久化；`restore_run_state`（`core/hook/run_state.py:199-240`）对**所有**运行都从 `ctx.state` / `request.state` 合并快照，仅对 `execute_kwargs.resume` 做特殊处理，非续流的新用户轮次没有重置。于是上一轮的 `model_pre_done=True`、`model_post_done=True`、`model_pre_override`、`model_request_count` 原样并入新运行。
  实测（MemorySaver，同一 thread 连续两次非续流 invoke，第二次为新用户消息）：`model_pre` 只在第 1 轮触发（`[1]`），`model_post` 只在第 1 轮触发（`(1, "one")`）；第二轮实际发出的消息是上一轮 `model_pre.replace` 的覆盖视图 `["SYS-FROM-TURN-1", "REPLACED-1"]`，新用户消息没有进入请求。生产链路中第 2 条及以后的所有用户消息都会命中该路径（`_execute` 非 resume 分支不清理该通道），`model_post.replace` 同样对新轮失效。
  修复后可判定标准：新增「同一 thread 连续两轮非续流对话」用例——每轮 `model_pre` / `model_post` 各 1 次；第二轮的模型请求不含第一轮的覆盖视图（`model_pre.replace` 只作用于本轮的首次请求）；`identity.turn` 按本轮确定的口径递增或区分，不得沿用上一轮的 `model_pre_done` / `model_post_done` / `model_pre_override`。

- **F-4（需求，REQ-3 / AC4）纯续流后产生的最终答复不再触发 `model_post`，`model_post.replace` 在审批 / `ask` 轮次彻底失效。**
  `restore_run_state` 在纯续流且 `model_request_count > 0` 时直接把 `model_post_done` 置为 `True`（`core/hook/run_state.py:228-237`），`_prepare_model_post` 见到已触发即返回（`core/hook/stages.py:199-207`）。中断出口的 `finalize_run` 已按 AC6 用中间响应触发过一次，于是续流后模型真正产生的最终答复永远不会进入 `model_post`（该次 `replace` 也不会成为实际输出）。
  实测：`tool_pre=ask` 中断 → 允许 → 模型产出 `"final"`，`model_post` 收到的内容只有中断时的中间响应 `""`；仓库内 `test_model_hooks_are_once_per_turn_across_resume` 只断言 `post_turns == [1]`，未断言最终答复，等于把该缺口固化为期望。
  AC4 明确要求「该轮产生最终答复时，`model_post` 必须在该轮最后一次响应返回后触发一次，其 `replace` 的文本必须成为该轮实际输出」。AC6 与 AC4 在「中断后又续流」的轮次上存在口径冲突，当前实现单方面选择 AC6 并静默丢弃 AC4，需要显式取舍而不是静默跳过。
  修复后可判定标准：纯续流并产生最终答复时，存在一次 `model_post` 调用的 `payload.response` 为该最终答复，`replace` 后会话中的实际输出为替换文本；测试需断言该次调用的内容而非仅计数，并在本轮说明中写清 AC4/AC6 的计数口径。

### 改进建议

- **W-1** 快照体积与陈旧数据：`HookRunState.to_mapping`（`core/hook/run_state.py:102-121`）每次模型节点返回都把完整 `model_pre_override`（整段消息列表）和所有 `tool_pre_decisions` 序列化进图状态，且从不裁剪；长会话 / 长工具循环会按轮次累积，checkpoint 体积与快照暴露面随对话增长。建议只在本轮确有 hook 写入时落快照，并把覆盖视图改为「本轮生效」的短生命周期数据（或压缩为引用）。
- **W-2** `restore_run_state` / `merge_mapping` 无同步：并行工具调用的多个 wrapper 会并发进入 `_tool_pre_decision` → `restore_run_state`，`persisted_merged` 是普通布尔、`tool_pre_decisions` 是共享 dict。当前靠 GIL 与「已有裁决不覆盖」降低风险，建议加锁或改为一次性不可变快照，避免并发下的重复合并。
- **W-3** 续接快照写入图状态是对已审批方案 D-1（明确排除「写入图状态」）与「数据变化」一节的偏离，本轮为修复 F-1/F-2 引入。规格未禁止，本次不据此打回；建议在后续评审/方案说明中显式记录该取舍（快照不含会话内容、仅在注入 hooks 时存在），并评估 checkpoint 体积与快照访问边界。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "REWORK",
  "change_name": "agent-plugin",
  "iteration": 2,
  "reviewed_sha": "9c4d6614175fc876f2f1322f87ff9be0b67443e4",
  "blocking": [
    {
      "id": "F-3",
      "severity": "correctness",
      "requirement_ids": ["REQ-3", "REQ-5", "REQ-7"],
      "evidence": "hook_run_state 为图状态通道（graph.py:149-157、1290-1293），按 thread 持久化；restore_run_state（run_state.py:199-240）对所有运行合并快照，仅对 resume 特殊处理，非续流新轮不重置。实测同一 thread 两次非续流 invoke：model_pre [1]、model_post [(1,'one')]，第二轮请求为 ['SYS-FROM-TURN-1','REPLACED-1']。",
      "acceptance": "同 thread 连续两轮非续流：每轮 model_pre/model_post 各 1 次；第二轮请求不含上一轮覆盖视图；不沿用上一轮 done 标记与 override；identity.turn 口径明确。"
    },
    {
      "id": "F-4",
      "severity": "requirement",
      "requirement_ids": ["REQ-3"],
      "evidence": "run_state.py:228-237 纯续流把 model_post_done 置 True；stages.py:199-207 见 done 即返回。实测 ask 中断→允许→final：model_post 只收到 ''，未收到 'final'；test_model_hooks_are_once_per_turn_across_resume 只断言计数。",
      "acceptance": "纯续流产生最终答复时存在一次 model_post，payload.response 为该最终答复且 replace 成为实际输出；测试断言内容与替换结果，并写明 AC4/AC6 计数口径。"
    }
  ],
  "warnings": [
    {"id": "W-1", "evidence": "to_mapping 每次模型调用序列化完整 model_pre_override 与全部 tool_pre_decisions，且不裁剪。", "suggestion": "仅在本轮有 hook 写入时落快照，覆盖视图改为短生命周期数据。"},
    {"id": "W-2", "evidence": "restore_run_state/merge_mapping 无同步，persisted_merged 为普通布尔，并行 wrapper 会并发合并。", "suggestion": "加锁或改为一次性不可变快照。"},
    {"id": "W-3", "evidence": "快照写入图状态偏离已审批方案 D-1 与「数据变化」（运行态不写图状态）。", "suggestion": "在后续说明中显式记录取舍并评估 checkpoint 体积与访问边界。"}
  ]
}
```

</details>

## 第 2 轮

### 评审范围

- 仓库：`git@github.com:ChrisJaunes/bk-aidev-agent.git`，分支 `feature/agent-plugin`，基线 `develop@952e2cb3`。
- 本轮范围：`f8121af3..9c4d6614`（2 个提交、10 个文件、+541/-12），实现改动为 `core/hook/{run_state,stages,runner}.py`、`core/graphs/react/graph.py`、`core/nodes/{model/node,pv,tool/hook_wrapper}.py`；测试新增 `tests/core/hook/test_resume.py`，其余为 `tasks.md` 勾选与验证记录。
- 评审开始时远端与本地 HEAD 均为 `9c4d6614175fc876f2f1322f87ff9be0b67443e4`，工作区 clean；`spec.md` 自 `4a899a12`、`plan.md` 自 `50b25b6f` 未改动，`tasks.md` 仅勾选与追加验证记录。

### 上轮阻塞项复核

- F-1（续流后 `tool_pre` 裁决丢失）：已修复。`hook_run_state` 通道在 pv 节点写入、工具 wrapper 入口续接；新增 `test_ask_denied_after_resume_never_executes`、`test_ask_allowed_after_resume_executes_once`、`test_deny_and_replace_survive_resume_for_whole_batch` 均基于真实 `interrupt()` + `Command(resume=...)` + `MemorySaver`，拒绝后工具执行 0 次、允许后 1 次、同批次 replace/ask 裁决续流后仍生效，复跑通过。
- F-2（续流后同轮 `model_pre` 重复）：已修复。`model_pre_done` 随快照续接，续流后 `model_pre` 只触发 1 次；`identity.turn` 在同一轮内为 1。

### 本轮发现

- F-3、F-4 见上：前者是快照跨轮泄漏导致正常多轮对话从第 2 轮起失去 `model_pre` / `model_post`（并可能把上一轮覆盖视图发给模型），属本轮新引入的正确性/数据阻塞；后者是续流轮次的最终答复不再进入 `model_post`，与 AC4 冲突。
- 复现均在 `tests/core/hook/test_resume.py` 的图夹具上完成（MemorySaver，不 monkeypatch `interrupt`），临时脚本不进入仓库。

### 验证

- `tests/core/hook`：59 passed，与记录一致。
- `tests/core/nodes/test_tool.py tests/core/graphs tests/core/ag_ui tests/core/interrupt`：698 passed / 1 skipped，无失败。
- 未重跑 `tests/services`（上轮已跑 1179 passed，本轮改动不涉 services 注入链路）；记录声称的 2500 passed 与本轮改动不冲突。

### 结论

上轮两项已修复，但本轮引入的快照跨轮泄漏（F-3）会让所有多轮会话的 `model_pre` / `model_post` 从第 2 轮起失效并串用上一轮覆盖视图，续流轮次的最终答复也无法进入 `model_post`（F-4）。两项均属正确性阻塞，本轮判定为需要返工，目标节点为代码实现。

## 第 1 轮

- 结论：需要返工（`reviewed_sha` = `070471e54c54f12ae45e17822ab197268683ccb6`）。
- 阻塞：F-1 续流后 `tool_pre` 裁决丢失、审批闸门被绕过；F-2 续流后同轮 `model_pre` / `model_post` 重复触发。
- 其余：REQ-1/2/5/6/7 满足；`tests/core/hook` 50 passed，回归无失败。详见上一版本评审记录。
