# -*- coding: utf-8 -*-
"""工作区：``WorkspaceIndex`` 列第一层文件（+ 目录）给 ``@`` 菜单。

**这里有什么、没有什么**
本地文件 runtime 的 agent 类与安装入口（``WorkspaceAgent`` / ``install_workspace_agent``）
是**运行时装配**，住在 :mod:`aidev_agent_cli.runtime.workspace_agent`——它们不带任何资源语义，
放在 ``resources`` 里会把「加载了什么资源」和「用什么 agent 类」混为一谈。本模块只留
资源侧需要的一件：文件列表（``WorkspaceIndex``）。

**根从哪来**
待读取目录就是 :func:`os.getcwd`（进程启动时的工作目录）；``WorkspaceIndex.read`` 也接受
调用方显式传入的 ``root``。没有会话级覆盖、没有环境变量兜底——工作区根只有一处判定。

``WorkspaceIndex.read()`` / ``rows_for()`` 保持无参 / 既有签名形态（它们被
:class:`LocalResourceManager` 与 loader 链直接调用）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)


# ============================================================================
# §4  工作区文件列表 —— WorkspaceIndex
#     工作区**第一层**（文件 + 目录）的实时读取；唯一所有者是 LocalResourceManager。
#     DirectoryListing 是它产出的值对象，留在模块级。
# ============================================================================

"""工作区文件列表，以及它为何这样读。

只列第一层，且每次投影都重读（D-28 已推翻）
-------------------------------------------
本类曾被要求「每个 worker 进程只爬取一次并永久缓存」（D-28）：递归 ``rg --files``
遍历约 8.1 万个路径开销很大，因此结果被缓存，并配了一套就绪/重试/截断机制。
**该决定已被用户推翻**，理由是实测与语义两条：

* 实测（本机，2026-10-14）：递归 ``rg --files`` = **75 ms / 81,170 条**；
  第一层 ``rg --files --max-depth 1`` = 5 ms / 38 条；
  **第一层 ``os.scandir`` = 0.02 ms / 63 条（25 目录 + 38 文件）**。
  ``os.scandir`` 比 ``rg`` 子进程快约 250 倍，且天然同时给出文件与目录。
* 语义：agent 运行过程中新建文件很常见，缓存会让用户看不到新文件。

因此本类现在**不缓存**：每次读 ``agent.info`` 时重新 ``os.scandir`` 一次第一层。
读取开销 0.02 ms，重复读取无妨，且各方法保持自洽。

为何没有 ripgrep
----------------
``rg`` 只能列**文件**，而用户裁决「文件与目录都要」，故改用 ``os.scandir``。
连带地整条 ``rg`` 解析/下载/子进程链（``resolve_ripgrep`` / ``_cached_binary`` /
``_download_ripgrep`` / ``RIPGREP_VERSION`` / ``CACHE_ENV_VAR`` / ``_which_rg``）
**已删除**——它在本用途上不再需要，且删除即缩小供应链面（不再运行时下载可执行文件）。

为何没有截断与拒绝清单
----------------------
第一层最多几千条，永远触及不到旧 ``MAX_PATHS = 30_000``，故 ``truncated`` 是死字段
（用户裁决删除）。旧 ``DENYLIST_DIRS``（``node_modules`` / ``.git`` 等）也随之删除：
第一层模式下没什么可过滤的，顶层若正好有 ``node_modules`` 目录就会出现在列表里
——这是用户明确接受的。

D-27 的 no-silent-degrade **保留**
----------------------------------
失败（root 缺失、权限错误）仍必须说明原因，由 ``reason`` 单独承载。空工作区与健康
读取都说不出话（``reason == ""``），两者在结构上不区分——因为确实没有区别。

读取是同步的
------------
``os.scandir`` 是单次 syscall 的迭代，无子进程、无网络，故 :meth:`read` 是**同步**方法。
旧设计需要 ``asyncio.to_thread`` 正是因为 ``rg`` 是**子进程**；删掉子进程后该理由消失。
其调用方 ``describe_agent_config`` 已被 ``asyncio.to_thread`` 包住，第一层读取自然落在
那个线程里。

条目形状
--------
:meth:`read` 的 ``entries`` 元素是**第一层的名字**：

* 文件：``"README.md"``（第一层下只有顶层文件，故不含 ``/``）。
* 目录：**带结尾 ``/``**，如 ``"src/"``。

:meth:`rows_for` 把它们变成 ``resources[]`` 的 ``{"code", "name", "type": "file"}`` 行，
其中 ``code`` 是工作区相对路径。前端据此渲染 ``@`` 菜单的「文件」分组。

