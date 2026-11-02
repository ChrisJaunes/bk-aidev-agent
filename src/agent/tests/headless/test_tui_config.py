"""Config-layer tests (env resolution, credential keys, plugin isolation).

These guard two defects that were invisible until the worker actually ran:

1. ``DJANGO_SETTINGS_MODULE`` came from the repository's deploy ``.env`` and
   pulled in ``aidev_bkplugin`` through ``bk_plugin.settings``. This component
   depends on ``aidev_agent`` only.
2. ``bkapi_client_core`` resolves its client credentials from ``BK_APP_CODE`` /
   ``BK_APP_SECRET``, while a PaaS env exports ``BKPAAS_APP_CODE`` /
   ``BKPAAS_APP_SECRET``. The mismatch stayed hidden because the library fell back
   to Django's settings proxy; removing Django exposed it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aidev_agent.headless import config


class TestTuiConfigEnv:
    """``bkai_tui.json`` 的 ``env`` 字段——固定值环境变量的唯一配置来源。"""

    def _write(self, tmp_path: Path, payload: dict) -> Path:
        path = tmp_path / "bkai_tui.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_the_env_field(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"env": {"BKPAAS_APP_CODE": "app-1", "BK_API_URL_TMPL": "https://x/{}/"}})

        env = config.TuiConfig.read(str(path)).env

        assert env == {"BKPAAS_APP_CODE": "app-1", "BK_API_URL_TMPL": "https://x/{}/"}

    def test_absent_env_field_is_an_empty_dict(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"agent_code": "a"})

        assert config.TuiConfig.read(str(path)).env == {}

    def test_a_malformed_env_field_degrades_to_empty(self, tmp_path: Path) -> None:
        """非 dict 形状整体丢弃；非 str 的值逐条丢弃——永不抛异常。

        （JSON 对象键经 ``json.loads`` 后恒为 str，因此"非 str 键"在文件层
        不可能出现；收窄逻辑仍防御它。）
        """
        path = self._write(tmp_path, {"env": "not-a-dict"})
        assert config.TuiConfig.read(str(path)).env == {}

        path = self._write(tmp_path, {"env": {"OK": "yes", "NESTED": {"a": 1}, "NONE": None, "NUM": 2}})
        assert config.TuiConfig.read(str(path)).env == {"OK": "yes"}

    def test_writes_preserve_the_env_field(self, tmp_path: Path) -> None:
        """/agents 与 /login 的写入不得丢掉用户手填的 env 块（保留未知键机制）。"""
        path = self._write(tmp_path, {"env": {"BKPAAS_APP_CODE": "app-1"}, "auth": {}})

        cfg = config.TuiConfig(path=str(path))
        cfg.write_agent_code("some-agent")
        cfg.write_access_token("tok")

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["env"] == {"BKPAAS_APP_CODE": "app-1"}
        assert raw["agent_code"] == "some-agent"

    def test_repr_never_echoes_env_values(self, tmp_path: Path) -> None:
        """env 可能携带凭据：repr 只报键数（日志纪律，与 token 同款）。"""
        path = self._write(tmp_path, {"env": {"BKPAAS_APP_SECRET": "SECRET-VALUE"}})

        rendered = repr(config.TuiConfig.read(str(path)))

        assert "SECRET-VALUE" not in rendered
        assert "env=1 key(s)" in rendered

    def test_credentials_resolve_from_the_env_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_credentials 的第一来源是配置 env 字段，第二来源是进程环境。

        全量运行时进程环境可能已被早期测试经 ``apply_env_to_process`` 灌入真实
        凭据（进程级泄漏、不经 monkeypatch），因此先清空全部凭据键再做断言。
        """
        for key in config.CREDENTIAL_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("LLM_GATEWAY_URL", "from-process-env")

        creds = config.get_credentials(
            {"BKPAAS_APP_CODE": "from-config", "LLM_GATEWAY_URL": "from-config"}
        )

        assert creds["BKPAAS_APP_CODE"] == "from-config"
        # 配置字段优先（既有语义，与 .env.tui 时代一致）
        assert creds["LLM_GATEWAY_URL"] == "from-config"

        only_process = config.get_credentials({})
        assert only_process == {"LLM_GATEWAY_URL": "from-process-env"}


