---
phase: 261109-wqo-import-boundaries
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  # --- new modules (pure values / ports / lightweight adapters) ---
  - aidev_agent_cli/runtime/models.py
  - aidev_agent_cli/runtime/boundary.py
  - aidev_agent_cli/runtime/terminal.py
  # --- runtime core ---
  - aidev_agent_cli/runtime/auth.py
  - aidev_agent_cli/runtime/contracts.py
  - aidev_agent_cli/runtime/inputs.py
  - aidev_agent_cli/runtime/service.py
  - aidev_agent_cli/runtime/runner.py
  - aidev_agent_cli/runtime/run.py
  - aidev_agent_cli/runtime/runs.py
  - aidev_agent_cli/runtime/worker.py
  # --- sdk package (worker side only) ---
  - aidev_agent_cli/runtime/sdk/__init__.py
  - aidev_agent_cli/runtime/sdk/executor.py
  - aidev_agent_cli/runtime/sdk/session.py
  - aidev_agent_cli/runtime/sdk/events.py
  - aidev_agent_cli/runtime/sdk/factory.py
  # --- infrastructure / resources / entry / adapters ---
  - aidev_agent_cli/infrastructure/startup.py
  - aidev_agent_cli/infrastructure/open_telemetry.py
  - aidev_agent_cli/infrastructure/environment.py
  - aidev_agent_cli/infrastructure/network_guard.py
  - aidev_agent_cli/infrastructure/logging.py
  - aidev_agent_cli/__init__.py
  - aidev_agent_cli/__main__.py
  - aidev_agent_cli/adapters/agui.py
  - aidev_agent_cli/adapters/acp.py
  - aidev_agent_cli/adapters/acp_dsh.py
  - aidev_agent_cli/resources/loaders/base.py
  - aidev_agent_cli/resources/loaders/platform.py
  - aidev_agent_cli/resources/manager.py
  # --- tests ---
  - tests/test_layer_graph.py
  - tests/integration/_paths.py
autonomous: true
requirements: [quick-261109-wqo]  # quick task: 无 ROADMAP 需求号，导入边界重构
must_haves:
  truths:
    - "AST 审计：aidev_agent_cli 下常规 import 在函数/类体内为 **0**（零豁免；可选依赖与动态边界一律 importlib.import_module，逐处登记）"
    - "fresh import aidev_agent_cli.runtime.service / .runner / .runs / .run / .auth / .models / .boundary 不加载 aidev_agent.*、ag_ui、acp、pydantic（子进程断言）"
    - "import aidev_agent_cli.runtime（含 sdk 子包遍历）在不具备 SDK 的环境下仍成功——sdk 根 __init__ 不再拉起 SDK"
    - "auth ↔ sdk 潜在环被物理消除：auth 顶部 import runtime.models，sdk 不再被 runtime 核心层任何模块模块级引用"
    - "真实 spawn 链路：Run → ProcessWorker(WorkerSpec('aidev_agent_cli.runtime.sdk.executor:build')) → env/出网守卫/stdout guard/OTel 之后才 import_module 执行体"
    - "真实 worker 的 phase 帧与保护顺序：guard 先于 SDK import；环境发布先于 SDK import；OTel 先于 SDK import"
    - "三协议（acp / acp-dsh / agui）入口与 full 734 tests 行为不变；ruff check 与 compileall 通过"
  artifacts:
    - path: aidev_agent_cli/runtime/models.py
      provides: "纯值对象：IdentitySnapshot / SettingsSnapshot / SessionSnapshot / TurnSnapshot / HookRequest / HookReply / HookHandler / event_payload"
      contains: "class TurnSnapshot"
    - path: aidev_agent_cli/runtime/boundary.py
      provides: "IPC 纯常量（MAX_FRAME_BYTES / CANCEL_GRACE_SECONDS / CALLBACK_CLEANUP_SECONDS）"
      contains: "CANCEL_GRACE_SECONDS"
    - path: aidev_agent_cli/runtime/terminal.py
      provides: "纯 stdlib 的 AG-UI 终止事件字典 builder（与 sdk 序列化同形；sdk.events 反向复用）"
      contains: "build_cancel_terminal_events"
    - path: aidev_agent_cli/runtime/sdk/executor.py
      provides: "SDKExecutor / _Cancel / _HookChannel / _tool / build（worker 工厂），顶部 SDK 依赖"
      contains: "def build("
    - path: aidev_agent_cli/runtime/sdk/__init__.py
      provides: "仅再导出纯值对象与 build，零 SDK 依赖"
      max_lines: 40
    - path: tests/test_layer_graph.py
      provides: "函数内 import 零容忍 AST 审计 + fresh-import 无 SDK sideeffects 子进程断言"
      contains: "test_no_function_level_imports"
  key_links:
    - from: aidev_agent_cli/runtime/auth.py
      to: aidev_agent_cli/runtime/models.py
      via: "模块级 from .models import IdentitySnapshot"
      pattern: "from \\.models import"
    - from: aidev_agent_cli/runtime/runner.py
      to: aidev_agent_cli/runtime/terminal.py
      via: "模块级 from .terminal import build_cancel_terminal_events, build_error_event"
      pattern: "from \\.terminal import"
    - from: aidev_agent_cli/runtime/sdk/executor.py
      to: aidev_agent_cli/infrastructure/startup.py
      via: "install_worker_startup（bootstrap/stdout guard/环境/出网/OTel 集中一处）"
      pattern: "install_worker_startup"
---

<objective>
把 `aidev_agent_cli` 里散落在函数体内的 import 收敛为**模块顶部 import**，并同时消除
`auth → sdk → auth` 的潜在循环。

Purpose：当前 46 处函数内 import 中，一部分是历史遗留（stdlib / 同层模块，纯冗余），
一部分是在掩盖真实的结构问题——`runtime/sdk/__init__.py` 同时装着**纯值对象**
（HookRequest / HookReply / IdentitySnapshot / SettingsSnapshot / SessionSnapshot /
TurnSnapshot）和**运行 Executor**（SDKExecutor 会在 import 期拉 `aidev_agent`），于是
`runtime.auth`、`runtime.service`、`runtime.runner` 想拿一个 dataclass 就不得不延迟
import 整个 SDK 执行体。用户要求：若存在循环引用就重设计，而不是保留遍地延迟 import。

