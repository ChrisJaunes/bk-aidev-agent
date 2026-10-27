# -*- coding: utf-8 -*-
"""T-1 / REQ-12 / AC-15：配置安全解析与 ``StoreConfig``。"""

from __future__ import annotations

import logging
import runpy

import pytest
from aidev_agent import config
from aidev_agent.core.packages.context_engine.settings import StoreConfig

ENV_DEFAULTS = {
    "BKAI_CONTEXT_STORE_HOT_CAPACITY": 1000,
    "BKAI_CONTEXT_STORE_COLD_CAPACITY": 5000,
    "BKAI_CONTEXT_STORE_IDLE_SECONDS": 900,
    "BKAI_CONTEXT_STORE_SWEEP_INTERVAL": 60,
}
LOG_PREFIX = "[ContextOffloading]"


def _reload_config(monkeypatch, **env_overrides):
    """清空四个变量后按需注入，再以 runpy 重新执行 ``config.py``（AC-15 的导入口径）。"""

    for name in ENV_DEFAULTS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env_overrides.items():
        monkeypatch.setenv(name, value)
    return runpy.run_path(config.__file__)


@pytest.mark.parametrize(("name", "default"), list(ENV_DEFAULTS.items()))
def test_unset_env_falls_back_to_default(monkeypatch, name, default):
    module = _reload_config(monkeypatch)

    assert module[name] == default
    assert module["settings"]._defaults[name] == default


@pytest.mark.parametrize(("name", "default"), list(ENV_DEFAULTS.items()))
def test_valid_env_overrides_default(monkeypatch, name, default):
    module = _reload_config(monkeypatch, **{name: str(default + 7)})

    assert module[name] == default + 7


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "", "  "])
def test_invalid_env_falls_back_with_single_warning(monkeypatch, caplog, raw):
    # runpy.run_path 以 ``<run_path>`` 作为模块名，故在 root 层捕获并按消息过滤
    with caplog.at_level(logging.WARNING):
        module = _reload_config(monkeypatch, BKAI_CONTEXT_STORE_HOT_CAPACITY=raw)

    assert module["BKAI_CONTEXT_STORE_HOT_CAPACITY"] == 1000
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "BKAI_CONTEXT_STORE_HOT_CAPACITY" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert LOG_PREFIX in warnings[0].getMessage()


def test_invalid_env_does_not_break_module_import(monkeypatch):
    """非法值不得把配置错误升级为进程启动失败（REQ-12）。"""

    module = _reload_config(
        monkeypatch,
        BKAI_CONTEXT_STORE_HOT_CAPACITY="oops",
        BKAI_CONTEXT_STORE_COLD_CAPACITY="0",
        BKAI_CONTEXT_STORE_IDLE_SECONDS="abc",
        BKAI_CONTEXT_STORE_SWEEP_INTERVAL="",
    )

    assert module["BKAI_CONTEXT_STORE_HOT_CAPACITY"] == 1000
    assert module["BKAI_CONTEXT_STORE_COLD_CAPACITY"] == 5000
    assert module["BKAI_CONTEXT_STORE_IDLE_SECONDS"] == 900
    assert module["BKAI_CONTEXT_STORE_SWEEP_INTERVAL"] == 60


def test_helper_is_not_registered_as_setting():
    """``_env_positive_int`` 定义在 ``existed_keys`` 之前，不得被注册为配置项。"""

    defaults = config.settings._defaults

    assert "_env_positive_int" not in defaults
    assert "logger" not in defaults
    assert hasattr(config, "_env_positive_int")


def test_store_config_from_settings_matches_constants():
    store_config = StoreConfig.from_settings()

    assert store_config.hot_capacity == config.BKAI_CONTEXT_STORE_HOT_CAPACITY
    assert store_config.cold_capacity == config.BKAI_CONTEXT_STORE_COLD_CAPACITY
    assert store_config.idle_seconds == config.BKAI_CONTEXT_STORE_IDLE_SECONDS
    assert store_config.sweep_interval == config.BKAI_CONTEXT_STORE_SWEEP_INTERVAL


def test_store_config_can_be_constructed_directly():
    store_config = StoreConfig(hot_capacity=5, cold_capacity=6, idle_seconds=7, sweep_interval=0)

    assert (store_config.hot_capacity, store_config.cold_capacity) == (5, 6)
    assert (store_config.idle_seconds, store_config.sweep_interval) == (7, 0)


@pytest.mark.parametrize("field", ["hot_capacity", "cold_capacity", "idle_seconds"])
def test_store_config_rejects_non_positive_fields(field):
    with pytest.raises(ValueError, match=field):
        StoreConfig(**{field: 0})
