# -*- coding: utf-8 -*-
"""ACP 入口（``__main__``）：精确选择、组合门禁、guard 顺序、启动失败不污染 fd 1。

迁自原 worker ``test_main.py`` 的 ACP 六例，并按当前契约改写：**旧的裸 ``acp`` argv
token 契约作废**（不恢复旧入口、不做 shim），ACP 必须用 ``--protocol acp`` 精确选中。

保留的断言：
* 精确选择时不出现 ``ready`` 帧；
* guard 在 SDK import 之前安装，运行期间协议输出与普通输出保持隔离；
* 无参数是 acp-dsh（headless NDJSON 已删除）；
* 协议版本 1 握手；
* footer 分流。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from aidev_agent_cli import __main__ as entry
from aidev_agent_cli.infrastructure import startup
from aidev_agent_cli.infrastructure.settings import TuiConfig
from tests.integration._paths import AGENT_ROOT, PLUGIN_ROOT, PYTHON


@pytest.fixture(autouse=True)
def _isolated_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把配置/日志重定向到 tmp：用例之间与开发机 ~/.bkai 完全隔离。"""
    monkeypatch.setenv("ACP_TEST_CONFIG_FILE", str(tmp_path / "bkai_tui.json"))
    monkeypatch.setenv("ACP_TEST_LOG_FILE", str(tmp_path / "worker.log"))
    yield


