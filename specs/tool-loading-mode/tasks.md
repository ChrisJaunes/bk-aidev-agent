# 工具加载模式（mode）任务清单

- 变更名: tool-loading-mode
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/tool-loading-mode/spec.md
- 方案: specs/tool-loading-mode/plan.md

## T-1 新增 `ExecuteKwargs.mode` 与取值校验

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/pydantic_models.py`、`src/agent/tests/services/test_pydantic_models.py`
- 需求: REQ-1、REQ-9
- 依赖: 无
- 验证: `cd src/agent && make test path=tests/services/test_pydantic_models.py`
- [x] 完成标准：`ExecuteKwargs()` 的 `mode=="default"`；三个合法值均可构造并出现在 `model_dump()`；
      `model_copy()` 保留 `mode`；`ExecuteKwargs(mode="disabled")` 抛 `ValidationError`。

## T-2 ask 工具名收为单一来源

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/packages/interrupt_manager/ask_user_question.py`、
  `src/agent/aidev_agent/packages/interrupt_manager/__init__.py`、
  `src/agent/aidev_agent/core/ag_ui/aidev_agent.py`
- 需求: REQ-5
- 依赖: 无
- 验证: `cd src/agent && make test path=tests/core/ag_ui`
- [x] 完成标准：`ASK_USER_QUESTION_TOOL_NAME` 只在 `packages/interrupt_manager/ask_user_question.py` 定义，
      由包 `__init__` 导出；`core/ag_ui/aidev_agent.py` 与 `services/agent/chat.py` 经同一来源取得常量，
      值仍为 `"ask_user_question"`；既有 ag_ui 测试全绿。

## T-3 装配期工具过滤纯函数

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/graphs/react/tool_loading_mode.py`（新增）、
  `src/agent/tests/core/graphs/react/test_tool_loading_mode.py`（新增）
- 需求: REQ-2、REQ-3、REQ-4、REQ-5、REQ-7
- 依赖: T-1、T-2
- 验证: `cd src/agent && make test path=tests/core/graphs/react/test_tool_loading_mode.py`
- [x] 完成标准：`filter_tools_by_loading_mode(tools, mode)` 在 `default` 下原样返回（工具名集合与入参逐一相等）；
      `dontAsk` 仅移除 `ask_user_question`，保留带审批工具与 `execute`；`dontInterrupt` 移除
      `ask_user_question` 与全部 `is_approval_configured` 为真的工具，保留 `execute` 与普通工具；
      `command_approval_allowed(mode)` 仅对 `dontInterrupt` 返回 `False`；参数化用例覆盖 3 种 mode × 4 类工具。

## T-4 `ReActAgentBuilder` 接入 mode

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/graphs/react/graph.py`
- 需求: REQ-2、REQ-3、REQ-4、REQ-10
- 依赖: T-3
- 验证: `cd src/agent && make test path=tests/core/graphs/react/test_tool_loading_mode.py`
- [x] 完成标准：`set_bkai_options` 保存 `options.execute_kwargs`（缺省时用 `ExecuteKwargs()`）；
      `_prepare_agent_tools` 在 `ignore_errors` 处理之后、返回之前按 `mode` 过滤，过滤后的列表同时传给
      model 节点与 ToolNode；`mode=default`（含未传）时工具清单与改动前一致；`_prepare_agent_tools`
      将 `command_approval_allowed(mode)` 传给 `get_client_tools_with_runtime`。

## T-5 命令级审批支持关闭开关（fail-closed）

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/packages/security/command/command_security.py`
- 需求: REQ-3、REQ-4
- 依赖: 无
- 验证: `cd src/agent && make test path=tests/packages/security/command/test_command_security.py`
- [x] 完成标准：`enforce_command_security` 新增仅关键字参数 `command_approval_enabled: bool = True`；
      该参数为 `False` 且处置档为 `approval` 时，灰名单命令抛 `ValueError` 且**不调用**
      `require_command_approval`、不创建审批单；`allow` 档仍放行、`block` 档仍拒绝；参数缺省时既有用例
      零改动通过（AC-5、AC-8、AC-9 的审批链路不变由既有用例保障）。

## T-6 execute 工具透传命令审批开关

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/runtime_tools/provider.py`、
  `src/agent/tests/core/tools/runtime_tools/test_command_approval.py`
- 需求: REQ-3
- 依赖: T-5
- 验证: `cd src/agent && make test path=tests/core/tools/runtime_tools/test_command_approval.py`
- [x] 完成标准：`get_client_tools_with_runtime` 与 `get_execute_tool` 新增可选参数
      `command_approval_enabled: bool = True`，同步与异步 `execute` 都以该值调用
      `enforce_command_security`；开关为 `False` 时灰名单命令返回拒绝结果且未触发 `interrupt`；
      工具仍以 `name="execute"` 正常构造并出现在返回的 7 件工具中。