Output：`runtime/models.py`（纯值对象）、`runtime/boundary.py`（IPC 常量）、
`runtime/terminal.py`（轻量 AG-UI 边界），`runtime/sdk/executor.py`（执行体），
`runtime/sdk/__init__.py` 退化为零 SDK 依赖的薄导出面；全包函数内 import 清零；
保护顺序（stdout guard → 环境 → 出网 → OTel → SDK import）集中到
`infrastructure.startup.install_worker_startup`。

**行为不变**：模型故障语义、强制终止语义、三协议帧格式、734 个测试全部保持。
**禁止**：不改 `/data/workspace/ai-agent-up-langgraph-v1` 任何代码；不 `git reset`；
不建 worktree；不改 `/data/workspace/agent-security` 之外的路径。
</objective>

<execution_context>
@$HOME/.codebuddy/get-shit-done/workflows/execute-plan.md
@$HOME/.codebuddy/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/PROJECT.md
@.planning/ROADMAP.md
@.planning/STATE.md

工作目录：`/data/workspace/agent-security/src/plugins/aidev_agent_cli`（git repo 根是
`/data/workspace/agent-security`）。基线：branch `feature/agent-hook`，commit `3d8b12922`，
`git status` 干净。

## 现状审计（本次实测：全包函数内 import **65** 处）

**基线数字（AST 实测，可直接复现）**：全包 `aidev_agent_cli/` 下函数体内 import 共 **65** 处；
按下面的白名单分类后，**必须修 34 处**、**保留 31 处**（有明确时序/可选依赖/冷路径理由）。
执行器可用 Task 2 的 verify 脚本在改动前后各跑一次，期望 `UNJUSTIFIED: 34 → 0`。

| 文件 | 行 | 现状 | 判定 |
|------|----|------|------|
| `runtime/sdk/__init__.py` | 118,124-127,133-140,155,240,273,275 | `os`、4 个 infrastructure、6 个 sdk/父层、`replace`、`install_stdout_guard` | **结构问题**：值对象与执行体同居，迫使下游延迟 import |
| `runtime/runner.py` | 35,37,38,44,47,100,103,127,145,150 | `asdict`/`replace`/`LocalHistory`/`ProcessWorker`/`Run`/`.sdk`/`.sdk.events` | 冗余（无环）+ `sdk.events` 拉 SDK |
| `runtime/service.py` | 117,233 | `.sdk` 值对象、`LocalDeclarations` | 冗余（`.sdk` 只是被 `sdk.session` 连带拉 SDK） |
| `runtime/auth.py` | 39 | `from .sdk import IdentitySnapshot` | **潜在环**：`.sdk` → `.worker`/`.contracts`，将来加一行就成环 |
| `runtime/inputs.py` | 13,26,43 | `.errors`（顶部已有 `.errors` 可用） | 纯冗余 |
| `runtime/contracts.py` | 208 | `uuid4` | 纯冗余 |
| `runtime/worker.py` | 138,237,239 | `install_from_env`、`ctypes`、`spawn_guard_descriptor` | `network_guard` 是 stdlib-only，可顶部 |
| `runtime/sdk/events.py` | 204,205,222,236,237,239 | `ag_ui.core.Event`、`TypeAdapter`、`AGUISessionWriter`、`uuid`、`datetime`、`BaseSessionWriter` | 本就在 worker 内，全部可顶部 |
| `runtime/sdk/session.py` | 389,433 | `RetryState`、`local_session_writer` | 本就在 worker 内，可顶部 |
| `runtime/sdk/factory.py` | 108,109 | `message_handler_factory`、`InMemoryQueueMessageHandler` | 本就在 worker 内，可顶部 |
| `infrastructure/open_telemetry.py` | 96-98 | `aidev_agent.packages.opentelemetry.*` | **真正的可选依赖**：保持"未安装可用" |
| `infrastructure/environment.py` | 61 | `bkapi_client_core.config` | 真可选（`except ImportError: return`） |
| `infrastructure/network_guard.py` | 376,388,389 | `DupFd`、`runpy`、`sys` | `__main__` 块的 `runpy`/`sys` 是脚本冷路径；`DupFd` 可顶部 |
| `infrastructure/logging.py` | 18 | docstring 示例代码块内的 `from logging import getLogger` | **误报**，AST 天然不匹配，无需改 |
| `adapters/agui.py` | 151,447,499,500 | `SessionOptions`、`import aidev_agent`、`startup`、`build_runtime` | 入口冷路径 + guard 之后 |
| `adapters/acp.py`/`acp_dsh.py` | — | 已全部顶部 | 仅需改 `HookReply`/`HookConfig` 的**来源模块** |
| `__main__.py` | 58,66,109,120,130,136,138 | argparse 冷路径 + guard 之前/之后的 adapter | guard 之前的必须留 |
| `__init__.py` | 13 | `. __main__` | re-export，保留 |
| `resources/loaders/platform.py` | 30,96 | `AgentResourceManager`、`BaseResourceManager` | **原因写在 docstring 第 20-29 行**：platform.py 必须早于 manager.py 导入，模块级会成环 |
| `resources/manager.py` | 518 | `McpToolsResult` | 本模块**顶部已 import SDK**（`AgentResourceManager`） |

## 目标结构

```
runtime/
  models.py      # 纯值对象 + event_payload（仅 stdlib + .contracts + .inputs）
  boundary.py    # 纯常量（仅 stdlib）
  terminal.py    # 纯 stdlib 字典 builder（零 SDK）；sdk.events 反过来用它构造模型
  contracts.py   # 顶部 uuid4
  auth.py        # 顶部 from .models import IdentitySnapshot
  inputs.py      # 顶部 from .errors import RuntimeFault
  worker.py      # 顶部 install_from_env / spawn_guard_descriptor / ctypes
  runner.py      # 顶部全部 + from .terminal import ...
  service.py     # 顶部全部（含 from .models import ...）
  run.py / runs.py  # from .models / .boundary
  sdk/
    __init__.py  # 只 re-export models + build（零 SDK 依赖）
    executor.py  # SDKExecutor + build，顶部全部 SDK 依赖
    session.py / events.py / factory.py / hooks.py / ...
resources/
  loaders/base.py   # 新：纯值对象 ResourceOverrides / ResourceLoader（无 SDK），供 sdk.session 顶部引用
  loaders/platform.py  # 顶部 from .base import BaseResourceManager（从 .base 转出，不再从 SDK 取）
infrastructure/
  startup.py     # 新增 install_worker_startup(env)
```

