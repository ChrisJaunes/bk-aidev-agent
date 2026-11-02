"""``aidev_agent.headless.runtime`` tracing tests: the default endpoint, the opt-out, and the degrading.

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
import os
import subprocess
import sys

import pytest

from aidev_agent.headless import runtime as otel


@pytest.fixture(autouse=True)
def recording_install(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture what ``install_otel`` resolved, without building a real exporter.

    ``install_otel`` imports ``BkAidevAgentInstrumentor`` from the SDK module at
    call time, so patching it THERE is the seam: the fake records the endpoints
    off the ``OTelConfig`` it is constructed with. This lets a test assert the
    collected/deduplicated set through the single public entry point.
    """
    import aidev_agent.packages.opentelemetry.instrumentor as inst_mod

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

    def test_the_bk_report_env_route_is_collected(
        self, recording_install: dict, only_bk_report_env: None
    ) -> None:
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

    def test_both_sources_are_merged(
        self, recording_install: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A collector AND bk-report is the documented multi-endpoint shape."""
        monkeypatch.setenv(
            otel.ENDPOINTS_ENV, '[{"url": "http://127.0.0.1:4318", "exporter_type": "http"}]'
        )
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

    def test_duplicates_are_collapsed(
        self, recording_install: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_it_does_not_log_a_token(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
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
        """Simulated by making the import fail — the shape a broken install has."""
        import builtins

        real_import = builtins.__import__

        def _fail(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("aidev_agent.packages.opentelemetry"):
                raise ImportError("simulated missing package")

            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail)

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
        """Runtime helpers remain importable before environment bootstrap."""
        code = (
            "import sys; import aidev_agent.headless.runtime;"
            "assert 'aidev_agent.config' not in sys.modules;"
            "assert 'aidev_agent.headless.dispatch' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr


def test_the_shutdown_event_is_created_before_the_loop_is_published() -> None:
    """G-05 / WR-05: no window where ``_loop`` is set but ``_shutdown_requested`` is None.

    A SIGTERM handler runs on the main thread and can fire between the two
    statements in ``main()``. If the loop is published first, the handler's
    ``_request_shutdown_threadsafe`` sees ``event is None`` and returns,
    dropping the signal — the worker then runs until stdin EOF. Pin the
    ORDER from the real source, because no runtime test can land a signal in
    a two-statement window deterministically.
    """
    source = inspect.getsource(otel.main)
    event_at = source.index("_shutdown_requested = asyncio.Event()")
    loop_at = source.index("_loop = asyncio.get_running_loop()")

    assert event_at < loop_at, (
        "the shutdown event must be created BEFORE the loop is published "
        "(G-05); the reverse order reopens the startup SIGTERM window"
    )


def test_main_wires_install_otel_after_bootstrap_and_before_the_dispatcher() -> None:
    """The instrumentation is actually CALLED from ``main()`` (it once was not).

    ``install_otel`` was defined, documented ("Call this from ``__main__``
    immediately after ``bootstrap_environment``"), and unit-tested in isolation —
    yet no commit ever invoked it, so the worker emitted no traces at all. Every
    existing test called the function directly, which is exactly why the missing
    wiring went unnoticed. Pin the CALL, not just the function.

    ``bootstrap_environment`` now runs at **module top level** (261015: the SDK's
    ``api.domains`` assembles ``BKAIDEV_URL`` at import time, and the top-level
    ``.dispatch`` import chain reaches it — a bootstrap inside ``main()`` would be
    too late; see ``test_bootstrap_runs_before_the_dispatch_import``). The ORDER
    assertions therefore span the module source: top-level bootstrap < ``main()``'s
    ``install_otel`` < the ``Dispatcher`` construction. The instrumentor
    monkey-patches ``ChatCompletionAgent._get_agent``, so it must run before the
    SDK's services layer is imported.
    """
    module_source = inspect.getsource(otel.main)

    def _statement_line(needle: str) -> int:
        """Index of the first line whose CODE is ``needle`` (commented-out lines excluded).

        A plain ``str.index`` matches inside a comment too — which is exactly how
        an earlier version of this test passed against a commented-out call. Strip
        the comment marker and require an exact statement match.
        """
        for index, raw in enumerate(module_source.splitlines()):
            line = raw.split("#", 1)[0].strip()
            if line == needle:
                return index
        raise AssertionError(f"no active statement {needle!r} found in __main__; is it wired?")

    bootstrap_at = _statement_line("bootstrap_environment(TuiConfig.read().env)")
    otel_at = _statement_line("install_otel(env_values)")
    dispatcher_at = _statement_line("dispatcher = Dispatcher(")

    assert bootstrap_at < otel_at, (
        "install_otel must run AFTER bootstrap_environment publishes the env — "
        "the SDK reads endpoints from the process environment"
    )
    assert otel_at < dispatcher_at, (
        "install_otel must run BEFORE the Dispatcher is built, whose import chain "
        "reaches aidev_agent; the instrumentor monkey-patches at activation and "
        "cannot take effect once the SDK is imported (constraint 1)"
    )


def test_bootstrap_runs_before_the_dispatch_import() -> None:
    """Bootstrap must precede dispatch, which freezes SDK platform domains.

    Both operations now live in main(). Type-checking-only imports do not
    execute; TestRuntimeImports separately checks the module import boundary.
    """
    module_source = inspect.getsource(otel.main)

    def _code_line(needle: str) -> int:
        for index, raw in enumerate(module_source.splitlines()):
            line = raw.split("#", 1)[0].strip()
            if line == needle:
                return index
        raise AssertionError(f"no active statement {needle!r} found in __main__")

    bootstrap_at = _code_line("bootstrap_environment(TuiConfig.read().env)")
    # 行尾的 ``# noqa: E402`` 已被 _code_line 剥掉，needle 只匹配代码部分。
    dispatch_import_at = _code_line("from .dispatch import Dispatcher")

    assert bootstrap_at < dispatch_import_at, (
        "bootstrap_environment must run BEFORE the .dispatch import: the SDK "
        "freezes BKAIDEV_URL at import time and a later publish never reaches it"
    )
