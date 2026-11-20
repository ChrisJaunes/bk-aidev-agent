---
phase: 261109-wqo-import-boundaries
plan: 02
subsystem: runtime-import-boundaries
tags: [imports, module-boundaries, sdk-isolation, worker-bootstrap, ast-audit, startup-order]
requires: ["261109-wqo/01"]
provides:
  - "runtime.sdk 空包根（零 re-export，仅作执行体命名空间）"
  - "aidev_agent_cli.runtime.sdk.executor:build（唯一 spawn import target）"
  - "runtime.terminal 纯 stdlib 字典 builder（parent 零 SDK 的终止事件形状）"
  - "runtime.hook_contract（HookConfig / 点位契约的纯值家）"
  - "infrastructure.logging.configured_logger（导入期无副作用的 logger）"
  - "全包函数内 import == 0 的零豁免 AST 断言"
affects:
  - aidev_agent_cli/runtime/{sdk/__init__,sdk/executor,sdk/events,sdk/hooks,terminal,hook_contract,worker,runner}.py
  - aidev_agent_cli/infrastructure/{logging,startup,open_telemetry,stdio,process,settings}.py
  - aidev_agent_cli/{__init__,__main__}.py + adapters/{agui,acp,acp_dsh}.py
  - aidev_agent_cli/resources/{loaders/__init__.py,loaders/base.py,loaders/platform.py,manager.py}
tech-stack:
  added: []
  patterns:
    - "空包根 + import target 指向执行体模块（装载时机由 _worker_entry 控制）"
    - "纯 stdlib 事件形状，SDK 模型反向复用同一份字典"
    - "可选依赖 / 动态边界只经 importlib.import_module，且逐处登记"
    - "logging 导入期零副作用，配置只由启动钩子显式发起"
key-files:
  created:
    - aidev_agent_cli/runtime/hook_contract.py
    - tests/runtime/test_terminal_events.py
    - tests/runtime/test_hook_points_contract.py
  modified:
    - aidev_agent_cli/runtime/sdk/__init__.py
    - aidev_agent_cli/runtime/sdk/{executor,events,hooks}.py
    - aidev_agent_cli/runtime/{terminal,worker,runner}.py
    - aidev_agent_cli/infrastructure/{logging,startup,open_telemetry,stdio,process,settings}.py
    - aidev_agent_cli/{__init__,__main__}.py
    - aidev_agent_cli/adapters/{agui,acp,acp_dsh}.py
    - aidev_agent_cli/resources/{loaders/__init__.py,loaders/base.py,loaders/platform.py,manager.py}
    - tests/test_layer_graph.py
    - tests/runtime/test_worker_bootstrap.py
    - tests/{infrastructure/test_tui_main.py,infrastructure/test_tui_stdout_guard.py,test_acp_entry.py}
    - tests/integration/{_driver,_fake_acp_agent,test_acp_protocol_handshake,test_no_platform,test_hook_no_platform}.py
    - tests/resources/test_tui_resource_manager.py
    - .planning/quick/261109-wqo-import-boundaries/{PLAN.md,01-SUMMARY.md}
decisions:
  - "runtime.sdk.__init__ 清空为 __all__ = []：任何 re-export 都是 SDK 门禁"
  - "worker factory target 改为 sdk.executor:build，_worker_entry 先保护后装载"
  - "terminal 改为纯 stdlib 字典；sdk.events 反向复用，形状只有一份事实来源"
  - "函数内 import 零豁免；可选依赖与动态边界一律 importlib.import_module 并登记理由"
  - "logging 导入期不建目录/不开文件；configured_logger 把解析推迟到首次记录"
metrics:
  duration: "约 6h"
  completed: "2026-11-09"
  tests: "760 passed / 0 failed / 0 skipped"
  fn_level_imports: "11 → 0（零豁免）"
---

# Phase 261109-wqo Plan 02: 导入边界回归修复 Summary

上一轮把函数内 import 从 65 降到 11 并声称「UNJUSTIFIED 0」，但其中两处是**架构缺陷
被写成了理由**：`runtime/sdk/__init__.py` 继续 re-export 旧 snapshot 并在 `build()` 内
import 执行体；`runtime/terminal.py` 顶层 `from .sdk.events import ...` 让
`service → runs → runner → terminal` 把 SDK 拉进父进程 fresh import（`ag_ui` 导入期
print 污染协议 stdout、`aidev_agent` 导入期读 settings）。后者是**生产启动次序回归**，
不能靠放宽测试掩盖。

本轮把这两处从结构上消除，并把"函数内 import"的判据收紧到**零豁免**。

## 最终实际行为（逐条对照要求）

