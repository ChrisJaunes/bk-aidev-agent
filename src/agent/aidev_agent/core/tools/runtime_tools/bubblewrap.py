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

Bubblewrap 子进程沙箱执行器（fail-open）。

为 ``FilesystemBackend`` 提供内核级文件隔离：read/write/ls/glob/grep/
edit/execute/upload/download 等受限能力改为在 bwrap 子进程内执行，
而非同进程 ``os.open``/``Path`` 直接读写。

设计原则（对齐 Claude Code / Langdata 中心化沙箱的四条围栏）：

1. **默认什么都不给**：沙箱根是空 tmpfs，仅显式挂载 ``readonly_paths``
   （默认 ``/usr,/bin,/lib,/lib64``，提供 cat/find/rg/python3 等命令所需的
   可执行文件与动态库）与工作区（root_dir）。``/etc``、``/root``、``/home``
   等敏感目录**不挂载**，沙箱内「不存在」而非「无权限」。
2. **能只读绝不给写**：系统目录 ``--ro-bind``，仅工作区可写（``--bind``）；
   read 类操作工作区也以 ``--ro-bind`` 暴露。
3. **默认关闭网络**：``--unshare-net``，read/write 默认无网，阻断外泄/SSRF。
4. **进程隔离 + 共存亡**：``--unshare-pid`` + ``--die-with-parent``，沙箱内
   进程随调用方退出而终止。

fail-open 语义：本模块**不抛异常**。``is_available()`` 探测 bwrap 二进制是否
存在且能创建沙箱；不可用或执行失败时，由调用方（``FilesystemBackend``）决定
退回同进程现状路径，仅记录日志——评估阶段关闭开关即可观测行为差异。
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from aidev_agent.pydantic_models import SandboxPolicy

logger = logging.getLogger(__name__)

# 默认只读挂载的系统目录：提供常用命令（cat/find/rg/python3/sh）及动态库，
# 不含 /etc、/root、/home 等敏感路径（按需最小暴露）。
DEFAULT_READONLY_PATHS = "/usr,/bin,/lib,/lib64"


@dataclass
class BwrapResult:
    """bwrap 子进程执行结果。

    Attributes:
        output: 合并后的 stdout + stderr（stdout 优先，stderr 追加）。
        exit_code: 退出码；``None`` 表示执行过程异常（如超时/未启动）。
    """

    output: str = ""
    exit_code: int | None = None


class SandboxBackend(Protocol):
    """沙箱后端协议：把平台无关策略翻译成隔离命令前缀。

    未来 macOS（Seatbelt）/ Windows（受限令牌+AppContainer）实现同一协议，
    复用同一份 :class:`SandboxPolicy`，避免每次重写隔离逻辑。
    """

    def render(self, policy: SandboxPolicy, root_dir: str, writable: bool) -> list[str]:
        """把策略翻译为命令前缀（不含具体命令）。"""
        ...


class BubblewrapBackend:
    """``SandboxPolicy`` → bwrap argv 的 Linux 实现。

    把平台无关策略翻译为 bubblewrap 命令前缀，承载原 ``BubblewrapSandbox._prefix``
    的组装逻辑。未来 macOS/Windows 后端实现同一 :class:`SandboxBackend` 协议。
    """

    def __init__(self, bwrap_path: str = "bwrap") -> None:
        self._bwrap_path = bwrap_path

    def render(self, policy: SandboxPolicy, root_dir: str, writable: bool) -> list[str]:
        """组装 bwrap 命令前缀（不含具体命令）。

        Args:
            policy: 平台无关沙箱策略。
            root_dir: 工作区根目录（唯一数据挂载点）。
            writable: True 时工作区以 ``--bind`` 可写挂载，False 时 ``--ro-bind``。
        """
        prefix = [self._bwrap_path, "--unshare-user"]
        for d in policy.readonly_paths:
            if os.path.isdir(d):
                prefix += ["--ro-bind", d, d]
        # 进程内依赖的虚拟文件系统（/proc 提供 procfs；/dev 提供 /dev/null 等）。
        # 注意：受限容器内 --unshare-pid 与 --proc 挂载会冲突，故不用 pid namespace，
        # 由 --die-with-parent 保证沙箱子进程随调用方退出。
        prefix += ["--proc", "/proc", "--dev", "/dev"]
        # 工作区：唯一数据挂载点
        flag = "--bind" if writable else "--ro-bind"
        prefix += [flag, root_dir, root_dir]
        prefix += ["--chdir", root_dir]
        if not policy.allow_network:
            prefix += ["--unshare-net"]
        prefix += ["--die-with-parent"]
        return prefix


