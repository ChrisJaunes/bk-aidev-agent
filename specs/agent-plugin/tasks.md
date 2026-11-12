# Agent Core 插件机制：hook 协议与注入 任务清单

- 变更名: agent-plugin
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/agent-plugin/spec.md
- 方案: specs/agent-plugin/plan.md

仓库路径简写：下文 `core/` 指 `src/agent/aidev_agent/core/`，`services/` 指 `src/agent/aidev_agent/services/`，
`tests/` 指 `src/agent/tests/`。执行顺序：T-1 → T-2 →（T-3 与 T-5、T-6 可并行）→ T-4 → T-7。

## T-1 hook 协议与执行器骨架

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `core/hook/__init__.py`、`core/hook/points.py`、`core/hook/decisions.py`、`core/hook/contexts.py`、
  `core/hook/registry.py`、`core/hook/runner.py`、`core/hook/errors.py`、`core/hook/run_state.py`、
  `tests/core/hook/__init__.py`、`tests/core/hook/test_contracts.py`
- 需求: REQ-1、REQ-6
- 依赖: 无
- 验证: `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook/test_contracts.py -q`
- [x] 完成标准：
  - `HookPoint` 五个取值与规格逐字一致，`HookSet` 对未知点位名抛 `ValueError`、对非可调用对象抛 `TypeError`。
  - 同点位处理器按注入顺序执行；首个 `deny` / `ask` 短路；`replace` / `append` 后写生效。
  - 处理器抛异常、返回未知裁决、缺 `reason` / `payload`、`ask` 出现在非 `tool_pre` 点位时，均按 `pass` 处理并记 warning。
  - 同步与异步处理器在同一输入下产出同一 `HookDecision`；单处理器超过 `TIMEOUT_SECONDS=5` 时按 `pass` 处理并记 warning（用可控延迟处理器断言）。
  - 每次执行产生一条含 `point` / `handler` / `decision` / `duration_ms` 的日志。

## T-2 注入链路贯通

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `services/agent/factory.py`、`services/agent/registry.py`、`pydantic_models.py`、
  `services/agent/chat.py`（`build` / `_get_agent`）、`core/graphs/react/graph.py`（`set_hooks` / `set_bkai_options` /
  `_prepare_agent_model_node` / `_prepare_agent_tool_node` / `_prepare_agent_pv_node`）、
  `core/nodes/model/node.py`、`core/nodes/tool/node.py`、`core/nodes/pv.py`（仅新增可选参数，不接行为）
- 需求: REQ-5、REQ-7
- 依赖: 阻塞于 T-1
- 验证: `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/services tests/core/graphs -q`
- [x] 完成标准：
  - `build_agent(hooks=...)` 可把集合带到 `ReActAgentBuilder`；不传时全链路为 `None`，三个 `_prepare_*` 产出与基线等价。
  - `ReActAgentBuilder.set_hooks()` 与 `AgentExecutorKwargs(hooks=...)` 两条入口都生效且互不覆盖。
  - 未注入时 `build_tool_node` 的 wrapper 列表与基线逐项相同（可用现有 `tests/core/nodes/test_tool.py` 断言链序不变）。
  - 现有 `tests/services`、`tests/core/graphs` 用例全绿。

## T-3 模型点位：`session_start` 与 `model_pre`

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `core/hook/stages.py`、`core/nodes/model/node.py`、`core/nodes/model/model_chain.py`、
  `services/agent/chat.py`（`_execute` 绑定运行态）、`tests/core/hook/test_model_hooks.py`
- 需求: REQ-2、REQ-3（`model_pre` 部分）、REQ-1
- 依赖: 阻塞于 T-2
- 验证: `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook/test_model_hooks.py -q -k "session_start or model_pre"`
- [x] 完成标准：
  - `session_start` 在首次请求前恰好一次；`append` 片段出现在该轮实际发送的消息中，且在后续请求中保持存在。
  - `session_start` 返回 `deny` 时不发出任何模型请求、不执行任何工具，运行以 `HookDeniedError` 结束且消息可读。
  - `model_pre` 在该轮首次请求前触发一次；recovery 重试（空内容→正常响应）与工具回灌后的请求都不再触发。
  - `model_pre` 的 `replace` 与该次实际发送的消息逐项相同，且该轮后续请求继承（工具回灌后仍带着覆盖内容）。
  - `model_pre` 返回 `deny` 时不发出该请求、运行以可读错误结束。
  - 未注入 hooks 时模型链步骤数与基线一致（不新增 Runnable 步骤）。