class TestPublishBkapiAliases:
    def test_maps_paas_names_onto_bkapi_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("BK_APP_CODE", "BK_APP_SECRET", "BKPAAS_APP_ID"):
            monkeypatch.delenv(key, raising=False)

        config.publish_bkapi_aliases({"BKPAAS_APP_CODE": "app-1", "BKPAAS_APP_SECRET": "sec-1"})

        assert os.environ["BK_APP_CODE"] == "app-1"
        assert os.environ["BK_APP_SECRET"] == "sec-1"
        # `aidev_agent.config` reads BKPAAS_APP_ID for APP_CODE.
        assert os.environ["BKPAAS_APP_ID"] == "app-1"

    def test_never_overwrites_an_existing_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BK_APP_CODE", "already-set")

        config.publish_bkapi_aliases({"BKPAAS_APP_CODE": "app-1"})

        assert os.environ["BK_APP_CODE"] == "already-set"

    def test_tolerates_absent_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty env must not RAISE and must not SET the credentials.

        All FOUR spellings are cleared, because the function falls back to the
        ambient ``BKPAAS_*`` values: with only the ``BK_*`` aliases deleted, a
        leaked ``BKPAAS_APP_CODE`` (see below) is read at ``config.py`` line 232
        and republished as ``BK_APP_CODE`` — so the alias pops back and an
        absence assertion fails.

        Why ``BKPAAS_*`` can be there at all: any earlier test that builds a real
        agent runs ``apply_env_to_process``, which publishes the credentials from
        the real ``.env.tui`` into ``os.environ`` directly, NOT through
        ``monkeypatch``. That leaks process-wide, so this test passed in
        isolation and failed in the full suite. Clearing the source variables
        makes it isolation-proof while keeping the intended contract.
        """
        for key in ("BK_APP_CODE", "BK_APP_SECRET", "BKPAAS_APP_CODE", "BKPAAS_APP_SECRET", "BKPAAS_APP_ID"):
            monkeypatch.delenv(key, raising=False)

        config.publish_bkapi_aliases({})  # must not raise

        assert "BK_APP_CODE" not in os.environ
        assert "BK_APP_SECRET" not in os.environ

    def test_does_not_set_credentials_when_given_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The isolation-proof form: sentinel values survive the call untouched.

        Both the aliases and their ``BKPAAS_*`` sources are pinned, so the
        function has nothing else to fall back to and must leave them alone.
        """
        monkeypatch.setenv("BK_APP_CODE", "sentinel-code")
        monkeypatch.setenv("BK_APP_SECRET", "sentinel-secret")
        monkeypatch.setenv("BKPAAS_APP_CODE", "sentinel-paas-code")
        monkeypatch.setenv("BKPAAS_APP_SECRET", "sentinel-paas-secret")

        config.publish_bkapi_aliases({})  # must not raise

        assert os.environ["BK_APP_CODE"] == "sentinel-code"
        assert os.environ["BK_APP_SECRET"] == "sentinel-secret"


class TestDetachBkapiFromDjango:
    def test_detaches_a_django_settings_proxy(self) -> None:
        pytest.importorskip("bkapi_client_core")

        from bkapi_client_core import config as bkapi_config

        original = bkapi_config.settings._settings

        class FakeDjangoSettings:
            pass

        FakeDjangoSettings.__module__ = "django.conf"

        try:
            bkapi_config.settings._settings = FakeDjangoSettings()

            config.detach_bkapi_from_django()

            assert bkapi_config.settings._settings is None
        finally:
            bkapi_config.settings._settings = original

    def test_leaves_an_injected_settings_object_alone(self) -> None:
        pytest.importorskip("bkapi_client_core")

        from bkapi_client_core import config as bkapi_config

        original = bkapi_config.settings._settings

        class Injected:
            pass

        Injected.__module__ = "myapp.settings"
        marker = Injected()

        try:
            bkapi_config.settings._settings = marker

            config.detach_bkapi_from_django()

            assert bkapi_config.settings._settings is marker
        finally:
            bkapi_config.settings._settings = original

    def test_is_idempotent(self) -> None:
        config.detach_bkapi_from_django()
        config.detach_bkapi_from_django()


class TestTuiConfigPaths:
    """261014-urj: 进程级 TUI 配置的两级路径（``bkai_tui.json``）。"""

    def test_user_path_is_bkai_tui_json_under_the_home_dirname(self) -> None:
        from aidev_agent.headless.utils import BKAI_HOME_DIRNAME

        path = config.TuiConfig.user_path()

        assert path.endswith(f"{BKAI_HOME_DIRNAME}/bkai_tui.json")
        assert BKAI_HOME_DIRNAME in path

    def test_project_path_is_omitted_without_a_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(config.TuiConfig.CWD_ENV, raising=False)

        assert config.TuiConfig.project_path() is None

    def test_project_path_follows_the_cwd_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(config.TuiConfig.CWD_ENV, str(tmp_path))

        from aidev_agent.headless.utils import BKAI_HOME_DIRNAME

        assert config.TuiConfig.project_path() == str(
            tmp_path / BKAI_HOME_DIRNAME / "bkai_tui.json"
        )