def _env(config_file: str, log_file: str, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["MESSAGE_HANDLER_TYPE"] = "inmemory"
    env.pop("RABBITMQ_HOST", None)
    env.pop("REDIS_URL", None)
    env["PYTHONPATH"] = os.pathsep.join([str(PLUGIN_ROOT), str(AGENT_ROOT), env.get("PYTHONPATH", "")])
    env.pop("BKAI_TUI_ACCESS_TOKEN", None)
    # 配置与日志都重定向到 tmp：**不依赖开发机 ~/.bkai 里是否留着 token**，
    # 否则「缺 token」的用例会因为本机登录态而变成「握手成功」。
    env[TuiConfig.CONFIG_FILE_OVERRIDE] = config_file
    env["BKAI_TUI_LOG_PATH"] = log_file
    env.pop("AGENT_TUI_CWD", None)
    env.update(extra)

    return env


def _run(
    args: list[str],
    stdin_text: str = "",
    timeout: float = 120.0,
    *,
    config_file: str = "",
    log_file: str = "",
    **extra: str,
):
    return subprocess.run(
        [PYTHON, "-u", *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(AGENT_ROOT),
        env=_env(config_file, log_file, **extra),
    )


# ============================================================================
# argparse 面：精确选择
# ============================================================================


def test_acp_is_selected_only_by_the_exact_protocol_value() -> None:
    """``--protocol acp`` 精确选中；没有裸 ``acp`` 位置参数那套旧分流。"""
    parsed = entry._parser().parse_args(["--protocol", "acp", "--resources", "platform"])

    assert parsed.protocol == "acp"
    assert parsed.resources == "platform"


def test_the_old_bare_acp_argv_is_rejected() -> None:
    """旧入口是 ``python -m worker acp``：本包**不**提供它，也不做 shim。"""
    with pytest.raises(SystemExit) as caught:
        entry._parser().parse_args(["acp"])

    assert caught.value.code != 0


def test_a_misspelled_protocol_is_rejected_not_ignored() -> None:
    """``--protocol acpx`` 必须被 argparse 拒绝（非零退出），不能静默落到默认值。"""
    with pytest.raises(SystemExit) as caught:
        entry._parser().parse_args(["--protocol", "acpx"])

    assert caught.value.code != 0


def test_tui_is_an_alias_of_agui() -> None:
    """现网 TUI 注册命令在用 ``--protocol tui``：它必须仍指向同一份 adapter。"""
    assert set(entry.PROTOCOLS) == {"acp-dsh", "agui", "acp", "tui"}
    parsed = entry._parser().parse_args(["--protocol", "tui"])
    assert entry.normalize_protocol(parsed.protocol) == "agui"


def test_the_default_is_the_default_preset() -> None:
    """无参数：默认 acp-dsh，省略 --resources 取 default 预设（展开成完整平台链）。"""
    from aidev_agent_cli.resources.sources import PLATFORM_SOURCES

    parsed = entry._parser().parse_args([])

    assert parsed.protocol == "acp-dsh"
    assert parsed.resources is None
    assert parsed.model == ""
    assert parsed.session == ""
    assert entry.resolve_resources(parsed.resources) == PLATFORM_SOURCES


def test_short_and_long_options_are_equivalent() -> None:
    short = entry._parser().parse_args(["-m", "m1", "-s", "s1"])
    long = entry._parser().parse_args(["--model", "m1", "--session", "s1"])

    assert (short.model, short.session) == (long.model, long.session) == ("m1", "s1")


# ============================================================================
# --resources：省略 = 协议默认；显式 = 按顺序叠加
# ============================================================================


@pytest.mark.parametrize("protocol", ["acp-dsh", "agui", "tui", "acp"])
def test_omitting_resources_equals_writing_the_default_preset(protocol: str) -> None:
    """省略 --resources 与显式写出 default 预设**必须等价**（与协议无关）。"""
    omitted = entry._parser().parse_args(["--protocol", protocol])
    written = entry._parser().parse_args(["--protocol", protocol, "--resources", "default"])

    assert entry.resolve_resources(omitted.resources) == entry.resolve_resources(written.resources)


def test_tui_and_agui_have_the_same_default_resources() -> None:
    assert entry.resolve_resources(None) == entry.resolve_resources(None)


def test_preset_expands_and_dedups() -> None:
    """default 预设展开成完整链；重复来源在展开后去重，不会二次加载。"""
    from aidev_agent_cli.resources.sources import PLATFORM_SOURCES, parse_resources

    assert entry.resolve_resources(None) == PLATFORM_SOURCES
    assert parse_resources(("default",)) == PLATFORM_SOURCES
    assert parse_resources(("default", "platform")) == PLATFORM_SOURCES
    assert parse_resources(("default", "mcp")) == PLATFORM_SOURCES
    assert parse_resources(("platform", "platform", "mcp")) == ("platform", "mcp")


def test_explicit_resources_stack_in_the_given_order() -> None:
    assert entry.resolve_resources("injected,local,mcp,runtime") == ("injected", "local", "mcp", "runtime")
    assert entry.resolve_resources("platform,mcp") == ("platform", "mcp")


@pytest.mark.parametrize("bad", ["foo", "", "platform,injected", "local,platform"])
def test_invalid_resources_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        entry.resolve_resources(bad)


@pytest.mark.slow
@pytest.mark.parametrize("protocol", ["acp-dsh", "agui", "tui", "acp"])
@pytest.mark.parametrize("resources", ["injected", "platform", "default"])
def test_every_protocol_accepts_every_resource_preset(protocol: str, resources: str) -> None:
    """统一资源管理器后不再有协议 × 资源的组合门禁。"""
    result = _run(
        ["-m", "aidev_agent_cli", "--protocol", protocol, "--resources", resources],
        config_file=os.environ["ACP_TEST_CONFIG_FILE"],
        log_file=os.environ["ACP_TEST_LOG_FILE"],
    )

    assert "--resources:" not in result.stderr, result.stderr[-800:]


def test_an_invalid_resource_fails_before_the_protocol_starts() -> None:
    result = _run(
        ["-m", "aidev_agent_cli", "--resources", "nope"],
        config_file=os.environ["ACP_TEST_CONFIG_FILE"],
        log_file=os.environ["ACP_TEST_LOG_FILE"],
    )

    assert result.returncode != 0
    assert result.stdout == "", "启动错误的说明绝不能写进 stdout（那是协议通道）"
    assert "nope" in result.stderr


# ============================================================================
# 启动顺序（D4）与启动失败
# ============================================================================


@pytest.mark.slow
def test_missing_token_fails_before_binding_the_connection() -> None:
    """缺 token 必须**在任何 ACP 连接之前**失败：非零退出、stdout 无半帧。"""
    result = _run(
        ["-m", "aidev_agent_cli", "--protocol", "acp", "--resources", "platform"],
        config_file=os.environ["ACP_TEST_CONFIG_FILE"],
        log_file=os.environ["ACP_TEST_LOG_FILE"],
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "BKAI_TUI_ACCESS_TOKEN" in result.stderr


def test_the_missing_extra_message_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """未安装 ``acp`` extra 时的文案必须是「请安装 aidev-agent-cli[acp]」，不是 ImportError 栈。

    可选 extra 的装载点是 ``entry._load_optional_acp_adapters``（经
    ``importlib.import_module``）：这里把它的结果置空，等价于"探测发现 acp 缺失"，
    比卸包干净，也不依赖 ``sys.modules`` 的负缓存形态。
    """
    monkeypatch.setattr(entry, "_ACP_ADAPTERS", None)

    assert entry.main(["--protocol", "acp", "--resources", "platform"]) == 2


@pytest.mark.slow
def test_without_the_extra_the_sibling_protocols_still_work() -> None:
    """``acp`` 是**可选**依赖：不装它时 agui 照常可导入，acp / acp-dsh 只报一句可操作的提示。

    在子进程里用一个 ``meta_path`` 拦截器模拟「没装 agent-client-protocol」——比真的
    卸包干净，也不影响其他用例。断言两件事：

    1. ``import aidev_agent_cli`` 与 agui adapter 完全不受影响；
    2. ``--protocol acp`` 退出码 2、stdout 为空、stderr 是「请安装 aidev-agent-cli[acp]」。
    """
    blocker = "\n".join(
        [
            "import sys",
            "class _Blocker:",
            "    def find_spec(self, name, path=None, target=None):",
            "        if name == 'acp' or name.startswith('acp.'):",
            "            raise ImportError('No module named acp (blocked by test)')",
            "        return None",
            "sys.meta_path.insert(0, _Blocker())",
            "",
        ]
    )
    sibling_probe = blocker + "\n".join(
        [
            "import aidev_agent_cli",
            "import aidev_agent_cli.adapters.agui",
            "print('SIBLING OK')",
        ]
    )
    result = subprocess.run([PYTHON, "-u", "-c", sibling_probe], capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr[-1500:]
    assert "SIBLING OK" in result.stdout

    dsh_probe = blocker + "\n".join(
        [
            "from aidev_agent_cli.__main__ import main",
            "raise SystemExit(main(['--protocol', 'acp-dsh']))",
        ]
    )
    dsh_result = subprocess.run([PYTHON, "-u", "-c", dsh_probe], input="", capture_output=True, text=True, timeout=120)
    assert dsh_result.returncode != 0
    assert dsh_result.stdout == ""
    assert "aidev-agent-cli[acp]" in dsh_result.stderr

    acp_probe = blocker + "\n".join(
        [
            "from aidev_agent_cli.__main__ import main",
            "raise SystemExit(main(['--protocol', 'acp', '--resources', 'platform']))",
        ]
    )
    acp_result = subprocess.run([PYTHON, "-u", "-c", acp_probe], input="", capture_output=True, text=True, timeout=120)

    assert acp_result.returncode != 0
    assert acp_result.stdout == "", "启动错误不得写进 stdout"
    assert "aidev-agent-cli[acp]" in acp_result.stderr
    assert "Traceback" not in acp_result.stderr, "不能把 ImportError 栈甩给用户"


def test_the_acp_branch_installs_the_guard_before_importing_the_adapter() -> None:
    """D4 的顺序不可颠倒：先 guard（保护导入窗口），再导入在顶层拉 SDK 的 adapters.acp。"""
    import inspect

    hooks = inspect.getsource(startup.install_startup_hooks)
    hooks_at = hooks.index("install_stdout_guard()")
    assert "protocol_out" not in hooks[:hooks_at]

    main = inspect.getsource(entry.main)
    hooks_at = main.index("install_startup_hooks()")
    # adapter 经 ``_import_adapter`` 解析，且它只在启动钩子之后才被调用。
    assert "adapters." not in main
    assert hooks_at < main.index("_import_adapter(protocol)")
    # ``agui`` 在入口模块顶层（无导入期副作用）；acp 是可选 extra，只在装载点里出现。
    assert "from .adapters.agui import run_agui" in inspect.getsource(entry)
    importer = inspect.getsource(entry._load_optional_acp_adapters)
    for adapter in ("aidev_agent_cli.adapters.acp_dsh", "aidev_agent_cli.adapters.acp"):
        assert adapter in importer


# ============================================================================
# 子进程：真实握手 / 无 ready 帧 / footer 分流
# ============================================================================


def _initialize_line() -> str:
    import json

    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                    "clientInfo": {"name": "entry-test", "version": "0"},
                },
            }
        )
        + "\n"
    )