## T-4 `model_post` 与运行收尾

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `core/hook/stages.py`、`core/hook/run_state.py`、`core/nodes/model/model_chain.py`、
  `services/agent/chat.py`（`_stream` / `_stream_with_legacy` / `_invoke` 出口）、
  `core/ag_ui/aidev_agent.py`（`_emit_run_end_extras`）、`tests/core/hook/test_model_hooks.py`
- 需求: REQ-3（`model_post` 部分）
- 依赖: 阻塞于 T-3
- 验证: `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook/test_model_hooks.py -q -k "model_post"`
- [x] 完成标准：
  - 每轮最终答复（无 `tool_calls` 的响应）在返回状态前恰好触发一次；含工具调用的中间响应不触发。
  - `replace` 的文本成为该轮实际输出并随状态落库；`deny` 以可读错误终止本次运行。
  - 无最终答复的三条终止路径（中断、达到续跑上限、取消）各补触发一次，且不重复触发（用 fake 图分别模拟三种终止）。
  - 终止路径上的 `replace` / `deny` 只记 warning、不改变已终止运行的结果。
  - 未注入 hooks 时收尾路径不执行任何额外读取（不调用 `get_state`）。

## T-5 `tool_pre`：pv 阶段决策与 wrapper 应用（含 `ask`）

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `core/hook/stages.py`、`core/nodes/pv.py`、`core/nodes/tool/hook_wrapper.py`、
  `core/nodes/tool/node.py`、`core/nodes/tool/tool_call_proxy_wrapper.py`（导出真实工具名判定）、
  `core/graphs/react/graph.py`（`_prepare_agent_pv_node` 传 `tools` 与阶段函数）、`tests/core/hook/test_tool_hooks.py`
- 需求: REQ-4（`tool_pre` 部分）、REQ-1
- 依赖: 阻塞于 T-2
- 验证: `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook/test_tool_hooks.py -q -k tool_pre`
- [x] 完成标准：
  - 每次工具调用在真正执行前触发一次；同一批次并行多个调用时按模型给出的顺序逐个触发（用三调用的 fake 模型断言顺序）。
  - 处理器收到的工具名是真实工具名（代理解包场景下不是代理名），`tool_call_id` 与模型给出的一致。
  - `deny` 时工具不执行、不进审批，模型收到含拒绝原因的 `status="error"` 结果。
  - `replace` 的参数生效（工具收到改写后的参数），且循环守卫仍按模型原始参数判定（构造重复调用用例断言守卫照常拒绝）。
  - `ask` 时走 `ask_user_question` 中断；未获批准工具不执行，获批准后继续（若工具配了 ITSM 审批则仍走审批）。
  - 应用 wrapper 位于代理解包之后、不可信净化 / 脱敏 / ITSM 审批之前（断言链序）。

## T-6 `tool_post`：最内层结果拦截

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `core/nodes/tool/hook_wrapper.py`、`core/nodes/tool/node.py`、`tests/core/hook/test_tool_hooks.py`
- 需求: REQ-4（`tool_post` 部分）
- 依赖: 阻塞于 T-2
- 验证: `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook/test_tool_hooks.py -q -k tool_post`
- [x] 完成标准：
  - 每次工具调用返回后触发一次；工具抛错时同样触发且 `is_error=true`，正常返回时 `is_error=false`。
  - 处理器拿到的是未脱敏原文（构造命中脱敏规则的返回值，断言 hook 侧看到原文、模型侧看到脱敏结果）。
  - `replace` 后的内容成为模型看到的结果，并继续经过 `_content_error` 的重新错误识别、去重、截断、脱敏、不可信净化。
  - `append` 不改原结果内容；`deny` 把结果换成含原因的说明并置 `status="error"`。
  - wrapper 位于 `deduplicate` 与 `_content_error` 之内（断言链序）。

## T-7 隔离、零回归与验收矩阵核对

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `tests/core/hook/test_isolation.py`、`tests/core/hook/test_zero_regression.py`、`tests/core/hook/conftest.py`
  （fake 模型 / fake 工具 / fake 事件录制夹具）
- 需求: REQ-5、REQ-7
- 依赖: 阻塞于 T-4、T-5、T-6
- 验证:
  `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook -q`
  以及
  `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/nodes tests/core/graphs tests/core/ag_ui tests/core/interrupt tests/services -q`