排序：目录在前，其次按名不区分大小写，再次按原名
（确定性平局裁决，不依赖宿主机 locale）。
"""


@dataclass(frozen=True)
class DirectoryListing:
    """一次工作区第一层读取的结果。

    替代旧的 :class:`CrawlResult`：后者承载 ``truncated`` / ``source``（ripgrep 渠道），
    在本模式下都不存在。

    ``entries`` 是**已排序**的 ``names``——目录条目带结尾 ``/``。读取不可用时为空列表
    （root 缺失或 ``OSError``），因此文件分组为空，而不是让 ``agent.info`` 失败。
    """

    entries: list[str] = field(default_factory=list)


class WorkspaceIndex:
    """工作区**第一层**（文件 + 目录）的实时读取。

    无状态：没有缓存槽、没有就绪标志、没有重试常量。待读取目录就是 :func:`os.getcwd`
    （或 :meth:`read` 显式传入的 ``root``）。

    它的所有者是 :class:`LocalResourceManager`（``self.files`` +
    ``self.workspace_files``）——``Session`` 不再持有第二个实例。
    """

    def read(self, root: str | None = None) -> DirectoryListing:
        """读取工作区**第一层**（文件 + 目录），**同步**。

        ``root`` 缺省取 :func:`os.getcwd`。目录条目在名字后追加 ``/``；
        文件用原名。**不递归**。

        对普通的环境问题**从不抛异常**：root 缺失或 ``OSError``（权限等）产生一个
        **空** :class:`DirectoryListing`，因为 TUI 必须仍能启动（文件分组只是 ``@``
        的一个来源，不是前置条件）。调用方因此只会看到一个空的文件分组。

        ``entry.is_dir()`` 默认**不跟随**符号链接，避免目录环与越界遍历。
        """
        resolved = Path(root or os.getcwd()).expanduser()

        if not resolved.is_dir():
            return DirectoryListing()

        try:
            with os.scandir(resolved) as scanner:
                # 只取 ``entry.name``（**不含路径**）：名字不参与任何 shell/子进程调用，
                # 且第一层不需要完整路径（T-t1s-01）。
                entries = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in scanner]
        except OSError:
            # 权限等错误降级为空列表，绝不抛——TUI 仍要能启动（T-t1s-05）。
            return DirectoryListing()

        entries.sort(key=lambda name: (not name.endswith("/"), name.removesuffix("/").lower(), name.removesuffix("/")))

        return DirectoryListing(entries=entries)

    def rows_for(self, directory: str, listing: DirectoryListing | None = None) -> list[dict[str, Any]]:
        """``directory`` 对应的 ``{"code", "name", "type": "file"}`` 行。

        经 :meth:`read` 实时派生（不缓存），并在每次调用时**重新折叠**。

        ``listing`` 可选：传入已读好的 :class:`DirectoryListing` 即**不再扫描**，
        使需要同一份读取派生多项的调用方只扫一遍。

        ``directory == ""`` 表示工作区根目录——这是生产路径唯一使用的取值。只出现
        ``directory`` **下一层**的条目（深层路径只贡献其第一段）。目录条目在 ``name`` 中
        带结尾 ``/``；``type`` 恒为 ``"file"``（``agent-info.ts`` 原样透传 ``type``）。

        一条条目是目录有**两个**来源：它以结尾 ``/`` 显式声明（:meth:`read` 对第一层
        目录这样做），或它作为更深条目的前缀出现。第一层模式下前者是常态，后者仍保留
        以支持传入更深层的 :class:`DirectoryListing`。
        """
        prefix = [segment for segment in directory.replace("\\", "/").split("/") if segment]
        by_name: dict[str, dict[str, Any]] = {}

        for path in (listing if listing is not None else self.read()).entries:
            # 结尾 ``/`` 是本条目“是目录”的显式声明（read 对第一层目录如此产出）。
            declares_directory = path.endswith("/")
            segments = [segment for segment in path.replace("\\", "/").split("/") if segment]

            # 跳过任何不在 `directory` 之下的路径，以及与之同级者（文件不能是自己的
            # 父级）。同时跳过共享前缀但更短的路径。
            if len(segments) <= len(prefix):
                continue

            if any(segments[index] != segment for index, segment in enumerate(prefix)):
                continue

            name = segments[len(prefix)]
            is_directory = declares_directory or len(segments) > len(prefix) + 1

            # 同时以直接条目和前缀形式出现的名称是目录——前缀胜出，因为目录与文件
            # 不能同名。
            existing = by_name.get(name)

            if existing is not None and existing["name"].endswith("/"):
                continue

            by_name[name] = {
                "code": "/".join([*prefix, name]),
                "name": f"{name}/" if is_directory else name,
                "type": "file",
                # 排序键携带在行本身（并在下面剥离）：目录在前，然后按名称不区分大小写排序。
                # 不留在线上形态中，因为该行被定义为 ``{code, name, type}``。
                "_is_directory": is_directory,
            }

        ordered = sorted(
            by_name.values(),
            key=lambda row: (
                not row["_is_directory"],
                row["name"].removesuffix("/").lower(),
                row["name"].removesuffix("/"),
            ),
        )

        return [{key: row[key] for key in ("code", "name", "type")} for row in ordered]
