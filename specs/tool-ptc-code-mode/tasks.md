# 工具 PTC（code mode）任务清单

- 变更名: tool-ptc-code-mode
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 方案: specs/tool-ptc-code-mode/plan.md
- 规格: specs/tool-ptc-code-mode/spec.md

## T-1 开关字段与限额配置

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/pydantic_models.py`、`src/agent/aidev_agent/config.py`
- 需求: REQ-1, REQ-2, REQ-13
- 依赖: 无
- 验证: `make test path=src/agent/tests/core/graphs/react/test_code_mode_switch.py`
- [ ] 完成标准：`ExecuteKwargs.tool_use_mode` 缺省为 `function_call`，非法取值的 `model_validate` 抛 `ValidationError`；五项 `CODE_MODE_*` 配置以默认值暴露，且默认路径装配结果与变更前逐项一致。

## T-2 装配期工具面与过滤

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/graphs/react/graph.py`、`src/agent/aidev_agent/core/graphs/react/code_mode.py`
- 需求: REQ-2, REQ-3, REQ-5
- 依赖: 阻塞于 T-1
- 验证: `make test path=src/agent/tests/core/graphs/react/test_code_mode_assembly.py`
- [ ] 完成标准：`code_mode` 下 `_prepare_agent_tools` 只返回 1 个 `code_execute`；内层集合剔除 ask 类与带审批工具，并与 `mode`、`tool_allow`、`tool_deny` 取交集；`function_call` 下返回对象与变更前一致。

## T-3 精简清单与代码桩生成

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/code_mode/manifest.py`
- 需求: REQ-3, REQ-4
- 依赖: 阻塞于 T-2
- 验证: `make test path=src/agent/tests/core/tools/code_mode/test_manifest.py`
- [ ] 完成标准：清单中的工具名集合与内层集合完全相等；生成的桩源码 `compile()` 通过，函数名、参数名、必填性与工具 schema 一致，`target_runtime` 保留。

## T-4 沙箱侧 prelude 桥客户端

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/code_mode/prelude.py`
- 需求: REQ-4, REQ-8
- 依赖: 阻塞于 T-3
- 验证: `make test path=src/agent/tests/core/tools/code_mode/test_prelude.py`
- [ ] 完成标准：生成的 `_aidev_bridge.py` 在子进程内可用；未授权工具名抛可捕获异常且不产生请求文件；请求/响应/状态文件按协议落盘，调用计数超限抛 `CodeModeBudgetExceeded`。

## T-5 宿主侧桥执行器

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/code_mode/bridge.py`
- 需求: REQ-4, REQ-6, REQ-9
- 依赖: 阻塞于 T-4、T-9
- 验证: `make test path=src/agent/tests/core/tools/code_mode/test_bridge.py`
- [ ] 完成标准：`FakeCodeModeBackend` 上完成一次含两次工具调用的往返，工具各执行一次且顺序与代码一致；执行输出超限被截断并带标记；返回内容只含输出，不含内层调用记录。

## T-6 内层工具执行器

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/code_mode/invoker.py`
- 需求: REQ-4, REQ-10
- 依赖: 阻塞于 T-2、T-5
- 验证: `make test path=src/agent/tests/core/tools/code_mode/test_invoker.py`
- [ ] 完成标准：内层调用经 `build_tool_node` 的 wrapper 链执行，脱敏与结果截断生效；调用未装配工具名时工具本体调用次数为 0；内层执行不向图事件流产生 `on_tool_*` 回调。

