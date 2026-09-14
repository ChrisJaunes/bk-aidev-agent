# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.

安全 hook 协议、注册表与分发。

安平现成服务（内容安全 / 威胁情报 / 注入检测 / DLP 等）通过实现
:class:`SecurityHook` 并 :func:`register_hook` 接入，成为「外置 hook」；
``core`` 层只需在检查点调用 :func:`run_hooks`，即可统一触发所有已接入的
安平服务，无需感知具体实现。默认 hook 集合由 :func:`default_hooks` 提供，
供无显式装配时的兜底。

依赖方向：本模块只依赖标准库 + 本包 :mod:`events`，禁止 import
``aidev_agent.core`` / ``aidev_agent.services`` / ``aidev_agent.api``。
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol, Sequence

from aidev_agent.packages.security.events import (
    SecurityEvent,
    SecurityVerdict,
    stricter,
)

logger = logging.getLogger(__name__)


class SecurityHook(Protocol):
    """安全 hook 协议。

    实现方需提供 ``name`` 与 ``inspect``。``inspect`` 返回 ``None`` 表示放行
    （本 hook 不处理该事件 / 未命中），否则返回 :class:`SecurityVerdict`。
    单个 hook 应自行按 ``event.stage`` 过滤它关心的检查点。
    """

    name: str

    def inspect(self, event: SecurityEvent) -> SecurityVerdict | None: ...


# ---------------------------------------------------------------------------
# 注册表（线程安全：注册/注销加锁，分发用快照，避免运行时互斥）
# ---------------------------------------------------------------------------

_registry: list[SecurityHook] = []
_registry_lock = threading.Lock()


def register_hook(hook: SecurityHook) -> None:
    """注册一个安全 hook（幂等：同名不重复注册）。"""
    if not hasattr(hook, "name") or not hook.name:
        raise ValueError("SecurityHook 必须提供非空 name")
    with _registry_lock:
        if all(existing.name != hook.name for existing in _registry):
            _registry.append(hook)


def unregister_hook(name: str) -> bool:
    """按 name 注销 hook，返回是否真的移除。"""
    with _registry_lock:
        for idx, hook in enumerate(_registry):
            if hook.name == name:
                _registry.pop(idx)
                return True
    return False


def registered_hooks() -> tuple[SecurityHook, ...]:
    """返回当前已注册 hook 的不可变快照（按注册顺序）。"""
    with _registry_lock:
        return tuple(_registry)


def _snapshot() -> Sequence[SecurityHook]:
    """取分发快照；空注册表时回落默认 hook 集合（开箱即用，不依赖显式装配）。"""
    hooks = registered_hooks()
    return hooks if hooks else default_hooks()


def run_hooks(event: SecurityEvent) -> SecurityVerdict:
    """运行所有 hook，聚合为最严格判定（fail-closed）。

    聚合语义：``block`` 优先于 ``review`` 优先于 ``allow``。任一 hook 抛出异常
    仅记录日志并跳过（不因单个服务故障拖垮主流程），但不会因此放行——若其余
    hook 要求拦截仍会拦截。

    Args:
        event: 待检测的安全事件。

    Returns:
        聚合后的 :class:`SecurityVerdict`；无任何命中时为 ``allow``。
    """
    verdict: SecurityVerdict | None = None
    for hook in _snapshot():
        try:
            result = hook.inspect(event)
        except Exception:  # noqa: BLE001 —— 单个安平服务故障不应阻断主流程
            logger.exception("security hook %s inspect failed", getattr(hook, "name", "?"))
            continue
        if result is None:
            continue
        verdict = stricter(verdict, result) if verdict is not None else result
    if verdict is None:
        return SecurityVerdict(action="allow")
    return verdict


def default_hooks() -> tuple[SecurityHook, ...]:
    """返回默认 hook 集合（内容安全 + 文件发送拦截）。

    放这里而非模块 import 时注册，避免 import 副作用（遵循 interrupt_manager
    D-01「不做模块级注册装配」约定）；默认集合仅在注册表为空时兜底启用。
    """
    from aidev_agent.packages.security.providers.content_safety import (
        content_safety_hook,
    )
    from aidev_agent.packages.security.providers.file_send_guard import (
        file_send_guard_hook,
    )

    return (content_safety_hook, file_send_guard_hook)


__all__ = [
    "SecurityHook",
    "register_hook",
    "unregister_hook",
    "registered_hooks",
    "run_hooks",
    "default_hooks",
]
