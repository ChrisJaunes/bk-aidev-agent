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
"""

from __future__ import annotations

from aidev_agent.exceptions import AgentException

from .points import HookPoint

__all__ = ["HookDeniedError"]


class HookDeniedError(AgentException):
    """hook 返回 ``deny`` 时可读的终止错误。

    消息固定形如 ``hook deny at <point>: <reason>``，供运行入口直接呈现给调用方。
    """

    def __init__(self, point: HookPoint, reason: str = ""):
        self.point = point
        self.reason = reason
        super().__init__(message=f"hook deny at {point.value}: {reason}")
