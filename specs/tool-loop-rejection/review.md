# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: 837598816b89c9f71792c2609b268826b9fafbbb

### 阻塞项

无。第 1 轮的 F-1（第二次拒绝反馈摘要取到上一条拒绝文案，违反 REQ-2）已修复并复验通过，复验证据见「第 2 轮」。

### 改进建议

- W-1 验证环境与锁定版本不一致：`requirements.txt:78` 锁 `langgraph==1.0.7`，本仓 `src/agent/.venv` 实装 `langgraph 1.0.5`（`langgraph-1.0.5.dist-info`），实现依赖私有 `_run_one/_arun_one`。实现方已核对两版签名一致，但仍建议在 1.0.7 下补跑 `test_loop_guard.py` 与 `test_tool_result_status.py` 并记录，避免升级或重建环境后才发现漂移。
- W-2 T-4 完成标准要求「父 span 为 `tools` 节点」，用例只断言 span 名/状态/属性，未断言父 span。复验实测拒绝 span 的父 span 与真实调用一致（均为当前 `chain.task`），建议补一条父 span 断言把该契约固化。
- W-3 AC7（异常路径）没有新增用例，只依赖既有行为「本变更不得回退」。建议补一条回归断言，防止后续改动 `on_tool_end` 时把异常路径的 `error.type=异常类名` 改坏。
- W-4 `specs/tool-loop-rejection/tasks.md` 记录 T-9 未完成（otel 目录 1 条既有失败、`make lint` 23 处既有告警）。两轮核对均确认失败用例 `test_otel_config_uses_central_attribute_limits_by_default` 在 `origin/develop` 同样成立（基线 `BKAI_AGENT_MAX_INPUT_ATTRIBUTE_LENGTH` 已是 `500 * 1024`，该用例文件未改动），属改动外既有缺陷，不构成打回理由；建议在 Issue 上单列跟踪。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "tool-loop-rejection",
  "iteration": 2,
  "reviewed_sha": "837598816b89c9f71792c2609b268826b9fafbbb",
  "blocking": [],
  "warnings": [
    {"id": "W-1", "evidence": "requirements.txt:78 锁 langgraph==1.0.7，src/agent/.venv 实装 1.0.5；实现依赖私有 _run_one/_arun_one", "suggestion": "在锁定版本 1.0.7 下补跑两个新增测试文件并记录结果"},
    {"id": "W-2", "evidence": "T-4 完成标准要求父 span 为 tools 节点，tests/packages/opentelemetry/test_tool_result_status.py 未断言父 span（复验实测与真实调用一致）", "suggestion": "补一条父 span 断言固化契约"},
    {"id": "W-3", "evidence": "AC7 异常路径无新增用例，改为依赖既有行为", "suggestion": "补一条异常路径回归断言，防止 on_tool_end 改动回退"},
    {"id": "W-4", "evidence": "T-9 记录 otel 目录 1 条失败与 make lint 23 处告警，两轮核对均为改动外既有问题", "suggestion": "在 Issue 上单列跟踪既有失败"}
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

## 第 2 轮

### 核对范围

- 评审版本 `837598816b89c9f71792c2609b268826b9fafbbb`（远端一致），基线 `origin/develop`（`fd06ab9a`），工作分支 `feature/tool-loop-rejection`。
- 本轮增量：相对第 1 轮评审版本 `e201a1fd` 新增 1 个实现提交，实际代码改动只有 `core/nodes/tool/loop_guard.py`（+31/-6）与 `tests/core/nodes/tool/test_loop_guard.py`（+32/-1），其余为 `tasks.md` 返工记录与上一轮 `review.md`。`spec.md`、`plan.md` 与各自交付版本逐字一致，`tasks.md` 仅新增勾选与记录，冻结定义未被改动。
- 未发现范围外改动，无新增依赖、无配置项变更。

### F-1 复验

- 修复点：`_scan` 新增 `allowed_limit` 参数，连续段按「由旧到新」排列后只把序号 `< T` 的调用的 `tool_call_id` 收进 `executed_ids`，`_nearest_result` 因此只可能命中真正执行过的调用；`_decide` 传入 `policy.repeat_limit`，`count_contiguous` 的计数语义（返回 `len(segment)`）未变。
- 独立复验（默认 `T=3/F=2`，逐轮驱动 `tools` 节点，工具返回值带递增序号）：

  ```text
  第 1～3 次：status=success，真实执行 3 次（REAL#1 / REAL#2 / REAL#3）
  第 4 次（首次拒绝）：status=error，次数 4，摘要段 = REAL#3   ← 正确
  第 5 次（第二次拒绝）：status=error，次数 5，摘要段 = REAL#3  ← 已修复，不再是第 4 次的拒绝文案
  第 6 次：抛 ToolLoopTerminationError，消息含「任务因工具循环调用被中断」、工具名与 6 次/3 次/2 次
  ```

- 判别用例 `test_two_rejections_track_last_real_result` 逐轮驱动 5 次调用，断言两次拒绝均为 `status="error"`、次数 4/5、摘要段等于第 3 次真实返回值、摘要段不含拒绝文案；实现方记录了回退修复后该用例 FAILED，与本次独立复验一致，用例具备判别力。

### 验证执行

- `tests/core/nodes/tool/test_loop_guard.py` + `tests/packages/opentelemetry/test_tool_result_status.py` → 38 passed（28 + 10）。
- `tests/core/nodes/test_tool.py` → 56 passed。
- `tests/core/nodes`（整目录）→ 568 passed, 27 skipped, 1 xfailed。
- `tests/packages/opentelemetry`（整目录）→ 255 passed, 1 failed，仍为 `test_otel_config_uses_central_attribute_limits_by_default`（`assert 512000 == 80 * 1024`）：`origin/develop` 的 `config.py:210` 默认值即为 500 KiB，该用例文件本次未改动，与第 1 轮判定一致，属改动外既有失败。
- `make lint` 未复跑：环境内无 ruff 可执行文件；实现记录称改动文件单独 `ruff check` / `ruff format --check` 通过，静态风格不作为打回理由。

### 结论

第 1 轮唯一阻塞项 F-1 已按要求修复：默认配置下两次拒绝的摘要均取自最近一次真实执行结果，且新增端到端用例可判别该回归。REQ-1～REQ-7 与 AC1～AC14 复核无新增偏差，拒绝 span 口径与终止语义保持不变，约定验证仅剩改动外既有失败。判定 `PASS_WITH_WARNING`，可进入后续节点；W-1～W-4 作为改进建议留存。