class TestTuiConfigRead:
    """``read()`` 两级合并、项目覆盖用户、永不抛异常。"""

    def _pin_user(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        monkeypatch.setattr(config.TuiConfig, "USER_CONFIG_FILE", str(path))
        monkeypatch.delenv(config.TuiConfig.CWD_ENV, raising=False)
        monkeypatch.delenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, raising=False)

    def test_defaults_when_no_file_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._pin_user(monkeypatch, tmp_path / "absent.json")

        cfg = config.TuiConfig.read()

        assert cfg.version == 1
        assert cfg.agent_code == ""
        assert cfg.access_token == ""

    def test_auth_username_is_read_alongside_the_token(self, tmp_path: Path) -> None:
        path = tmp_path / "bkai_tui.json"
        path.write_text(
            json.dumps({"auth": {"username": "alice", "access_token": "tok"}}),
            encoding="utf-8",
        )

        cfg = config.TuiConfig.read(str(path))

        assert cfg.username == "alice"
        assert cfg.access_token == "tok"

    def test_project_overrides_user(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        user = tmp_path / "home" / "bkai_tui.json"
        user.parent.mkdir()
        user.write_text(json.dumps({"agent_code": "user-agent"}), encoding="utf-8")
        project_root = tmp_path / "proj"
        project = project_root / ".bkai" / "bkai_tui.json"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({"agent_code": "project-agent"}), encoding="utf-8")

        self._pin_user(monkeypatch, user)
        monkeypatch.setenv(config.TuiConfig.CWD_ENV, str(project_root))

        assert config.TuiConfig.read().agent_code == "project-agent"

    def test_missing_file_is_not_fatal(self, tmp_path: Path) -> None:
        assert config.TuiConfig.read(str(tmp_path / "nope.json")) == config.TuiConfig(version=1)

    def test_malformed_json_is_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "bkai_tui.json"
        path.write_text("{not json", encoding="utf-8")

        assert config.TuiConfig.read(str(path)).agent_code == ""

    def test_a_non_object_toplevel_is_not_fatal(self, tmp_path: Path) -> None:
        for body in ("[1, 2]", '"str"'):
            path = tmp_path / "bkai_tui.json"
            path.write_text(body, encoding="utf-8")

            assert config.TuiConfig.read(str(path)).agent_code == ""

    def test_a_wrongly_typed_field_degrades_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "bkai_tui.json"
        path.write_text(
            json.dumps({"version": "one", "agent_code": 123, "auth": "nope"}),
            encoding="utf-8",
        )

        cfg = config.TuiConfig.read(str(path))

        assert cfg.version == 1
        assert cfg.agent_code == ""
        assert cfg.access_token == ""

    def test_a_broken_file_does_not_echo_its_content(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """T-261014urj-02: 只记异常类别名与路径，绝不记文件片段。"""
        path = tmp_path / "bkai_tui.json"
        path.write_text('{"agent_code": "SECRET-FRAGMENT",', encoding="utf-8")

        config.TuiConfig.read(str(path))

        diagnostics = capsys.readouterr().err
        assert "SECRET-FRAGMENT" not in diagnostics
        assert "JSONDecodeError" in diagnostics


class TestTuiConfigWrite:
    """写入：允许 token 落盘（D4），保留未知键，日志/repr 不含 token 取值。"""

    def _pin(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> str:
        monkeypatch.setenv(config.TuiConfig.CONFIG_FILE_OVERRIDE, str(path))
        # Clear the two-level scope so only the pinned file is used.
        monkeypatch.delenv(config.TuiConfig.CWD_ENV, raising=False)

        return str(path)

    def test_write_access_token_lands_on_disk_and_reads_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D4 正面断言：``auth.access_token`` **可以**落盘（推翻 mw9 D5 的 TUI 侧）。"""
        path = self._pin(monkeypatch, tmp_path / "bkai_tui.json")

        config.TuiConfig().write_access_token("SECRET-TOK")

        assert json.loads(Path(path).read_text(encoding="utf-8"))["auth"]["access_token"] == "SECRET-TOK"
        assert config.TuiConfig.read().access_token == "SECRET-TOK"

    def test_write_agent_code_preserves_unknown_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = Path(self._pin(monkeypatch, tmp_path / "bkai_tui.json"))
        path.write_text(
            json.dumps({"agent_code": "old", "auth": {"access_token": "keep"}, "future": 1}),
            encoding="utf-8",
        )

        config.TuiConfig().write_agent_code("new")

        written = json.loads(path.read_text(encoding="utf-8"))

        assert written["agent_code"] == "new"
        # Unrelated keys — including the token and an unknown future key — survive.
        assert written["auth"] == {"access_token": "keep"}
        assert written["future"] == 1

    def test_write_access_token_preserves_other_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = Path(self._pin(monkeypatch, tmp_path / "bkai_tui.json"))
        path.write_text(json.dumps({"agent_code": "keep"}), encoding="utf-8")

        config.TuiConfig().write_access_token("tok")

        written = json.loads(path.read_text(encoding="utf-8"))

        assert written["agent_code"] == "keep"
        assert written["auth"]["access_token"] == "tok"

    def test_repr_never_echoes_the_token(self) -> None:
        """D4 未推翻的那一半：日志纪律——``repr`` 只报 set/unset。"""
        cfg = config.TuiConfig(agent_code="a", access_token="SECRET-TOK")

        text = repr(cfg)

        assert "SECRET-TOK" not in text
        assert "access_token=set" in text
        assert "agent_code='a'" in text

    def test_repr_reports_unset_for_empty_token(self) -> None:
        assert "access_token=unset" in repr(config.TuiConfig())

    def test_write_access_token_logs_no_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._pin(monkeypatch, tmp_path / "bkai_tui.json")

        config.TuiConfig().write_access_token("SECRET-TOK")

        diagnostics = capsys.readouterr().err
        assert "SECRET-TOK" not in diagnostics
        assert "kind=access_token" in diagnostics
