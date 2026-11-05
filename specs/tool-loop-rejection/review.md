# 代码评审

## 最新结论

- 结论: 需要返工
- 评审版本: e201a1fd3022e5f4e056fbf51d53ebe9f2056cbd

### 阻塞项

#### F-1 第二次拒绝反馈的「最后一次真实返回内容摘要」取到了上一条拒绝文案（REQ-2）

- **需求**: REQ-2「拒绝反馈中的『最后一次真实返回内容』必须取自该判定键最近一次真实执行的返回结果」；方案 D-3「摘要取自该判定键连续段内最近一条有对应 ToolMessage 的**真实结果**」。
- **证据**:
  - `src/agent/aidev_agent/core/nodes/tool/loop_guard.py:144-151` 的 `_nearest_result` 只按 `tool_call_id ∈ segment_ids` 取窗口内最近一条 ToolMessage，不区分该消息是真实执行结果还是守卫自己生成的拒绝反馈；`_scan`（`:186-193`）把连续段内**全部**历史调用（含被拒绝的调用）都并入 `segment_ids`。
  - 按默认 `T=3 / F=2`、同一判定键连续 5 次同参调用逐轮驱动 `tools` 节点实测：

    ```text
    第 1～3 次：status=success，真实执行 3 次
    第 4 次（首次拒绝）：status=error，摘要段 = REAL_OUTPUT::x        ← 正确
    第 5 次（第二次拒绝）：status=error，摘要段 = 第 4 次的拒绝文案     ← 错误
                           （正文为「工具 echo 以相同参数已连续调用 4 次……」，
                             真实返回内容被嵌套到第二层）
    第 6 次：抛 ToolLoopTerminationError（符合预期）
    ```

    复现方式（与本轮评审相同）：构造 `HumanMessage` + 3 轮 `AIMessage(tool_call)+ToolMessage` + 当前 `AIMessage`，逐次 `compiled.invoke`，读取最后一条 `ToolMessage.content`。
- **影响**: 默认配置下第 5 次调用就命中，是常规路径而非边界；模型把守卫自己的拒绝文案当成工具返回内容，反馈的引导作用失真；且反馈正文会随拒绝次数嵌套膨胀（`_truncate` 只截 `[0:limit]`），一旦触到 `result_limit`，真正需要展示的工具输出会被挤掉。
- **用例缺口**: `tests/core/nodes/tool/test_loop_guard.py:159` 的 `test_rejection_feedback_fields` 只断言第 4 次（首次拒绝），未覆盖第二次拒绝，因此既有 27 条用例全绿仍然漏掉该问题。
- **修复后可判定标准**:
  1. 默认 `T=3 / F=2`、同一判定键连续 5 次调用时，第 5 次返回的 `status="error"` ToolMessage 中「最后一次真实返回内容摘要」段只含最近一次**真实执行**的返回内容；可用独特返回值断言其出现，并断言第 4 次的拒绝文案不出现在摘要段中。
  2. 新增一条逐轮驱动 `tools` 节点的端到端用例，覆盖第 4、5 次两次拒绝，同时断言两次摘要都来自真实执行结果、`status="error"` 与调用次数（4 / 5）。
  3. 其余 AC1～AC14 的既有断言保持全绿，且修复不得改变拒绝 span 的 `error.type=ToolLoopRejection`、`tool.name`/`tool.input` 口径与终止行为。

### 改进建议

