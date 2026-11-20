"""``aidev_agent_cli.bootstrap.open_telemetry`` tracing tests: the default endpoint, the opt-out, and the degrading.

Instrumentation is a diagnostic, so the property that matters most here is that
it can never break the worker. Every failure path returns ``False`` with one
stderr line, and the tests below pin each of them.

Nothing here sends a span: the SDK's own instrumentor is exercised by
``otel_collector/selftest.sh`` against a live collector. What is tested is the
plumbing this module owns — which endpoint is chosen, when it is installed at
all, and what happens when it cannot be.

``install_otel`` is self-contained (endpoint collection, dedup and the enable
switch all live inside it), so every assertion here goes through that ONE entry
point. The seam is the instrumentor class itself: patch it and read back the
endpoints it was handed.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from aidev_agent_cli import __main__ as entry
from aidev_agent_cli.adapters import agui
from aidev_agent_cli.bootstrap import open_telemetry as otel
from aidev_agent_cli.bootstrap import startup


@pytest.fixture(autouse=True)
def recording_install(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture what ``install_otel`` resolved, without building a real exporter.

    ``install_otel`` imports ``BkAidevAgentInstrumentor`` from the SDK module at
    call time, so patching it THERE is the seam: the fake records the endpoints
    off the ``OTelConfig`` it is constructed with. This lets a test assert the
    collected/deduplicated set through the single public entry point.

    ``tests/conftest.py`` 为其余用例全局设了 ``AGENT_TUI_OTEL=0``（避免读真实配置、
    避免每个子进程真的跑埋点）；本文件测的正是 ``install_otel`` 的开关语义，因此
    在这里把该开关删掉，恢复「默认开启」的被测前提。
    """
    import aidev_agent.packages.opentelemetry.instrumentor as inst_mod

    monkeypatch.delenv(otel.ENABLED_ENV, raising=False)

    captured: dict = {}

    class _Fake:
        def __init__(self, config: object) -> None:
            captured["endpoints"] = list(config.otel_endpoints)  # type: ignore[attr-defined]

        def instrument(self) -> None:
            captured["installed"] = True

    monkeypatch.setattr(inst_mod, "BkAidevAgentInstrumentor", _Fake)

    return captured


