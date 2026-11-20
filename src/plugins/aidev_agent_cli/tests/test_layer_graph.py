# -*- coding: utf-8 -*-
"""分层依赖图断言：「四职责目录」与「导入边界」都不只是目录名。

判定依据是 AST 与子进程里的 ``sys.modules``，不是口头约定。五组规则：

1. **禁止的层间方向**：``runtime`` 不得 import ``adapters`` / ``sdk``（SDK 执行体只经 import
   target 跨 spawn 装载），``infrastructure`` 不得 import 任何其他层。
2. **契约在 infrastructure**：adapters 只经 ``infrastructure.ports`` / ``runs`` / ``inputs`` 认识运行时，
   不得 import ``runtime`` / ``sdk``。runtime 的**实现**模块不得出现在 resources 的模块级导入里。
3. **编码注入**：``resources`` 不得 import ``adapters``——线上格式由 adapter 注入。
4. **函数内 import 为零**：包内不得出现函数/类体里的 ``import`` / ``from ... import``；
   可选依赖与真正的动态边界一律用 ``importlib.import_module``（集中、显式、可枚举）。
5. **fresh import 无 SDK 副作用**：核心层与 adapter 的独立进程导入都不得拉起
   ``aidev_agent.*`` / ``ag_ui`` / ``acp`` / ``pydantic``。``sdk`` 包根为空包。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "aidev_agent_cli"
LAYERS = ("runtime", "sdk", "adapters", "resources", "infrastructure", "hooks", "utils", "bootstrap")

#: 允许的**模块级**层间方向。adapter 只经 ``infrastructure.ports`` 认识运行时（实现由 ``__main__`` 注入）；
#: ``sdk`` 是 worker 侧执行体，**不得**依赖 runtime（共用值契约在 ``utils``，worker 端点按结构协议注入）。
#: ``utils`` 是叶子：只依赖标准库与自身。
ALLOWED = {
    "runtime": {"runtime", "resources", "infrastructure", "hooks", "utils", "bootstrap"},
    "sdk": {"sdk", "resources", "hooks", "utils"},
    "adapters": {"adapters", "resources", "infrastructure", "hooks", "utils"},
    # hooks 只依赖 utils：取消信号用 hooks.models.CancelSignal 协议表达，不反向依赖 runtime。
    "hooks": {"hooks", "utils"},
    "resources": {"resources", "infrastructure", "utils"},
    "infrastructure": {"infrastructure", "utils"},
    "utils": {"utils"},
    # bootstrap 是进程启动纪律（入口与 runtime.worker 调用），只依赖 utils，必须早于任何 SDK 装载。
    "bootstrap": {"bootstrap", "utils"},
}

#: ``resources`` 在**模块级**不得 import 的 runtime 子模块（防环）。
RESOURCES_MODULE_LEVEL_FORBIDDEN = (
    "runtime.service",
    "runtime.session",
    "runtime.runs",
)


def _layer_of(path: Path) -> str | None:
    parts = path.relative_to(ROOT).parts
    if len(parts) < 2 or parts[0] not in LAYERS:
        return None

    return parts[0]


def _import_target(node: ast.ImportFrom) -> tuple[str, str] | None:
    """把一条相对 import 解析成 ``(layer, submodule)``；包内引用之外返回 ``None``。"""
    if node.level == 0 or not node.module:
        return None

    head, *rest = node.module.split(".")
    if head not in LAYERS:
        return None

    return head, ".".join(rest)


def test_no_forbidden_module_level_cross_layer_imports() -> None:
    """``runtime`` 不得 import ``adapters``；``adapters`` 不得 import ``runtime``；
    ``infrastructure`` 不得 import 其他层。
    """
    violations: list[str] = []

    for path in ROOT.rglob("*.py"):
        layer = _layer_of(path)
        if layer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _import_target(node)
            if target is None:
                continue
            target_layer, submodule = target
            if target_layer in ALLOWED[layer]:
                continue
            violations.append(f"{path.relative_to(ROOT)}: {layer} -> {target_layer}.{submodule}")

    assert violations == [], "分层依赖被破坏：\n" + "\n".join(violations)


def test_resources_never_imports_adapters_at_all() -> None:
    """``resources`` 与 ``adapters`` 之间只能是**注入**关系（编码器作为参数）。"""
    offenders: list[str] = []

    for path in (ROOT / "resources").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "adapters":
                offenders.append(f"{path.relative_to(ROOT)}: {node.module}")

    assert offenders == [], "resources 不得 import adapters（编码/解码应注入）：\n" + "\n".join(offenders)


@pytest.mark.parametrize("forbidden", RESOURCES_MODULE_LEVEL_FORBIDDEN)
def test_resources_module_level_runtime_imports_do_not_close_a_cycle(forbidden: str) -> None:
    """``resources`` 对 runtime 执行实现的引用必须不存在（模块级或函数级都不成环）。"""
    offenders: list[str] = []

    for path in (ROOT / "resources").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(forbidden):
                offenders.append(f"{path.relative_to(ROOT)}: {node.module}")

    assert offenders == [], f"resources import 了 {forbidden}（会成环）：\n" + "\n".join(offenders)


def test_importing_the_package_pulls_neither_the_sdk_nor_a_runtime() -> None:
    """``import aidev_agent_cli`` 仍不拉 SDK、也不拉任一 runtime/adapters/resources 模块。

    这条断言是分层重排的**启动延迟契约**：包根 ``main`` 是延迟代理，真正入口在
    ``aidev_agent_cli.__main__``（首次调用时才装载）。
    """
    import subprocess
    import sys

    code = (
        "import sys, aidev_agent_cli;"
        "bad=[m for m in sys.modules if m.startswith(('aidev_agent_cli.runtime','aidev_agent_cli.adapters',"
        "'aidev_agent_cli.resources'))];"
        "assert not bad, bad;"
        "blocked=[m for m in sys.modules if m.startswith(('aidev_agent.','ag_ui')) or m.split('.')[0] in ('acp','pydantic')];"
        "assert not blocked, blocked;"
        "assert callable(aidev_agent_cli.main);"
        "print('LAZY OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT.parent),
    )

    assert result.returncode == 0, result.stderr
    assert "LAZY OK" in result.stdout


# ============================================================================
# §4  导入边界审计：函数内 import 零容忍 + 可选边的显式装载点
# ============================================================================
#
# 判定标准只有一条：**被导入的模块在 import 期是否有副作用、或是否拉起 SDK**。凡两条都
# 不成立的，必须在模块顶部 import；真正"不能到顶部"（可选依赖、进程边界的动态装载）一律
# 用 ``importlib.import_module`` 表达，而不是函数体的 ``import`` / ``from ... import``。
# 这样 AST 审计是**零豁免**的：函数体内 import 计数必须为 0。

#: 允许出现 ``importlib.import_module`` 的显式动态装载点，逐个写明理由。
#: （``importlib.import_module`` 不是 AST 意义上的 import 节点，但它是唯一被允许的
#: "延迟装载"手段——列在这里是为了让"哪些地方在运行时决定装什么"可枚举。）
DYNAMIC_IMPORT_POINTS = {
    "aidev_agent_cli/__init__.py": "包根 main() 延迟代理到 __main__（import 包必须零副作用）",
    "aidev_agent_cli/__main__.py": "acp 是可选 extra：缺它时入口仍可导入并给出可读提示",
    "aidev_agent_cli/bootstrap/open_telemetry.py": "OTel 是可选依赖：未安装时必须降级可用",
    "aidev_agent_cli/bootstrap/startup.py": "bkapi_client_core 是可选依赖：未安装时跳过 Django 代理解除",
    "aidev_agent_cli/resources/loaders/__init__.py": "platform 加载器持 SDK：注册表按需构建",
    "aidev_agent_cli/runtime/worker.py": "跨 spawn 边界：import target 在 install_worker_startup 后装载",
    "aidev_agent_cli/sdk/hooks.py": "module 模式的 hook 模块由 --hooks 配置给出，在 worker 内装载",
    "aidev_agent_cli/sdk/executor.py": "tool factories 的 module:factory 串由快照给出",
}

#: 核心层：fresh import 必须**零 SDK**。每一个都在独立进程里断言（同进程会互相污染）。
CORE_MODULES = (
    "aidev_agent_cli.runtime.auth",
    "aidev_agent_cli.runtime.models",
    "aidev_agent_cli.runtime.worker",
    "aidev_agent_cli.infrastructure",
    "aidev_agent_cli.utils.errors",
    "aidev_agent_cli.utils.inputs",
    "aidev_agent_cli.utils.runs",
    "aidev_agent_cli.infrastructure.ports",
    "aidev_agent_cli.runtime.handles",
    "aidev_agent_cli.utils.history",
    "aidev_agent_cli.runtime.session",
    "aidev_agent_cli.hooks",
    "aidev_agent_cli.hooks.contract",
    "aidev_agent_cli.hooks.models",
    "aidev_agent_cli.hooks.handler",
    "aidev_agent_cli.utils.terminal",
    "aidev_agent_cli.runtime.runs",
    "aidev_agent_cli.runtime.service",
    "aidev_agent_cli.sdk",
)

#: adapter 侧：**只**断言不拉 ``aidev_agent.*`` / ``ag_ui``。
#:
#: ``acp`` / ``pydantic`` 是 adapter 自己的协议 SDK——``adapters.acp`` 顶层导入 ``acp`` 是
#: 这个 adapter 的存在方式，不是"父进程提前踩进执行体 SDK"。执行体侧（``aidev_agent`` /
#: ``ag_ui``）才是启动次序回归的判据。
ADAPTER_MODULES = (
    "aidev_agent_cli.adapters.agui",
    "aidev_agent_cli.adapters.acp",
    "aidev_agent_cli.resources.loaders.base",
    "aidev_agent_cli.resources.local_config",
    "aidev_agent_cli.resources.sources",
)


#: "拉到了 SDK" 的判据前缀（``aidev_agent`` 自身不算——它是包，子模块才算）。
SDK_PREFIXES = ("aidev_agent.", "ag_ui", "acp", "pydantic")


def _function_level_imports() -> list[str]:
    """``aidev_agent_cli`` 下出现在函数/类体内的 ``import`` / ``from ... import``。

    模块级 = ``tree.body`` 及其 ``ast.walk``（覆盖顶层 ``if`` / ``try`` 块）；其余即函数/类
    体内的节点。返回 ``文件:行号``，便于断言信息直接定位。
    """
    found: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        at_module: set[ast.AST] = set()
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            at_module.update(ast.walk(stmt))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in at_module:
                found.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}")

    return found


def _dynamic_import_points() -> set[str]:
    """``importlib.import_module`` 出现的位置（按文件）。"""
    points: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "importlib.import_module(" in source:
            points.add(path.relative_to(ROOT.parent).as_posix())

    return points


def _module_import_edges() -> dict[str, set[str]]:
    """``aidev_agent_cli`` 内每个模块 → 它**模块级** import 到的兄弟模块（点分全名）。"""
    edges: dict[str, set[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT.parent)
        dotted = ".".join(relative.with_suffix("").parts)
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = dotted.rsplit(".", 1)[0] if path.name != "__init__.py" else dotted
        targets: set[str] = set()
        for statement in tree.body:
            if not isinstance(statement, ast.ImportFrom) or not statement.module:
                continue
            if statement.level:
                base = package.split(".")
                if statement.level > 1:
                    base = base[: -(statement.level - 1)]
                module = ".".join([*base, statement.module])
            else:
                module = statement.module
            if not module.startswith("aidev_agent_cli"):
                continue
            targets.add(module)
            targets.update(f"{module}.{alias.name}" for alias in statement.names)
        edges[dotted] = targets

    return edges


def _reachable(edges: dict[str, set[str]], start: str) -> set[str]:
    """从 ``start`` 出发的**传递**可达模块集合（模块级 import 图）。"""
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for target in edges.get(current, ()):  # 未收录的子模块按叶子处理
            if target in seen:
                continue
            seen.add(target)
            pending.append(target)

    return seen


def _run_python(code: str, *, timeout: int = 180):
    """在插件根目录起一个干净的解释器（PYTHONPATH 走 ``_paths``）。"""
    import subprocess
    import sys

    from tests.integration._paths import plugin_pythonpath

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "PYTHONPATH": plugin_pythonpath(),
        "AGENT_TUI_OTEL": "0",
        "HOME": __import__("os").environ.get("HOME", "/tmp"),
    }

    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(ROOT.parent),
        env=env,
    )


def test_no_function_level_imports_anywhere() -> None:
    """**零豁免**：函数/类体内不得出现任何 ``import`` / ``from ... import``。

    真正的可选依赖与动态边界必须写成 ``importlib.import_module(...)``，并登记在
    :data:`DYNAMIC_IMPORT_POINTS` 里说明理由。这条断言取代了上一版按文件豁免的白名单——
    白名单一旦存在，就会不断被用来把新延迟 import 塞进去。
    """
    offenders = _function_level_imports()

    assert offenders == [], (
        "函数/类体内不得有 import：要么提到模块顶部（真循环就改结构），"
        "要么改成显式 importlib.import_module 并登记理由：\n" + "\n".join(offenders)
    )


def test_dynamic_import_points_are_declared() -> None:
    """``importlib.import_module`` 只出现在已声明的位置，且每个声明都真实存在。"""
    actual = _dynamic_import_points()
    undeclared = actual - set(DYNAMIC_IMPORT_POINTS)
    stale = set(DYNAMIC_IMPORT_POINTS) - actual

    assert undeclared == set(), f"未声明的动态装载点：{sorted(undeclared)}"
    assert stale == set(), f"声明已失效（文件里不再有 importlib.import_module）：{sorted(stale)}"


def test_runtime_core_imports_close_no_cycle() -> None:
    """``models`` / ``auth`` / ``handles`` / ``service`` 不得（含间接）到达 ``sdk``。

    ``sdk`` 是 SDK 执行体命名空间：父进程任何模块到达它就是 SDK 传播。
    """
    edges = _module_import_edges()
    for module in (
        "aidev_agent_cli.runtime.models",
        "aidev_agent_cli.runtime.auth",
        "aidev_agent_cli.runtime.handles",
        "aidev_agent_cli.runtime.service",
        "aidev_agent_cli.runtime.runs",
        "aidev_agent_cli.utils.terminal",
    ):
        reached = _reachable(edges, module)
        assert not any(item.startswith("aidev_agent_cli.sdk") for item in reached), (
            f"{module} 不得到达 sdk：{sorted(reached)}"
        )

    # models 也不该反向依赖父进程的实现模块（只允许 infrastructure 的值契约 / stdlib）。
    from_models = _reachable(edges, "aidev_agent_cli.runtime.models")
    allowed = (
        "aidev_agent_cli.utils.inputs",
        "aidev_agent_cli.utils.errors",
        "aidev_agent_cli.utils.runs",
    )
    forbidden = ("aidev_agent_cli.runtime.worker", "aidev_agent_cli.infrastructure", "aidev_agent_cli.resources")
    from_models = {m for m in from_models if not m.startswith(allowed)}
    assert not [m for m in from_models if m.startswith(forbidden)], (
        f"runtime.models 只允许 infrastructure 值契约：{sorted(from_models)}"
    )


def test_terminal_is_a_pure_stdlib_module_referenced_only_by_runs_and_events() -> None:
    """``utils.terminal`` 只产出字典，只被 ``runtime.runs`` 与 ``sdk.events`` 引用。

    它曾经 re-export ``sdk.events`` 的 builder，于是 ``service → runs → terminal``
    这条链路把 SDK 拉进父进程（``ag_ui`` 导入期 print 污染协议 stdout）。现在事件形状由
    纯 stdlib 定义，SDK 侧 ``sdk.events`` 反过来调用它。
    """
    edges = _module_import_edges()
    importers = sorted(
        module
        for module, targets in edges.items()
        if "aidev_agent_cli.utils.terminal" in targets and module != "aidev_agent_cli.utils.terminal"
    )

    assert importers == ["aidev_agent_cli.runtime.runs", "aidev_agent_cli.sdk.events"], (
        f"utils.terminal 只允许 runs / sdk.events 引用，实际：{importers}"
    )

    source = (ROOT / "utils" / "terminal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_modules = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) if node.module}
    assert top_modules <= {"__future__", "typing"}, f"terminal 只允许标准库：{sorted(top_modules)}"
    imported = {node.names[0].name for node in tree.body if isinstance(node, ast.Import) and node.names}
    assert imported <= {"time"}, f"terminal 只允许标准库：{sorted(imported)}"


def test_runtime_core_has_no_sdk_side_effects_on_fresh_import() -> None:
    """核心层与 adapter 逐个 fresh import 都不得拉起执行体 SDK。

    一次一个模块的独立进程断言：同一进程里先导入 A 会影响 B 的判定，必须隔离。
    核心层判据是全部 :data:`SDK_PREFIXES`；adapter 只判执行体侧（``aidev_agent.*`` /
    ``ag_ui``），因为 ``acp`` / ``pydantic`` 就是它们自己的协议 SDK。
    """
    import subprocess
    import sys

    from tests.integration._paths import plugin_pythonpath

    def probe_for(prefixes: tuple[str, ...]) -> str:
        return (
            "import sys, importlib\n"
            "module = sys.argv[1]\n"
            "importlib.import_module(module)\n"
            f"bad = sorted(n for n in sys.modules if n.startswith({prefixes!r}))\n"
            "assert not bad, bad\n"
            "print('OK')\n"
        )

    environment = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "PYTHONPATH": plugin_pythonpath(),
        "AGENT_TUI_OTEL": "0",
        "HOME": __import__("os").environ.get("HOME", "/tmp"),
    }

    cases = [(module, probe_for(SDK_PREFIXES)) for module in CORE_MODULES]
    cases += [(module, probe_for(("aidev_agent.", "ag_ui"))) for module in ADAPTER_MODULES]

    for module, probe in cases:
        result = subprocess.run(
            [sys.executable, "-c", probe, module],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT.parent),
            env=environment,
        )
        assert result.returncode == 0, f"{module} 拉起了 SDK：\n{result.stderr[-2000:]}"


def test_sdk_package_root_is_empty() -> None:
    """``sdk`` 的包根是**空包**：不 re-export 任何东西，导入它不拉 SDK。

    SDK 执行体只能经 import target ``aidev_agent_cli.sdk.executor:build`` 装载——
    那条串是 ``runtime.worker.SDK_ENTRY``，子进程在 ``install_worker_startup`` 之后才
    ``import_module``。包根一旦 re-export，父进程与 target 装载就会提前踩进 SDK。
    """
    code = (
        "import sys\n"
        "import aidev_agent_cli.sdk as sdk\n"
        f"bad = sorted(n for n in sys.modules if n.startswith({SDK_PREFIXES!r}))\n"
        "assert not bad, bad\n"
        "assert list(sdk.__all__) == [], sdk.__all__\n"
        "assert not [n for n in dir(sdk) if not n.startswith('_') and n != 'annotations'], dir(sdk)\n"
        "print('SDK ROOT EMPTY')\n"
    )
    result = _run_python(code)

    assert result.returncode == 0, result.stderr[-2000:]
    assert "SDK ROOT EMPTY" in result.stdout

    source = (ROOT / "sdk" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [node.module for node in module_imports] == ["__future__"], module_imports


def test_executor_holds_sdk_dependencies_at_its_module_top() -> None:
    """执行体是 worker 侧代码：SDK 依赖就在模块顶部——**不**留任何函数内 import。

    "晚"由 import target 决定（``_worker_entry`` 在 ``install_worker_startup`` 之后
    ``import_module``），因此这里不存在"要不要延迟"的问题：模块级就是最合适的位置。
    """
    source = (ROOT / "sdk" / "executor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_modules = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) if node.module}
    # 保护由 runtime.worker._worker_entry 在装载前完成；执行体不 import 启动设施。
    assert not any(module and ("startup" in module or "stdio" in module) for module in top_modules), sorted(top_modules)
    assert "aidev_agent.enums" in top_modules, sorted(top_modules)

    at_module: set[ast.AST] = set()
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            at_module.update(ast.walk(statement))
    offenders = [
        node.lineno for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in at_module
    ]
    assert offenders == [], f"executor 内不得再有函数内 import：{offenders}"


def test_optional_dependencies_are_loaded_at_one_declared_point() -> None:
    """可选依赖的"延迟"必须集中，且未安装时降级而非崩。

    * ``open_telemetry._sdk_otel`` 是 OTel 的唯一装载点——``install_otel`` 在没有 SDK 时
      返回 ``False``（一行 info 日志），绝不抛。
    * ``startup._bkapi_config`` 缺失时为 ``None``，``_detach_bkapi_from_django`` 静默返回。
    """
    code = (
        "import sys\n"
        "from aidev_agent_cli.bootstrap import open_telemetry\n"
        "assert not [n for n in sys.modules if n.startswith(('aidev_agent.',))], "
        "'import open_telemetry must not pull SDK'\n"
        "assert callable(open_telemetry._sdk_otel)\n"
        "assert open_telemetry.install_otel({}) in (True, False)\n"
        "from aidev_agent_cli.bootstrap import startup\n"
        "assert callable(startup._bkapi_config)\n"
        "startup._detach_bkapi_from_django()\n"
        "print('OPTIONAL OK')\n"
    )
    result = _run_python(code)

    assert result.returncode == 0, result.stderr[-2000:]
    assert "OPTIONAL OK" in result.stdout


def test_sdk_does_not_own_storage_or_cross_turn_state() -> None:
    """sdk 只做单轮组装与合并：不认识本机目录约定、不读写 history（路径与持久化都在 runtime）。"""
    offenders = []
    for path in sorted((ROOT / "sdk").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for needle in ("BKAI_HOME_DIRNAME", "utils.history", "LocalHistory", "BKAI_TUI_CHECKPOINT_FILE"):
            if needle in source:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == [], offenders
