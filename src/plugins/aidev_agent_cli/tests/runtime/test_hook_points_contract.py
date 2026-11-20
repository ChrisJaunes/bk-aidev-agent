# -*- coding: utf-8 -*-
"""Hook 纯值契约与协议点位 → SDK hook 点位的映射。

``HookConfig`` / 点位名 / 降级原因 / ``hook_degraded`` 事件名住在纯 stdlib 的
:mod:`aidev_agent_cli.hooks.contract`（父进程解析 ``hooks`` 配置、写 worker 快照，
不需要 SDK）。协议点位经 ``sdk.hooks.build_hook_set`` 挂到 SDK 的 ``HookSet``：
``turn_start`` / ``tool_pre`` / ``tool_post`` 映射到同名 SDK 点位。
"""

from __future__ import annotations

import pytest
from aidev_agent.core.hook import HookPoint
from aidev_agent_cli.hooks import contract as hook_contract
from aidev_agent_cli.sdk.hooks import HOOK_SET_GRACE_SECONDS, HeadlessHookBus, build_hook_set


def _bus(points: set[str], timeout_ms: int = 2000) -> HeadlessHookBus:
    config = hook_contract.HookConfig(timeout_ms=timeout_ms, points=frozenset(points))
    return HeadlessHookBus(config=config, session_code="s1", channel=None, writer=None)


def test_point_value_accepts_sdk_enums_and_plain_strings() -> None:
    for point in HookPoint:
        assert hook_contract.point_value(point) == point.value
    assert hook_contract.point_value("tool_pre") == "tool_pre"


def test_default_timeout_is_used_when_hooks_are_omitted() -> None:
    config = hook_contract.HookConfig.parse(None)

    assert config.timeout_ms == hook_contract.DEFAULT_TIMEOUT_MS
    assert config.points() == hook_contract.ALL_HOOK_POINTS


@pytest.mark.parametrize(
    "points, expected",
    [
        ({"turn_start"}, {HookPoint.TURN_START}),
        ({"tool_pre"}, {HookPoint.TOOL_PRE}),
        # tool_post 需要 tool_pre 处理器记录本次调用的 tool_input
        ({"tool_post"}, {HookPoint.TOOL_PRE, HookPoint.TOOL_POST}),
        (set(hook_contract.ALL_HOOK_POINTS), {HookPoint.TURN_START, HookPoint.TOOL_PRE, HookPoint.TOOL_POST}),
    ],
)
def test_protocol_points_map_to_sdk_points(points, expected) -> None:
    hook_set = build_hook_set(_bus(points), turn_id="t1", input_text="hi", first_turn=True)

    assert {point for point in HookPoint if hook_set.handlers(point)} == expected


def test_no_enabled_point_injects_nothing_and_timeout_follows_config() -> None:
    assert build_hook_set(_bus(set()), turn_id="t1", input_text="", first_turn=True) is None

    hook_set = build_hook_set(_bus({"tool_pre"}, timeout_ms=7000), turn_id="t1", input_text="", first_turn=True)
    assert hook_set.timeout_seconds == 7 + HOOK_SET_GRACE_SECONDS
