# 任务清单 · mcp-duplicate-tool-name

- 变更名: mcp-duplicate-tool-name
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: docs/superpowers/specs/mcp-duplicate-tool-name/spec.md
- 方案: docs/superpowers/plans/mcp-duplicate-tool-name/plan.md

## T-1 工具命名纯函数模块

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/utils/tool_naming.py`、`src/agent/tests/utils/test_tool_naming.py`
- 需求: REQ-1、REQ-2、REQ-3、REQ-9、REQ-10
- 依赖: 无
- 验证: `python -m pytest -c ./pyproject.toml tests/utils/test_tool_naming.py -q`
- [x] 完成标准：`is_same_service("msdk-console-prod-mcp","msdk-console-prod-mcp2")` 为真；`("foo-mcp","foo-mcp-admin")`、`("a-prod-x-prod-y","a-prod-x-prod-z")`、`("a","a")` 为假；`compose_tool_name` 对超长输入截断前缀并追加 `sha1[:8]`，同输入两次调用结果相同，输出恒满足 `^[A-Za-z0-9_-]{1,64}$`；`dedupe_mcp_tools` 对「同服务重名」只留首个且不改名、对「跨服务重名」保留首个原名其余加前缀、顺序与输入一致；传入的 `approval_matcher` 对组内任一成员返回真时该组不合并、全部保留并改为改名。

## T-2 `construct_mcp()` 接线去重、审批豁免与日志

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/packages/resource_manager/base.py`、`src/agent/aidev_agent/services/agent/chat.py`、`src/agent/tests/packages/langchain_core/tools/test_base.py`
- 需求: REQ-1、REQ-2、REQ-3、REQ-6、REQ-7、REQ-8、REQ-10
- 依赖: T-1
- 验证: `python -m pytest -c ./pyproject.toml tests/packages/langchain_core/tools/test_base.py -k "mcp or construct" -q`
- [x] 完成标准：`construct_mcp()` 汇总后工具名唯一；同服务同名只保留配置顺序中先出现的 Server 实例且名字不变，被丢弃 Server 记入 `metadata["mcp_merged_servers"]`；跨服务同名首个保留原名、其余为 `<MCP_NAME>__<TOOL_NAME>`；组内任一成员命中审批配置时该组不合并（改为改名保留），并由 `build_tools()` 复用 `_normalize_tool_approval_bindings()` 的结果作为 `approval_matcher` 传入；每个被改名/合并的工具写 `metadata["original_name"]`、`metadata["display_name"]`；仅发生改名、合并或审批豁免时输出 INFO，无重名配置不新增任何日志；`selected_tools` 过滤仍先于去重生效，过滤后不重名的工具不被改名；`make_mcp_tools()` 不传 `approval_matcher` 时行为与主链路同一实现。

## T-3 展示名下沉到事件层

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/ag_ui/types.py`、`src/agent/aidev_agent/core/ag_ui/event_builders.py`、`src/agent/aidev_agent/core/ag_ui/aidev_agent.py`、`src/agent/aidev_agent/core/ag_ui/utils.py`、`src/agent/tests/core/ag_ui/test_event_builders.py`
- 需求: REQ-5、REQ-8
- 依赖: T-2
- 验证: `python -m pytest -c ./pyproject.toml tests/core/ag_ui -q`
- [x] 完成标准：改名工具的 `ExtendFunctionCall.name`、`TOOL_CALL_START.tool_call_name`、`TOOL_CALL_RESULT.tool_call_name` 均为原始名，任何事件与 DB 账本中都不出现 `<MCP_NAME>__` 前缀；`ExtendFunctionCall.model_name` 携带模型名（仅当与原始名不同）；`mcp_name`/`description` 仍取改名后工具对象的 metadata；未改名工具的事件与 payload 与改动前逐字段一致。

## T-4 DB 账本与模型历史一致性

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/nodes/model/chat_history_assembly.py`、`src/agent/tests/core/nodes/model/test_chat_history_assembly.py`
- 需求: REQ-4
- 依赖: T-3
- 验证: `python -m pytest -c ./pyproject.toml tests/core/nodes/model/test_chat_history_assembly.py -q`
- [x] 完成标准：`_extract_tool_calls()` 在 `function.model_name` 存在时取模型名（`<MCP_NAME>__<TOOL_NAME>`），不存在时回退 `function.name`；构造出的 `AIMessage.tool_calls[].name` 与本轮 `bind_tools` 的工具名集合一致，历史中同名不同服务的两个调用可区分。