### (1) `sdk/__init__.py` 空包 + 新 import target + 保护先于装载

* `runtime/sdk/__init__.py` 只剩 docstring 与 `__all__: list[str] = []`——模块级 import
  仅 `__future__`，**不 re-export 任何旧 snapshot / build**。
* worker factory target 改为 `aidev_agent_cli.runtime.sdk.executor:build`，集中为
  `runtime.runner.WORKER_FACTORY` 一个常量。
* `runtime/worker._worker_entry` 的执行顺序（由 `ENTRY_ORDER` 断言钉住）：

  ```
  os.setsid → publish_environment(snapshot env) → os.chdir(snapshot cwd)
            → [有 descriptor 时] 出网守卫 install_from_env
            → install_worker_startup(env)   # bootstrap / stdout guard / 出网 / harden_sys_path / OTel
            → importlib.import_module(target)  ← 只有到这里 SDK 才进来
            → phase=building → factory(frame, context) → phase=running → execute
  ```
* `sdk/executor.py` 顶部持全部 SDK 依赖，函数内 import 归零；`build()` 的 fd 重定向抽成
  `infrastructure.stdio.apply_worker_stdio_guard()`（行为逐字不变）。

### (2) `terminal` 纯 stdlib + `event_payload` 接受 dict + service fresh import 零 SDK

* `runtime/terminal.py` 只 import `time` / `typing`，产出与 `ag_ui` 序列化**同形**的字典：
  `threadId` / `runId` 别名、`timestamp` 墙钟毫秒、`outcome == {"type": "success"}`。
  与 SDK 真序列化器的逐键比对在 `tests/runtime/test_terminal_events.py`。
* `runtime/sdk/events.py` 的同类 builder 改为 `RunErrorEvent(**terminal.build_error_event(...))`
  ——形状只有一份事实来源，worker 与父进程不会漂移。
* `runtime.models.event_payload` 对 dict 原样返回，`runner` 因此可以直接投递字典事件。
* **真实闭包断言恢复**：`test_runtime_core_has_no_sdk_side_effects_on_fresh_import` 对
  `runtime.service` / `runner` / `runs` / `terminal` / `sdk` 逐个起独立进程，要求在
  `import` 后 `aidev_agent.*` / `ag_ui` / `acp` / `pydantic` **均不在 `sys.modules`**——
  没有豁免、没有 skip。

### (3) 函数内 import 计数 0，可选/动态边界只走 `importlib.import_module`

AST 实测：`aidev_agent_cli/` 下函数/类体内的 `Import` / `ImportFrom` **= 0**。

动态装载点只有 6 个文件 9 处，全部登记在 `tests/test_layer_graph.py:DYNAMIC_IMPORT_POINTS`
（未登记即失败，声明失效也失败）：

| 文件 | 用途 |
|---|---|
| `__init__.py` | 包根 `main()` 延迟代理到 `__main__`（导入包零副作用） |
| `__main__.py` | acp 是可选 extra：缺它时入口仍可导入并给出可读提示 |
| `infrastructure/open_telemetry.py` | OTel 是可选依赖：未安装时必须降级可用 |
| `resources/loaders/__init__.py` | platform 加载器持 SDK：注册表按需构建 |
| `runtime/hook_contract.py` | ready 帧的 SDK 路径是可选诊断字段 |
| `runtime/worker.py` / `runtime/sdk/executor.py` | 跨 spawn 的 import target / tool factory 串 |

配套的结构改动：

* `infrastructure/logging.py` **导入期不再建目录、不开文件**（`configure_logging()` 只在
  启动钩子里显式调用；早期诊断经 `configured_logger` 落到仅 stderr 的兜底 handler，
  随后被真实配置替换）。
* `__main__` / 各 adapter 的 startup 导入因此可以放到模块顶部；
  `test_importing_the_entry_module_has_no_log_side_effect` 仍然通过（入口模块无文件副作用）。

### (4) resources 纯核不触发 SDK；audit 允许零函数内 import

* `resources/loaders/base.py` 恢复为**纯值叶子**（`ResourceOverrides` / `ResourceLoader`），
  不再从 SDK 取 `BaseResourceManager`；`platform.py` 顶部自持 SDK 依赖。
* `resources/loaders/__init__.py` 的注册表按需构建：`PURE_LOADERS` 直接可用，platform 经
  `registry()` 动态装载。因此 `import aidev_agent_cli.resources.loaders.base` 零 SDK。
* `runtime.hook_contract` 承接 `HookConfig` / 点位名 / 降级原因，值由
  `tests/runtime/test_hook_points_contract.py` 与 SDK 常量比对。