## 关键接口（执行器直接照此写，不必再探索）

```python
# aidev_agent_cli/runtime/models.py
event_payload(event) -> Any                       # 原 sdk/__init__.py:15
@dataclass(frozen=True) class IdentitySnapshot    # username, access_token(repr=False), revision
@dataclass(frozen=True) class SettingsSnapshot    # cwd, env(repr=False), virtual_mode, revision
@dataclass(frozen=True) class SessionSnapshot     # session_code, agent_code, resources, identity, settings,
                                                  # model, base_url, auth_headers(repr=False), temperature, max_tokens, tool_factories
@dataclass(frozen=True) class TurnSnapshot        # session, kind, correlation_id, run_id, input, execute_options, hooks
                                                  # def payload(self) -> dict  (asdict(self))
@dataclass(frozen=True) class HookRequest         # line_id, params, timeout_ms
@dataclass(frozen=True) class HookReply           # status: Literal["reply","timeout","cancelled"], payload
HookHandler = Callable[[HookRequest, CancelToken], Awaitable[HookReply]]
```

约束：`runtime/models.py` 只允许 `import copy/asyncio/threading/stdlib`、`from dataclasses import ...`、
`from typing import ...`、`from .contracts import CancelToken`、`from .inputs import ChatInput, ContextInput, ResumeInterrupt`。
**不得**出现 `.worker` / `.sdk` / `..infrastructure` / `..resources`。

## 上游事实（已核对，勿再猜）

- `runtime.service` **当前**fresh import 已不拉 SDK（实测 0 个 `aidev_agent.*`/`ag_ui`/`pydantic`）——
  因为 `service.py:117` 的 `.sdk` 是函数内。改顶部后必须**仍然**为 0，这是本任务最容易回归的点。
- `runtime.sdk.events` 顶层 import `ag_ui.core` + `aidev_agent.*`；
  `runtime.sdk.hooks` 顶层 import `ag_ui.core` + `aidev_agent.core.hooks`。
  所以 parent 侧**不得**在模块级引用这两者（`runner.py:145/150`、`acp.py:41`、`acp_dsh.py:45`）。
- `runtime.sdk.session` 顶层 import `resources.manager` → `aidev_agent.packages.resource_manager`
  与 `resources.loaders.base` → `resources.manager`。
  `resources/manager.py` 顶部**已经**import SDK，因此 sdk 侧把 `ResourceOverrides` / `HeadlessLedger` 顶部化
  **不会**新增 SDK 传播面（它们本来就一起被拉起）。这条不扩大改动。
- `resources/loaders/platform.py` 的延迟 import 有**明确防环理由**（docstring 20-29 行）：
  `platform.py` 必须早于 `manager.py` 被导入，模块级 `from ..manager import` 会成环。
  本次把 `BaseResourceManager` 从 SDK 直取改为从同层 `loaders/base.py` 取，即可消除这两处。
- 干净基线全量：`PYTHONPATH=$PWD .venv/bin/python -m pytest -q -o addopts="" --maxfail=0`
  → 734 passed（约 767s）。`ruff check .` → All checks passed。
- 测试注入路径：`tests/runtime/sdk_worker_fixtures.py:install_sdk_faults` 会 patch
  `SDKSession.close / ensure_platform_session / _build_agent / _build_agent_via_resume`。
  `WorkerSpec` 工厂串在第二轮改为 `aidev_agent_cli.runtime.sdk.executor:build`——所有测试
  fixture 的目标同步到新串，**不留** `runtime.sdk:build` 兼容面。
</context>

<tasks>

<task type="auto">
  <name>Task 1: 抽出 runtime.models / boundary / terminal，把值对象与 SDK 执行体物理分离</name>
  <files>
    aidev_agent_cli/runtime/models.py,
    aidev_agent_cli/runtime/boundary.py,
    aidev_agent_cli/runtime/terminal.py,
    aidev_agent_cli/runtime/sdk/__init__.py,
    aidev_agent_cli/runtime/sdk/executor.py,
    aidev_agent_cli/runtime/auth.py,
    aidev_agent_cli/runtime/contracts.py,
    aidev_agent_cli/runtime/inputs.py,
    aidev_agent_cli/runtime/service.py,
    aidev_agent_cli/runtime/runner.py,
    aidev_agent_cli/runtime/run.py,
    aidev_agent_cli/runtime/runs.py
  </files>
  <action>
**这一步是本次重构的骨架：先切模块，再把所有使用点改成顶部 import。**

1. **新建 `runtime/boundary.py`**（纯 stdlib，无本包 import）。把 `worker.py:18-20` 的三个
   常量搬过来：`MAX_FRAME_BYTES = 256 * 1024`、`CANCEL_GRACE_SECONDS = 2.0`、
   `CALLBACK_CLEANUP_SECONDS = 0.1`。`worker.py` 改为
   `from .boundary import CANCEL_GRACE_SECONDS, MAX_FRAME_BYTES, CALLBACK_CLEANUP_SECONDS`
   并写 `__all__` 保持原有可导出名不变（`runs.py` 与 `service.py` 都从 `.worker` / `.runs`
   取 `CANCEL_GRACE_SECONDS`，那条再导出链**保持不变**，避免动外部可见面）。

2. **新建 `runtime/models.py`**。把 `runtime/sdk/__init__.py` 第 15-79 行整段搬过来：
   `event_payload`、`IdentitySnapshot`、`SettingsSnapshot`、`SessionSnapshot`、`TurnSnapshot`、
   `HookRequest`、`HookReply`、`HookHandler`。**逐字搬，不改字段、默认值、`repr=False`、
   `payload()` 实现**——这些值对象会 `asdict()` 后进 IPC 快照，字段名变了就是协议变更。
   import 头部按上面 `<context>` 的「关键接口」约束写：
   `import copy`、`from dataclasses import asdict, dataclass, field`、
   `from typing import Any, Awaitable, Callable, Literal`、
   `from .contracts import CancelToken`、
   `from .inputs import ChatInput, ContextInput, ResumeInterrupt`。
   **不要** import `.worker`（`WorkerContext` 只在 executor 里用）。