## T-7 `code_execute` 工具与事件

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/code_mode/tool.py`、`src/agent/aidev_agent/core/graphs/react/code_mode.py`
- 需求: REQ-3, REQ-7
- 依赖: 阻塞于 T-5、T-6
- 验证: `make test path=src/agent/tests/core/ag_ui/test_code_mode_events.py`
- [ ] 完成标准：`code_execute` 无审批元数据；每次内层调用产生 1 组不含新事件类型的三元组，`tool_call_id` 两两不同且与外层 id 不同，事件不回填上下文。

## T-8 异常分类、超时与重试终止

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/code_mode/errors.py`、`src/agent/aidev_agent/core/tools/code_mode/session.py`
- 需求: REQ-8
- 依赖: 阻塞于 T-7
- 验证: `make test path=src/agent/tests/core/tools/code_mode/test_retry.py`
- [ ] 完成标准：语法错误、运行时异常、工具异常、超时、超限五类状态各自可判定并回传模型；连续失败达到配置上限时抛 `CodeModeRetryExhausted`（`GraphBubbleUp` 子类）使本轮失败结束，实际重试次数等于配置值。

## T-9 运行时契约与四类覆盖

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/tools/runtime_tools/types.py`、`local_backend.py`、`bubblewrap_backend.py`、`e2b_backend.py`、`paas_backend.py`
- 需求: REQ-9, REQ-10
- 依赖: 无
- 验证: `make test path=src/agent/tests/core/tools/code_mode/test_runtime_backends.py`
- [ ] 完成标准：`RuntimeBackend` 基类声明 `ls_info`/`upload_files`/`download_files` 与 `scratch_dir`；四类 backend 均满足契约，`paas` 的 scratch 目录落在 `$STORAGE_PATH` 下；能力缺失时抛 `CodeModeUnsupported` 且不降级。

## T-10 A2A 继承

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/tools/a2a_tools/test_code_mode_inheritance.py`
- 需求: REQ-11
- 依赖: 阻塞于 T-2
- 验证: `make test path=src/agent/tests/core/tools/a2a_tools/test_code_mode_inheritance.py`
- [ ] 完成标准：父 Agent 以 `code_mode` 调用的 A2A 子 Agent，构造出的 `execute_kwargs.tool_use_mode` 同为 `code_mode`，子 Agent 装配后只绑定 1 个 `code_execute`。

## T-11 契约兼容与前端零 diff

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/graphs/react/test_code_mode_contract.py`
- 需求: REQ-13
- 依赖: 阻塞于 T-7
- 验证: `make test path=src/agent/tests/core/graphs/react/test_code_mode_contract.py`
- [ ] 完成标准：同一 `code_mode` 请求在流式、非流式、resume 三条路径装配出的可调用集合与精简清单一致；`src/frontend/ai-blueking/packages/chat-x` 的 `git diff` 为空；历史会话未携带该字段时按 `function_call` 装配。

## T-12 收益测算方案文档

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/docs/code-mode-benefit-measurement.md`
- 需求: REQ-12
- 依赖: 无
- 验证: 人工评审：五个章节齐全（任务集、四项指标口径、对照方法、采样与统计口径、判据）；仓内不新增评测代码与数据文件
- [ ] 完成标准：文档可直接指导后续评测执行，每项指标写明采集点与计算方式。

## T-13 全量回归与本地端到端

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/tools/code_mode/test_local_e2e.py`
- 需求: REQ-2, REQ-4, REQ-9
- 依赖: 阻塞于 T-5、T-6、T-7、T-8、T-9
- 验证: `make test path=src/agent/tests/core/tools/code_mode`，再跑 `make test path=src/agent/tests/core` 做回归
- [ ] 完成标准：`local` 真实后端完成一次含两次工具调用、含分支与聚合的代码执行；`bubblewrap` 具备环境时同断言，缺环境按跳过处理；`function_call` 全量回归无新增失败。

## 覆盖对照

| 需求 | 任务 |
| :--- | :--- |
| REQ-1 | T-1 |
| REQ-2 | T-1, T-2, T-13 |
| REQ-3 | T-2, T-3, T-7 |
| REQ-4 | T-3, T-4, T-5, T-6, T-13 |
| REQ-5 | T-2 |
| REQ-6 | T-5 |
| REQ-7 | T-4, T-7 |
| REQ-8 | T-4, T-8 |
| REQ-9 | T-5, T-9, T-13 |
| REQ-10 | T-6, T-9 |
| REQ-11 | T-10 |
| REQ-12 | T-12 |
| REQ-13 | T-1, T-11 |
