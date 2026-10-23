# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: 7f9687b8f67f2c4788f3710f37d9ff40fcfa2f1e
- 评审基线: `7a171a8af285c24733d75a16428ef31edfcbbd05`（`origin/develop`）
- 累计改动: 21 个文件，+1071 / -9（含 `specs/tool-loading-mode/` 三份规格与 6 个测试文件）

### 阻塞项

无。

### 改进建议

- **W-1 ask 工具名仍有两处字面量，与「单一来源」的目标口径不一致**：`src/agent/aidev_agent/core/tools/ask_user_question.py:95` 仍写 `name="ask_user_question"`，`src/agent/aidev_agent/core/ag_ui/event_builders.py:141` 仍写 `== "ask_user_question"`；而 `ASK_USER_QUESTION_TOOL_NAME` 的 docstring 声明「工具装配（`core/tools/ask_user_question.py` 的 `StructuredTool.name`）…均经此常量判定」，与现状不符。T-2 的完成标准只约束了 `core/ag_ui/aidev_agent.py` 与 `services/agent/chat.py`，故不构成返工；但 `filter_tools_by_loading_mode` 按名字判定 ask 类工具，若工具名变更而未同步常量，`dontInterrupt` / `dontAsk` 会静默漏剔 ask 工具。建议把工具定义改为 `name=ASK_USER_QUESTION_TOOL_NAME`，`event_builders` 同步改用常量。
- **W-2 AC-12 的交集证据只在中间件层，普通 Agent 下 `tool_allow` / `tool_deny` 本就不生效**：`ToolFilterMiddleware` 仅在 `self._a2a_specs and self._a2a_resolver` 成立时注册（`core/graphs/react/graph.py:861-864`），非 A2A 装配路径上 `tool_allow` / `tool_deny` 没有任何消费方（改动前既有行为，本次未触及）。`test_mode_intersects_tool_allow/deny` 通过直接构造 `ProcessorContext` 调中间件验证交集，因此「三约束端到端交集」目前只在 A2A 场景可观察。mode 侧是纯移除，不会覆盖 allow/deny 语义，风险低；建议在注释或规格中说明交集的可观测范围。
- **W-3 AC-8 / AC-9 只有函数级证据**：`command_approval_allowed("dontAsk") is True`、`dontAsk` 下带审批工具仍在集合中均有单测，但「`dontAsk` 下真实发起审批中断并在通过后继续执行」没有集成用例，靠既有审批链路用例间接保障。本次未改动该链路，可接受；建议补一条集成用例固定语义。
- **W-4 AC-10 的 4xx 只在插件入口有证据**：`ChatCompletionRequestSerializer.validate_execute_kwargs` 已把 pydantic `ValidationError` 转为 DRF 400（`serializers/chat_completion.py:89-92`，有测试）；SDK 直接构造入口仍以 pydantic `ValidationError` 冒泡，是否映射 4xx 取决于调用方。建议在 REQ-9 的适用范围上写明「HTTP 4xx 以内置插件链路为准」。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "tool-loading-mode",
  "iteration": 1,
  "reviewed_sha": "7f9687b8f67f2c4788f3710f37d9ff40fcfa2f1e",
  "blocking": [],
  "warnings": [
    {
      "id": "W-1",
      "evidence": "src/agent/aidev_agent/core/tools/ask_user_question.py:95 仍为 name=\"ask_user_question\"，src/agent/aidev_agent/core/ag_ui/event_builders.py:141 仍按字面量比较；ASK_USER_QUESTION_TOOL_NAME 的 docstring（packages/interrupt_manager/ask_user_question.py:63-70）声称工具装配也经该常量判定",
      "suggestion": "把 StructuredTool 的 name 与 event_builders 的判定改为引用 ASK_USER_QUESTION_TOOL_NAME，使「单一来源」与实现一致"
    },
    {
      "id": "W-2",
      "evidence": "ToolFilterMiddleware 仅在 core/graphs/react/graph.py:861-864 的 A2A 分支注册；tests/core/graphs/react/test_tool_loading_mode.py 通过直接构造 ProcessorContext 调用中间件验证 AC-12，非 A2A 装配路径没有 tool_allow/tool_deny 消费方（改动前既有行为）",
      "suggestion": "在 plan/review 或代码注释中说明交集语义仅在 A2A 场景生效；如要覆盖普通 Agent，需要另立需求把中间件改为无条件注册"
    },
    {
      "id": "W-3",
      "evidence": "AC-8/AC-9 的证据为 tests/core/graphs/react/test_tool_loading_mode.py::test_dont_ask_drops_ask_only 与 tests/core/graphs/react/test_tool_loading_mode.py::test_command_approval_allowed[dontAsk-True]，均为函数/装配级；无 dontAsk 下审批中断-通过的集成用例",
      "suggestion": "补一条 dontAsk + 带审批工具 + 命令灰名单的集成用例，固定「审批链路与 default 一致」"
    },
    {
      "id": "W-4",
      "evidence": "src/plugins/aidev_bkplugin/aidev_bkplugin/serializers/chat_completion.py:89-92 将 ValidationError 转为 DRF 400 并有测试；SDK 直接构造（core/graphs/react/graph.py:231、services/common_agent/agent.py:30）仍以 pydantic ValidationError 冒泡，无 4xx 映射",
      "suggestion": "在 spec/plan 中注明 REQ-9 的 4xx 以内置插件 HTTP 链路为准，避免后续按「所有入口 4xx」验收"
    }
  ]
}
```

</details>

## 第 1 轮

### 本轮范围

- 仓库: `git@github.com:ChrisJaunes/bk-aidev-agent.git`（单仓，`spec.md` 声明无跨仓依赖与执行顺序）
- 工作分支: `feature/tool-loading-mode`，操作模式 `沿用只读` 评审 + 仅追加 `review.md`
- 基线: `origin/develop` = `7a171a8a`；评审对象: `7a171a8a..7f9687b8` 的累计改动（代码实现节点 `60156e24` 交付）
- 前置校验: 分支名合法、远端存在且 SHA 与本地 `HEAD` 一致（`7f9687b8…`）；`specs/tool-loading-mode/spec.md` 与已审批附件 `diff` 无输出；`plan.md` 自 `460fd609` 起未变
- 冻结文档核对: `spec.md`、`plan.md` 实现阶段未改；`tasks.md` 仅新增勾选与「验证记录」（T-1～T-10 完成标准、验证命令与结果、T-9 手工结论、环境说明），属实现节点交付范围，冻结文档未被改写

### 逐条需求核对

| 需求 | 结论 | 证据 |
| --- | --- | --- |
| REQ-1 mode 参数定义 | 满足 | `pydantic_models.py:56-62` 新增 `Literal["default","dontInterrupt","dontAsk"]`，缺省 `default`；`tests/services/test_pydantic_models.py::TestExecuteKwargsMode` 覆盖默认值、三个合法值、`model_dump`/`model_copy` 保留 |
| REQ-2 default 全量加载 | 满足 | `core/graphs/react/tool_loading_mode.py:43-71` 的 `default` 分支原样返回入参列表（`is` 同一对象）；装配级 `test_default_without_mode_equals_explicit_default`、`test_default_keeps_ask_and_approval` |
| REQ-3 dontInterrupt 跳过所有中断来源 | 满足 | 过滤 `tool_loading_mode.py:64-69`（ask 名 + `is_approval_configured`）；命令级审批开关经 `graph.py:911` → `provider.py:847-853/882-888` → `command_security.py:1177-1180` fail-closed；`dontInterrupt` 仍保留 `execute`（`test_dont_interrupt_drops_ask_and_approval_but_keeps_execute`、`test_tool_set_still_contains_execute` 断言 7 件含 `execute`） |
| REQ-4 dontAsk 仅跳过 ask 类工具 | 满足 | `dontAsk` 只命中 ask 分支（`tool_loading_mode.py:64-66`）；`command_approval_allowed("dontAsk") is True`（`tool_loading_mode.py:78-84`）；`test_dont_ask_drops_ask_only` |
| REQ-5 可中断工具判定口径 | 满足（口径正确，见 W-1） | ask 名集合 `ASK_TOOL_NAMES`（`tool_loading_mode.py:29`）单一登记；审批判定复用 `packages/interrupt_manager/approval.py:409-410 is_approval_configured`，与 `approval_wrapper` 同源；未新增工具名单配置项 |
| REQ-6 A2A 子 Agent 继承 mode | 满足 | `extract_child_execute_kwargs` 走 `model_copy` 全量继承；`tests/core/tools/a2a_tools/test_utils.py::TestChildExecuteKwargsModeInheritance`（含 BKAI payload 序列化路径）与 `test_local_backend.py::test_execute_inherits_tool_loading_mode` 断言子 `mode` 与父相同、缺父参数回落 `default` |
| REQ-7 与 tool_allow / tool_deny 叠加 | 满足（观测范围见 W-2） | 装配期 mode 裁剪为纯移除，运行期 allow/deny 仍由 `ToolFilterMiddleware`（`team_middleware.py:113-116`）应用，结构上构成交集；`test_mode_intersects_tool_allow/deny`（3 种 mode 组合） |
| REQ-8 内置插件模板透传 | 满足 | `template/builtin/{{cookiecutter.project_name}}/bk_plugin/versions/assistant.py:87-91` 在 `CommonInputsFormMixin.execute_kwargs.properties` 增加 `mode`（三项 `enum`、无默认值），`1.0.0assistant` 与 `2.0.0sse` 两个 `InputsForm` 共用该 mixin；表单未选择时不写入键，`ExecuteKwargs` 取默认 |
| REQ-9 非法取值被拒绝 | 满足（4xx 范围见 W-4） | 模型层 `Literal` 在 `model_validate` 阶段拒绝；插件链路 `serializers/chat_completion.py:89-92` 转 DRF `ValidationError({"execute_kwargs": ...})` → 400，`test_illegal_mode_is_rejected_as_400_on_execute_kwargs` |
| REQ-10 生效范围限定在工具装配 | 满足 | 过滤发生在 `_prepare_agent_tools` 末尾（`graph.py:950`），同一 `tools` 变量分别交给 model 节点（`graph.py:1346`）与 ToolNode（`graph.py:1357`）；未新增事件类型、未改会话结构、未改审批单字段（`git diff --stat` 无 `ag_ui/types.py`、`events`、存储层改动） |

### 边界、错误处理与安全核对

- **fail-closed 取向正确**：`command_approval_enabled=False` 只在 `disposition == "approval"` 的派发点之后生效（`command_security.py:1172-1180`），`allow` 仍放行、`block` 仍拒绝；预分流结论为 `approval` 时同样拒绝（`test_smart_approval_also_rejected`），不调用 `require_command_approval`、不创建 ITSM 单、命令不下发 backend（`test_switch_off_rejects_without_interrupt_and_without_execution` 同时覆盖同步/异步）。
- **未知取值不放大权限**：`tool_loading_mode.py` 对未知 mode 按 `default` 处理、`command_approval_allowed` 返回 `True`，与 `Literal` 层拒绝一致，不会因未知值静默关闭审批。
- **default 兼容性**：过滤 `default` 分支不做拷贝，`handle_validation_error` / `handle_tool_error` 的处理顺序（过滤前）与改动前一致；`provider.py`、`command_security.py` 的开关参数均为缺省 `True` 的可选关键字，既有调用方零改动。
- **数据与状态**：`mode` 为请求级字段，未落库、未写会话配置；`ExecuteKwargs` 新增字段随 `model_copy` / `model_dump` 透传，A2A BKAI 远端 payload 自动携带。
- **依赖方向**：新增模块 `core/graphs/react/tool_loading_mode.py` 只依赖 `packages/interrupt_manager`，未反向依赖 `core` 内部实现，符合既有分层。

### 验证记录复核

实现节点记录 8 组定向命令全绿。本轮对其中 7 组可自动化的命令在 `7f9687b8` 上原地重跑，结果与记录逐项一致（T-9 为手工评审项，脚本不可自动化）：

| 命令 | 记录 | 本轮重跑 |
| --- | --- | --- |
| `cd src/agent && make test path=tests/services/test_pydantic_models.py` | 33 passed | 33 passed |
| `cd src/agent && make test path=tests/core/ag_ui` | 225 passed | 225 passed |
| `cd src/agent && make test path=tests/core/graphs/react/test_tool_loading_mode.py` | 18 passed | 18 passed |
| `cd src/agent && make test path=tests/packages/security/command/test_command_security.py` | 538 passed | 538 passed |
| `cd src/agent && make test path=tests/core/tools/runtime_tools/test_command_approval.py` | 35 passed | 35 passed |
| `cd src/agent && make test path=tests/core/tools/a2a_tools` | 257 passed, 4 skipped, 3 xfailed | 257 passed, 4 skipped, 3 xfailed |
| `cd src/plugins/aidev_bkplugin && PYTHONPATH=. make test path=tests/views/test_chat_completion_serializer.py` | 28 passed | 28 passed |
| 回归 `cd src/agent && make test path=tests/core/graphs/test_react.py` | 128 passed, 1 deselected | 128 passed, 1 deselected |

环境说明由实现节点登记（插件侧 `uv sync` 因 `bkoauth==0.1.0` 哈希不一致失败，改用 `uv export --frozen` + pip 填充；插件测试需 `PYTHONPATH=.`），本轮重跑遵循同一方式，未出现与记录不符的结果。

### 其他核对

- 提交粒度与 `tasks.md` 的 T-1～T-10 一一对应（9 个提交，`33c92335` → `7f9687b8`），末位提交只改 `tasks.md` 验证记录。
- 常量上收未破坏兼容：`core/ag_ui/aidev_agent.py` 仍可通过模块属性导出 `ASK_USER_QUESTION_TOOL_NAME`，`services/agent/chat.py` 改为从 `packages.interrupt_manager` 直接导入，无循环依赖。
- 人工预览：本仓为 SDK 库与插件模板，无可预览环境，沿用 `tasks.md` 的占位说明，不作为阻塞项。

### 结论

固定验收规则三项均满足：已审批需求 REQ-1～REQ-10 全部落地，无可判定为正确性/安全/数据阻塞的问题（命令级审批关闭为 fail-closed，未放宽既有安全档位），约定验证命令在评审版本上复现记录结果。4 条改进建议均属可维护性、证据覆盖面与适用范围说明，不构成返工。本轮判定 `PASS_WITH_WARNING`。
