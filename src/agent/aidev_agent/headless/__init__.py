# -*- coding: utf-8 -*-
"""headless 运行面：以 stdio NDJSON 驱动 aidev-agent（REQ-6 ~ REQ-10）。

用法::

    python -m aidev_agent.headless

方法集：``create`` / ``turn`` / ``resume`` / ``cancel`` / ``close``；
事件沿用 ``ag_ui.core.events`` 原对象，stdout 只承载协议数据，日志走 stderr。
该入口默认不启用——不设置任何开关时进程行为与基线一致。
"""

from .cli import main, serve
from .errors import HeadlessError

__all__ = ["HeadlessError", "main", "serve"]
