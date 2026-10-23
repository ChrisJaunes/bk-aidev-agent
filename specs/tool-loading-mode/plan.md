# 工具加载模式（mode）技术方案

- 变更名: tool-loading-mode
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/tool-loading-mode/spec.md

## 背景与目标

同一套工具集会经 Web、企微、APIGW、内置插件等渠道对外服务。ask 类工具与带审批的工具会在执行中
`interrupt()` 暂停图，等待人工回答或 ITSM 审批；无人值守渠道没有响应方，流程会挂起或失败。

本方案在请求参数 `ExecuteKwargs` 中新增 `mode`，在**工具装配阶段**按渠道裁剪会中断的工具，使
`dontInterrupt` / `dontAsk` 渠道不再出现等待人类输入的挂起点，同时保证 `default`（含未传）行为
与变更前逐字节一致。

## 非目标

- 不新增第四种模式，不做 UI 上的 mode 展示或控制。
- 不改变 `default` 下的工具集合、审批链路、事件协议、会话数据结构。
- 不为外部 MCP 工具、第三方 `StructuredTool` 推断是否会中断。
- 不新增流式事件与追踪属性，不做命令黑/白名单策略变更。
- 不改变 FLOW 类型 Agent（其路径不装配工具）。

## 现状

| 关注点 | 位置 | 现状 |
| --- | --- | --- |
| 请求参数模型 | `src/agent/aidev_agent/pydantic_models.py` `ExecuteKwargs` | 已有 `tool_allow` / `tool_deny`、`spawn_depth`、`sandbox_pv_id`，无 `mode` |
| 工具装配唯一入口 | `core/graphs/react/graph.py` `ReActAgentBuilder._prepare_agent_tools` | 集中收集 extra_tools / 中间件工具 / `ask_user_question` / skill / runtime / task / a2a / agentic-rag，收集后**无过滤**，直接交给 model 节点与 ToolNode |
| 既有声明式过滤 | `core/graphs/react/team_middleware.py` `ToolFilterMiddleware` | 处理 `tool_deny` / `tool_allow` 与 spawn 深度剥离；**仅在 A2A 场景注册**（`graph.py` `if self._a2a_specs and self._a2a_resolver`） |
| ask 类工具 | `core/tools/ask_user_question.py`；常量 `core/ag_ui/aidev_agent.py` `ASK_USER_QUESTION_TOOL_NAME` | 工具本体直调 `interrupt()`；无「ask 类工具」统一判定函数，现有判定散落为按名比较 |
| 工具级审批判定 | `packages/interrupt_manager/approval.py` `is_approval_configured` | 读 `tool.metadata["approval"]`，已被 `approval_wrapper`、`event_builders` 复用，可直接作为判定口径 |
| 命令级审批 | `packages/security/command/command_security.py` `enforce_command_security` | 由 `core/tools/runtime_tools/provider.py` 的 `execute` / `async_execute` 在工具内部调用；`review` 档按 `command_review_disposition` 派发 `allow` / `approval` / `block` |
| A2A 继承 | `core/tools/a2a_tools/utils.py` `extract_child_execute_kwargs` | `ek.model_copy()` 全量继承父字段；BKAI 远端走 `execute_kwargs.model_dump()`，新增字段自动透传 |
| 内置插件模板 | `template/builtin/{{cookiecutter.project_name}}/bk_plugin/versions/assistant.py` | `InputsForm.execute_kwargs` 只声明 5 个 caller 字段；`build_bkplugin_runner_from_plugin` 把 `inputs.execute_kwargs` 直接交给 `build_execute_kwargs` → `ExecuteKwargs.model_validate` |

两个必须先补的前置事实：

1. `ReActAgentBuilder` 目前**不持有** `execute_kwargs`：`set_bkai_options` 未读取 `options.execute_kwargs`，
   而 `ChatCompletionAgent._get_agent` 已经把 `execute_kwargs=execute_kwargs` 传入 `AgentExecutorKwargs`。
2. 插件链路 `aidev_bkplugin/serializers/chat_completion.py` 的 `validate_execute_kwargs` 任由 pydantic
   `ValidationError` 冒出，DRF 不会转换 → 非法字段会成为 500，与 REQ-9 的 4xx 不符。

## 方案对比

### 过滤位置

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| **A（选中）装配期过滤** | `_prepare_agent_tools` 末尾按 `mode` 裁剪一次，model 节点与 ToolNode 共用同一列表 | 与请求一一对应、`stream` / 非流式 / resume 三条路径天然一致；不依赖 A2A 才注册的中间件；单点可测 |
| B 运行期中间件 | 扩展 `ToolFilterMiddleware` 并改为无条件注册 | 需把「仅 A2A 注册」改成全量注册，改变所有 Agent 的运行时链路；过滤时机推后到每轮模型节点，收益仅是复用一处声明式写法 |
| C 两层都做 | A + B | 同一语义两处实现，重复且难保证一致 |

