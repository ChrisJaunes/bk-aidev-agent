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

文件发送拦截 hook。

对应「Agent 向用户投递文件 / 产物前」的安全闸门：阻止可执行 / 脚本 / 密钥等
危险文件类型，以及文件名命中敏感关键字、文件内容命中内容安全基线的场景。
与 ``utils/file_safety``（文件读写敏感路径拒绝）互补：后者管「Agent 能读写哪些
路径」，本模块管「Agent 能向用户投递哪些文件」。
"""

from __future__ import annotations

import logging

from aidev_agent.packages.security.content_safety import scan_content_safety
from aidev_agent.packages.security.events import (
    SecurityEvent,
    SecurityStage,
    SecurityVerdict,
)
from aidev_agent.packages.security.file_safety import SENSITIVE_KEYWORDS, SENSITIVE_SUFFIXES

logger = logging.getLogger(__name__)

# 可执行二进制扩展名（Agent 不应向用户投递）
_EXECUTABLE_SUFFIXES = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".msi",
    ".apk",
    ".app",
    ".com",
    ".scr",
)

# 高风险脚本扩展名（shell / powershell / 批处理，可携带恶意载荷）
_SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd")

# 危险 / 敏感扩展名（可执行 + 高风险脚本 + 密钥）
FILE_SEND_DENY_SUFFIXES = frozenset(_EXECUTABLE_SUFFIXES + _SCRIPT_SUFFIXES + tuple(SENSITIVE_SUFFIXES))


class FileSendGuardHook:
    """文件发送拦截 hook：阻止 Agent 投递危险 / 敏感文件。

    在 ``SecurityStage.FILE_SEND`` 检查点，依据事件 ``metadata`` 中的文件名 /
    扩展名与正文内容判定：可执行 / 脚本 / 密钥文件，或内容命中内容安全基线，
    一律硬拦截（``block``）。
    """

    name = "file_send_guard"

    def inspect(self, event: SecurityEvent) -> SecurityVerdict | None:
        if event.stage is not SecurityStage.FILE_SEND:
            return None

        filename = str(event.metadata.get("filename") or event.metadata.get("name") or "")
        suffix = str(event.metadata.get("suffix") or "")
        if not suffix and "." in filename:
            suffix = filename.rsplit(".", 1)[-1].lower()

        # 1) 扩展名：可执行 / 脚本 / 密钥
        if suffix:
            normalized = suffix if suffix.startswith(".") else f".{suffix}"
            if normalized in FILE_SEND_DENY_SUFFIXES:
                return SecurityVerdict(
                    action="block",
                    hook=self.name,
                    reason=f"文件类型 {normalized} 禁止发送",
                    findings=[{"type": "suffix", "value": normalized}],
                )

        # 2) 文件名敏感关键字（private_key / api_key / secret 等）
        lower_name = filename.lower()
        for kw in SENSITIVE_KEYWORDS:
            if kw in lower_name:
                return SecurityVerdict(
                    action="block",
                    hook=self.name,
                    reason=f"文件名包含敏感关键字 {kw}",
                    findings=[{"type": "filename", "value": kw}],
                )

        # 3) 文件内容命中内容安全基线
        if event.content:
            findings = scan_content_safety(event.content)
            if findings:
                return SecurityVerdict(
                    action="block",
                    hook=self.name,
                    reason="文件内容触发安全策略，已拦截",
                    findings=findings,
                )

        return None


# 默认单例
file_send_guard_hook = FileSendGuardHook()


__all__ = [
    "FILE_SEND_DENY_SUFFIXES",
    "FileSendGuardHook",
    "file_send_guard_hook",
]