def _select_backend(bwrap_path: str) -> SandboxBackend:
    """按平台选择沙箱后端。

    Linux → :class:`BubblewrapBackend`（bwrap argv）。macOS（Seatbelt）/
    Windows（受限令牌+AppContainer）后端在后续阶段落地，当前统一回落
    bwrap 语义；非 Linux 平台上 bwrap 不可用时由 ``is_available`` 探测
    fail-open 降级同进程执行，不影响功能。

    Args:
        bwrap_path: bwrap 二进制路径（仅 Linux 后端使用）。
    """
    system = platform.system()
    if system == "Linux":
        return BubblewrapBackend(bwrap_path=bwrap_path)
    logger.warning(
        "当前平台 %s 尚无专用沙箱后端（macOS Seatbelt / Windows 受限令牌待接入），"
        "暂回落 Linux bwrap 语义；bwrap 不可用时 fail-open 降级同进程",
        system,
    )
    return BubblewrapBackend(bwrap_path=bwrap_path)


class BubblewrapSandbox:
    """bwrap 子进程沙箱执行器（编排器：持有策略 + 后端）。

    Args:
        root_dir: 工作区根目录（沙箱内唯一数据挂载点）。
        policy: 平台无关沙箱策略（:class:`SandboxPolicy`）。优先于
            ``readonly_paths`` / ``allow_network`` 旧参数。
        readonly_paths: 逗号分隔的只读挂载目录（系统命令 + 动态库），
            默认 ``/usr,/bin,/lib,/lib64``。空字符串则不挂载任何系统目录。
            （兼容旧接口；``policy`` 未提供时用于构建默认策略。）
        allow_network: 是否保留网络。默认 False（``--unshare-net``）。
            （兼容旧接口。）
        bwrap_path: bwrap 二进制路径（默认 ``bwrap``，从 PATH 解析）。
    """

    def __init__(
        self,
        root_dir: str,
        *,
        policy: SandboxPolicy | None = None,
        readonly_paths: str | None = None,
        allow_network: bool | None = None,
        bwrap_path: str = "bwrap",
    ) -> None:
        self._root_dir = os.path.abspath(root_dir)
        if policy is None:
            # 兼容旧接口：从 readonly_paths / allow_network 构建默认策略。
            rp = readonly_paths if readonly_paths is not None else DEFAULT_READONLY_PATHS
            policy = SandboxPolicy(
                readonly_paths=[p.strip() for p in rp.split(",") if p.strip()],
                allow_network=bool(allow_network),
            )
        # model_copy 隔离：探测降级（改 allow_network）只影响自身拷贝，不污染调用方传入对象。
        self._policy = policy.model_copy()
        self._backend = _select_backend(bwrap_path)
        self._bwrap_path = bwrap_path
        # 惰性探测结果缓存：None=未探测 / True / False
        self._available: bool | None = None

    def is_available(self) -> bool:
        """探测 bwrap 是否可用（二进制存在且能真实创建沙箱）。

        首次调用会执行一次真实探测（``bwrap --ro-bind / / /bin/true``），
        结果缓存。探测失败（未安装 / user namespace 被禁）返回 False，
        调用方据此 fail-open 降级到同进程路径。
        """
        if self._available is None:
            self._available = self._probe()
        return self._available

    def _probe(self) -> bool:
        if shutil.which(self._bwrap_path) is None:
            logger.warning("bwrap 不可用：未找到二进制 %s，文件能力将降级为同进程执行", self._bwrap_path)
            return False
        # 探测核心能力：user namespace + mount namespace + 只读 bind。
        # 显式 --unshare-user 是关键：容器/受限环境下（无有效 CAP_SYS_ADMIN，
        # 或被 seccomp 拦 mount namespace）仍需靠 user namespace 获得 mount 权限。
        try:
            proc = subprocess.run(  # noqa: S603
                [self._bwrap_path, "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("bwrap 探测失败，文件能力将降级为同进程执行", exc_info=True)
            return False
        if proc.returncode != 0:
            logger.warning(
                "bwrap 探测返回非零（%s）：%s，文件能力将降级为同进程执行",
                proc.returncode,
                (proc.stderr or b"").decode("utf-8", errors="replace").strip(),
            )
            return False
        # 若需关网，进一步探测 net namespace；不可用则降级为保留网络（文件隔离仍在）。
        if not self._policy.allow_network:
            try:
                proc_net = subprocess.run(  # noqa: S603
                    [self._bwrap_path, "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "/bin/true"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                proc_net = None
            if proc_net is None or proc_net.returncode != 0:
                logger.warning("bwrap net namespace 不可用，网络隔离降级为放行（文件隔离仍生效）")
                self._policy.allow_network = True
        return True

    def _prefix(self, writable: bool) -> list[str]:
        """组装 bwrap 命令前缀（委托给后端 ``render``）。

        Args:
            writable: True 时工作区以 ``--bind`` 可写挂载，False 时 ``--ro-bind``。
        """
        return self._backend.render(self._policy, self._root_dir, writable)

    def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 120,
        writable: bool = True,
    ) -> BwrapResult:
        """在 bwrap 沙箱内执行命令。

        Args:
            args: 具体命令及其参数（如 ``["cat", path]``、``["sh", "-c", cmd]``）。
            input_text: 通过 stdin 传入的文本（write 类操作使用）。
            timeout: 超时秒数。
            writable: 工作区是否可写挂载。

        Returns:
            BwrapResult（不抛异常；超时/启动失败返回 exit_code=None）。
        """
        cmd = self._prefix(writable=writable) + ["--"] + args
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return BwrapResult(output=f"Error: command timed out after {timeout} seconds", exit_code=None)
        except OSError as e:
            return BwrapResult(output=f"Error executing in bwrap sandbox: {e}", exit_code=None)

        output = proc.stdout or ""
        if proc.stderr:
            output = output + ("\n" + proc.stderr if output else proc.stderr)
        return BwrapResult(output=output, exit_code=proc.returncode)

    # --- 高层文件操作原语（供 FilesystemBackend 复用） ---

    def read_text(self, path: str) -> str | None:
        """读取文件全文；失败返回 None。"""
        res = self.run(["cat", path], writable=False)
        if res.exit_code != 0:
            return None
        return res.output

    def write_text(self, path: str, content: str) -> bool:
        """写入文件（创建/覆盖），通过 stdin 传内容；返回是否成功。"""
        res = self.run(["sh", "-c", f"cat > {shlex.quote(path)}"], input_text=content, writable=True)
        return res.exit_code == 0

    def read_bytes(self, path: str) -> bytes | None:
        """读取文件原始字节；失败返回 None（二进制模式，供 upload/download 复用）。"""
        cmd = self._prefix(writable=False) + ["--", "cat", path]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)  # noqa: S603
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def write_bytes(self, path: str, content: bytes) -> bool:
        """写入原始字节（创建/覆盖），通过 stdin 传内容；返回是否成功。"""
        cmd = self._prefix(writable=True) + ["--", "sh", "-c", f"cat > {shlex.quote(path)}"]
        try:
            proc = subprocess.run(cmd, input=content, capture_output=True, timeout=120, check=False)  # noqa: S603
        except (subprocess.TimeoutExpired, OSError):
            return False
        return proc.returncode == 0

    def list_entries(self, path: str) -> list[dict]:
        """非递归列目录，返回 ``[{"path","is_dir","size","modified_at"}]``。"""
        res = self.run(
            ["find", path, "-maxdepth", "1", "-mindepth", "1", "-printf", "%y\t%p\t%s\t%T@\n"],
            writable=False,
        )
        if res.exit_code != 0:
            return []
        entries: list[dict] = []
        for line in res.output.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            typ, p, size, mtime = parts[0], parts[1], parts[2], parts[3]
            is_dir = typ == "d"
            try:
                modified_at = datetime.fromtimestamp(float(mtime)).isoformat()
            except (ValueError, OSError):
                modified_at = None
            entries.append(
                {"path": p, "is_dir": is_dir, "size": int(size) if not is_dir else 0, "modified_at": modified_at}
            )
        return entries

    def glob_entries(self, path: str, pattern: str) -> list[dict]:
        """递归 glob 匹配文件，返回 ``[{"path","size","modified_at"}]``。

        ``find -name`` 匹配的是 basename，故此处将 ``**/*.py``、``subdir/*.py``
        这类带目录的 pattern 归一为最后一段（``*.py``）；``find`` 本身默认递归，
        语义与 ``rglob('*.py')`` 一致。
        """
        name = pattern.rsplit("/", 1)[-1] if "/" in pattern else pattern
        res = self.run(
            ["find", path, "-type", "f", "-name", name, "-printf", "%p\t%s\t%T@\n"],
            writable=False,
        )
        if res.exit_code != 0:
            return []
        entries: list[dict] = []
        for line in res.output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            p, size, mtime = parts[0], parts[1], parts[2]
            try:
                modified_at = datetime.fromtimestamp(float(mtime)).isoformat()
            except (ValueError, OSError):
                modified_at = None
            entries.append({"path": p, "size": int(size), "modified_at": modified_at})
        return entries

    def grep(self, pattern: str, path: str, glob: str | None = None) -> list[tuple[str, int, str]] | None:
        """用 ripgrep 搜索，返回 ``[(path, line_no, line_text)]``；rg 不可用返回 None。"""
        cmd = ["rg", "--json"]
        if glob:
            cmd += ["--glob", glob]
        cmd += ["--", pattern, path]
        res = self.run(cmd, writable=False)
        if res.exit_code not in (0, 1):
            # 非 0/1 表示 rg 启动失败或参数错误（1 = 无匹配）
            return None
        matches: list[tuple[str, int, str]] = []
        for line in res.output.splitlines():
            try:
                import json

                data = json.loads(line)
            except (ValueError, ImportError):
                continue
            if data.get("type") != "match":
                continue
            pdata = data.get("data", {})
            ftext = pdata.get("path", {}).get("text")
            ln = pdata.get("line_number")
            lt = pdata.get("lines", {}).get("text", "").rstrip("\n")
            if not ftext or ln is None:
                continue
            matches.append((ftext, int(ln), lt))
        return matches

    def run_shell(self, command: str, timeout: int = 120) -> BwrapResult:
        """在沙箱内执行 shell 命令（execute 工具的核心）。"""
        return self.run(["sh", "-c", command], timeout=timeout, writable=True)


__all__ = [
    "BubblewrapSandbox",
    "BubblewrapBackend",
    "SandboxBackend",
    "BwrapResult",
    "DEFAULT_READONLY_PATHS",
]
