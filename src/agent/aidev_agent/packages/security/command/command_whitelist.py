# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command.command_whitelist

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

命令白名单数据表与禁止模式正则（纯数据，无算法）。

集中维护白名单命令集合、参数限制、禁止操作符/模式正则、
脚本扩展名与可配置项（脚本目录 / 额外放行命令）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ========== 白名单命令定义 ==========

# 类别一：系统信息查询
_SYSTEM_INFO_COMMANDS: frozenset[str] = frozenset(
    {
        "pwd",
        "date",
        "hostname",
        "whoami",
        "id",
        "uptime",
        "uname",
        "free",
        "df",
    }
)

# 类别二：文件/目录操作
_FILE_DIR_COMMANDS: frozenset[str] = frozenset(
    {
        "ls",
        "dir",
        "cd",
        "stat",
        "readlink",
        "file",
    }
)

# 类别三：文件内容操作
_FILE_CONTENT_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "awk",
        "sed",
        "diff",
    }
)

# 类别四：基础工具
_BASIC_TOOL_COMMANDS: frozenset[str] = frozenset(
    {
        "echo",
        "printf",
        "true",
        "false",
        "sleep",
        "clear",
        "reset",
    }
)

# 类别五：压缩/解压
_ARCHIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "tar",
        "gzip",
        "zip",
    }
)

# 类别六：脚本执行
_SCRIPT_COMMANDS: frozenset[str] = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "python",
        "python3",
    }
)

# 完整白名单命令集合
ALLOWED_COMMANDS: frozenset[str] = (
    _SYSTEM_INFO_COMMANDS
    | _FILE_DIR_COMMANDS
    | _FILE_CONTENT_COMMANDS
    | _BASIC_TOOL_COMMANDS
    | _ARCHIVE_COMMANDS
    | _SCRIPT_COMMANDS
)

# ========== 命令参数限制 ==========

# uname 仅允许的参数集合（"" 表示允许无参数）
_UNAME_ALLOWED_FLAGS: frozenset[str] = frozenset({"", "-s", "-v"})

# df 仅允许的参数集合
_DF_ALLOWED_FLAGS: frozenset[str] = frozenset({"", "-h"})


class ParameterRestriction:
    """参数限制基类。"""

    def is_allowed(self, args: list[str]) -> tuple[bool, str]:
        """检查参数是否符合限制。

        Args:
            args: 命令参数列表。

        Returns:
            (是否允许, 拒绝原因) 元组。
        """
        raise NotImplementedError


@dataclass
class AllowedFlagsOnly(ParameterRestriction):
    """仅允许指定的标志参数。其他标志参数拒绝，非标志参数（不以 - 开头）允许。"""

    flags: frozenset[str]
    """允许的 flag 集合，"" 表示允许无参数（即纯非 flag 参数场景）"""

    def is_allowed(self, args: list[str]) -> tuple[bool, str]:
        for arg in args:
            # 非标志参数（不以 - 开头）一律放行
            if not arg.startswith("-"):
                continue
            # 长选项如 --help 不在允许列表中则拒绝
            if arg not in self.flags:
                return False, f"不允许使用参数 '{arg}'"
        return True, ""


@dataclass
class ForbiddenFlags(ParameterRestriction):
    """禁止指定的标志，其他允许。"""

    forbidden: frozenset[str]

    def is_allowed(self, args: list[str]) -> tuple[bool, str]:
        for arg in args:
            if arg in self.forbidden:
                return False, f"不允许使用参数 '{arg}'"
        return True, ""


# 命令参数限制映射
PARAMETER_RESTRICTIONS: dict[str, ParameterRestriction] = {
    "uname": AllowedFlagsOnly(flags=_UNAME_ALLOWED_FLAGS),
    "df": AllowedFlagsOnly(flags=_DF_ALLOWED_FLAGS),
}

# ========== 禁止的操作符/模式 ==========

# 重定向操作符检测（在拆分子命令后检测）
_REDIRECT_PATTERN = re.compile(
    r"""
    (?:
        \d?>|>>|>\&    # 输出重定向：> >> 2> &>
        |
        <(?!\()         # 输入重定向 <（但不是 <( 进程替换）
        |
        <<<|<<          # Here doc/string
    )
    """,
    re.VERBOSE,
)

# 命令替换检测
_COMMAND_SUBSTITUTION_PATTERN = re.compile(r"\$\(|`[^`]*`")

# 重定向到 /dev/null 的安全模式（允许放行）
# 匹配形式：>/dev/null、2>/dev/null、&>/dev/null、>>/dev/null、2>>/dev/null 等
_REDIRECT_TO_DEV_NULL_PATTERN = re.compile(
    r"""
    (?:
        (?:\d|&)?>>?\s*/dev/null   # [n]>/dev/null 或 [n]>>/dev/null 或 &>/dev/null
    )
    """,
    re.VERBOSE,
)

# 大括号扩展检测
_BRACE_EXPANSION_PATTERN = re.compile(r"\{[^{}]*,[^{}]*\}")

# ========== 配置 ==========


def _get_env_list(name: str, default: list[str] | None = None) -> list[str]:
    """从环境变量读取逗号分隔的列表。"""
    value = os.environ.get(name)
    if value is None:
        return default if default is not None else []
    return [item.strip() for item in value.split(",") if item.strip()]


# 默认允许的脚本目录
DEFAULT_ALLOWED_SCRIPT_DIRS: list[str] = _get_env_list(
    "AIDEV_ALLOWED_SCRIPT_DIRS",
    default=["/workspace", "/home", "/tmp", "/app"],
)

# 额外放行的命令（谨慎使用）
_ADDITIONAL_ALLOWED_COMMANDS: frozenset[str] = frozenset(_get_env_list("AIDEV_ADDITIONAL_ALLOWED_COMMANDS", default=[]))

# 合并后的白名单（基础白名单 + 额外命令）
EFFECTIVE_ALLOWED_COMMANDS: frozenset[str] = ALLOWED_COMMANDS | _ADDITIONAL_ALLOWED_COMMANDS
