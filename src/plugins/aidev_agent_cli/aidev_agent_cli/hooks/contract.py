# -*- coding: utf-8 -*-
"""Hook 的**纯值契约**：点位名、降级原因、``hook_degraded`` 事件名与 ``hooks`` 配置解析。

为什么住在这里
--------------
``HookConfig`` 只是「超时上限 + 启用点位集合」这对值，父进程解析它、把它写进 worker 快照，
**不需要** SDK。而 SDK 在导入期会拉起 ``ag_ui``（``ag_ui`` 导入期会 print 到 fd 1，那是协议
通道）——把它放在本模块就等于让每个 adapter 的导入都提前踩进 SDK 边界。因此：

* 点位名 / 降级原因 / 事件名 / ask 中断 reason 是本协议自己的**线上契约字符串**，在这里以
  常量固化；协议点位到 SDK hook 点位的映射在 ``sdk.hooks.build_hook_set``；
* ``point_value`` 的语义（枚举取值 / 原样字符串）在这里重写，标准库实现；
* SDK 侧的 ``HeadlessHookBus`` 住在 ``sdk.hooks``，它是执行体的一部分。

本模块只允许标准库 + ``utils.errors``：不得 import ``ag_ui`` /
``aidev_agent`` / ``pydantic``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils.errors import HOOK_VALUE_INVALID, HeadlessError

#: 支持的 hook 点位。取值是线上契约。
ALL_HOOK_POINTS: frozenset[str] = frozenset({"turn_start", "tool_pre", "tool_post"})

#: SDK 原生的 6 个点位：``module`` 模式下 hook 模块按这些名字定义处理器。
SDK_HOOK_POINTS: frozenset[str] = frozenset(
    {"turn_start", "model_pre", "model_post", "tool_pre", "tool_post", "turn_end"}
)

#: 降级原因（写入 ``hook_degraded`` 事件的 ``value.reason``）。
DEGRADE_TIMEOUT = "timeout"
DEGRADE_HOST_ERROR = "host_error"
DEGRADE_INVALID_SHAPE = "invalid_shape"
DEGRADE_UNKNOWN_DECISION = "unknown_decision"

#: hook「转人工」中断的 reason（沿用 ``aidev:`` 命名空间）。
HOOK_ASK_REASON = "aidev:hook_ask"

#: 降级事件的 ``name``。
HOOK_DEGRADED_EVENT_NAME = "hook_degraded"

#: ``turn_start`` 结果里携带上下文的键。
TURN_START_CONTEXT_KEY = "context"

#: 单点等待裁决的缺省上限（毫秒）
DEFAULT_TIMEOUT_MS = 10000
#: 超时上限（1 小时）——超过按上限钳制
MAX_TIMEOUT_MS = 3_600_000
#: 等待期间巡检取消标志的粒度（秒）
CANCEL_POLL_INTERVAL = 0.05

#: 等待结果
WAIT_REPLY = "reply"
WAIT_TIMEOUT = "timeout"
WAIT_CANCELLED = "cancelled"


def point_value(point: Any) -> str:
    """把点位入参统一成字符串：枚举取 ``.value``，字符串原样，其余 ``str()``。

    它只是"枚举或字符串"的归一，不涉及 SDK 类型。
    """
    value = getattr(point, "value", None)

    return value if isinstance(value, str) else str(point)


class HookConfig:
    """``create.params.hooks`` 解析结果（REQ-13：超时上限 + 启用点位）。"""

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        points: frozenset[str] | None = None,
        module: str | None = None,
    ) -> None:
        self.timeout_ms = timeout_ms
        #: ``module`` 模式：worker 内直接加载的 hook 模块（模块名或 ``.py`` 绝对路径）；None 为客户端回调模式。
        self.module = module
        default = SDK_HOOK_POINTS if module else ALL_HOOK_POINTS
        self._points = frozenset(points) if points is not None else frozenset(default)

    @classmethod
    def parse(cls, raw: Any) -> "HookConfig":
        """解析 ``hooks`` 配置；形状不合法一律 ``hook.value_invalid``。"""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise HeadlessError(
                HOOK_VALUE_INVALID,
                f"hooks 配置类型不符（期望 object，实际 {type(raw).__name__}）",
                {"fields": ["hooks"]},
            )

        timeout_ms = raw.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise HeadlessError(
                HOOK_VALUE_INVALID,
                "hooks.timeout_ms 必须是正整数（毫秒）",
                {"fields": ["hooks.timeout_ms"]},
            )
        timeout_ms = min(timeout_ms, MAX_TIMEOUT_MS)

        module = raw.get("module")
        if module is not None:
            if not isinstance(module, str) or not module.strip():
                raise HeadlessError(
                    HOOK_VALUE_INVALID, "hooks.module 必须是模块名或 .py 文件路径", {"fields": ["hooks.module"]}
                )
            module = module.strip()
            if module.endswith(".py"):
                # worker 会切换 cwd：文件路径在父进程解析时就固定成绝对路径。
                module = str(Path(module).expanduser().resolve())
        allowed = SDK_HOOK_POINTS if module else ALL_HOOK_POINTS

        raw_points = raw.get("points")
        if raw_points is None:
            points = frozenset(allowed)
        else:
            if not isinstance(raw_points, (list, tuple, set, frozenset)):
                raise HeadlessError(
                    HOOK_VALUE_INVALID,
                    "hooks.points 类型不符（期望字符串列表）",
                    {"fields": ["hooks.points"]},
                )
            unknown = [item for item in raw_points if item not in allowed]
            if unknown:
                raise HeadlessError(
                    HOOK_VALUE_INVALID,
                    f"hooks.points 含未知点位：只支持 {' / '.join(sorted(allowed))}",
                    {"fields": ["hooks.points"], "unknown": sorted(str(item) for item in unknown)},
                )
            points = frozenset(raw_points)
        return cls(timeout_ms=timeout_ms, points=points, module=module)

    def snapshot(self) -> dict[str, Any]:
        """写进 worker 快照的形状（``parse`` 可原样读回）。"""
        value: dict[str, Any] = {"timeout_ms": self.timeout_ms, "points": sorted(self._points)}
        if self.module:
            value["module"] = self.module
        return value

    def enabled(self, point: Any) -> bool:
        return point_value(point) in self._points

    def points(self) -> frozenset[str]:
        return self._points


__all__ = [
    "ALL_HOOK_POINTS",
    "CANCEL_POLL_INTERVAL",
    "DEFAULT_TIMEOUT_MS",
    "DEGRADE_HOST_ERROR",
    "DEGRADE_INVALID_SHAPE",
    "DEGRADE_TIMEOUT",
    "DEGRADE_UNKNOWN_DECISION",
    "HOOK_ASK_REASON",
    "HOOK_DEGRADED_EVENT_NAME",
    "HookConfig",
    "SDK_HOOK_POINTS",
    "MAX_TIMEOUT_MS",
    "TURN_START_CONTEXT_KEY",
    "WAIT_CANCELLED",
    "WAIT_REPLY",
    "WAIT_TIMEOUT",
    "point_value",
    "sdk_package_path",
]
