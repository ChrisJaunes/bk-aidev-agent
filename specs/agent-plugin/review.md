# 代码评审

## 最新结论
- 结论: 需要返工
- 评审版本: 070471e54c54f12ae45e17822ab197268683ccb6

### 阻塞项

- **F-1（安全 / 正确性，REQ-4、AC8）中断续流后 `tool_pre` 裁决全部丢失，审批闸门被绕过。**
  `HookRunState` 只活在单次运行的 `config["configurable"]` 里（`core/hook/run_state.py:95-114`），而 `_execute` 每次执行（含 `execute_kwargs.resume`）都会重新 `bind_run_state`（`services/agent/chat.py:1057`、`:1091-1103`）。`tool_pre` 只在 `pv_node` 阶段触发（`core/hook/stages.py:214-249`、`core/nodes/pv.py:177-181`），续流是从被中断的工具任务继续，`pv_node` 不会再跑；续流后 `_tool_pre_decision` 取不到裁决返回 `None`（`core/nodes/tool/hook_wrapper.py:113-118`），wrapper 直接 `execute(request)`（`:200-206`），`_ask_approved` 里的 `interrupt()` 也不会被再次调用，人工答案被丢弃。
  实测（MemorySaver 最小图，pv 阶段 + `build_tool_node(hooks=...)`，首次 invoke 得到 `__interrupt__`；以新 config + 新运行态模拟续流，`Command(resume=[{"payload":{"answers":[{"selectedOptions":["拒绝执行"]}]}}])`）：工具仍执行一次并返回 `echo:origin` / `status=success`。同一路径下 `deny` 同样失效；任何触发中断续流的场景（`ask`、以及既有 ITSM 审批中断后再续流）都会绕过 hook 的拒绝与参数改写。
  修复后可判定标准：新增基于真实 `interrupt()` + `Command(resume=...)`（MemorySaver，不 monkeypatch `interrupt`）的用例——拒绝时工具执行次数为 0 且模型收到含原因的 error 结果；批准时执行 1 次；同一批次被 `deny` 的调用在续流后仍不执行；`tool_pre` 裁决在续流前后可被同一次运行读取。

- **F-2（需求，REQ-3）中断续流后同一轮内 `model_pre` / `model_post` 重复触发。**
  续流重建运行态后 `turn=1`、`model_pre_done=False`（`core/hook/run_state.py:107-113`），`stages.py:142-156` 的去重只在当前运行态生效，因此续流后的首个模型请求会再次触发 `model_pre`；`model_post` 也会再触发一次（中断出口已按 AC6 用 `finalize_run` 触发过一次）。计划「风险 4」明确要求「`turn` 序号延续、`model_pre` 不会重复触发（该轮已打开）」。实测两段 `_execute` 等价调用（各自新 config + `bind_run_state`）得到 `model_pre` 调用序列 `["run-1", "run-2"]`。此外 `turn` 始终为 1，与「序号延续」也不一致。
  修复后可判定标准：同一 thread 中断续流后，整轮 `model_pre` 恰好 1 次；有最终答复时 `model_post` 恰好 1 次（无最终答复时按 AC6 在终止路径触发 1 次、续流后不再重复）；同一轮内 `identity.turn` 保持同一值。

### 改进建议