## T-7 A2A 子 Agent 继承 mode 回归测试

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/tools/a2a_tools/test_utils.py`、`src/agent/tests/core/tools/a2a_tools/test_local_backend.py`
- 需求: REQ-6
- 依赖: T-1
- 验证: `cd src/agent && make test path=tests/core/tools/a2a_tools`
- [x] 完成标准：参数化覆盖 `dontInterrupt` / `dontAsk`，`extract_child_execute_kwargs` 产出的子
      `ExecuteKwargs.mode` 与父相同且不回落到 `default`；父为 `default` 时子亦为 `default`；
      BKAI 后端构造的 payload 中 `execute_kwargs` 含 `mode`。

## T-8 装配级工具集合回归测试

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/graphs/react/test_tool_loading_mode.py`
- 需求: REQ-2、REQ-3、REQ-4、REQ-7、REQ-10
- 依赖: T-4
- 验证: `cd src/agent && make test path=tests/core/graphs/react/test_tool_loading_mode.py`
- [x] 完成标准：对同一套注入工具（含 `ask_user_question`、带 `metadata.approval` 的工具、`execute`、
      普通工具），三种 `mode` 下 build 出的工具名集合分别等于预期；`default` 与不传 `mode` 结果相同
      （AC-1/AC-2）；`dontInterrupt` 缺 ask 与带审批工具但仍有 `execute`（AC-3/4/6）；`dontAsk` 仅缺 ask
      且带审批工具仍在集合中（AC-7、AC-8 前半）；同时设置 `tool_allow` 时结果等于两者交集（AC-12）。

## T-9 内置插件模板增加 mode 选项

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `template/builtin/{{cookiecutter.project_name}}/bk_plugin/versions/assistant.py`
- 需求: REQ-8
- 依赖: T-1
- 验证: 手工评审依据 —— 检查 `CommonInputsFormMixin.execute_kwargs.properties` 含 `mode` 且
      `enum` 为三项；用 `python -c` 构造 `ExecuteKwargs.model_validate({"mode": "dontInterrupt"})` 与
      不含 `mode` 的 dict，确认分别得到 `dontInterrupt` 与 `default`（模板不在 pytest 工程内）。
- [x] 完成标准：两个版本（`1.0.0assistant` / `2.0.0sse`）经共用 mixin 均暴露 `mode` 选项；
      未选择时表单结果不含 `mode` 键，等价于 `default`；模板不写死任何模式。

## T-10 插件链路非法 mode 返回 4xx

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/plugins/aidev_bkplugin/aidev_bkplugin/serializers/chat_completion.py`、
  `src/plugins/aidev_bkplugin/tests/views/test_chat_completion_serializer.py`
- 需求: REQ-9
- 依赖: T-1
- 验证: `cd src/plugins/aidev_bkplugin && make test path=tests/views/test_chat_completion_serializer.py`
- [x] 完成标准：`validate_execute_kwargs` 捕获 pydantic `ValidationError` 并转成
      `serializers.ValidationError({"execute_kwargs": ...})`；`mode="disabled"` 时
      `serializer.is_valid()` 返回 `False` 且错误定位到 `execute_kwargs`；合法值与缺省仍通过。

## 交付约定

- 工作分支沿用 `feature/tool-loading-mode`，每个任务独立提交，提交信息以 `--story=1070093903139306621`
  结尾并按仓库 `AGENTS.md` 追加 `committed by AI assistant`。
- 本阶段只写实现与测试，不修改 `spec.md`，不创建 MR。

## 验证记录

执行时间：2026-10-23。工作分支 `feature/tool-loading-mode`，基线 `460fd609`。全部命令按任务声明定向执行，未扩大范围。

| 任务 | 验证命令 | 结果 |
| --- | --- | --- |
| T-1 | `cd src/agent && make test path=tests/services/test_pydantic_models.py` | 通过：33 passed |
| T-2 | `cd src/agent && make test path=tests/core/ag_ui` | 通过：225 passed |
| T-3 / T-4 / T-8 | `cd src/agent && make test path=tests/core/graphs/react/test_tool_loading_mode.py` | 通过：18 passed |
| T-5 | `cd src/agent && make test path=tests/packages/security/command/test_command_security.py` | 通过：538 passed |
| T-6 | `cd src/agent && make test path=tests/core/tools/runtime_tools/test_command_approval.py` | 通过：35 passed |
| T-7 | `cd src/agent && make test path=tests/core/tools/a2a_tools` | 通过：257 passed, 4 skipped, 3 xfailed |
| T-9 | 手工评审 + `python -c` 构造 `ExecuteKwargs` | 通过（见下） |
| T-10 | `cd src/plugins/aidev_bkplugin && PYTHONPATH=. make test path=tests/views/test_chat_completion_serializer.py` | 通过：28 passed |

回归补充（T-4 改动直接波及，非新范围）：`cd src/agent && make test path=tests/core/graphs/test_react.py` → 通过：128 passed, 1 deselected。

T-9 手工评审结论：

- `CommonInputsFormMixin.execute_kwargs.properties` 含 `mode`，`enum` 恰为 `["default", "dontInterrupt", "dontAsk"]`，仅一处声明，`1.0.0assistant` 与 `2.0.0sse` 两个 `InputsForm` 共用同一 mixin。
- 表单未写死任何模式（无 `"mode": "default"` 默认值），未选择时结果不含 `mode` 键。
- `ExecuteKwargs.model_validate({"mode": "dontInterrupt"}).mode == "dontInterrupt"`；`model_validate({}).mode == "default"`，两条路径均符合 REQ-8。

环境说明（非代码问题）：

- `src/plugins/aidev_bkplugin` 的 `uv sync` 因镜像 `bkoauth==0.1.0` 哈希与 `uv.lock` 不一致而失败（Expected `243e2399…` / Computed `8aded9a8…`），改用 `uv export --frozen --no-hashes` + `pip install -r` 填充同一 `.venv` 后执行；测试命令额外需要 `PYTHONPATH=.`，否则 pytest-django 报 `No module named 'tests'`。
- `ruff check` 对本次改动文件无新增问题；`ask_user_question.py` 的 SIM114 / SIM102 为改动前既有告警。

人工预览: 无可预览（本仓为 Agent SDK 库与插件模板，无已部署的可预览环境展示本次改动）。