3. **新建 `runtime/terminal.py`** —— parent 侧的**纯 stdlib** 事件形状（第二轮修正：不再是
   re-export，因为 re-export 会把 SDK 拉回父进程）：
   ```python
   def build_cancel_terminal_events(thread_id) -> tuple[dict, dict]
   def build_error_event(message) -> dict
   def build_finished_event(thread_id, run_id) -> dict
   ```
   键名用线上别名（``threadId`` / ``runId``）、``timestamp`` 为墙钟毫秒、成功终止的
   ``outcome`` 为 ``{"type": "success"}``——与 SDK ``model_dump(mode="json",
   by_alias=True, exclude_none=True)`` 逐键相同（由 ``tests/runtime/test_terminal_events.py`` 比对）。
   ``runtime/sdk/events.py`` 的同类 builder 改为调用它（形状只有一份事实来源）。
   旧版本条写的是「re-export sdk.events 的 builder」，那会让 ``runner → terminal`` 把 SDK
   拉进 ``service`` 的 fresh import，属于作废方案。
   ```python
   """AG-UI 终止事件的轻量边界：parent 只经此取 builder，不加载 SDK writer。"""
   from __future__ import annotations
   from .sdk.events import build_cancel_terminal_events, build_error_event, build_finished_event
   __all__ = ["build_cancel_terminal_events", "build_error_event", "build_finished_event"]
   ```
   模块 docstring 必须写明：**本模块会连带加载 SDK，只允许 runtime 的 runner 引用；
   不得从 `service` / `contracts` / `models` / `auth` 引用。**

4. **新建 `runtime/sdk/executor.py`**。把 `runtime/sdk/__init__.py` 第 82-285 行整体搬来：
   `_Cancel`、`_HookChannel`、`_tool`、`SDKExecutor`、`build`。**所有函数内 import 提到模块顶部**：
   ```python
   import copy, importlib, os
   from dataclasses import asdict, replace
   from typing import Any, Literal
   from ...infrastructure.environment import bootstrap_environment
   from ...infrastructure.network_guard import install_from_env
   from ...infrastructure.open_telemetry import install_otel
   from ...infrastructure.startup import install_worker_startup
   from ...infrastructure.stdio import harden_sys_path, install_stdout_guard
   from ..auth import AuthManager
   from ..context import ProcessConfig
   from ..contracts import LaunchOptions
   from ..history import LocalHistory
   from ..models import HookRequest
   from ..worker import WorkerContext
   from .factory import validate_message_handler
   from .hooks import HookConfig
   from .session import SDKSession
   from .workspace import install_workspace_agent
   ```
   **`SDKExecutor.__init__` 的保护顺序必须逐字保留**（这是本 worker 的正确性契约）：
   ```
   os.environ.clear(); os.environ.update(settings["env"]); os.chdir(settings["cwd"])
   install_worker_startup(settings["env"])      # bootstrap → guard → 出网 → harden_sys_path → OTel
   validate_message_handler(); install_workspace_agent()
   ```
   删除重复的 `os.chdir(settings["cwd"])`（第 155-157 行那次）——同一个 cwd 设两遍没有第二次效果，
   但保留第一处（第 120-123 行）为唯一设置点。`build()` 里的 `os.dup2` / `os.open` /
   逻辑**逐字保留**（抽成 `infrastructure.stdio.apply_worker_stdio_guard`），只是 import 提到顶部。
   `execute()` 里第 240 行的 `from dataclasses import replace` 提到顶部并用顶部名。

5. **`runtime/sdk/__init__.py` 退化为薄导出面**（≤40 行、零 SDK 依赖）：
   ```python
   """SDK 执行边界的空包根：不 re-export 任何符号。"""
   from __future__ import annotations
   __all__: list[str] = []
   ```
   **第二轮修正：任何 re-export 都不要。** 包根 ``runtime/sdk/__init__.py`` 现在是**空包**
   （只有 ``__all__ = []``），工厂串直接指向执行体模块
   ``aidev_agent_cli.runtime.sdk.executor:build``；``_worker_entry`` 在 ``import_module``
   之前完成 env 发布 / 出网守卫 / ``install_worker_startup``，因此"装载时机"由
   ``_worker_entry`` 的调用顺序决定，不需要一个函数内 import 来充当边界。
   旧版本条要求的"唯一函数内 import"是作废方案：它让包根继续扮演 SDK 门禁。
   装载时机由 `_worker_entry` 保证：env 发布 → 出网守卫 → `install_worker_startup`
   （bootstrap / stdout guard / harden_sys_path / OTel）**之后**才 `import_module`。

6. **改使用点（全部**顶部** import，删掉函数内的）**：
   - `auth.py:39` → 顶部 `from .models import IdentitySnapshot`（**这消除潜在环**）。
   - `contracts.py:208` → 顶部 `from uuid import uuid4`。
   - `inputs.py:13/26/43` → 顶部 `from .errors import RuntimeFault`。（确认 `.errors` 只依赖
     `typing`，不反向 import `.inputs`；`grep -n "^from\|^import" aidev_agent_cli/runtime/errors.py`
     必须为空。）
   - `service.py:117` → 顶部 `from .models import IdentitySnapshot, SessionSnapshot, SettingsSnapshot, TurnSnapshot`；
     第 233 行 `LocalDeclarations` → 顶部 `from ..resources.local_config import LocalDeclarations`
     （`local_config` 是 stdlib-only + `.logging`，`service` 顶部已有 `..infrastructure.settings`
     与 `..resources.sources`，无新增 SDK 面）。
     **`service.py` 修完必须重新做 fresh-import 断言**（见 Task 2 verify）。
   - `run.py:6` → `from .models import HookHandler, TurnSnapshot`。
   - `runs.py:12` → `from .models import HookHandler, TurnSnapshot`；第 14 行
     `from .worker import CANCEL_GRACE_SECONDS, WorkerReapError` 保持（`worker` 届时已 stdlib-only）。
   - `runner.py`：删掉 35/37/38/44/47/100/103/127 的 8 处，全部提顶部：
     `from dataclasses import asdict, replace`；`from .history import LocalHistory`；
     `from .models import HookReply, HookRequest, TurnSnapshot, event_payload`；
     `from .run import Run`；`from .terminal import build_cancel_terminal_events, build_error_event`；
     `from .worker import ProcessWorker, WorkerSpec, stop_callback`。
     第 145/150 的 `.sdk.events` 改为引用顶部 `terminal` 的名字。
     **注意**：`runner` 顶部 import `.run` 与 `.run` 顶部 import `.models`/`.worker` 之间无环
     （`run.py` 不 import `runner`），但 `runs.py` 顶部 import `runner`——所以 `runner` **不得**
     在模块级 import `runs`。现状满足，改完保持满足。