### (5) 文档更正

* 本 `PLAN.md` 已按最终行为改写：`sdk:build` → `sdk.executor:build`、`terminal` 从
  "re-export sdk.events" 改为"纯 stdlib 字典"、"唯一函数内 import 边界"改为"零豁免"。
* `01-SUMMARY.md` 顶部加了更正块，明确"函数内 import 11 处"**不是**最终状态。
* 所有测试 fixture 的 import target 同步新串，**没有**保留 `runtime.sdk:build` shim。

## 验证（真实计数）

| 检查 | 结果 |
|---|---|
| `ruff check .` | All checks passed |
| `python -m compileall -q aidev_agent_cli tests` | 无输出 |
| 函数内 import（AST） | **0**（上一轮 11） |
| `tests/test_layer_graph.py` | 14 passed |
| `tests/runtime/test_worker_bootstrap.py` | 11 passed |
| `tests/runtime/test_terminal_events.py` | 4 passed |
| `tests/runtime/test_hook_points_contract.py` | 3 passed |
| `tests/runtime/test_process_worker.py`（真实 spawn / 强杀） | 20 passed |
| `tests/integration/test_acp_dsh_protocol.py`（三协议之一，真子进程） | 7 passed |
| **全量** `pytest -q -o addopts="" --maxfail=0` | **760 passed, 0 failed, 0 skipped**（707s） |

fresh import 零 SDK（独立进程逐个断言）：

```
aidev_agent_cli                     0
aidev_agent_cli.runtime.service     0
aidev_agent_cli.runtime.runner      0
aidev_agent_cli.runtime.terminal    0
aidev_agent_cli.runtime.sdk         0
aidev_agent_cli.runtime.worker      0
aidev_agent_cli.adapters.agui       0
aidev_agent_cli.resources.loaders.base  0
```

`aidev_agent_cli.__main__` 会拉起 `pydantic` / `acp`：那是**入口**导入 adapter 的结果
（`acp` 是协议 SDK 本身），不是执行体 SDK；`aidev_agent.*` 与 `ag_ui` 仍为 0。

## Deviations from Plan

### 1. [Rule 1 — Bug] `publish_environment` 返回 `None` 导致 worker 启动即崩

- **发现于**：`tests/integration/test_acp_dsh_protocol.py` 报
  `AttributeError: 'NoneType' object has no attribute 'pop'`
- **原因**：`_worker_entry` 写成 `env = publish_environment(...)`，而该函数返回 `None`，
  随后 `bootstrap_environment(env)` 在 `env.pop("DJANGO_SETTINGS_MODULE", None)` 上炸。
- **修复**：`publish_environment` 返回同一个 dict（并在 docstring 说明为什么必须返回实参）。

### 2. [Rule 2 — 缺失的健壮性] worker 快照可省略 `session.settings.env/cwd`

- **发现于**：`tests/runtime/test_process_worker.py`（只关心 IPC 契约的 fixture 快照只有
  `{"hook": true}` 之类）在改动后全部失败。
- **修复**：`worker_environment` / `worker_cwd` 在缺失时回退到**当前**进程 env / cwd，
  形状不对时才报错。生产快照始终带着这两项，语义没有变化。

### 3. [Rule 3 — 阻塞] 测试侧 import target / PYTHONPATH 与新结构对齐

- `test_the_missing_extra_message_names_the_extra` 改为直接把 `_ACP_ADAPTERS` 置空（可选
  extra 的装载点是 `importlib`，`sys.modules` 负缓存不再适用）。
- `test_acp_protocol_handshake` 的 fake 后端入口补 `install_startup_hooks()`——与真实入口同
  一条启动路径；否则它不配置日志，"cwd 出现在日志里"的断言是空断言。
- `test_no_platform` / `test_hook_no_platform` 的子进程补 `PYTHONPATH`（它们按脚本路径起跑，
  cwd 是 SDK 根，`tests.*` 需要插件根在路径上）。
- `_driver.encode_event` 接受字典事件（父进程补的终止事件现在是 dict）。

### 4. [Rule 1 — Bug] `building` 阶段帧位置

- `building` 原先在 `import_module` **之前**发出，于是"导入执行体"的时间被算进构建超时。
  修正为 import target 之后：导入期仍属于启动保护窗口。

## 未做（刻意）

* 没有把 `runtime.sdk:build` 保留为兼容 shim——上一轮的形式本身就是缺陷。
* 没有为了凑绿放宽 `service` / `runner` 的零 SDK 断言：断言是对**真实闭包**求值的。
* 没有改动 `/data/workspace/agent-security` 之外的任何路径；`git status` 只出现本插件的文件。