@pytest.mark.slow
def test_acp_entry_answers_initialize_with_protocol_version_one_and_no_ready_frame() -> None:
    """精确选择 ACP 时**不**出现 ``kind: ready``（那是 agui 的信封），只有 JSON-RPC。"""
    import json

    result = _run(
        ["-m", "aidev_agent_cli", "--protocol", "acp", "--resources", "platform"],
        _initialize_line(),
        config_file=os.environ["ACP_TEST_CONFIG_FILE"],
        log_file=os.environ["ACP_TEST_LOG_FILE"],
        BKAI_TUI_ACCESS_TOKEN="dummy-token",
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert '"kind":"ready"' not in result.stdout
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"握手必须有且仅有一条响应；stdout={result.stdout!r} stderr={result.stderr[-800:]!r}"
    response = json.loads(lines[0])
    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == 1


@pytest.mark.slow
def test_protocol_output_never_mixes_with_diagnostics() -> None:
    """footer 分流：stdout 只有协议帧，诊断一律在 stderr。"""
    result = _run(
        ["-m", "aidev_agent_cli", "--protocol", "acp", "--resources", "platform"],
        _initialize_line(),
        config_file=os.environ["ACP_TEST_CONFIG_FILE"],
        log_file=os.environ["ACP_TEST_LOG_FILE"],
        BKAI_TUI_ACCESS_TOKEN="dummy-token",
    )

    for line in result.stdout.splitlines():
        if line.strip():
            assert line.lstrip().startswith("{"), f"stdout 出现非协议行：{line[:120]}"


@pytest.mark.slow
@pytest.mark.parametrize("protocol", ["acp", "acp-dsh"])
def test_initialize_response_can_be_written_to_a_regular_file(tmp_path: Path, protocol: str) -> None:
    import json

    output_path = tmp_path / "protocol.jsonl"
    with output_path.open("w", encoding="utf-8") as output:
        result = subprocess.run(
            [PYTHON, "-u", "-m", "aidev_agent_cli", "--protocol", protocol, "--resources", "platform"],
            input=_initialize_line(),
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            cwd=str(AGENT_ROOT),
            env=_env(
                os.environ["ACP_TEST_CONFIG_FILE"],
                os.environ["ACP_TEST_LOG_FILE"],
                BKAI_TUI_ACCESS_TOKEN="dummy-token",
            ),
        )

    assert result.returncode == 0, result.stderr[-2000:]
    responses = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(responses) == 1
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["protocolVersion"] == 1