- W-1 验证环境与锁定版本不一致：`requirements.txt:78` 锁 `langgraph==1.0.7`，本轮本仓 `src/agent/.venv` 实装 `langgraph 1.0.5`（`langgraph-1.0.5.dist-info`），`_run_one/_arun_one` 是私有方法，建议在 1.0.7 下补跑 `test_loop_guard.py` 与 `test_tool_result_status.py` 并记录，避免升级/装依赖后才发现签名漂移。
- W-2 T-4 完成标准要求「父 span 为 `tools` 节点」，用例只断言 span 名/状态/属性，未断言父 span。本轮实测拒绝 span 的父 span 与真实调用一致（均为当前 `chain.task`），建议补一条父 span 断言把该契约固化。
- W-3 AC7（异常路径）没有新增用例，只依赖既有行为「本变更不得回退」。建议补一条回归断言，防止后续改动 `on_tool_end` 时把异常路径的 `error.type=异常类名` 改坏。
- W-4 `specs/tool-loop-rejection/tasks.md` 记录了 T-9 未完成（otel 目录 1 条既有失败、`make lint` 23 处既有告警）。本轮核对：失败用例 `test_otel_config_uses_central_attribute_limits_by_default` 在 `origin/develop` 上同样成立（`develop` 的 `BKAI_AGENT_MAX_INPUT_ATTRIBUTE_LENGTH` 已是 `500 * 1024`，该用例文件本次未改动），确属改动外既有缺陷，不构成本次返工理由；建议在 Issue 上单列跟踪。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "REWORK",
  "change_name": "tool-loop-rejection",
  "iteration": 1,
  "reviewed_sha": "e201a1fd3022e5f4e056fbf51d53ebe9f2056cbd",
  "blocking": [
    {
      "id": "F-1",
      "severity": "requirement",
      "requirement_ids": ["REQ-2"],
      "evidence": "src/agent/aidev_agent/core/nodes/tool/loop_guard.py:144-151 的 _nearest_result 只按 tool_call_id 归属筛选 ToolMessage，不排除守卫自己产生的拒绝反馈；_scan(:186-193) 把连续段内被拒绝的调用也并入 segment_ids。默认 T=3/F=2 连续 5 次同参调用实测：第 5 次（第二次拒绝）反馈的「最后一次真实返回内容摘要」为第 4 次的拒绝文案，而非最近一次真实执行结果。",
      "acceptance": "默认 T=3/F=2 连续 5 次同参调用时，第 5 次 ToolMessage 的「最后一次真实返回内容摘要」段只含最近一次真实执行的返回内容，且不含上一次拒绝文案；新增逐轮驱动 tools 节点的端到端用例覆盖第 4、5 次拒绝并断言两次摘要均来自真实执行结果；其余 AC1～AC14 断言保持全绿，拒绝 span 与终止行为不变。"
    }
  ],
  "warnings": [
    {"id": "W-1", "evidence": "requirements.txt:78 锁 langgraph==1.0.7，src/agent/.venv 实装 1.0.5；实现依赖私有 _run_one/_arun_one", "suggestion": "在锁定版本 1.0.7 下补跑两个新增测试文件并记录结果"},
    {"id": "W-2", "evidence": "T-4 完成标准要求父 span 为 tools 节点，tests/packages/opentelemetry/test_tool_result_status.py 未断言父 span（本轮实测与真实调用一致）", "suggestion": "补一条父 span 断言固化契约"},
    {"id": "W-3", "evidence": "AC7 异常路径无新增用例，改为依赖既有行为", "suggestion": "补一条异常路径回归断言，防止 on_tool_end 改动回退"},
    {"id": "W-4", "evidence": "T-9 记录 otel 目录 1 条失败与 make lint 23 处告警，均为改动外既有问题", "suggestion": "在 Issue 上单列跟踪既有失败，不计入本次返工"}
  ]
}
```

</details>

## 第 1 轮

### 核对范围

- 仓: `git@github.com:ChrisJaunes/bk-aidev-agent.git`；基线 `origin/develop`（`fd06ab9a`）；工作分支 `feature/tool-loop-rejection`；评审起点 HEAD `e201a1fd3022e5f4e056fbf51d53ebe9f2056cbd`（远端一致）。
- 累计改动 3 个提交、11 个文件：`spec.md`/`plan.md`/`tasks.md` + 6 个源码 + 2 个测试。
- 冻结文档核对：`spec.md` 与需求澄清交付（`6bf8c1ec`）一致；`plan.md` 与方案交付（`2e1bfb21`）一致；`tasks.md` 仅新增勾选与验证记录（+70/-8），任务定义未改。未发现冻结定义被实现阶段篡改。
- 逐条比对 REQ-1～REQ-7 与 AC1～AC14：判定键/连续计数/批次折叠、阈值放行、拒绝不执行工具本体且不写 `gen_ai.tool.call.result`、`T+F+1` 终止并保留中间结果、内容路径 `[ERROR]` 前缀、拒绝 span 的 `error_type` 与载荷、两项环境变量启动期校验、判定异常放行，实现与需求一致；唯一不一致为 F-1。

### 验证执行

- `tests/core/nodes/tool/test_loop_guard.py` → 27 passed。
- `tests/packages/opentelemetry/test_tool_result_status.py` → 10 passed。
- `tests/core/nodes/test_tool.py` → 56 passed。
- `tests/packages/opentelemetry`（整目录）→ 255 passed, 1 failed；失败用例为 `test_otel_config_uses_central_attribute_limits_by_default`（`assert 512000 == 80 * 1024`），经 `git show origin/develop:src/agent/aidev_agent/config.py:210` 确认基线默认值即为 500 KiB、该用例文件本次未改动，属改动外既有失败。
- `make lint` 未复跑：本仓环境无 ruff 可执行文件（`src/agent/.venv/bin` 下不存在），实现记录称改动文件单独 `ruff check`/`ruff format --check` 通过；静态风格不作为返工理由。
- 额外按默认配置逐轮驱动 `tools` 节点复现拒绝/终止链路，发现 F-1；拒绝 span 的父 span 与真实调用一致（同为当前 `chain.task`），`error.type=ToolLoopRejection`、无 `gen_ai.tool.call.result`。

### 结论

实现主体符合已审批规格，拒绝 span 口径、终止语义与内容路径失败标记均可用；但默认配置下第二次拒绝反馈的摘要取错来源，违反 REQ-2 且会削弱打断循环的关键信号。判定 `REWORK`，回到代码实现补充修复与端到端用例。
