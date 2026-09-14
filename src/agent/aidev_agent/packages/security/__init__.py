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

外置安全 hook 框架（安平现成服务的可插拔接入层）。

本包承载「复用安平现成服务」的落地：安平内容安全 / 威胁情报 / 注入检测 / DLP
等服务以 :class:`SecurityHook` 形式实现并 :func:`register_hook` 接入，``core``
层只需在检查点调用 :func:`run_hooks` 统一触发，不感知具体服务。

依赖方向：本包只允许依赖标准库 / pydantic / langchain_core / ``pydantic_models`` /
本包内模块，禁止 ``from aidev_agent.core`` / ``from aidev_agent.services`` / ``from aidev_agent.api``。
"""

from __future__ import annotations

import logging

from aidev_agent.packages.security.events import (
    SecurityEvent,
    SecurityStage,
    SecurityVerdict,
    stricter,
)
from aidev_agent.packages.security.hooks import (
    SecurityHook,
    default_hooks,
    register_hook,
    registered_hooks,
    run_hooks,
    unregister_hook,
)

logger = logging.getLogger(__name__)

__all__ = [
    # 事件 / 判定类型
    "SecurityStage",
    "SecurityEvent",
    "SecurityVerdict",
    "stricter",
    # hook 协议 / 注册表 / 分发
    "SecurityHook",
    "register_hook",
    "unregister_hook",
    "registered_hooks",
    "run_hooks",
    "default_hooks",
]
