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

from pydantic import BaseModel, Field

from aidev_agent.config import settings


class ToolNodeSettings(BaseModel):
    """ToolNode wrappers settings.

    安全相关字段（use_security_guard / use_network_allowlist / use_reward_hacking_guard /
    use_result_limit / allow_domains / block_domains）默认取安全开启态，最终值由
    ``ReActAgentBuilder`` 在 graph 装配层从 ``SecuritySettings`` 拆解后注入。本类
    不持有 ``SecuritySettings`` 对象、也不直接读取环境变量。
    """

    use_timer: bool = True
    use_result_limit: bool = True
    result_limit_thrd: int = Field(default=settings.TOOL_RESULT_LIMIT_THRD, ge=1, description="结果长度限制阈值")
    result_truncate_head: int = Field(
        default=settings.TOOL_RESULT_TRUNCATE_HEAD, ge=0, description="截断时头部保留字符数（0=自动 80%）"
    )
    result_truncate_tail: int = Field(
        default=settings.TOOL_RESULT_TRUNCATE_TAIL, ge=0, description="截断时尾部保留字符数（0=自动 20%）"
    )
    use_json_repair_on_error: bool = True
    use_security_guard: bool = True
    use_network_allowlist: bool = True
    use_reward_hacking_guard: bool = True
    allow_domains: list[str] = Field(
        default_factory=list,
        description="网络白名单域名（由 graph 装配层从 SecuritySettings.network_allow_domains 拆解注入）",
    )
    block_domains: list[str] = Field(
        default_factory=list,
        description="网络黑名单域名（由 graph 装配层从 SecuritySettings.network_block_domains 拆解注入）",
    )