排除 B/C：本需求的过滤输入（`mode`）在 build 期即完全确定，放到运行期只会扩大影响面。

### 命令级审批关闭

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| **A（选中）参数透传** | `_prepare_agent_tools` 把「是否允许审批」传给 `get_client_tools_with_runtime` → `get_execute_tool` → `enforce_command_security(command_approval_enabled=...)` | 与装配期过滤同一决策点；不篡改安全配置；执行路径不反向读请求参数 |
| B 覆写处置档 | 请求级把 `SecurityCommandSettings.command_review_disposition` 改成 `block` | 需复制安全配置，语义上把「不审批」伪装成「配置拒绝」，其它消费该配置的地方会被误伤 |
| C 运行期读 config | `execute` 内从 `RunnableConfig` 取 `execute_kwargs.mode` | 执行路径耦合请求模型，工具构造与行为不一致，单测需伪造 config |

### `command_review_disposition=allow` 的处置（已确认）

`allow` 是部署侧的显式选择，本身不产生中断；`dontInterrupt` 只关闭「审批」这一中断源，故 `allow`
档保持放行。仅当处置档为 `approval` 时，`dontInterrupt` 将其改为拒绝（fail-closed）。

## 决策

### D-1 `mode` 字段

在 `ExecuteKwargs` 中新增：

```python
mode: Literal["default", "dontInterrupt", "dontAsk"] = Field(
    default="default",
    description="工具加载模式：default 全量；dontInterrupt 剔除 ask 类与带审批工具并关闭命令级审批；dontAsk 仅剔除 ask 类工具",
)
```

- 用 `Literal` 而非自定义校验器：与既有 `stream_mode: Literal["start", "attach"]` 同风格，非法值由
  pydantic 在参数解析阶段拒绝，`model_json_schema()` 可直接带出枚举，模板/文档无需另立常量。
- 字段随 `execute_kwargs` 走既有链路（ChatCompletion 入参、APIGW、内置插件），不新增接口、不落库。

### D-2 装配期过滤

1. `set_bkai_options` 增补 `self._execute_kwargs = options.execute_kwargs`（`None` 时按 `ExecuteKwargs()` 取值）。
2. `_prepare_agent_tools` 在 `ignore_errors` 处理之后、`return tools` 之前调用新增的纯函数：

```python
# core/graphs/react/tool_loading_mode.py
def filter_tools_by_loading_mode(tools: list[BaseTool], mode: str) -> list[BaseTool]: ...
def command_approval_allowed(mode: str) -> bool: ...
```

3. 过滤结果同时用于 `_prepare_agent_model_node(tools=...)` 与 `_prepare_agent_tool_node(tools, ...)`——
   两者本就共用 `build()` 里的同一个 `tools` 变量，无需额外改动。

`default` 分支直接返回入参列表（不做拷贝），保证 `default` 与变更前的工具体完全一致。

### D-3 可中断工具的判定口径

- ask 类：工具 `name` 属于内置 ask 工具名集合，当前集合仅 `{"ask_user_question"}`。
  该字面量上收为 `packages/interrupt_manager/ask_user_question.py` 的 `ASK_USER_QUESTION_TOOL_NAME`，
  由该包 `__init__` 导出，`core/ag_ui/aidev_agent.py` 改为引用同一常量（消除现有重复定义）。
- 带审批：复用 `packages/interrupt_manager/approval.py` 的 `is_approval_configured(tool)`。
- 不新增任何人工维护的工具名单配置项；外部 MCP 工具不参与推断。

### D-4 命令级审批 fail-closed

`enforce_command_security(..., *, command_approval_enabled: bool = True)`：

- `disposition == "approval"` 且 `command_approval_enabled` 为 `False` → 直接抛
  `ValueError(f"命令执行被拒绝：{_format_report_details(report)}")`，与 `block` 档同一措辞，
  不调用 `require_command_approval`，不创建 ITSM 单；异常经 `handle_tool_error` 变成工具结果回给模型。
- `allow` / `block` 两档不受该参数影响；参数缺省 `True` 时行为与现在完全一致。
- `execute` 工具本身照常注入，`dontInterrupt` 仍保留 `ls` / `read_file` / `write_file` / `edit_file` /
  `glob` / `grep` 等无中断能力的工具。

### D-5 A2A 继承

`extract_child_execute_kwargs` 已用 `ek.model_copy()` 全量继承，`dontInterrupt` / `dontAsk` 会随
`ExecuteKwargs` 复制到子 Agent；BKAI 远端 `execute_kwargs.model_dump()` 也会带上 `mode`。**本项零代码
改动，只补回归测试**，避免后续有人把复制改成白名单字段映射时静默失效。

### D-6 内置插件模板

在 `CommonInputsFormMixin.execute_kwargs.properties` 增加 `mode`（`type: string` + 三项 `enum` +
`title`）。表单未选择时该键不出现，`build_execute_kwargs` → `ExecuteKwargs.model_validate` 取默认
`default`，等价于未传。两个版本（`1.0.0assistant` / `2.0.0sse`）共用同一 mixin，一处改动覆盖两者。

