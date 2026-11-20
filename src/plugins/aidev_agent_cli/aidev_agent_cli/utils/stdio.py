# -*- coding: utf-8 -*-
"""stdio 通道纪律：stdout guard、协议流、``sys.path`` 加固。

本模块仅依赖标准库，入口可在导入 SDK 之前安装 stdout guard，避免导入时的普通输出
污染协议通道。协议帧通过 ``protocol_out`` 返回的独立流写出。

``install_stdout_guard`` 将普通 stdout 输出转发到 stderr；``captured_stdout``
用于执行期间的临时重定向。两者只替换 Python 的 ``sys.stdout``，不拦截旧流引用
或直接写入 fd 1 的输出。
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Iterator

#: 协议输出流：显式传入的流，或包装 fd 1 副本的行缓冲文本流。
_protocol_stream: Any = None

#: 初始化标记，使安装幂等。
_installed = False


class _StderrProxy:
    """将文本写入转发到安装时的 stderr。

    忽略文本写入和刷新异常，避免杂散输出导致启动失败；覆盖终端探测和关闭行为，
    其他属性委托给底层流。通过 ``buffer`` 访问底层流时不经过文本写入的容错逻辑。
    """

    __slots__ = ("_stderr",)

    def __init__(self, stderr: Any) -> None:
        self._stderr = stderr

    def write(self, data: str) -> int:
        try:
            return self._stderr.write(data)
        except Exception:  # noqa: BLE001 - stderr 坏了也不能让 print() 崩
            return len(data)

    def writelines(self, lines) -> None:  # type: ignore[no-untyped-def]
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self._stderr.flush()

    def isatty(self) -> bool:
        # 强制非终端，避免库输出颜色转义或进度条，不依赖实际传输类型。
        return False

    def fileno(self) -> int:
        # 库用 fileno() 做缓冲决策。返回 stderr 的 fd 才是诚实的答案，因为字节实际去了那里。
        try:
            return self._stderr.fileno()
        except Exception:  # noqa: BLE001
            return 2

    def close(self) -> None:
        # 拒绝真正关闭任何东西——真正的流由解释器与 writer 管理。
        return None

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._stderr, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._stderr, "errors", None) or "replace"

    @property
    def buffer(self) -> Any:
        return getattr(self._stderr, "buffer", self)

    def __getattr__(self, name: str) -> Any:
        # 未显式建模的一切都委托给 stderr，使属性探测（``stdout.name``、``stdout.newlines``
        # 等）表现正常。
        return getattr(self._stderr, name)


def install_stdout_guard(protocol_stream: Any = None) -> None:
    """把 ``sys.stdout`` 的写入改道到 stderr，并把 fd 1 留给协议使用。

    必须在导入 ``aidev_agent`` 或任何其他重 SDK **之前**调用，因为本 guard 防范的杂散
    输出最常发生在导入时。

    参数：
        protocol_stream：协议帧的可选显式文本流。省略时，复制 fd 1 并包装为行缓冲文本流。
    """
    global _protocol_stream, _installed

    if _installed:
        return

    if protocol_stream is not None:
        _protocol_stream = protocol_stream
    else:
        # 复制 fd 1，使协议流拥有独立描述符；包装为行缓冲文本流，换行会触发刷新。
        _protocol_stream = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace", buffering=1)

    sys.stdout = _StderrProxy(sys.stderr)
    _installed = True


def apply_worker_stdio_guard() -> None:
    """worker 的 fd 层定型：fd 1 指到 stderr、fd 0 指到 ``/dev/null``，再装 stdout guard。

    只有在 **spawned worker 进程**里调用（``sdk.executor.build`` 的调用点）。顺序
    是契约：先把原生侧的 fd 换掉（C 扩展 / 子进程直接写 fd 1 也不行），再用
    :func:`install_stdout_guard` 让 Python 侧 ``sys.stdout`` 也走 stderr，并把当前 fd 1
    的副本留作协议流——``install_stdout_guard`` 在 fd 1 已被重定向之后再复制，协议流因此
    指向 stderr，而协议帧由父进程经 IPC 收走，不经过 fd 1。
    """
    os.dup2(2, 1)
    stdin = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(stdin, 0)
    finally:
        os.close(stdin)
    install_stdout_guard()


def is_stdout_guard_installed() -> bool:
    """返回 guard 是否已完成安装。"""
    return _installed


def protocol_out() -> Any:
    """返回协议输出流。

    使用安装时配置的协议流；尚未安装 guard 时回退到当前 ``sys.stdout``，
    使测试或嵌入式调用无需启动整个入口即可使用 writer。
    """
    if _protocol_stream is not None:
        return _protocol_stream

    return sys.stdout


@contextlib.contextmanager
def captured_stdout() -> Iterator[None]:
    """在代码块内将通过当前 ``sys.stdout`` 进行的 Python 输出重定向到 stderr。

    用于 SDK 执行外围；不拦截已保存的旧流引用或直接写入 fd 1 的输出。
    替换作用于整个进程，而非单个线程或异步任务。
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def harden_sys_path() -> None:
    """从 ``sys.path`` 中剔除 CWD 条目。

    空串与 ``.`` 在导入时都解析为当前工作目录，因此本地一个杂散的 ``utils/`` 或同名目录
    可能遮蔽已安装的包。worker 是以任意 cwd 启动的，所以这一点在此处的影响不同于普通脚本。

    ``sys.path`` 没有需要**添加**的东西。SDK 由解释器已有的东西解析：``.pth`` 文件、可编辑
    安装或 ``PYTHONPATH``。本函数不导入 ``aidev_agent``。
    """
    sys.path = [p for p in sys.path if p not in {"", "."}]


__all__ = [
    "apply_worker_stdio_guard",
    "captured_stdout",
    "harden_sys_path",
    "install_stdout_guard",
    "is_stdout_guard_installed",
    "protocol_out",
]
