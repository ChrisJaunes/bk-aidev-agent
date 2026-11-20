"""stdout guard 的用例（``aidev_agent_cli.infrastructure.stdio``）。

WHY THIS FILE EXISTS
--------------------
``sys.stdout`` 是 worker 的 **协议通道**：TUI 读到的每一帧都是 fd 1 上的一行 NDJSON。SDK 的
任何一句杂散 ``print()`` 落进那个流，TUI 就解析不了。guard 就是为了挡住它——而且它**曾经
没有任何测试**，于是以「已实现、未接线」的形态发布：docstring 把它写成启动第 1 步，
``main()`` 却从不调用。

迁移后 guard 住在 :mod:`aidev_agent_cli.infrastructure.stdio`（只依赖标准库），**入口**在导入
任何 adapter 之前安装它；本文件最后三条用例钉住这件事——顺序错了，守护窗口就不存在。
"""

from __future__ import annotations

import inspect
import sys
from typing import Any

import pytest

from aidev_agent_cli import __main__ as entry
from aidev_agent_cli.infrastructure import startup, stdio


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每个用例前后把 guard 的**全部**进程级状态还原到干净值。

    guard 是进程级单例：泄漏一次安装（包括 ``_protocol_stream`` 这个 fd 1 的包装）会污染同一
    会话里之后的每个用例——实测形态是「后面的用例拿到上一次跑的 protocol stream」。因此这里
    显式回到"从未安装"的状态，而不是"还原成进入时的样子"（进入时可能已经是代理）。
    """
    saved_stdout = sys.stdout

    stdio._installed = False
    stdio._protocol_stream = None
    sys.stdout = saved_stdout

    yield

    sys.stdout = saved_stdout
    stdio._installed = False
    stdio._protocol_stream = None


class _FakeStream:
    """记录写入内容的最小文本流。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, data: str) -> int:
        self.chunks.append(data)

        return len(data)

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        return 2

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def test_guard_reroutes_print_to_stderr() -> None:
    """装上之后，``print()`` 绝不能到达协议通道。

    必须在安装**之前**替换 ``sys.stderr``：代理在安装时抓取当时的 ``sys.stderr``，先装再换
    会绑到真正的 stderr 上，下面那条断言就会因为错误的原因通过。
    """
    stderr = _FakeStream()
    protocol = _FakeStream()

    original_stderr = sys.stderr
    sys.stderr = stderr  # type: ignore[assignment]
    try:
        stdio.install_stdout_guard(protocol_stream=protocol)
        print("stray SDK output")
    finally:
        sys.stderr = original_stderr

    assert "stray SDK output" in stderr.text
    assert protocol.text == "", "协议流上只能出现协议帧"


def test_guard_is_idempotent() -> None:
    """第二次安装不得把代理换成代理。"""
    protocol = _FakeStream()

    stdio.install_stdout_guard(protocol_stream=protocol)
    after_first = sys.stdout

    stdio.install_stdout_guard(protocol_stream=protocol)

    assert sys.stdout is after_first


def test_protocol_out_is_the_protocol_stream() -> None:
    """帧必须写到专用流上，绝不能写 ``sys.stdout``。"""
    protocol = _FakeStream()

    stdio.install_stdout_guard(protocol_stream=protocol)

    assert stdio.protocol_out() is protocol
    assert stdio.protocol_out() is not sys.stdout


def test_protocol_out_without_guard_uses_current_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _FakeStream()
    monkeypatch.setattr(sys, "stdout", output)

    assert stdio.protocol_out() is output


def test_captured_stdout_reroutes_within_the_block() -> None:
    """第二层，用于运行期间产生的输出。"""
    stderr = _FakeStream()
    original_stderr = sys.stderr
    sys.stderr = stderr  # type: ignore[assignment]
    try:
        with stdio.captured_stdout():
            print("during a run")
    finally:
        sys.stderr = original_stderr

    assert "during a run" in stderr.text


def test_harden_sys_path_drops_cwd_entries() -> None:
    """空串与点号会被解析成 cwd，可能遮蔽已安装的包。"""
    saved = list(sys.path)
    try:
        sys.path[:] = [entry.__file__, "", ".", "/usr/lib"]
        stdio.harden_sys_path()
        assert "" not in sys.path
        assert "." not in sys.path
        assert "/usr/lib" in sys.path
    finally:
        sys.path[:] = saved


# ============================================================================
# 启动顺序：guard 必须在**导入 adapter / SDK 之前**安装
# ============================================================================