**不改**：任何 dataclass 字段、任何 IPC 帧形状、任何错误码、任何阶段名
（`"created" / "building" / "running" / "exited"`）。
  </action>
  <verify>
    <automated>
cd /data/workspace/agent-security/src/plugins/aidev_agent_cli && PYTHONPATH=$PWD .venv/bin/python - <<'PY'
import ast
from pathlib import Path
root = Path("aidev_agent_cli")
fn_level = []
for p in sorted(root.rglob("*.py")):
    if "__pycache__" in p.parts: continue
    tree = ast.parse(p.read_text(encoding="utf-8"))
    at_module = set()
    for stmt in tree.body:
        # 函数/类体里的 import 不算模块级；类体里的注解导入本包没有
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(stmt):
            at_module.add(sub)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in at_module:
            fn_level.append(f"{p}:{node.lineno}")
print("FN-LEVEL IMPORTS:", len(fn_level))
print("\n".join(fn_level))
# Task 1 结束时唯一允许的遗留：sdk/__init__.build 的 import-target 装载
allowed_prefixes = ("aidev_agent_cli/runtime/sdk/__init__.py:",)
bad = [x for x in fn_level if not x.startswith(allowed_prefixes)]
assert bad == [], bad
PY
    </automated>
  </verify>
  <done>
- `runtime/models.py`、`runtime/boundary.py`、`runtime/terminal.py`、`runtime/sdk/executor.py` 存在。
- `runtime/sdk/__init__.py` 是空包：模块级 import 只有 `__future__`，`__all__ == []`。
- 全包函数/类体内 `import` **为 0**（零豁免）。
- `from aidev_agent_cli.runtime.auth import AuthManager` 的模块级依赖里没有 `.sdk`：
  `grep -n "sdk" aidev_agent_cli/runtime/auth.py` 无输出。
- `runtime.service` fresh import 仍不拉 SDK（Task 2 的断言脚本可先在本地手工跑通）。
- 值对象的 `dataclasses.fields()` 与重构前逐项相同（字段名、顺序、默认值、`repr=False`）。
  </done>
</task>

<task type="auto">
  <name>Task 2: 收敛 infrastructure / worker / resources / entry 的 import，并把启动顺序收成一个显式入口</name>
  <files>
    aidev_agent_cli/infrastructure/startup.py,
    aidev_agent_cli/infrastructure/open_telemetry.py,
    aidev_agent_cli/infrastructure/environment.py,
    aidev_agent_cli/infrastructure/network_guard.py,
    aidev_agent_cli/__main__.py,
    aidev_agent_cli/adapters/agui.py,
    aidev_agent_cli/adapters/acp.py,
    aidev_agent_cli/adapters/acp_dsh.py,
    aidev_agent_cli/resources/loaders/base.py,
    aidev_agent_cli/resources/loaders/platform.py,
    aidev_agent_cli/resources/manager.py,
    aidev_agent_cli/runtime/worker.py,
    aidev_agent_cli/runtime/sdk/session.py,
    aidev_agent_cli/runtime/sdk/events.py,
    aidev_agent_cli/runtime/sdk/factory.py
  </files>
  <action>
**这一步处理"真正必须晚"的四类，其余全部顶部化。判断标准只有一条：被导入模块在
import 期是否有副作用或拉 SDK。**

1. **`infrastructure/startup.py` 新增 `install_worker_startup(env: dict[str, str]) -> None`**，
   把 worker 启动顺序**集中到一个函数**（顺序是本 worker 的正确性契约，必须逐条保留）：
   ```python
   def install_worker_startup(env):
       """worker 启动顺序：环境 → stdout guard → 出网限制 → sys.path 加固 → OTel。"""
       bootstrap_environment(env)   # SDK import 期要读 settings
       install_stdout_guard()       # 之后 SDK 导入期的 print 才落到 stderr
       install_from_env()
       harden_sys_path()
       install_otel(env)            # 必须在首次 aidev_agent import 之前 monkey-patch
   ```
   与现有 `install_startup_hooks()` 的差异只在**少了 `configure_logging()` 与 `TuiConfig.read()`**
   （worker 的 env 来自快照而非配置文件）。docstring 必须写明两者关系：
   *入口用 `install_startup_hooks`（读配置、装日志）；worker 用 `install_worker_startup`
   （env 来自 JSON 快照、日志已由父进程建立）。两者共用同一组底层步骤，顺序一致。*
   两个函数的模块级 import 都放顶部（`startup.py` 现状已是顶部，保持）。

2. **`infrastructure/open_telemetry.py`** —— 这是**真正的可选依赖**，保持"未安装也可用"，
   但把"延迟"从分散改为**一处集中**：
   - 顶部 import 全部**非 SDK** 依赖（已有 `os`、`getLogger`）。
   - 在模块顶部加一个集中的可选装载点：
     ```python
     def _sdk_otel():
         """SDK 的 OTel 组件（可选依赖）。未安装时抛 ImportError，由 install_otel 降级。"""
         from aidev_agent.packages.opentelemetry import OTelConfig
         from aidev_agent.packages.opentelemetry.instrumentor import BkAidevAgentInstrumentor
         from aidev_agent.packages.opentelemetry.utils import (
             get_otel_endpoint_by_env, get_otel_endpoint_by_json_str)
         return OTelConfig, BkAidevAgentInstrumentor, get_otel_endpoint_by_env, get_otel_endpoint_by_json_str
     ```
   - `install_otel` 第 96-101 行改为 `OTelConfig, BkAidevAgentInstrumentor, get_otel_endpoint_by_env, get_otel_endpoint_by_json_str = _sdk_otel()`，
     **try/except 结构与降级日志逐字保留**（`except Exception` → 一行 info + `return False`）。
   - 理由写进 docstring：*OTel 是可选依赖；在这里集中一次可选装载，而不是让
     `infrastructure.startup` / `sdk.executor` / adapters 各自延迟 import。*
   - **这条 import 计入白名单**（`_sdk_otel` 函数体），因为它是"未安装时可用"的契约本身。