## T-5 审批按原始名匹配并展示原始名

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/services/agent/chat.py`、`src/agent/tests/services/test_chat_tool_approval_matching.py`（新建）
- 需求: REQ-10、REQ-5
- 依赖: T-2
- 验证: `python -m pytest -c ./pyproject.toml tests/services/test_chat_tool_approval_matching.py -q`
- [x] 完成标准：因跨服务重名或审批豁免被改名的工具仍能命中 `approval_settings` 中按原始工具名 + `mcp_name` 配置的审批，调用时照常触发审批、不绕过；审批 metadata 的 `target.display_name` 与审批单标题为原始工具名；`target.name`/`target.code` 仍为模型名；`_apply_tool_approval_settings()` 复用 `build_tools()` 已算出的 `approval_items`，不再重复归一化；未改名工具的审批 metadata 逐字不变。

## T-6 回归与端到端校验

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/packages/langchain_core/tools/test_base.py`、`src/agent/tests/core/ag_ui/test_event_builders.py`
- 需求: REQ-1、REQ-4、REQ-8
- 依赖: T-3、T-4、T-5
- 验证: `python -m pytest -c ./pyproject.toml tests -q`（全量）；手工按 `ai-query-sops` 的同服务双端点配置跑一轮对话
- [ ] 完成标准：单 Server 与无重名配置的工具名列表、顺序、metadata 与改动前逐项相同；`<MCP_NAME>__<TOOL_NAME>` 被调用时实际请求体中下发的是原始 `<TOOL_NAME>`（用 mock `ClientSession.call_tool` 断言入参）；同服务重名且该组命中审批配置时工具不被合并（两个实例都在、文件名唯一），调用仍触发审批；手工回归确认请求不再返回 `duplicate tool name` 400，前端卡片显示 `<来源 MCP> / <原始工具名>`。

## 验证记录

执行环境：本节点工作区 worktree（基线 `origin/fix/mcp-duplicate-tool-name` = `dbcf0d02`），Python 3.11。
本机 `uv run` 会重建空 venv，故统一用等价直跑方式：
`cd src/agent && PYTHONPATH=$PWD <repo>/.venv/bin/python -m pytest -c ./pyproject.toml <target> -q -p no:randomly`。

- T-1 通过：`tests/utils/test_tool_naming.py`，28 passed。覆盖 `-prod-` 判定（同服务 / 无 `-prod-` / 双 `-prod-` / 同名）、卫生化、超长截断与 `sha1[:8]` 确定性、同服务合并、跨服务改名、输入顺序保持、审批豁免整组改名为改名。
- T-2 通过：`tests/packages/langchain_core/tools/test_base.py -k "mcp or construct"`，29 passed（含新增 6 例：同服务合并、跨服务改名、无重名零副作用、`selected_tools` 先过滤、`approval_matcher` 不合并且、`make_mcp_tools` 同实现）。
- T-3 通过：`tests/core/ag_ui`，231 passed（含新增 `TestRenamedToolDisplayName` 5 例：账本 `function.name` 为原始名 + `function.model_name` 为模型名、未改名不落 `model_name` 键、`TOOL_CALL_RESULT`/重放路径展示名）。
- T-4 通过：`tests/core/nodes/model/test_chat_history_assembly.py`，22 passed（含 `TestExtractToolCallsModelName` 3 例：优先 `model_name`、旧记录回退 `name`、同名不同服务可区分）。
- T-5 通过：`tests/services/test_chat_tool_approval_matching.py`（新建），7 passed。覆盖按 `original_name` 命中 `mcp_tool` 绑定、`target.display_name` 为原始名而 `target.name`/`code` 为模型名、`_apply_tool_approval_settings` 复用外部 `approval_items`（重复归一化会断言失败）、`build_tools` 传入的 matcher 产物与无审批时传 `None`、同服务重名审批组不合并后仍带审批。
- T-6 部分通过 / 手工回归未运行：全量 `pytest tests -q` 为 6505 passed、124 skipped、17 xfailed、1 failed；唯一失败 `tests/packages/opentelemetry/test_otel_service.py::test_otel_config_uses_central_attribute_limits_by_default` 与本变更无关——`aidev_agent/config.py:200` 的默认值 `500 * 1024` 与用例断言的 `80 * 1024` 不一致，两个文件均不在本次改动内（改动前即如此）。新增执行路由用例 `test_renamed_mcp_tool_routes_to_original_tool_name` 通过：改名工具 `ainvoke` 时 `ClientSession.call_tool` 收到原始 `get_task_list`，且 OpenAI 工具载荷名单为 `["get_task_list", "bar-mcp__get_task_list"]`（无重名）。「手工按 ai-query-sops 同服务双端点配置跑一轮对话」未运行：本节点无可用部署环境，无法验证线上 400 消失与前端卡片渲染，需在可预览环境补测，故 T-6 保持未勾选。
- 人工预览: 无可预览（本节点未部署环境，无展示本次改动的真实预览地址）。