- **W-1** `tests/core/hook/test_tool_hooks.py:117-157` 的 `ask` 用例用 monkeypatch 把 `interrupt` 换成直接返回答案，没有走真实的「中断 → 落盘 → 续流」生命周期，F-1 因此漏检。建议按 F-1 的验收补真实图用例。
- **W-2** `_prepare_model_post`（`core/hook/stages.py:194-203`）未检查 `state.model_post_done`，「每轮一次」依赖「一轮只有一个最终答复」的图拓扑假设；后续若新增复核/重试回路导致模型节点在最终答复后再次进入，会重复触发。建议显式检查该标记。
- **W-3** `HookRunner._call_with_timeout`（`core/hook/runner.py:158-180`）把同步处理器放到新线程执行，`contextvars`（追踪、请求上下文）不会传播，且每次调用新建线程。建议用 `contextvars.copy_context().run(...)` 传递上下文；线程不可强杀的风险方案已接受，可保留。
- **W-4** `_call_with_timeout` 用 `except BaseException` 回传处理器异常（`core/hook/runner.py:169-172`），而上层 `_execute_sync/_execute_async` 只捕获 `Exception`；处理器抛 `KeyboardInterrupt` / `SystemExit` 会穿透 fail-open 直接终止运行。建议回传时收窄为 `Exception`。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "REWORK",
  "change_name": "agent-plugin",
  "iteration": 1,
  "reviewed_sha": "070471e54c54f12ae45e17822ab197268683ccb6",
  "blocking": [
    {
      "id": "F-1",
      "severity": "security",
      "requirement_ids": ["REQ-4"],
      "evidence": "run_state.py:95-114 每次 _execute 新建 HookRunState；chat.py:1057、1091-1103 续流同样重建；tool_pre 仅在 pv_node 阶段触发（stages.py:214-249、pv.py:177-181），续流不重跑 pv；hook_wrapper.py:113-118 续流后取不到裁决返回 None，:200-206 直接 execute，interrupt 不再调用。MemorySaver 实测：拒绝后工具仍执行一次（echo:origin, success）。",
      "acceptance": "补真实 interrupt/Command(resume) 用例：拒绝时工具执行 0 次且模型收到含原因 error 结果；批准时执行 1 次；同批次 deny 在续流后仍不执行；裁决在续流前后同一次运行内可读。"
    },
    {
      "id": "F-2",
      "severity": "requirement",
      "requirement_ids": ["REQ-3"],
      "evidence": "续流重建运行态（run_state.py:107-113，turn=1、model_pre_done=False），stages.py:142-156 去重仅限当前运行态；实测两段等价 _execute 的 model_pre 序列为 [run-1, run-2]；中断出口已由 finalize_run 触发一次 model_post。计划风险 4 要求该轮 model_pre 不重复触发。",
      "acceptance": "同一 thread 续流后整轮 model_pre 恰好 1 次；有最终答复时 model_post 恰好 1 次（无最终答复按 AC6 1 次且续流后不重复）；同一轮 identity.turn 一致。"
    }
  ],
  "warnings": [
    {"id": "W-1", "evidence": "test_tool_hooks.py:117-157 用 monkeypatch 替换 interrupt，未覆盖真实中断/续流生命周期，F-1 漏检。", "suggestion": "补 MemorySaver + Command(resume) 的真实图用例。"},
    {"id": "W-2", "evidence": "stages.py:194-203 未检查 model_post_done，去重依赖图拓扑只有一次最终答复。", "suggestion": "在 _prepare_model_post 中显式检查该标记。"},
    {"id": "W-3", "evidence": "runner.py:158-180 在新建线程执行同步处理器，contextvars 不传播且每次调用新建线程。", "suggestion": "用 contextvars.copy_context().run 传递上下文。"},
    {"id": "W-4", "evidence": "runner.py:169-172 以 except BaseException 回传，上层只捕获 Exception。", "suggestion": "收窄为 Exception，避免 KeyboardInterrupt/SystemExit 穿透 fail-open。"}
  ]
}
```

</details>

## 第 1 轮

### 评审范围

- 仓库：`git@github.com:ChrisJaunes/bk-aidev-agent.git`，工作分支 `feature/agent-plugin`，基线 `develop` = `952e2cb3`。
- 累计改动：`952e2cb3..070471e5`，31 个文件、+3319/-49；实现为 `core/hook/`（9 个新文件）、模型链/模型节点、pv、工具 wrapper/工具节点、图装配与 `services/agent` 注入链路，其余为 `specs/agent-plugin/` 三份文档与 `tests/core/hook/` 六个文件。
- 评审开始时远端 `refs/heads/feature/agent-plugin` 与本地 HEAD 均为 `070471e54c54f12ae45e17822ab197268683ccb6`，工作区 clean。
- 冻结文档：`spec.md` 自 `4a899a12`、`plan.md` 自 `50b25b6f` 均未改动；`tasks.md` 仅勾选完成项并追加「验证记录」，任务定义未改写。

### 复核方式与验证

- 定向：`tests/core/hook` 复跑 50 passed，与记录一致。
- 回归：`tests/core/nodes/test_tool.py tests/core/graphs tests/core/ag_ui tests/core/interrupt` 698 passed / 1 skipped；`tests/services` 1179 passed / 47 skipped，无既有失败。
- 另用独立脚本复现 F-1、F-2（脚本不进入仓库）：F-1 用 MemorySaver + 真实 `interrupt()` 验证拒绝后续流仍执行工具；F-2 用两段新运行态验证 `model_pre` 重复触发。

### 逐条需求核对

- REQ-1 点位与裁决：`HookPoint` 五取值逐字一致；`HookSet` 未知点位 `ValueError`、非可调用 `TypeError`；`HookRunner` 按序执行、`deny`/`ask` 短路、`replace`/`append` 后写生效；非法裁决/缺载荷按 `pass` 降级（`points.py`、`registry.py`、`runner.py:207-231`）。同步与异步共用归一化逻辑。
- REQ-2 `session_start`：模型链装配后首次请求前触发（`model_chain.py` 插入 `_apply_pre_hooks`、`stages.py:126-137`）；`append` 以独立 `SystemMessage` 插到 system 段末尾（`stages.py:72-84`）；`deny` 抛 `HookDeniedError`，测试确认无模型请求。
- REQ-3 `model_pre`/`model_post`：单轮内去重、中间响应不触发、`replace` 改写实际发送/输出，均有对应用例；**但续流后重复触发，见 F-2**。
- REQ-4 `tool_pre`/`tool_post`：`tool_pre` 在 pv 阶段按模型顺序串行触发，代理解包后取真实工具名（`tool_call_proxy_wrapper.py:50-78`）；应用 wrapper 位于解包之后、净化/脱敏/审批之前（`tool/node.py:367-378`）；循环守卫仍在链首，`replace` 不影响守卫输入。`tool_post` 在 `_content_error` 之内的最内层，看到未脱敏原文，结果继续流经 `_content_error`/去重/截断/脱敏/净化。**中断续流后 `tool_pre` 裁决丢失，见 F-1。**
- REQ-5 注入与隔离：`build_agent(hooks=...)` → `ChatBuildExtras.hooks` → `ChatCompletionAgent.hooks` → `AgentExecutorKwargs.hooks` → `ReActAgentBuilder._hooks` → 三个节点；`HookRunner` 每次 `build()` 新建，`HookSet` 只在实例字段上；空集合/未注入时输出与基线一致（`test_zero_regression.py`）。
- REQ-6 执行语义：合并、短路、5 秒超时、结构化日志、非法返回值降级均已实现并有契约用例。
- REQ-7 零回归：未注入时模型链不增步骤、wrapper 列表形状不变、`_execute` 不写运行态键；`test_no_hooks_matches_baseline_sequence` 与回归面跑通。

### 结论

REQ-1、REQ-2、REQ-5、REQ-6、REQ-7 满足；REQ-3 的「每轮各一次」与 REQ-4/AC8 的「未获批准不得执行」在中断续流路径上不成立（F-1、F-2），其中 F-1 会让人工拒绝后工具照常执行，属安全/正确性阻塞项。本轮判定为需要返工，目标节点为代码实现。