- [x] 完成标准：
  - 同进程两个实例注入不同集合并发运行，各自只执行自己的处理器，日志中不出现对方的处理器标识；`HookSet` 无模块级可变状态。
  - 未注入时同一输入下模型请求序列、工具执行序列、最终输出、AG-UI 事件序列与基线逐项一致（事件录制比对）。
  - 未注入时不新增模型 / 工具调用，不新增图状态键写入（比对 state 键集合）。
  - 规格 AC1–AC15 每条都能在 `tests/core/hook` 中找到对应断言（在测试文件内以 `# AC<n>` 标注，完整跑一遍）。
  - 上述两条回归命令全绿。

## T-8 文档与提交

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `specs/agent-plugin/plan.md`、`specs/agent-plugin/tasks.md`
- 需求: REQ-1～REQ-7（口径落纸）
- 依赖: 阻塞于 T-7
- 验证: `git -C . show --stat HEAD`（仅含上述两个文件与实现/测试提交）、远端 `feature/agent-plugin` HEAD 与本地一致
- [x] 完成标准：
  - 文档中的点位、裁决、落点与实际实现一致；未修改 `specs/agent-plugin/spec.md`。
  - 提交信息携带 `--story=1070093903138045802`，推送后远端 SHA 校验通过。

## 验证记录

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git；分支 `feature/agent-plugin`；基线 `develop` = `952e2cb3`
- 人工预览: 无可预览（本期为进程内协议与注入，无独立可访问环境，未部署任何地址）

| 命令 | 结果 | 说明 |
| --- | --- | --- |
| `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/hook -q` | 通过（58 passed） | AC1–AC15 全部在 `tests/core/hook` 有对应用例，文件头以 `# AC<n>` 标注 |
| `cd src/agent && uv run --no-sync pytest -c pyproject.toml tests/core/nodes tests/core/graphs tests/core/ag_ui tests/core/interrupt tests/services -q` | 通过（2500 passed, 73 skipped, 1 xfailed） | 模型节点、工具节点、图装配、AG-UI、中断与 services 回归全绿 |
| `uvx ruff@0.14.10 check` / `ruff format --check`（改动文件与 `tests/core/hook`） | 通过 | 无告警 |

补充说明：

- `tests/core/graphs/react/test_tool_loading_mode.py` 的 `build_tool_node` 签名断言同步加入新增可选参数 `hooks`；未注入时 wrapper 列表形状与基线逐项一致（链序断言用例仍全绿）。
- `model_post` 的三条终止出口（中断 / 续跑上限 / 取消）统一经 `_execute` 出口的 `finalize_run` 补触发；`_invoke` 走 `finally`，流式走 `_finalize_stream` 包装，未注入 hooks 时不做任何包装与读取。

## 返工记录（第 1 轮评审，reviewed_sha `070471e5`）

阻塞项：hook 运行态按 run 重建，中断续流后 `tool_pre` 裁决丢失（人工拒绝 `ask` 后工具仍执行、
`deny` / `replace` 同样失效），同一根因导致续流后同一轮 `model_pre` / `model_post` 重复触发。

根因与修复：

- 根因：运行态只存在 `config["configurable"]`，续流是新运行（新 config），裁决与「每轮一次」标记全部重置；
  续流时 pv 节点不会重跑，工具 wrapper 取不到原裁决。
- 修复：新增图状态续接通道 `hook_run_state`（仅在注入 hooks 时进入 state schema）。模型节点与 pv 节点
  在返回时写入快照（`turn`、`model_pre_done`、`model_pre_override`、`model_post_done`、
  `model_request_count`、`tool_pre_decisions`），模型节点 / pv 节点 / 工具 wrapper 在入口按快照续接，
  只在每个运行内并入一次，已下发的裁决不被覆盖。
- `model_post` 只按「每轮一次」触发：最终答复触发后置位；终止路径（中断）按 AC6 补触发并计为本轮一次，
  纯续流（无新用户输入）不再重复；续流携带新用户输入（跳过提问）视为新一轮，`turn` +1 并重新开放点位。
- 新增 `tests/core/hook/test_resume.py`：基于真实 `interrupt()` + `Command(resume=...)` + `MemorySaver`
  （不 monkeypatch interrupt）覆盖：拒绝不执行 / 允许执行一次、同批次 `deny`+`replace` 续流后仍生效、
  `model_pre` 与 `model_post` 整轮各一次且 `identity.turn` 不变、正常路径仍触发 `model_post`。

验证结果（本轮）：

- `tests/core/hook`：58 passed（含 `test_resume.py` 8 条）。
- `tests/core/nodes tests/core/graphs tests/core/ag_ui tests/core/interrupt tests/services`：2500 passed,
  73 skipped, 1 xfailed。
- 未注入 hooks 时 `hook_run_state` 通道不进入 state schema、不写入，零回归用例保持通过。
