# -*- coding: utf-8 -*-
"""module 模式：``--hooks`` 的 ``module`` 指向一个 Python 文件 / 模块，worker 内按 SDK 6 个点位名取处理器。"""

from __future__ import annotations

import pytest
from aidev_agent.core.hook import HookPoint

from aidev_agent_cli.__main__ import HOOKS_ENV
from aidev_agent_cli.hooks.contract import SDK_HOOK_POINTS, HookConfig
from aidev_agent_cli.hooks.handler import parse_hooks_option
from aidev_agent_cli.sdk.hooks import HeadlessHookBus, build_hook_set
from aidev_agent_cli.utils.errors import HeadlessError

HOOK_FILE = """
calls = []

def turn_start(ctx):
    calls.append(ctx.point.value)

def tool_pre(ctx):
    calls.append(ctx.point.value)

not_a_point = lambda ctx: None
"""


def test_module_config_parses_and_round_trips(tmp_path) -> None:
    path = tmp_path / "my_hook.py"
    path.write_text(HOOK_FILE, encoding="utf-8")
    config = HookConfig.parse({"module": str(path), "timeout_ms": 1500})
    assert config.module == str(path.resolve())
    assert config.points() == SDK_HOOK_POINTS
    assert HookConfig.parse(config.snapshot()).module == config.module

    restricted = HookConfig.parse({"module": "pkg.hooks", "points": ["model_pre", "turn_end"]})
    assert restricted.points() == {"model_pre", "turn_end"}
    with pytest.raises(HeadlessError):
        HookConfig.parse({"points": ["model_pre"]})  # 客户端回调模式不支持模型点位
    with pytest.raises(HeadlessError):
        HookConfig.parse({"module": ""})


def test_module_hook_set_registers_defined_points_only(tmp_path) -> None:
    path = tmp_path / "my_hook.py"
    path.write_text(HOOK_FILE, encoding="utf-8")
    config = HookConfig.parse({"module": str(path), "timeout_ms": 1500})
    bus = HeadlessHookBus(config=config, session_code="s1", channel=None, writer=None)

    hook_set = build_hook_set(bus, turn_id="t1", input_text="hi", first_turn=True)

    registered = {point for point in HookPoint if hook_set.handlers(point)}
    assert registered == {HookPoint.TURN_START, HookPoint.TOOL_PRE}
    assert hook_set.timeout_seconds == 1.5
    assert hook_set.handlers(HookPoint.TURN_START)[0].__module__.startswith("aidev_hook_module_")

    narrowed = HookConfig.parse({"module": str(path), "points": ["tool_pre"]})
    narrowed_set = build_hook_set(
        HeadlessHookBus(config=narrowed, session_code="s1", channel=None, writer=None),
        turn_id="t1",
        input_text="",
        first_turn=False,
    )
    assert {point for point in HookPoint if narrowed_set.handlers(point)} == {HookPoint.TOOL_PRE}


def test_env_var_is_the_hooks_fallback(monkeypatch, tmp_path) -> None:
    assert HOOKS_ENV == "BKAI_AGENT_HOOKS"
    handler = parse_hooks_option('{"module": "x.y"}')
    assert handler.config.module == "x.y"