### D-7 非法值返回 4xx

- 模型层：`Literal` 使 `ExecuteKwargs.model_validate` 抛 `ValidationError`，`run_agent` 前即失败，
  不装配工具、不产生会话消息。
- 插件链路：`ChatCompletionRequestSerializer.validate_execute_kwargs` 捕获 pydantic `ValidationError`
  并转成 DRF `serializers.ValidationError({"execute_kwargs": ...})` → 400。该转换对该字段的**所有**
  非法输入生效（如 `stream="abc"`），是错误码从 500 收敛到 400 的附带变化，见风险。

### D-8 与 `tool_allow` / `tool_deny` 叠加

装配期按 `mode` 裁剪后，运行期仍由 `ToolFilterMiddleware` 应用 allow/deny，两步都是「移除」，天然
构成交集（REQ-7 / AC-12），不需要额外的合并逻辑。

## 接口与数据

- `ExecuteKwargs.mode: Literal["default", "dontInterrupt", "dontAsk"] = "default"`，请求级、不落库。
- `enforce_command_security(command, target_runtime, security_settings, risk_assessor=None, *,
  command_approval_enabled: bool = True)`；既有位置参数不变。
- `get_client_tools_with_runtime(..., command_approval_enabled: bool = True)` /
  `get_execute_tool(..., command_approval_enabled: bool = True)`：仅新增可选关键字参数。
- 无新增 HTTP 路由、无新增事件类型、无会话结构变更、无数据库迁移。

## 跨仓契约

- 单仓变更，变更名 `tool-loading-mode`，无跨仓依赖与执行顺序。
- `mode` 是 `execute_kwargs` 的普通可选字段，与 `stream` / `tool_allow` / `tool_deny` 同层；Web、企微、
  前端不传即行为不变，不构成破坏性接口变更。
- A2A 子 Agent 继承由 `ExecuteKwargs` 复制/序列化保证，本地后端与 BKAI 后端两条路径一致。

## 风险

1. **续流与 mode 不一致**：`mode` 不落库，中断后续流必须重发同一 `mode`。若首跑为 `default` 触发了
   ask/审批中断、续流时改成 `dontInterrupt`，被剔除的工具无法承接挂起的 tool_call。该行为符合「同一
   mode 下三条路径一致」的规格，但调用方需自行保证续流沿用首跑的 `mode`；本轮不做会话级持久化。
2. **错误码变化**：D-7 使插件链路非法 `execute_kwargs` 字段由 500 变为 400，若调用方依赖 500 需知会。
3. **外部 MCP 未覆盖**：MCP 工具内部是否存在人工等待无法在装配期判定，沿用 spec 假设。
4. **模板无自动化测试**：cookiecutter 模板不在两套 pytest 工程内，只能手工评审表单与透传行为。
5. **审批判定依赖 metadata**：`is_approval_configured` 只看工具 `metadata.approval`；若未来出现不走该
   metadata 的审批工具，需要同步扩充判定而不是改动 `mode` 逻辑。
6. **`allow` 档保留放行**：已与用户确认；`dontInterrupt` + `disposition=allow` 的组合下灰名单命令仍会
   执行，安全语义依赖部署侧显式配置。

## 测试策略

| 层次 | 覆盖点 | 位置 |
| --- | --- | --- |
| 参数模型 | 默认值 / 三个合法值 / 非法值拒绝 / `model_dump` 与 `model_copy` 保留 | `tests/services/test_pydantic_models.py` |
| 过滤纯函数 | 三种 mode × 四类工具（ask、带审批、execute、普通工具）的保留/剔除矩阵 | 新增 `tests/core/graphs/react/test_tool_loading_mode.py` |
| 装配级 | `ReActAgentBuilder` 在三种 mode 下 build 出的工具名集合；`default` 与基线一致 | 同上 |
| 命令安全 | `command_approval_enabled=False` + `approval` 档 → 拒绝且不触发 `interrupt`；`allow` 档仍放行；缺省不变 | `tests/packages/security/command/test_command_security.py` |
| execute 工具 | 开关透传到 `enforce_command_security` | `tests/core/tools/runtime_tools/test_command_approval.py` |
| A2A 继承 | 子 `ExecuteKwargs.mode` 等于父；BKAI payload 含 `mode` | `tests/core/tools/a2a_tools/test_utils.py`、`test_local_backend.py` |
| 插件链路 | 非法 `mode` → DRF `ValidationError`；合法/缺省通过；选中 `dontInterrupt` 时透传 | `src/plugins/aidev_bkplugin/tests/views/test_chat_completion_serializer.py` |
| 手工 | 模板 `InputsForm` 显示三项、未选不写入 `execute_kwargs` | 评审 `assistant.py` + 本地 `ExecuteKwargs` 构造验证 |

统一用 `make test path=<测试路径>` 定向执行（`src/agent` 与 `src/plugins/aidev_bkplugin` 各自 Makefile）。
