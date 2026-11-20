# -*- coding: utf-8 -*-
"""worker 启动顺序契约：env / guard / 出网 / sys.path / OTel 必须全部早于 import target。

为什么需要这一组断言
--------------------
``aidev_agent_cli.sdk.executor:build`` 是唯一跨越 spawn 边界的 import target。它的
正确性不在于"能不能导入"，而在于**装载时机**：``ag_ui`` 在导入期会 print（fd 1 是协议
通道）、``BakeAidevAgentInstrumentor`` 是模块属性 monkey-patch、``aidev_agent`` 在导入期读
settings——三者都要求 stdout guard / 环境发布 / OTel 已经就位。

因此契约是：``_worker_entry`` 先做完全部保护，**再** ``import_module``。执行体顶层持有
SDK 依赖是**正确**的（那是 worker 侧代码），包根 ``sdk`` 必须是空包——它一旦
re-export，父进程与 target 装载就会提前踩进 SDK。

三类证据：

1. **静态**：``_worker_entry`` 的调用顺序、``sdk/__init__.py`` 无 re-export、
   ``executor.py`` 顶部持 SDK 且函数内零 import。
2. **动态**：子进程里导入 ``sdk`` / ``runtime.worker`` / ``runtime.runs`` 后
   SDK 仍未装载；``_worker_entry`` 的调用序列由注入的记账器验证。
3. **真实 spawn**：``tests/runtime/test_production_runtime.py`` 跑真进程（不动这里）。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path

import pytest

from aidev_agent_cli import __main__ as entry_module
from aidev_agent_cli.bootstrap import startup
from aidev_agent_cli.runtime import worker

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "aidev_agent_cli"

STARTUP_STEPS = (
    "install_stdout_guard()",
    "_publish_env(env)",
    "install_from_env()",
    "harden_sys_path()",
    "install_otel(env)",
)

ENTRY_ORDER = (
    "install_worker_startup(",
    "import_module(",
)


def _calls_in_order(source: str, steps: tuple[str, ...]) -> list[int]:
    """每一步首次出现的位置；缺任一步返回 ``[-1]``（调用方据此报错）。"""
    positions = [source.find(step) for step in steps]

    return positions if all(position >= 0 for position in positions) else [-1]


def _code_lines(source: str) -> str:
    """去掉 docstring 与注释后的源码——用"是否**执行**某一步"而不是"是否提到它"来判定。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    stripped = ast.unparse(tree)

    return "\n".join(line.split("#", 1)[0] for line in stripped.splitlines())


def test_startup_keeps_the_five_steps_in_order() -> None:
    """order is the contract: guard → 环境 → 出网 → sys.path → OTel。"""
    source = _code_lines(inspect.getsource(startup._install))
    positions = _calls_in_order(source, STARTUP_STEPS)

    assert positions != [-1], f"_install 缺少步骤：{STARTUP_STEPS}"
    assert positions == sorted(positions), f"启动顺序被打乱：{list(zip(STARTUP_STEPS, positions))}"


def test_worker_startup_shares_the_entry_steps_and_adds_no_config_reads() -> None:
    """入口与 worker 共用同一组步骤；worker 不读配置文件、不重配日志。"""
    entry = _code_lines(inspect.getsource(startup.install_startup_hooks))
    worker_source = _code_lines(inspect.getsource(startup.install_worker_startup))

    assert "_install(env)" in entry
    assert "_install(env)" in worker_source
    assert "configure_logging()" in entry
    assert "configure_logging()" not in worker_source
    # 配置文件只由 __main__ 读一次：两条启动路径都不再读。
    assert "BkAiConfig.read()" not in entry
    assert "BkAiConfig.read()" not in worker_source
    assert "BkAiConfig.read()" in inspect.getsource(entry_module.main)