3. **`infrastructure/environment.py:61`** 的 `bkapi_client_core` 是**真可选**
   （`except ImportError: return`，SDK 装不上也要能跑）。提到顶部：
   ```python
   try:
       from bkapi_client_core import config as bkapi_config
   except ImportError:  # pragma: no cover - SDK always ships this
       bkapi_config = None
   ```
   `detach_bkapi_from_django` 改为 `if bkapi_config is None: return`。
   **语义不变**：导入失败仍然静默返回，不新增任何日志/异常。

4. **`infrastructure/network_guard.py`**：`DupFd`（376 行，`multiprocessing.reduction` 是 stdlib）
   提到顶部；`if __name__ == "__main__":` 块里的 `runpy` / `sys` 是**脚本冷路径**，
   保持函数内（AST 无法也无需消除）——**这是白名单第二条**。

5. **`runtime/worker.py:138/237/239`** → 顶部：
   `import ctypes`、`from ..infrastructure.network_guard import install_from_env, spawn_guard_descriptor`。
   `network_guard` 届时已 stdlib-only，`worker` 因此仍**不拉 SDK**（这是父进程 IPC 层的关键约束）。

6. **`runtime/sdk/{session,events,factory}.py`**：这些模块本来就只在 worker 内、且**顶部已
   import SDK**，函数内 import 纯属遗留。全部提到顶部：
   - `session.py:389` `RetryState` → 顶部 `from aidev_agent.packages.langchain_core.models.llm_retry import RetryState`；
     第 433 行 `local_session_writer` → 已在 `events` 的顶部 import 里补上（与 19 行合并）。
   - `events.py:204/205` `ag_ui.core.Event`、`pydantic.TypeAdapter` → 与第 9 行合并
     `from ag_ui.core import Event, EventType, RunErrorEvent, RunFinishedEvent`、
     新增 `from pydantic import TypeAdapter`；`222` `AGUISessionWriter`、`239` `BaseSessionWriter`
     → 顶部；`236/237` `uuid`、`datetime` → 顶部。
     **注意**：`_coerce_event` 的 `try/except` 仍然要包住 **调用**（`TypeAdapter(Event).validate_python(event)`），
     只把 import 移出 try——这是行为等价所必需的（try 保护的是校验失败，不是导入失败）。
   - `factory.py:108/109` → 顶部。

7. **`resources/loaders/base.py`**：新增同层转出，消除 `platform.py` 的两处延迟 import 的**根因**。
   在 `base.py` 顶部加
   `from aidev_agent.packages.resource_manager.base import BaseResourceManager` 并在 `__all__`
   里导出它（`base.py` 是 loaders 的叶子，顶部已无 SDK 依赖；这一行会把它变成"顶部带 SDK"，
   与 `manager.py` 现状同级，不新增传播面）。
   然后 `platform.py:30` / `platform.py:96` 改为顶部 `from .base import BaseResourceManager`
   与顶部 `from aidev_agent.packages.resource_manager.agent import AgentResourceManager`。
   **改完必须复核**：`python -c "import aidev_agent_cli.resources.loaders.platform"` 与
   `python -c "import aidev_agent_cli.resources.manager"` 都成功（docstring 20-29 行担心的
   是 `platform → manager` 反向成环，改后方向是 `platform → base`，不成环）。
   若实测成环，**不要**退回函数内 import——把 `AgentResourceManager` 的判定改为结构化判据
   （`__mro__` 名字比较），并在 action 中记录原因。

8. **`__main__.py`** —— 只留**真正必须在 guard 之前**的：
   - 保留：`main()` 内 `from .infrastructure.startup import install_startup_hooks`（第 109 行，
     guard 安装的发起者，必须晚于 argv 解析）、`from .runtime.service import build_runtime`（120）、
     `_import_adapter` 的三个（130/136/138）。
   - 提到顶部：第 58 行 `parse_resources`、第 66 行 `PLATFORM_SOURCES, SOURCES`
     （`resources/sources.py` 是 stdlib-only 叶子，docstring 明写"只依赖标准库，
     入口在装 stdout guard 之前就要用它"）。
   - 顶部新增 `from .infrastructure.startup import install_startup_hooks` 是**禁止**的——
     `startup` 顶部 import 了 `logging`（导入期打开日志文件）与 `network_guard`；
     保持第 109 行在 `main()` 内。**这是白名单第三条，理由写进注释。**
   - `_import_adapter` 的 docstring 补一句：*acp 未安装时返回 None；这就是为什么不能用顶部
     try/except import——顶部失败的 module 会进 `sys.modules` 负缓存，且 protocol 选择在 argv 之后。*

9. **`adapters/agui.py`**：第 151 行 `SessionOptions` / `new_session_code` 提顶部
   （`runtime/contracts.py` 届时 stdlib-only）；第 447、499、500 行在 `__main__` 块/inner 入口，
   保留并补注释：447 是"adapter 顶层已 import SDK，这行只是探测 SDK 是否可用"；
   499/500 是"嵌入式入口必须先装 guard 再建 runtime"。
   `adapters/acp.py:41` `from ..runtime.sdk import HookReply` → `from ..runtime.models import HookReply`；
   `adapters/acp.py:42` / `acp_dsh.py:45` `from ..runtime.sdk.hooks import HookConfig` 保持
   （adapter 顶层已拉 acp SDK + `aidev_agent`，`sdk.hooks` 不新增面；且 `HookConfig` 是 SDK 点位
   配置，不属于纯值对象）。docstring 里写明这条边界。
   为了让第 9 条的判定在**审计测试里可解释**，在 `runtime/sdk/hooks.py` 顶上补一行注释：
   *`HookConfig` 解析 SDK hook 点位，属于 SDK 边界；纯值对象在 `runtime.models`。*

10. **`resources/manager.py:518`** `McpToolsResult` 提顶部（本模块顶部已有 SDK）。

**不改**：`infrastructure/logging.py`（第 18 行是 docstring 示例，AST 不匹配）。
  </action>
  <verify>
    <automated>
cd /data/workspace/agent-security/src/plugins/aidev_agent_cli && PYTHONPATH=$PWD .venv/bin/python - <<'PY'
import ast, subprocess, sys
from pathlib import Path

root = Path("aidev_agent_cli")
fn_level = []
for p in sorted(root.rglob("*.py")):
    if "__pycache__" in p.parts:
        continue
    tree = ast.parse(p.read_text(encoding="utf-8"))
    at_module = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(stmt):
            at_module.add(sub)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in at_module:
            fn_level.append(f"{p}:{node.lineno}")

