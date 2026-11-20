# -*- coding: utf-8 -*-
"""进程级设施：信号处理与守护线程执行器。与协议无关，headless / agui / acp 共用。

* :func:`install_shutdown_signals`：SIGPIPE 忽略、SIGINT 忽略、SIGTERM / SIGHUP 转为一次
  「请求关闭」回调——由各协议决定如何把它汇入自己的关闭路径（与 stdin EOF 同一条）。
* :func:`async_shutdown_event`：异步协议的便捷封装——返回一个由信号设置的
  ``asyncio.Event``（线程安全地编组到循环线程）。
* :class:`DaemonThreadPoolExecutor` / :func:`use_daemon_default_executor`：工作线程不阻塞
  解释器退出，避免停在 ``os.read(0)`` 或 SDK 调用上的线程把进程拖成孤儿。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker  # noqa: F401 - 供守护执行器使用
from typing import Callable

from .logging import configured_logger

logger = configured_logger(__name__)

_SIGNAL_NAMES = ("SIGPIPE", "SIGTERM", "SIGHUP", "SIGINT", "SIGBREAK")


def _signal_name(signum: int) -> str:
    for attr in _SIGNAL_NAMES:
        value = getattr(signal, attr, None)
        if value is not None and int(value) == signum:
            return attr
    return str(signum)


def install_shutdown_signals(on_shutdown: Callable[[], None]) -> None:
    """安装处理器；SIGTERM / SIGHUP（Windows 为 SIGBREAK）到达时调用 ``on_shutdown()``。

        处理器只**请求**关闭，不自行退出：调用方把它汇入与 stdin EOF 相同的收尾路径
        （取消运行、关 owned saver、flush、以 0 退出）。

    安装处理器，每次调用都用 hasattr 守卫。

        SIGPIPE/SIGHUP 在 Windows 上不存在，因此不加守卫的 ``signal.signal`` 会让
        ``python -m`` 在那些平台上失败。

        ``SIGPIPE → SIG_IGN`` 是重要的那个。默认处置下，任何任务一写向父进程已停止读取的
        管道，进程就会被**静默**杀死——而 worker 会从许多 asyncio 任务写。忽略它把该事件
        变成可捕获的 ``BrokenPipeError``，writer 会把它报告为干净的 peer-gone，于是 worker
        可以主动退出而不是毫无痕迹地消失。
    """

    def _handler(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        logger.info(f"received signal {_signal_name(signum)}; requesting shutdown")
        on_shutdown()

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handler)
    elif hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handler)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)


def async_shutdown_event() -> asyncio.Event:
    """在运行中的循环上安装信号处理，返回由信号设置的关闭事件。

    **先建事件、再捕获循环**（G-05）：处理器在主线程运行，可能恰在两步之间触发；
    闭包同时持有两者，因此不存在「循环已知而事件尚无」的窗口。
    ``asyncio.Event.set`` 不是线程安全的，故经 ``call_soon_threadsafe`` 编组。
    """
    event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request() -> None:
        if loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    install_shutdown_signals(_request)

    return event


def use_daemon_default_executor(thread_name_prefix: str) -> None:
    """把运行中循环的默认执行器（``asyncio.to_thread`` 用它）换成守护执行器。"""
    asyncio.get_running_loop().set_default_executor(DaemonThreadPoolExecutor(thread_name_prefix=thread_name_prefix))


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """工作线程不阻塞解释器退出的 ``ThreadPoolExecutor``。

    ``asyncio.run`` 通过 join 线程来关闭循环的默认执行器。stdin 读取线程停在
    ``os.read(0, ...)`` 上，那条管道再也不会有人写，因此它永不返回——读循环早已结束，
    解释器却挂住。这是个孤儿进程。

    ``ThreadPoolExecutor`` 硬编码 ``threading.Thread(...)`` 为默认（非守护）处置，且未
    提供开关。覆写 ``_adjust_thread_count`` 是改变线程创建方式的标准做法；下面函数体镜像
    CPython 实现，只加了 ``daemon=True``。

    对私有 API 的依赖是刻意且狭窄的：只碰标准库类上的**一个**方法，且 ``tests/worker/``
    覆盖了它所保护的行为（进程在 SIGTERM 时退出），因此破坏它的标准库变更会让测试变红，
    而不是静默地重新引入孤儿。
    """

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        # SDK HTTP/build work cannot be interrupted; it must not hold the child
        # process open after its owning run has been cancelled.
        super().shutdown(wait=False, cancel_futures=cancel_futures)

    def _adjust_thread_count(self) -> None:  # noqa: D102 - 镜像标准库
        # 有空闲线程可用——无需新建。
        if self._idle_semaphore.acquire(timeout=0):  # noqa: SLF001
            return

        def weakref_cb(_: object, queue: object = self._work_queue) -> None:  # noqa: SLF001
            queue.put(None)  # type: ignore[attr-defined]

        num_threads = len(self._threads)  # noqa: SLF001

        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)  # noqa: SLF001

            thread = threading.Thread(
                name=thread_name,
                target=_worker,  # noqa: F821 - 从 concurrent.futures.thread 导入
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,  # noqa: SLF001
                    self._initializer,  # noqa: SLF001
                    self._initargs,  # noqa: SLF001
                ),
                # 与标准库唯一的差别：守护线程不能把解释器拖住。
                daemon=True,
            )
            thread.start()
            self._threads.add(thread)  # noqa: SLF001


#: 用于阻塞式 stdin 读取的专用执行器。它为何不能是循环默认执行器见
#: :class:`_DaemonThreadPoolExecutor`。


__all__ = [
    "DaemonThreadPoolExecutor",
    "async_shutdown_event",
    "install_shutdown_signals",
    "use_daemon_default_executor",
]
