# -*- coding: utf-8 -*-
"""上下文存储的取值配置。

配置来源为 ``aidev_agent.config`` 中的四个 ``BKAI_CONTEXT_STORE_*`` 常量，
在 ``ContextStore`` 构造时读取一次（等价「随进程启动生效、不支持热更新」）；
测试与上层可直接用 :class:`StoreConfig` 覆盖，无需改动环境变量。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoreConfig:
    """热/冷两层容量与维护阈值。"""

    hot_capacity: int = 1000
    cold_capacity: int = 5000
    idle_seconds: int = 900
    sweep_interval: int = 60

    def __post_init__(self) -> None:
        for name, value in (
            ("hot_capacity", self.hot_capacity),
            ("cold_capacity", self.cold_capacity),
            ("idle_seconds", self.idle_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} 必须为正整数，当前为 {value!r}")
        # 节流周期允许为 0：表示不做节流，写/更新/失效入口每次都触发维护
        if not isinstance(self.sweep_interval, int) or isinstance(self.sweep_interval, bool) or self.sweep_interval < 0:
            raise ValueError(f"sweep_interval 必须为非负整数，当前为 {self.sweep_interval!r}")

    @classmethod
    def from_settings(cls) -> "StoreConfig":
        """从模块级配置常量构造（非法值已在 ``config._env_positive_int`` 中回落默认）。"""

        from aidev_agent import config

        return cls(
            hot_capacity=config.BKAI_CONTEXT_STORE_HOT_CAPACITY,
            cold_capacity=config.BKAI_CONTEXT_STORE_COLD_CAPACITY,
            idle_seconds=config.BKAI_CONTEXT_STORE_IDLE_SECONDS,
            sweep_interval=config.BKAI_CONTEXT_STORE_SWEEP_INTERVAL,
        )