# 零豁免：函数/类体内不得有任何 import（可选依赖与动态边界只走 importlib.import_module）。
print("TOTAL FN-LEVEL:", len(fn_level))
print("\n".join(fn_level))
assert fn_level == [], fn_level

# fresh import：核心层逐个不得连带 SDK
mods = ["service", "runner", "runs", "run", "auth", "models", "boundary",
        "worker", "contracts", "inputs"]
probe = (
    "import sys, importlib\n"
    "m = sys.argv[1]\n"
    "importlib.import_module('aidev_agent_cli.' + m)\n"
    "bad = [n for n in sys.modules if n.startswith(('aidev_agent.', 'ag_ui', 'acp', 'pydantic'))]\n"
    "assert not bad, (m, bad[:8])\n"
    "print(m, 'clean')\n"
)
for m in mods:
    out = subprocess.run([sys.executable, "-c", probe, m],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, f"{m} leaked SDK:\n{out.stderr[-1500:]}"
    print(out.stdout.strip())
print("BOUNDARY OK")
PY
    </automated>
  </verify>
  <done>
- 全包函数内 import 只剩白名单六类，且每类都在代码注释里写明了"为什么必须在函数内"。
- `runtime.service / .runner / .runs / .run / .auth / .models / .boundary / .worker / .contracts / .inputs`
  逐个 fresh import 均**不出现** `aidev_agent.*` / `ag_ui` / `acp` / `pydantic`。
- `install_worker_startup` 存在，`sdk/executor.py` 只经它完成启动顺序，
  且 bootstrap → guard → 出网 → harden_sys_path → OTel 的顺序与 `install_startup_hooks` 一致。
- `import aidev_agent_cli.runtime.sdk` 在**没有 SDK** 的临时 venv 里也能成功
  （只 import `models` + 定义 `build`）：用
  `PYTHONPATH=$PWD .venv/bin/python -X importtime -c "import aidev_agent_cli.runtime.sdk"`
  的 stderr 里不出现 `aidev_agent` / `ag_ui` 即可作为本机等价证据。
- `resources.loaders.platform` 与 `resources.manager` 都能独立 import，无环。
  </done>
</task>

<task type="auto">
  <name>Task 3: 把"函数内 import 为零 / 无环 / fresh import 无 SDK 副作用"写成可执行审计，并跑全量回归</name>
  <files>
    tests/test_layer_graph.py,
    tests/integration/_paths.py
  </files>
  <action>
**把 Task 1/2 的验收变成仓库里的常驻断言，防止下次又长回来。**

1. **`tests/integration/_paths.py` 新增 `plugin_pythonpath()`**：
   ```python
   def plugin_pythonpath() -> str:
       """子进程用的 PYTHONPATH：插件根 + SDK 根（未装 wheel 时两侧都要可解析）。"""
       return os.pathsep.join([str(PLUGIN_ROOT), str(AGENT_ROOT), os.environ.get("PYTHONPATH", "")])
   ```
   供下面所有子进程断言复用，**不要**在各测试里各写一遍。

2. **`tests/test_layer_graph.py` 新增三个测试**（沿用本文件现有的 AST + subprocess 风格）：

   **a) `test_no_function_level_imports_outside_declared_boundaries()`**
   用 Task 2 verify 里那段 AST 脚本的逻辑（模块级 = `tree.body` 及其 `ast.walk`；
   其余节点即函数内），断言"未声明白名单之外为零"。白名单**显式写成常量**
   **零豁免**：不做按文件白名单；`importlib.import_module` 的装载点单独由
   `DYNAMIC_IMPORT_POINTS` 登记理由（可选依赖 / 进程边界 / 可选 extra）。
   断言失败信息里输出 `文件:行号` 列表。

   **b) `test_runtime_core_imports_close_no_cycle()`**
   确定性环审计（不依赖运行期形态）：从 `aidev_agent_cli/runtime` 起做模块级 import 图的
   DFS（只取 `ast.ImportFrom` + `ast.Import`，用 `importlib.util.find_spec` 或路径存在性
   解析相对 import），断言 `runtime.auth` **不**（含间接）到达 `runtime.sdk`，
   且 `runtime.service` 也不到达 `runtime.sdk` / `runtime.terminal`。
   显式列出"必须互不引用"的边：`models → worker/sdk/infrastructure/resources` 为空，
   `auth → sdk` 为空，`service → sdk` 为空。

   **c) `test_runtime_core_has_no_sdk_side_effects_on_fresh_import()`**
   用 `_paths.plugin_pythonpath()` 起子进程，一次跑完全部核心模块：
   ```python
   code = (
       "import sys, importlib\n"
       "mods = ['aidev_agent_cli.runtime.service','aidev_agent_cli.runtime.runner',"
       "'aidev_agent_cli.runtime.runs','aidev_agent_cli.runtime.run','aidev_agent_cli.runtime.auth',"
       "'aidev_agent_cli.runtime.models','aidev_agent_cli.runtime.boundary',"
       "'aidev_agent_cli.runtime.worker','aidev_agent_cli.runtime.contracts',"
       "'aidev_agent_cli.runtime.inputs']\n"
       "for m in mods: importlib.import_module(m)\n"
       "bad = [n for n in sys.modules if n.startswith(('aidev_agent.','ag_ui','acp','pydantic'))]\n"
       "assert not bad, bad\n"
       "print('BOUNDARY OK')\n"
   )
   ```
   注意 `runtime.terminal` **会**拉 `sdk.events`（它 import `ag_ui.core`），所以**不要**把它放进
   这一条断言；单独一条 `test_terminal_boundary_is_the_only_parent_sdk_edge()` 断言
   `runtime.terminal` 只被 `runtime.runner` 引用（AST 扫描），说明它是有意保留的唯一 parent→SDK 边。
   `timeout=120`，`cwd=str(ROOT.parent)`，`env` 里带 `AGENT_TUI_OTEL=0`（与 `conftest` 一致）。
   失败时打印 `result.stderr[-2000:]`。

   **d) `test_sdk_package_root_is_sdk_free()`**
   子进程断言 `import aidev_agent_cli.runtime.sdk` 后 `aidev_agent.config`、`ag_ui` **不在**
   `sys.modules`（即 sdk 根 __init__ 不再拉起 SDK），且 `build`、`TurnSnapshot`、`HookReply`
   均可取到。这条直接锁住 Task 1 的结构意图。

