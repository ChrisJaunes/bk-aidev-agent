# -*- coding: utf-8 -*-
"""审批裁剪：置空 approval_settings。"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from .base import ResourceLoader

logger = getLogger(__name__)


class ApprovalLoader(ResourceLoader):
    """审批清空：TUI 的固定裁剪，置空 ``raw["approval_settings"]``。

    它**不属于**六源之一，而是 TUI 对平台配置的固定裁剪（用户裁决 D3）。原先这段
    逻辑在 ``get_agent_config`` 的编排层（``cfg.approval_settings = {}``）；D3 把它
    移进加载器链：

    * 审批不在 TUI 的范围内——其副作用 worker 是 SDK 指向插件方向
      （``aidev_bkplugin``）的最后一处反向引用；清空绑定意味着该 interrupt
      **永远不会被触发**。完整理由见 ``agent_factory`` 中同一处说明。
    * 进链后，清空对**所有**读平台配置的路径都生效（``agent.info`` 的两半、快照
      路径）——这比留在编排层更好（那里只有 ``get_agent_config`` 那一条清）。

    链序位置：**链首**（裁决 (c)）。它是**置空**而非合并，唯一正确性要求是「最终为
    空」。除本类外没有任何 loader 写 ``approval_settings``，所以顺序本身无实质影响；
    放链首让「裁剪」在任何来源叠加之前就完成，可读性最好，且与「审批不属于六源」的
    定位一致。

    ``or {}`` 语义保留：``approval_settings=None`` 时 ``super()`` 的映射结果是
    ``{}``（``approval_settings=res.get("approval_settings") or {}``），置空为 ``{}``
    与之等价——因此 ``test_tolerates_absent_approval_settings`` 继续通过。
    """

    name = "approval"

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw.get("approval_settings"):
            logger.info("clearing approval_settings (approval is out of scope for the TUI)")

        raw["approval_settings"] = {}

        return raw
