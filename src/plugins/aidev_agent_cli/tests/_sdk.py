# -*- coding: utf-8 -*-
"""测试侧构造单轮 :class:`AgentRun`：关键字参数按 ``AgentSpec`` 字段与运行期参数拆开。

生产路径里 spec 由 executor 从 runtime 快照构造；测试直接给字段。``checkpoint_path`` 缺省时与 runtime
同源（ProcessConfig.checkpoint_path 的环境变量覆盖，conftest 已把它指到临时文件）。
"""

from __future__ import annotations

import os

from dataclasses import fields
from typing import Any

from aidev_agent_cli.runtime.process_config import CHECKPOINT_FILE_ENV
from aidev_agent_cli.sdk.executor import AgentRun
from aidev_agent_cli.sdk.spec import AgentSpec, LocalPorts

_SPEC_FIELDS = {item.name for item in fields(AgentSpec)}


class ChannelPorts(LocalPorts):
    """把测试的 hook 通道（``request(**payload)``）接成 ``TurnPorts``。"""

    def __init__(self, channel: Any = None, cancelled: Any = None) -> None:
        self._channel = channel
        self._cancelled = cancelled

    def cancelled(self) -> bool:
        return bool(self._cancelled is not None and self._cancelled())

    def hook(self, **payload: Any):
        if self._channel is None:
            return super().hook(**payload)
        return self._channel.request(**payload)


def make_run(
    *,
    state: Any = None,
    ports: Any = None,
    ledger: Any = None,
    checkpointer: Any = None,
    hook_channel: Any = None,
    **spec_fields: Any,
) -> AgentRun:
    unknown = set(spec_fields) - _SPEC_FIELDS
    if unknown:
        raise TypeError(f"unknown AgentSpec fields: {sorted(unknown)}")
    if "checkpoint_path" not in spec_fields:
        spec_fields["checkpoint_path"] = os.environ[CHECKPOINT_FILE_ENV]
    if ports is None and hook_channel is not None:
        ports = ChannelPorts(hook_channel)
    return AgentRun(AgentSpec(**spec_fields), state, ports, ledger=ledger, checkpointer=checkpointer)


__all__ = ["ChannelPorts", "make_run"]