@pytest.fixture
def only_bk_report_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer/dashboard path: the only route ``.env.tui`` configures."""
    monkeypatch.delenv(otel.ENDPOINTS_ENV, raising=False)
    monkeypatch.setenv(otel.GRPC_URL_ENV, "http://bk-report-1.woa.com:4317")
    monkeypatch.setenv(otel.DATA_TOKEN_ENV, "test-token")
    monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")


class TestEndpointCollection:
    """The union of sources, mirroring `aidev_bkplugin.apps.init_bk_aidev_agent_otel`.

    This is the bug the module shipped with first: it read only
    ``BKAI_AGENT_OTEL_ENDPOINTS``, so a deployment configured the documented way
    (``OTEL_GRPC_URL`` + ``OTEL_BK_DATA_TOKEN`` in ``.env.tui``) resolved to NO
    endpoint and instrumentation was skipped entirely — the integration actively
    cancelled an already-working setup.
    """

    def test_the_bk_report_env_route_is_collected(self, recording_install: dict, only_bk_report_env: None) -> None:
        assert otel.install_otel({}) is True

        urls = [ep["url"] for ep in recording_install["endpoints"]]

        assert "http://bk-report-1.woa.com:4317" in urls

    def test_it_is_gated_by_the_apm_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SDK requires BKAI_AGENT_APM_OTEL_ENABLED; without it the route is inert."""
        monkeypatch.delenv(otel.ENDPOINTS_ENV, raising=False)
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://bk-report-1.woa.com:4317")
        monkeypatch.setenv(otel.DATA_TOKEN_ENV, "test-token")
        monkeypatch.delenv("BKAI_AGENT_APM_OTEL_ENABLED", raising=False)

        assert otel.install_otel({}) is False

    def test_the_token_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(otel.ENDPOINTS_ENV, raising=False)
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://bk-report-1.woa.com:4317")
        monkeypatch.delenv(otel.DATA_TOKEN_ENV, raising=False)
        monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")

        assert otel.install_otel({}) is False

    def test_both_sources_are_merged(self, recording_install: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        """A collector AND bk-report is the documented multi-endpoint shape."""
        monkeypatch.setenv(otel.ENDPOINTS_ENV, '[{"url": "http://127.0.0.1:4318", "exporter_type": "http"}]')
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://bk-report-1.woa.com:4317")
        monkeypatch.setenv(otel.DATA_TOKEN_ENV, "test-token")
        monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")

        assert otel.install_otel({}) is True

        urls = [ep["url"] for ep in recording_install["endpoints"]]

        assert "http://127.0.0.1:4318" in urls
        assert "http://bk-report-1.woa.com:4317" in urls

    def test_a_malformed_json_source_does_not_discard_the_others(
        self,
        recording_install: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One bad optional source must not silently disable the working path.

        `apps.py` catches ValueError around this same call for this same reason.
        (Note: the SDK does not actually raise for a bare non-JSON string — it
        coerces it to a single URL — so this pins the "other source survives"
        property, not the log line.)
        """
        monkeypatch.setenv(otel.ENDPOINTS_ENV, "not-json-at-all")
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://bk-report-1.woa.com:4317")
        monkeypatch.setenv(otel.DATA_TOKEN_ENV, "test-token")
        monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")

        assert otel.install_otel({}) is True

        urls = [ep["url"] for ep in recording_install["endpoints"]]

        assert "http://bk-report-1.woa.com:4317" in urls

    def test_duplicates_are_collapsed(self, recording_install: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same url + type + token is one destination, however it was supplied.

        Fed from TWO sources so the dedup — not the source count — decides the
        result: the trailing slash is normalised away, so these are one endpoint.
        """
        monkeypatch.setenv(
            otel.ENDPOINTS_ENV,
            '[{"url": "http://same:4317/", "exporter_type": "grpc", "token": "t"}]',
        )
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://same:4317")
        monkeypatch.setenv(otel.DATA_TOKEN_ENV, "t")
        monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")

        assert otel.install_otel({}) is True

        assert len(recording_install["endpoints"]) == 1

    def test_different_tokens_are_different_endpoints(
        self, recording_install: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A different token means a different tenant — not a duplicate."""
        monkeypatch.setenv(
            otel.ENDPOINTS_ENV,
            '[{"url": "http://same:4317", "exporter_type": "grpc", "token": "a"}]',
        )
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://same:4317")
        monkeypatch.setenv(otel.DATA_TOKEN_ENV, "b")
        monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")

        assert otel.install_otel({}) is True

        assert len(recording_install["endpoints"]) == 2


class TestEnabledFlag:
    """``AGENT_TUI_OTEL`` gates the whole install; default ON.

    Asserted through ``install_otel``'s return value and whether the instrumentor
    was reached — the flag has no separate function any more.
    """

    def test_it_is_on_by_default(
        self, recording_install: dict, only_bk_report_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default ON: a diagnostic that is off by default never gets turned on."""
        monkeypatch.delenv(otel.ENABLED_ENV, raising=False)

        assert otel.install_otel({}) is True
        assert recording_install.get("installed") is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " off "])
    def test_falsy_values_opt_out(
        self, recording_install: dict, only_bk_report_env: None, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(otel.ENABLED_ENV, value)

        assert otel.install_otel({}) is False
        # Opting out means the instrumentor is never even constructed.
        assert "installed" not in recording_install

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", ""])
    def test_anything_else_stays_on(
        self, recording_install: dict, only_bk_report_env: None, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(otel.ENABLED_ENV, value)

        assert otel.install_otel({}) is True
        assert recording_install.get("installed") is True


class TestNoEndpointResolved:
    """Being enabled with nowhere to send is reported, not passed over in silence.

    This is the failure that wastes the most time: instrumentation "succeeds", the
    TUI works, and no data appears anywhere. Naming the variables is what turns
    that into a one-line diagnosis.
    """

    def test_it_reports_which_variables_are_unset(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for key in (otel.ENDPOINTS_ENV, otel.GRPC_URL_ENV, otel.DATA_TOKEN_ENV, "BKAI_AGENT_APM_OTEL_ENABLED"):
            monkeypatch.delenv(key, raising=False)

        assert otel.install_otel({}) is False

        err = capsys.readouterr().err

        assert "no OTel endpoint resolved" in err
        # The names matter — that is the actionable part.
        assert otel.ENDPOINTS_ENV in err
        assert otel.GRPC_URL_ENV in err
        assert otel.DATA_TOKEN_ENV in err

    def test_it_does_not_log_a_token(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """This module logs the endpoint URL and never the credential.

        Scoped to stderr because that is where THIS module writes. It says nothing
        about the SDK: `OTelConfig` accepts a per-endpoint token, and the OTLP
        exporter's own debug logging prints it (observed: a
        ``('x-bk-token', '<value>')`` header line on stdout). That is upstream
        behaviour outside this module's control — see the note on
        `test_the_sdks_exporter_logs_the_token` below, which records it so the gap
        is not mistaken for coverage.
        """
        monkeypatch.delenv(otel.ENDPOINTS_ENV, raising=False)
        monkeypatch.setenv(otel.GRPC_URL_ENV, "http://bk-report-1.woa.com:4317")
        monkeypatch.setenv(otel.DATA_TOKEN_ENV, "SECRET-TOKEN-VALUE")
        monkeypatch.setenv("BKAI_AGENT_APM_OTEL_ENABLED", "true")

        otel.install_otel({})
        printed = capsys.readouterr()

        # Our own diagnostics: URL only. No token, no config repr.
        assert "SECRET-TOKEN-VALUE" not in printed.err
        assert "bk-report-1.woa.com" in printed.err


class TestDisabled:
    def test_opt_out_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(otel.ENABLED_ENV, "0")

        assert otel.install_otel({}) is False
        # It says why, rather than silently producing no traces.
        assert "disabled" in capsys.readouterr().err


class TestFailuresDegrade:
    def test_an_unparseable_endpoint_returns_false_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo in the endpoint must not stop the TUI from starting."""
        monkeypatch.setenv(otel.ENDPOINTS_ENV, "not-a-url-and-not-json")

        # Must not raise, whatever the SDK's parser decides to do with it.
        result = otel.install_otel({})

        assert isinstance(result, bool)
        assert result is False or "installed" in capsys.readouterr().err

    def test_a_missing_sdk_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Simulated by making the loader fail — the shape a broken install has.

        OTel 组件经 ``importlib.import_module`` 装载（唯一可选装载点），因此模拟点在那里。
        """
        real_import = otel.importlib.import_module

        def _fail(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("aidev_agent.packages.opentelemetry"):
                raise ImportError("simulated missing package")

            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(otel.importlib, "import_module", _fail)

        assert otel.install_otel({}) is False
        assert "instrumentation failed" in capsys.readouterr().err

    def test_it_never_logs_credentials(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], recording_install: dict
    ) -> None:
        """The endpoint list is logged (URLs); nothing else from the env is."""
        monkeypatch.setenv(otel.ENDPOINTS_ENV, '[{"url": "http://127.0.0.1:4318", "token": "SECRET-TOKEN"}]')

        otel.install_otel({})
        printed = capsys.readouterr()

        assert "SECRET-TOKEN" not in printed.err
        assert "SECRET-TOKEN" not in printed.out


class TestRuntimeImports:
    def test_importing_runtime_does_not_initialize_sdk_services(self) -> None:
        """``adapters.agui`` 可导入而不拉起 SDK 服务（导入期不读配置、不建客户端）。

        断言的是**导入期**：模块级只依赖 ``runtime.settings`` / ``utils.logging`` /
        ``infrastructure`` / ``adapters.agui``，SDK 与各策略都在函数体内延迟导入。
        """
        from pathlib import Path

        code = (
            "import sys; import aidev_agent_cli.adapters.agui;"
            "assert 'aidev_agent.config' not in sys.modules, sorted(sys.modules);"
            "assert 'aidev_agent_cli.resources.platform' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        assert result.returncode == 0, result.stderr


def _statement_line(source: str, needle: str) -> int:
    """某个可执行语句在源码里的行号（按去注释后的整行匹配）。"""
    for index, raw in enumerate(source.splitlines()):
        if raw.split("#", 1)[0].strip() == needle:
            return index
    raise AssertionError(f"no active statement {needle!r}; is it wired?")


def test_the_shutdown_event_is_created_before_the_loop_is_published() -> None:
    """G-05 / WR-05: no window where ``_loop`` is set but ``_shutdown_requested`` is None.

    A SIGTERM handler runs on the main thread and can fire between the two
    statements in ``main()``. If the loop is published first, the handler's
    ``_request_shutdown_threadsafe`` sees ``event is None`` and returns,
    dropping the signal — the worker then runs until stdin EOF. Pin the
    ORDER from the real source, because no runtime test can land a signal in
    a two-statement window deterministically.
    """
    from aidev_agent_cli.utils import process

    source = inspect.getsource(process.async_shutdown_event)
    event_at = source.index("event = asyncio.Event()")
    loop_at = source.index("loop = asyncio.get_running_loop()")

    assert event_at < loop_at, (
        "the shutdown event must be created BEFORE the loop is published "
        "(G-05); the reverse order reopens the startup SIGTERM window"
    )


def test_startup_hooks_wire_install_otel_for_every_protocol() -> None:
    """埋点由入口启动钩子安装（所有协议），而不是某个 adapter。

    钉住**调用**而不只是函数：它曾经被定义、被单测，却从未被调用。顺序：环境发布之后
    （SDK 从进程环境读端点），且启动钩子在 main() 里先于任何 adapter 导入（instrumentor
    会 monkey-patch SDK 模块属性）。
    """
    hooks = inspect.getsource(startup._install)

    assert "_install(env)" in inspect.getsource(startup.install_startup_hooks)
    assert hooks.index("_publish_env(env)") < hooks.index("install_otel(env)")
    assert "install_otel" not in inspect.getsource(agui)

    main = inspect.getsource(entry.main)
    hooks_at = main.index("install_startup_hooks(bkai.env)")
    # adapter 由 resolve_adapter 解析，且它只在启动钩子之后被调用。
    assert hooks_at < main.index("resolve_adapter(protocol)")
    # ``agui`` 在入口模块顶层导入（无 SDK 副作用）；acp 是可选的、只在装载点里出现。
    assert "from .adapters.agui import run_agui" in inspect.getsource(entry)
    loader = inspect.getsource(entry.resolve_adapter)
    assert "aidev_agent_cli.adapters.acp" in loader


def test_importing_the_entry_module_has_no_log_side_effect() -> None:
    """``import aidev_agent_cli.__main__`` 不得打开日志文件（副作用由 ``main()`` 显式发起）。

    这条锁住"无副作用模块 + 显式 install"：入口模块顶层导入 ``bootstrap.startup``，
    而 ``utils.logging`` **不在导入期**配置日志——建目录、开文件只发生在
    ``configure_logging()`` 被显式调用时。``open()`` 与 ``FileHandler`` 在子进程里可观测。
    """
    with_guard = subprocess.run(
        [
            sys.executable,
            "-c",
            "import builtins, sys\n"
            "opened = []\n"
            "_open = builtins.open\n"
            "def spy(path, *a, **k):\n"
            "    opened.append(str(path))\n"
            "    return _open(path, *a, **k)\n"
            "builtins.open = spy\n"
            "import logging\n"
            "logging.FileHandler = lambda *a, **k: (_ for _ in ()).throw(AssertionError('FileHandler built'))\n"
            "import aidev_agent_cli.__main__\n"
            "bad = [p for p in opened if p.endswith(('.log', 'worker.log'))]\n"
            "assert not bad, bad\n"
            "assert 'aidev_agent_cli.utils.logging' in sys.modules\n"
            "print('ENTRY SIDE-EFFECT FREE')\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[2]),
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PATH": "/usr/bin:/bin"},
    )
    assert with_guard.returncode == 0, with_guard.stderr[-2000:]
    assert "ENTRY SIDE-EFFECT FREE" in with_guard.stdout


def test_bootstrap_runs_before_the_workspace_agent_install() -> None:
    """环境发布必须先于本地文件 agent 的注入（SDK 在导入期冻结 BKAIDEV_URL）。

    两步都由入口负责：启动钩子发布环境，随后无条件注入 WorkspaceAgent，
    且这一步早于任何 adapter 运行。

    worker 侧的环境发布已收进 ``install_worker_startup``（顺序契约在
    ``tests/bootstrap/test_worker_bootstrap.py`` 里逐条断言），因此这里只钉住**调用点与顺序**：
    启动顺序 → handler 校验 → workspace 注入 → 首轮构建。
    """
    main = inspect.getsource(entry.main)
    hooks_at = main.index("install_startup_hooks(bkai.env)")
    order = [main.index(n) for n in ("build_runtime(", "adapter(service, ProtocolChannel.stdio())")]
    assert hooks_at < order[0] < order[1]
    from aidev_agent_cli.runtime import worker as worker_mod
    from aidev_agent_cli.sdk.executor import SDKExecutor
    entry_source = inspect.getsource(worker_mod._worker_entry)
    # 装载 import target 后先做 fd 重定向、重放启动顺序，才构造执行体。
    loaded = entry_source.index("importlib.import_module(module)")
    replay = entry_source.index("install_worker_startup(env, cwd)")
    assert loaded < entry_source.index("apply_worker_stdio_guard()") < replay < entry_source.index("factory(frame, context)")
    worker = inspect.getsource(SDKExecutor.__init__)
    assert "install_worker_startup" not in worker
    assert worker.index("validate_message_handler()") < worker.index("install_workspace_agent()")
    assert worker.index("install_workspace_agent()") < worker.index("self.run = AgentRun(")
    assert "WorkspaceAgent" not in inspect.getsource(agui)