def test_worker_entry_protects_before_loading_the_import_target() -> None:
    """``_worker_entry``：env → chdir → 出网守卫 → 启动五步 → **才** ``import_module``。"""
    source = _code_lines(inspect.getsource(worker._worker_entry))
    positions = _calls_in_order(source, ENTRY_ORDER)

    assert positions != [-1], f"_worker_entry 缺少步骤：{ENTRY_ORDER}\n{source}"
    assert positions == sorted(positions), f"装载晚于保护被打破：{list(zip(ENTRY_ORDER, positions))}"
    # phase 帧必须在 import 之后（"building" 表示正在构建执行体，不是正在导入模块）。
    building_at = source.index("building")
    assert source.index("install_worker_startup(") < building_at
    assert source.index("import_module(") < building_at


def test_sdk_package_root_does_not_reexport_anything() -> None:
    """``sdk/__init__.py`` 是空包：只有 ``__future__`` 与 ``__all__ = []``。"""
    source = (PACKAGE / "sdk" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [node.module for node in module_imports] == ["__future__"], module_imports
    assert not [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))], (
        "包根不得定义任何东西（执行体在 .executor）"
    )
    assert "__all__: list[str] = []" in source


def test_executor_holds_sdk_dependencies_at_module_top() -> None:
    """执行体是 worker 侧代码：SDK 依赖在模块顶部，函数内零 import。"""
    tree = ast.parse((PACKAGE / "sdk" / "executor.py").read_text(encoding="utf-8"))
    top_modules = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) if node.module}
    # 进程级启动纪律只在 runtime.worker：执行体不 import runtime，也不重放启动步骤。
    assert not any(module.startswith(("runtime", "..runtime")) or "startup" in module or "stdio" in module for module in top_modules), sorted(top_modules)
    assert "aidev_agent.enums" in top_modules, sorted(top_modules)

    at_module: set[ast.AST] = set()
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            at_module.update(ast.walk(statement))
    offenders = [
        node.lineno for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in at_module
    ]
    assert offenders == [], f"executor 内不得再有函数内 import：{offenders}"


def test_worker_entry_runs_protection_then_loads_the_target(monkeypatch) -> None:
    """动态证据：``_worker_entry`` 的真实调用序列（保护 → 装载 → 构建 → 运行）。

    import target 被替换成记账器，因此断言的是**真实执行顺序**；SDK 侧的保护函数同样被替换，
    它们自身的五步顺序由上面的静态断言（同一源码）兜住。
    """
    calls: list[str] = []

    class _Connection:
        def close(self) -> None:
            calls.append("connection.close")

    class _Context:
        def __init__(self, connection) -> None:
            self._cancelled = False

        def send_frame(self, frame):
            calls.append(f"frame:{frame['kind']}:{frame.get('value') if frame['kind'] != 'result' else frame['status']}")

        def cancelled(self):
            return self._cancelled

    def _recorder(name, wrapped=None):
        def call(*args, **kwargs):
            calls.append(name)

            return None if wrapped is None else wrapped(*args, **kwargs)

        return call

    class _Executor:
        def execute(self, context):
            calls.append("execute")

            return {"outcome": {"status": "finished"}}

        def close(self):
            calls.append("executor.close")

    class _TargetModule:
        @staticmethod
        def build(snapshot, context):
            calls.append("target.build")

            return _Executor()

    monkeypatch.setattr(worker, "WorkerContext", _Context)
    monkeypatch.setattr(worker.os, "setsid", _recorder("setsid"))
    monkeypatch.setattr(worker, "install_worker_startup", _recorder("install_worker_startup"))
    monkeypatch.setattr(worker, "apply_worker_stdio_guard", _recorder("apply_worker_stdio_guard"))
    monkeypatch.setattr(worker.importlib, "import_module", _recorder("import_module", lambda name: _TargetModule))

    snapshot = json.dumps({"process": {"cwd": "/tmp", "env": {"K": "V"}}}).encode("utf-8")
    # ``guard_descriptor`` 为 None 时不装出网守卫（与真实父进程"未开启守卫"同路径）。
    worker._worker_entry(_Connection(), "fake.target:build", snapshot, None)

    assert calls[:5] == [
        "setsid",
        "install_worker_startup",
        "import_module",
        "apply_worker_stdio_guard",
        "install_worker_startup",
    ], calls
    assert calls[5:] == [
        "frame:phase:building",
        "target.build",
        "frame:phase:running",
        "execute",
        "frame:result:finished",
        "executor.close",
        "connection.close",
    ], calls


