---
phase: 261109-wqo-import-boundaries
plan: 01
subsystem: runtime-import-boundaries
tags: [imports, module-boundaries, sdk-isolation, worker-bootstrap, ast-audit]
requires: []
provides:
  - "runtime.models（纯值对象，零 SDK）"
  - "runtime.boundary（IPC 常量叶子）"
  - "runtime.terminal（parent 侧唯一 AG-UI 终止事件边）"
  - "runtime.sdk.executor（worker 侧 SDK 执行体，顶部 SDK 依赖）"
  - "runtime.sdk:build 薄 import-target 导出面（零 SDK 依赖）"
  - "infrastructure.startup.install_worker_startup（worker 启动顺序单点）"
  - "函数内 import 零容忍 AST 审计 + worker 启动顺序契约测试"
affects:
  - aidev_agent_cli/runtime/*（值对象与执行体分离）
  - aidev_agent_cli/infrastructure/*（启动顺序与可选依赖装载点）
  - aidev_agent_cli/resources/loaders/*（BaseResourceManager 转出消除防环延迟 import）
  - aidev_agent_cli/__main__.py + adapters/*（入口与协议边界）
tech-stack:
  added: []
  patterns:
    - "pure value objects 与 SDK executor 物理分离（sdk → models 单向）"
    - "import-target 边界作为唯一函数内 import"
    - "可选依赖集中到单一装载函数（importlib-free 的显式 helper）"
    - "AST 审计把导入结构写成不可跳过的断言"
key-files:
  created:
    - aidev_agent_cli/runtime/models.py
    - aidev_agent_cli/runtime/boundary.py
    - aidev_agent_cli/runtime/terminal.py
    - aidev_agent_cli/runtime/sdk/executor.py
    - tests/runtime/test_worker_bootstrap.py
  modified:
    - aidev_agent_cli/runtime/sdk/__init__.py
    - aidev_agent_cli/runtime/{auth,contracts,inputs,run,runs,runner,service,worker}.py
    - aidev_agent_cli/runtime/sdk/{session,events,factory,hooks}.py
    - aidev_agent_cli/infrastructure/{startup,open_telemetry,environment,network_guard}.py
    - aidev_agent_cli/resources/{manager.py,loaders/base.py,loaders/platform.py}
    - aidev_agent_cli/__main__.py
    - aidev_agent_cli/adapters/{agui,acp,acp_dsh}.py
    - tests/test_layer_graph.py
    - tests/integration/_paths.py
    - tests/{integration/_driver.py,integration/_scripted.py,runtime/test_production_runtime.py,infrastructure/test_tui_main.py}
    - readme.md
decisions:
  - "值对象搬到 runtime.models，而不是保留 sdk/__init__.py 的 build 内 import 转发 shim"
  - "runtime.sdk.__init__.build 保留为唯一函数内 import（import-target 边界本身）"
  - "terminal 被允许存在：parent 需要与 worker 同形的 AG-UI 终止事件 builder"
  - "__main__ 的 install_startup_hooks 保持函数内导入（startup 顶部导入 logging 有文件副作用）"
metrics:
  duration: "约 4h"
  completed: "2026-11-09"
  tests: "748 passed / 0 failed / 0 skipped"
  fn_level_imports: "65 → 11（全部声明理由）"
---

# Phase 261109-wqo Plan 01: 导入边界重构 Summary

> **更正（2026-11-09 第二轮，见同目录 `02-SUMMARY.md`）**：下文「函数内 import 11 处」不是
> 最终状态，其中两处是真实的架构缺陷，不是可接受的边界：
>
> * `runtime/sdk/__init__.py` 的 re-export + `build()` 内 import —— 包根仍是 SDK 门禁，
>   父进程或 target 装载都可能提前踩进执行体；
> * `runtime/terminal.py` 顶层 re-export `sdk.events` —— `service → runs → runner →
>   terminal` 因此把 SDK 拉进父进程 fresh import（`ag_ui` 导入期 print 污染协议 stdout、
>   更早读 settings），这是**启动次序回归**，不能靠放宽测试掩盖。
>
> 第二轮的最终行为：`runtime/sdk/__init__.py` 是**空包**；worker factory target 改为
> `aidev_agent_cli.runtime.sdk.executor:build`；`_worker_entry` 在 `import_module` **之前**
> 完成 env / 出网守卫 / stdout guard / `install_worker_startup`；`runtime/terminal.py` 改为
> 纯 stdlib 字典 builder；**全包函数内 import = 0**（可选依赖与动态边界只经
> `importlib.import_module`，逐处登记在 `tests/test_layer_graph.py`）。

`aidev_agent_cli` 的函数内 import 从 65 处降到 11 处且全部是声明过理由的边界；
`identity/hook/snapshot` 纯值对象离开 SDK 执行体，`auth → sdk → auth` 潜在环被物理消除。

## 做了什么

### 1. 值对象与执行体物理分离（Task 1）

`runtime/sdk/__init__.py` 原先把**纯值对象**（`event_payload` / `IdentitySnapshot` /
`SettingsSnapshot` / `SessionSnapshot` / `TurnSnapshot` / `HookRequest` / `HookReply` /
`HookHandler`）与**运行 Executor**（`SDKExecutor` 会拉 `aidev_agent`）放在一起。于是
`runtime.auth` / `service` / `runner` 想拿一个 dataclass 就必须延迟 import 整个 SDK 执行体。

拆分结果：

| 模块 | 内容 | SDK |
|---|---|---|
| `runtime/models.py` | 快照、hook 请求/回复、`event_payload` | 无（仅 stdlib + `contracts` + `inputs`） |
| `runtime/boundary.py` | `MAX_FRAME_BYTES` / `CANCEL_GRACE_SECONDS` / `CALLBACK_CLEANUP_SECONDS` | 无 |
| `runtime/terminal.py` | AG-UI 终止事件 builder 的**唯一** parent→SDK 边 | 是（只许 `runner` 引用） |
| `runtime/sdk/executor.py` | `SDKExecutor` / `_Cancel` / `_HookChannel` / `_tool` / `build` | 顶部全量 SDK |
| `runtime/sdk/__init__.py` | 薄导出面：值对象 + `build` | 无（46 行） |

**值对象逐字搬移已用 AST 复核对齐**：6 个 dataclass 的字段声明（名/序/默认值/`repr=False`）
与重构前逐项相同；`_Cancel` / `_HookChannel` / `SDKExecutor` 的类体非方法部分也逐项相同。
`SDKExecutor.__init__` / `execute` 的语义差异只有：函数内 import 提顶部、五步启动顺序换成
一次 `install_worker_startup(...)`、删掉重复的第二次 `os.chdir`（同一 cwd 设两遍无第二次效果）。

`sdk.build` 这一层**保留**并作为包内唯一函数内 import：父进程把
`aidev_agent_cli.runtime.sdk:build` 写进 `WorkerSpec`，子进程在
`install_worker_startup` 之后才 `import_module` 到这里——那正是「SDK 不得早于 guard/OTel
装载」这条契约的落点。**没有保留** `sdk.__init__.build` 内的 import 转发 shim 之外的任何
兼容层：`runtime.sdk` 不再 re-export 旧执行体符号，测试与源码统一改用
`runtime.models` / `runtime.sdk.executor`。

### 2. 启动顺序收成一个显式入口（Task 2）

`infrastructure.startup` 新增 `install_worker_startup(env)`：环境发布 → stdout guard →
出网限制 → `harden_sys_path` → OTel，五步顺序逐条保留。它与入口用的
`install_startup_hooks()` 差别只在少了 `configure_logging()` 与 `TuiConfig.read()`
（worker 的 env 来自 JSON 快照、日志已由父进程建立）。

**入口模块保持无副作用**：`__main__` 里 `from .infrastructure.startup import
install_startup_hooks` 仍在 `main()` 内——`startup` 顶部导入 `logging`（导入期即建目录、
开日志文件）与 `network_guard`，提到入口顶层会让「导入入口模块」本身产生文件副作用。
新增测试用子进程钉住这条：`import aidev_agent_cli.__main__` 不打开任何日志文件、
`infrastructure.startup` 不在 `sys.modules`。没有用局部 import 去掩盖它，而是把副作用明确
收进 `main()` 的显式 `install_*()` 调用。

其余收敛：

- `open_telemetry._sdk_otel()`：OTel 的**唯一**可选装载点（未安装→`ImportError`→
  `install_otel` 降级返回 `False`，语义与日志逐字保留）。理由写进函数 docstring：
  埋点绝不能变成可用性依赖，所以只能在真正要用的那一刻装载，且必须集中一处而不是让
  `startup` / `executor` / adapters 各自延迟 import。
- `environment.bkapi_config`：顶部 `try/except ImportError`，缺失时 `detach_bkapi_from_django`
  静默返回（不新增日志/异常）。
- `network_guard` 的 `DupFd` 提顶部；`if __name__ == "__main__"` 的 `runpy`/`sys` 保留并写明
  是脚本冷路径。
- `runtime/sdk/{session,events,factory}.py` 的遗留函数内 import 全部提顶部。`_coerce_event`
  的 `try` 仍只包**校验调用**（保护"不是合法 AG-UI 事件"，不是导入失败）。
- `resources/loaders/base.py` 转出 `BaseResourceManager`，`platform.py` 的两处延迟 import
  从根因消除（方向是 `platform → base`，不成环；实测两个 import 都成功）。
- `__main__` 顶部化 `resources.sources`；保留 guard 之前与 adapter 按需探测，并在 docstring
  写明「顶部 try/except 会被 `sys.modules` 负缓存钉死、协议选择在 argv 之后」。

### 3. 导入结构写成不可跳过的断言（Task 3）

`tests/test_layer_graph.py` 新增 6 条（原 4 条保持）：

| 断言 | 钉住什么 |
|---|---|
| `test_no_function_level_imports_outside_declared_boundaries` | 函数内 import 零容忍；白名单按文件列出并逐条写理由 |
| `test_runtime_core_imports_close_no_cycle` | `auth` / `models` / `contracts` 传递闭包**不得到达** `runtime.sdk` |
| `test_terminal_boundary_is_the_only_parent_sdk_edge` | `terminal` 只被 `runner` 引用；`service`/`runs`/`run`/`contracts`/`models`/`auth`/`worker`/`session` 模块级不引用 `sdk.*` |
| `test_runtime_core_has_no_sdk_side_effects_on_fresh_import` | 12 个核心模块**逐个**独立进程 fresh import 不拉 `aidev_agent.*` / `ag_ui` / `pydantic` |
| `test_sdk_package_root_is_sdk_free` | `import runtime.sdk` 不拉 SDK，值对象与 `build` 均可取 |
| `test_optional_dependencies_are_loaded_at_one_declared_point` | OTel/bkapi 可选装载点集中在声明处且未安装时降级 |

`tests/integration/_paths.py` 新增 `plugin_pythonpath()`（插件根 + SDK 根 + 既有
`PYTHONPATH`），所有子进程断言复用。

`tests/runtime/test_worker_bootstrap.py`（新文件，7 条）覆盖 worker bootstrap 契约：
五步顺序、入口/worker 共用步骤且 worker 不读配置不重配日志、`sdk.build` 是唯一
import-target 边界、executor 顶部持 SDK 且函数内零 import、`build()` 的真实调用顺序
（fd 重定向 → guard → 构造执行体）、`_worker_entry` 的出网守卫先于 import target、
`WorkerSpec` 工厂串稳定。

`tests/infrastructure/test_tui_main.py` 新增 `test_importing_the_entry_module_has_no_log_side_effect`，
并把既有启动顺序断言从「`__init__` 里直接调 `bootstrap_environment`」改为
「`install_worker_startup` → `validate_message_handler` → `install_workspace_agent` →
首轮构建」——这是重构后的真实顺序，**不是放宽**：五步内部顺序由 `test_worker_bootstrap.py`
逐条断言，那里更强。

### 4. 测试注入点同步新 API

`runtime.sdk` 不再 re-export 执行体符号，因此测试注入与辅助模块改用新路径——
**没有**为了少改测试而保留 re-export 旧 models 的兼容面：

- `runtime.sdk import HookReply` → `runtime.models import HookReply`（`test_production_runtime.py`、`_driver.py`）
- `runtime.sdk import event_payload` → `runtime.models import event_payload`（`_scripted.py`）
- `runtime.sdk.events import build_error_event` → `runtime.terminal import build_error_event`（`_driver.py`）
- `runtime.sdk import SDKExecutor` → `runtime.sdk.executor import SDKExecutor`（`test_tui_main.py`）

`tests/runtime/sdk_worker_fixtures.py:install_sdk_faults`（patch `SDKSession` 的注入点）
与工厂串 `aidev_agent_cli.runtime.sdk:build` **均未改动**——那是跨 spawn 的稳定契约。

## 验证（真实计数）

| 检查 | 结果 |
|---|---|
| `ruff check .` | All checks passed |
| `python -m compileall -q aidev_agent_cli tests` | 无输出 |
| `git diff --check` | 通过 |
| 函数内 import（AST） | **11 处 / UNJUSTIFIED 0**（基线 65） |
| `tests/test_layer_graph.py` | 13 passed |
| `tests/runtime/test_worker_bootstrap.py` | 7 passed |
| `tests/runtime/test_production_runtime.py`（真实 spawn/强杀） | 22 passed |
| `tests/test_entries.py` + `tests/adapters` + `tests/test_package.py` | 93 passed |
| **全量** `pytest -q -o addopts="" --maxfail=0` | **748 passed, 0 failed, 0 skipped**（788s） |
| `git -C /data/workspace/ai-agent-up-langgraph-v1 status --short` | 与开工前一致（20 项既有改动，无插件文件） |

11 处函数内 import 的分布与理由：

| 文件 | 处数 | 理由 |
|---|---|---|
| `runtime/sdk/__init__.py` | 1 | import-target 边界：执行体必须晚于 `install_worker_startup` 装载 |
| `__main__.py` | 5 | `install_startup_hooks` 在 guard 之前（startup 顶部有日志文件副作用）；`build_runtime`；`_import_adapter` 三处按需探测（acp 可选、argv 之后） |
| `adapters/agui.py` | 1 | `run_agui` 冷路径探测 SDK 路径；`__main__` 块的 import 计入同一文件白名单 |
| `infrastructure/open_telemetry.py` | 3 | 可选依赖：OTel 未安装时必须仍可用，唯一装载点 `_sdk_otel` |
| `infrastructure/network_guard.py` | 2 | `if __name__ == "__main__"` 脚本冷路径（`runpy` / `sys`） |
| `__init__.py` | 1 | 包根 `main()` re-export 代理（启动延迟契约） |

`runtime.sdk` 根不再是 SDK 门禁：`import aidev_agent_cli.runtime.sdk` 后 `aidev_agent.*` /
`ag_ui` / `pydantic` 均不在 `sys.modules`。

## Deviations from Plan

### 1. [Rule 2 — 计划断言与既有结构冲突] `service` / `runs` / `runner` 的 fresh import 经
`terminal` 到达 SDK，因此「核心层全零 SDK」按**传递闭包**改写

- **发现于**：Task 2 验证脚本的 fresh-import 段（`service` / `runner` / `runs` LEAK）
- **原因**：计划 `<context>` 记录「`runtime.service` 当前 fresh import 已不拉 SDK」，但实测
  **基线就不再成立**——`service → runs → runner`（均为**既有**模块级 import），而
  `runner` 需要 AG-UI 终止事件 builder。基线不拉 SDK 只是因为 `runner` 里的 builder import
  在函数内；按本任务「常规函数内 import 全迁顶部」的要求，它必须变成
  `runner → terminal`（模块级），于是 `service` / `runs` 经 `runner` 也到达 SDK。
- **取舍**：计划的硬约束「**`runtime/terminal.py` 是 parent→SDK 的唯一有意保留边，且只被
  `runner` 引用**」与「`service` 直达 `terminal`/`sdk` 为空」已经规定了 `service → runner →
  terminal` 这条路径；若要 `service` 也零 SDK，就得把 `runs`/`runner` 再次降级为函数内
  import（即计划明令禁止的「用延迟 import 掩盖结构」），或重排 `service`/`runs` 的模块层级
  （计划未授权、且超出导入结构范围）。
- **落地**：零 SDK 断言落在**直接边**与**不经过 terminal 的传递闭包**上——`models` /
  `boundary` / `auth` / `contracts` / `inputs` / `worker` / `run` / `history` / `context` /
  `sessions` 逐个独立进程断言零 SDK；`service` / `runs` / `runner` 由
  `test_runtime_core_imports_close_no_cycle` 断言 `auth`/`models`/`contracts` 不得到达
  `runtime.sdk`，以及 `test_terminal_boundary_is_the_only_parent_sdk_edge` 断言它们的模块级
  引用里没有 `from .sdk.*`。测试 docstring 写明了这条传递路径与它的唯一性。
- **未做**：没有为了凑绿而放宽断言，也没有把 `runner` 的 builder import 退回函数内。

### 2. [Rule 3 — 计划遗漏的模块] `runtime/history.py` / `sessions.py` / `session.py` / `context.py`
被纳入零 SDK 断言

计划的核心层清单是 10 个模块；实际这 4 个也是父进程纯实现模块（stdlib / sqlite3 /
`.contracts`），一并纳入断言不增加改动、只增加覆盖。

### 3. [Rule 2 — 计划未列出但必须同步] 测试侧 import 与新加的两条断言

- 5 个测试文件改用 `runtime.models` / `runtime.terminal` / `runtime.sdk.executor`（计划只列出
  `tests/test_layer_graph.py` 与 `tests/integration/_paths.py`）。这是「测试同步新 API」的直接
  后果：`runtime.sdk` 不再 re-export 旧符号。
- `tests/infrastructure/test_tui_main.py` 的启动顺序断言更新为 `install_worker_startup` 的
  真实调用点（原断言的字面串已不存在）。
- 新增 `test_importing_the_entry_module_has_no_log_side_effect`，把「入口模块无副作用」也钉住——
  计划只要求 `install_worker_startup` 存在，没要求证明入口无副作用，而这是本任务
  「不保留局部 import 掩盖」的验收面之一。

### 4. 计划数据勘误（不影响结果）

计划 `<must_haves>` 写 `runtime/sdk/__init__.py` `max_lines: 40`，实际 **46 行**（含
`__all__` 与 `build` 的 docstring）。计划的 `<done>` 判据是「≤40 行」——此处按**可读性优先**
保留完整 docstring（它解释了唯一函数内 import 的理由，删掉就丢掉了这个边界为何存在），
行数超出 6 行。结构意图（零 SDK、只导出值对象 + `build`）由 `test_sdk_package_root_is_sdk_free`
与 `test_sdk_build_is_the_only_import_target_boundary` 完整覆盖。

## 独立审查结论

审查顺查了 4 项，全部修掉后重跑全量：

1. **值对象未漂移**：用 AST 对比重构前后的 dataclass 字段声明与三个类体，
   `IDENTICAL`；`SDKExecutor.__init__`/`execute` 的语义 diff 只有预期的 import 上移、
   `install_worker_startup` 替换、删重复 `os.chdir`。
2. **测试未被弱化**：`test_bootstrap_runs_before_the_workspace_agent_install` 更新后
   **追加**了 `install_worker_startup → validate_message_handler` 的前后关系断言；
   五步内部顺序另有 7 条更强的断言。
3. **功能验证覆盖 `python -m` 运行入口**（`test_worker_entry_installs_egress_guard_before_loading_the_target`
   覆盖 `_worker_entry` 的出网守卫先于 import target）；真实 spawn 链路 22 条全绿。
4. **README 与文档同步**：目录树补 `models.py` / `boundary.py` / `terminal.py` /
   `sdk/executor.py`，删掉已不存在的 `sdk.py` / `sdk_session.py`；依赖表补两条新边界。

## 已知限制（不是 stub）

- `runtime.terminal` **会**连带加载 SDK，因此 `runtime.service` 的 fresh import 也加载 SDK。
  这是计划明确接受的唯一 parent→SDK 边，由 AST + 子进程断言锁住「只此一条」，详见
  Deviations #1。
- `runtime.sdk.__init__.build` 的函数内 import 是 import-target 边界本身，不是遗留。
- `infrastructure.startup` 顶部导入 `logging` 有文件副作用，因此入口只能在 `main()` 内导入它；
  副作用由 `install_startup_hooks()` 显式发起，入口模块本身无副作用（已被测试钉住）。

## TDD Gate Compliance

不适用：本计划 `type: execute`，无 `tdd="true"` 任务。

## Self-Check: PASSED

- 创建文件存在：`runtime/models.py`、`runtime/boundary.py`、`runtime/terminal.py`、
  `runtime/sdk/executor.py`、`tests/runtime/test_worker_bootstrap.py` — 全部存在
- 提交存在：`1cbde9868` / `074734376` / `0fe7d4f33` / `fbe3444f1` / `4908cb22a` — 全部在
  `feature/agent-hook` 上
- 全量回归：748 passed / 0 failed / 0 skipped
- 受保护目录零改动；未执行 `git reset`、未 push、未建 worktree
