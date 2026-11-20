"""协议无关的有界行读取与串行写入。

阻塞 I/O 只运行在守护线程中。取消异步等待不能中断任意文件对象的系统调用；
关闭使用截止时间，超时后停止接受数据并报告失败，不关闭调用方拥有的流。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import threading
from collections.abc import Callable
from typing import Any

from .process import DaemonThreadPoolExecutor

_PEER_ERRNOS = {errno.EPIPE, errno.ECONNRESET, errno.EBADF, errno.ESHUTDOWN}


async def until_stopped(awaitable: Any, stop: asyncio.Event) -> Any:
    """等待操作或停止事件，取消并回收落败的异步任务。"""
    operation = asyncio.ensure_future(awaitable)
    waiter = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({operation, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            return None
        return operation.result()
    finally:
        for task in (operation, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, waiter, return_exceptions=True)


class LineReader:
    """按字节限长的增量读取器；超长行丢弃到换行后继续读取。

    按需读取，不预读到无界队列。EOF 的残留行是否有效由协议调用方决定。
    """

    def __init__(
        self,
        stream: Any,
        *,
        limit: int = 10 * 1024 * 1024,
        eof_line: bool = True,
        on_oversized: Callable[[], None] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._stream = stream
        self._limit = limit
        self._eof_line = eof_line
        self._on_oversized = on_oversized
        self._buffer = bytearray()
        self._chunk = b""
        self._dropping = False
        self._eof = False
        self._executor = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="protocol-read")

    def _read(self) -> bytes:
        try:
            fd = self._stream.fileno()
        except (AttributeError, OSError, ValueError):
            read = getattr(self._stream, "read1", self._stream.read)
            return read(65536)
        # 真实描述符直接读取，避免守护线程在解释器退出时持有 stdin 的缓冲锁。
        return os.read(fd, 65536)

    async def receive(self) -> bytes | None:
        while True:
            if not self._chunk:
                if self._eof:
                    if self._buffer and self._eof_line and not self._dropping:
                        line = bytes(self._buffer)
                        self._buffer.clear()
                        return line
                    return None
                self._chunk = await asyncio.get_running_loop().run_in_executor(self._executor, self._read)
                if not self._chunk:
                    self._eof = True
                    continue
            part, separator, rest = self._chunk.partition(b"\n")
            self._chunk = rest
            if not self._dropping:
                if len(self._buffer) + len(part) > self._limit:
                    self._buffer.clear()
                    self._dropping = True
                    if self._on_oversized is not None:
                        self._on_oversized()
                else:
                    self._buffer.extend(part)
            if separator:
                dropping = self._dropping
                self._dropping = False
                line = bytes(self._buffer)
                self._buffer.clear()
                if not dropping:
                    return line

    def close(self) -> None:
        self._eof = True
        self._buffer.clear()
        self._chunk = b""
        self._executor.shutdown(wait=False, cancel_futures=True)


class LineWriter:
    """单消费者有界写入器，区分入队与 write/flush 完成。

    每个已接受项有写出截止时间。任何写入故障或超时都会唤醒所有等待者，
    设置 stopped 事件，且不再发送后续帧，避免丢帧后继续使用失同步的连接。
    """

    def __init__(self, stream: Any, *, maxsize: int = 256, write_timeout: float = 30.0) -> None:
        if maxsize <= 0 or write_timeout <= 0:
            raise ValueError("maxsize and write_timeout must be positive")
        self._stream = stream
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._write_timeout = write_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._closing = asyncio.Event()
        self.stopped = asyncio.Event()
        self._peer_gone = False
        self._failure: BaseException | None = None
        self._pending: dict[asyncio.Future[bool], asyncio.TimerHandle] = {}

    @property
    def peer_gone(self) -> bool:
        return self._peer_gone

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def queue(self) -> asyncio.Queue[Any]:
        return self._queue

    async def start(self) -> None:
        if self._thread is not None or self._closed:
            raise RuntimeError("writer already started or closed")
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._drain, name="protocol-write", daemon=True)
        self._thread.start()

    def _finish(self, future: asyncio.Future[bool], success: bool) -> None:
        timer = self._pending.pop(future, None)
        if timer is not None:
            timer.cancel()
        if not future.done():
            future.set_result(success)

    def _fail(self, error: BaseException | None) -> None:
        if self.stopped.is_set():
            return
        self._failure = error
        self._peer_gone = True
        self.stopped.set()
        self._closing.set()
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                self._finish(item[1], False)
        self._queue.put_nowait(None)
        for future in list(self._pending):
            self._finish(future, False)

    async def write(self, line: str, *, wait_written: bool = False) -> bool:
        """默认确认入队；wait_written 确认 write/flush 完成，失败返回 False。

        取消写出等待不会撤回已接受的帧；调用方应关闭不再使用的连接。
        """
        if self._closed or self.stopped.is_set():
            return False
        if self._loop is None:
            raise RuntimeError("writer not started")
        future = self._loop.create_future()
        item = (line, future)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                await asyncio.wait_for(until_stopped(self._queue.put(item), self._closing), self._write_timeout)
                if self._closed or self.stopped.is_set():
                    future.cancel()
                    return False
            except asyncio.TimeoutError:
                self._fail(TimeoutError("protocol output backpressure deadline exceeded"))
                future.cancel()
                return False
            except asyncio.CancelledError:
                future.cancel()
                raise
        if not future.done():
            self._pending[future] = self._loop.call_later(
                self._write_timeout, self._fail, TimeoutError("protocol output write deadline exceeded")
            )
        if not wait_written:
            return True
        return await asyncio.shield(future)

    def _drain(self) -> None:
        loop = self._loop
        assert loop is not None
        while not self.stopped.is_set():
            try:
                get = asyncio.run_coroutine_threadsafe(self._queue.get(), loop)
                item = get.result()
            except Exception:
                return
            if item is None:
                return
            line, future = item
            if future.cancelled():
                continue
            if self.stopped.is_set():
                return
            try:
                self._stream.write(line)
                self._stream.flush()
            except Exception as exc:
                peer = (
                    isinstance(exc, BrokenPipeError)
                    or (isinstance(exc, OSError) and exc.errno in _PEER_ERRNOS)
                    or (isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) and "closed file" in str(exc))
                )
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._fail, None if peer else exc)
                return
            try:
                loop.call_soon_threadsafe(self._finish, future, True)
            except RuntimeError:
                return

    async def close(self, *, timeout: float = 3.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing.set()

        async def drain() -> None:
            if self._pending:
                await asyncio.gather(*(asyncio.shield(f) for f in list(self._pending)))
            if self._thread is not None and not self.stopped.is_set():
                await self._queue.put(None)
            while self._thread is not None and self._thread.is_alive():
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(drain(), timeout)
        except asyncio.TimeoutError:
            self._fail(TimeoutError("protocol output close deadline exceeded"))
        except asyncio.CancelledError:
            self._fail(None)
            raise
        finally:
            self.stopped.set()
            for future in list(self._pending):
                self._finish(future, False)
        if self._failure is not None:
            raise self._failure