3. **`test_importing_the_package_pulls_neither_the_sdk_nor_a_runtime`** 保持原样不动
   （它是启动延迟契约，改后仍应通过）。

**不要**把这些断言写成 skip-able；失败就是失败。
  </action>
  <verify>
    <automated>
cd /data/workspace/agent-security/src/plugins/aidev_agent_cli && PYTHONPATH=$PWD .venv/bin/python -m pytest -q -o addopts="" tests/test_layer_graph.py -v && PYTHONPATH=$PWD .venv/bin/python -m ruff check . && PYTHONPATH=$PWD .venv/bin/python -m compileall -q aidev_agent_cli tests && echo "GUARDS OK"
    </automated>
  </verify>
  <done>
- `tests/test_layer_graph.py` 含五个新断言（函数内 import 零、无环、核心层无 SDK 副作用、
  terminal 是唯一 parent→SDK 边、sdk 根无 SDK），全部通过且不可 skip。
- `tests/integration/_paths.py` 提供 `plugin_pythonpath()`。
- `ruff check .` All checks passed；`compileall` 无输出。
- 全量回归 `PYTHONPATH=$PWD .venv/bin/python -m pytest -q -o addopts="" --maxfail=0`
  = **734 passed**（与基线 3d8b12922 一致，不允许 skip 换绿）。
- 三协议入口 smoke 通过（`tests/test_entries.py`），真实 spawn/guard/shutdown 用例通过
  （`tests/runtime/test_process_worker.py`、`tests/runtime/test_production_runtime.py`）。
- `git status` 只出现本 PLAN 列出的文件；`/data/workspace/ai-agent-up-langgraph-v1` 未被触碰
  （`git -C /data/workspace/ai-agent-up-langgraph-v1 status --short` 与开工前一致）。
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | 描述 |
|----------|------|
| parent → spawned worker | 只有 import target（`module:name`）与 JSON 快照跨边界 |
| worker → parent IPC | 事件 / hook 请求回复 / phase / result 帧，`MAX_FRAME_BYTES` 上限 |
| SDK import 时序 | 首次 `aidev_agent` import 之前必须完成 stdout guard、环境发布、OTel patch |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-261109-01 | Tampering | 协议 stdout 通道 | mitigate | `install_worker_startup` 把 `install_stdout_guard()` 固定在 SDK 首次 import 之前；`sdk/__init__.build` 是唯一 import-target 入口，`executor` 只在其内部装载。Task 3 的 fresh-import 断言防止有人在模块级 import `ag_ui`/`aidev_agent` 提前拉 SDK。 |
| T-261109-02 | Elevation of Privilege | 首次 SDK import 时的 OTel monkey-patch | mitigate | `install_otel` 保持在 `install_worker_startup` 中、`import` 之前；`_sdk_otel()` 只集中可选装载，不改变 install 时机。 |
| T-261109-03 | Denial of Service | 循环 import 导致 worker 启动崩 | mitigate | Task 3 的确定性无环审计 + fresh-import 子进程断言；`sdk.build` 的延迟装载保留为唯一豁免，并在注释里写明理由。 |
| T-261109-04 | Information Disclosure | 快照中 token / env | mitigate | 值对象搬移时**逐字保留** `field(repr=False)`（`access_token` / `env` / `auth_headers`）；不新增日志。 |
| T-261109-05 | Tampering | 可选依赖缺失（OTel / acp / bkapi） | accept | `_sdk_otel()` 与 `bkapi_config` 的 `ImportError` 降级路径逐字保留；未安装时功能降级为"无 trace / 无 alias 桥接"，是既有设计意图。 |
</threat_model>

<verification>
按顺序执行，任一失败即回改而非放行：

1. `ruff check .` → All checks passed
2. `python -m compileall -q aidev_agent_cli tests` → 无输出
3. `pytest -q -o addopts="" tests/test_layer_graph.py -v` → 全部通过（含五个新断言）
4. 核心层 fresh-import 逐模块：无 `aidev_agent.*` / `ag_ui` / `acp` / `pydantic`
5. `import aidev_agent_cli.runtime.sdk` → `aidev_agent.config`、`ag_ui` 不在 `sys.modules`
6. 真实链路：`pytest -q -o addopts="" tests/runtime/test_process_worker.py tests/runtime/test_production_runtime.py -v`
   → spawn / phase / hook / cancel / forced kill / shutdown / reaping 全绿
7. 三协议入口：`pytest -q -o addopts="" tests/test_entries.py tests/adapters -v`
8. 全量：`PYTHONPATH=$PWD .venv/bin/python -m pytest -q -o addopts="" --maxfail=0`
   → **734 passed**（与基线一致；skip 数不得增加）
9. `git status --short` 只含本 PLAN 的文件；`git -C /data/workspace/ai-agent-up-langgraph-v1 status --short` 无变化
</verification>

<success_criteria>
- `aidev_agent_cli` 下**常规**函数内 import 为零；仅剩六处**声明过理由**的边界
  （sdk `build` 的 import-target、`__main__` 的 guard 之前、agui 的嵌入式冷路径、
  OTel 可选装载、network_guard 的 `__main__` 脚本块、包根 re-export）。
- `auth → sdk → auth` 潜在环**物理消除**：`auth` 只依赖 `runtime.models`；`models` 只依赖
  `contracts` / `inputs` / stdlib；核心层（service / runner / runs / run / worker / contracts /
  inputs / models / boundary）模块级均不出现 `.sdk`。
- `runtime/terminal.py` **不**碰 SDK（纯 stdlib 字典），只被 `runner` 引用；父进程零 SDK。
- SDK 执行体在 `runtime/sdk/executor.py`，顶部 SDK 依赖；worker 只经
  `aidev_agent_cli.runtime.sdk.executor:build` 在 `install_worker_startup` 之后装载。
- OTel / ACP / bkapi 的"未安装可用"契约不变，且"延迟"集中在一处（不散落）。
- 行为零变更：模型故障语义、强制终止语义、IPC 帧、三协议格式、734 tests 全绿。
- 受保护目录 `/data/workspace/ai-agent-up-langgraph-v1` 零改动；未执行 `git reset`、未建 worktree。
</success_criteria>

<output>
After completion, create `.planning/quick/261109-wqo-import-boundaries/01-SUMMARY.md`
</output>