def test_the_guard_lives_in_infrastructure_and_only_needs_the_standard_library() -> None:
    """guard 模块本身不得 import 本包其他层或任何第三方 SDK。

    这是「守护窗口」成立的全部理由：入口 import 它时不能连带把 ``ag_ui`` / ``aidev_agent``
    拉进来。用 AST 判定，比 grep 更精确（注释与字符串里的名字不算依赖）。
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(stdio.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, "infrastructure/stdio.py 不得有相对导入（它只依赖标准库）"
            if node.module:
                imported.add(node.module.split(".")[0])

    allowed = {"__future__", "contextlib", "os", "sys", "typing", "logging"}
    assert imported <= allowed, f"guard 模块引入了非标准库依赖：{sorted(imported - allowed)}"


def test_the_entry_installs_the_guard_before_any_adapter_is_imported() -> None:
    """入口的 composition root 里，guard 的安装点必须早于 adapter 的导入点。

    旧实现的失败形态：入口为了调用 guard 得先 ``from .adapters.agui import
    install_stdout_guard``，而那一步已经把 SDK 导进来了。因此这条断言检查的是**函数源码
    里**的相对次序。
    """
    source = inspect.getsource(startup.install_startup_hooks)
    guard_at = source.index("install_stdout_guard()")
    for heavier in ("adapters", "aidev_agent"):
        assert heavier not in source[:guard_at], f"{heavier} 出现在 guard 之前"

    main = inspect.getsource(entry.main)
    hooks_at = main.index("install_startup_hooks()")
    # adapter 经 ``_import_adapter`` 解析，而它只在启动钩子之后才被调用。
    assert "adapters." not in main
    assert hooks_at < main.index("_import_adapter(protocol)")
    # ``agui`` 在入口模块顶层导入（导入期无 SDK 副作用）；acp 是可选 extra，只在装载点里出现。
    assert "from .adapters.agui import run_agui" in inspect.getsource(entry)
    importer = inspect.getsource(entry._load_optional_acp_adapters)
    for adapter in ("aidev_agent_cli.adapters.acp_dsh", "aidev_agent_cli.adapters.acp"):
        assert adapter in importer


def test_the_agui_adapter_no_longer_owns_the_guard_implementation() -> None:
    """guard 的实现只有 ``infrastructure.stdio`` 一份；agui 只引用 protocol_out，不再转出 guard。"""
    from aidev_agent_cli.adapters import agui

    assert not hasattr(agui, "install_stdout_guard")
    assert agui.protocol_out is stdio.protocol_out
    # 私有状态不再住在 adapter 上（否则两份 _installed 会让幂等判断失效）。
    assert not hasattr(agui, "_installed")
    assert not hasattr(agui, "_real_stdout")
    assert not hasattr(agui, "_protocol_stream")


@pytest.mark.parametrize("protocol", ["acp", "acp-dsh"])
@pytest.mark.parametrize("handle_signals", [False, True])
async def test_acp_servers_keep_guard_and_use_protocol_stream(
    monkeypatch: pytest.MonkeyPatch, protocol: str, handle_signals: bool
) -> None:
    import io
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aidev_agent_cli.adapters import acp, acp_dsh

    module = acp if protocol == "acp" else acp_dsh
    server = acp.run_acp_server if protocol == "acp" else acp_dsh.run_acp_dsh_server
    service = SimpleNamespace(close_all=AsyncMock())
    adapter = SimpleNamespace(service=service)
    factory = "AcpAdapter" if protocol == "acp" else "AcpDshAdapter"
    monkeypatch.setattr(module, factory, lambda *args, **kwargs: adapter)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    stderr = _FakeStream()
    protocol_stream = _FakeStream()
    monkeypatch.setattr(sys, "stderr", stderr)
    stdio.install_stdout_guard(protocol_stream=protocol_stream)
    guard = sys.stdout

    async def run_agent(actual_adapter, transport) -> None:
        assert actual_adapter is adapter
        assert sys.stdout is guard
        print("runtime SDK output")
        await transport.send({"id": 1, "result": {}})
        await transport.close()

    async def serve_until_signal(serving) -> None:
        await serving

    monkeypatch.setattr(module, "run_agent", run_agent)
    monkeypatch.setattr(module, "_serve_until_signal", serve_until_signal)
    assert await server(service, handle_signals=handle_signals) == 0
    assert sys.stdout is guard
    print("after shutdown")
    assert json.loads(protocol_stream.text) == {"id": 1, "result": {}}
    assert "runtime SDK output" in stderr.text
    assert "after shutdown" in stderr.text
    service.close_all.assert_awaited_once()