def test_worker_entry_installs_the_egress_guard_when_a_descriptor_is_given(monkeypatch) -> None:
    """给了 broker 描述符时，出网守卫必须**先于**启动五步与 import target。"""
    calls: list[str] = []

    class _Connection:
        def close(self) -> None:
            pass

    class _Context:
        def __init__(self, connection) -> None:
            pass

        def send_frame(self, frame):
            pass

        def cancelled(self):
            return False

    class _Executor:
        def execute(self, context):
            return {}

        def close(self):
            pass

    def _recorder(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))

        return call

    monkeypatch.setattr(worker, "WorkerContext", _Context)
    monkeypatch.setattr(worker.os, "setsid", _recorder("setsid"))
    monkeypatch.setattr(worker, "install_worker_startup", _recorder("install_worker_startup"))
    monkeypatch.setattr(worker, "apply_worker_stdio_guard", _recorder("apply_worker_stdio_guard"))
    monkeypatch.setattr(worker.importlib, "import_module", lambda name: type("M", (), {"build": staticmethod(lambda s, c: _Executor())}))

    snapshot = json.dumps({"process": {"cwd": "/tmp", "env": {}}}).encode("utf-8")
    worker._worker_entry(_Connection(), "fake.target:build", snapshot, 7)

    assert calls == [
        ("setsid", (), {}),
        ("install_worker_startup", ({}, "/tmp"), {"egress_fd": 7}),
        ("apply_worker_stdio_guard", (), {}),
        ("install_worker_startup", ({}, "/tmp"), {}),
    ], calls


def test_runner_spawns_the_executor_import_target() -> None:
    """worker 的默认 import target 指向执行体模块，且不经包根。"""
    assert worker.SDK_ENTRY == "aidev_agent_cli.sdk.executor:build"
    assert worker.ProcessWorker({}).entry == worker.SDK_ENTRY
    source = inspect.getsource(worker)
    assert "aidev_agent_cli.sdk:build" not in source


def test_importing_parent_side_modules_pulls_nothing_from_the_sdk() -> None:
    """父进程侧（worker / runner / service）导入后，执行体 SDK 仍未装载。"""
    import subprocess
    import sys

    from tests.integration._paths import plugin_pythonpath

    code = (
        "import sys, importlib\n"
        "for module in ('aidev_agent_cli.runtime.worker', 'aidev_agent_cli.runtime.runs',"
        " 'aidev_agent_cli.runtime.service', 'aidev_agent_cli.sdk'):\n"
        "    importlib.import_module(module)\n"
        "bad = sorted(n for n in sys.modules if n.startswith(('aidev_agent.', 'ag_ui', 'acp', 'pydantic')))\n"
        "assert not bad, bad\n"
        "print('PARENT CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(ROOT),
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "PYTHONPATH": plugin_pythonpath(),
            "AGENT_TUI_OTEL": "0",
            "HOME": __import__("os").environ.get("HOME", "/tmp"),
        },
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "PARENT CLEAN" in result.stdout


@pytest.mark.parametrize("target", ["aidev_agent_cli.sdk", "aidev_agent_cli.runtime.worker"])
def test_package_roots_stay_light(target: str) -> None:
    """包根与 IPC 层都不在导入期装载 SDK。"""
    module = importlib.import_module(target)

    assert module.__name__ == target