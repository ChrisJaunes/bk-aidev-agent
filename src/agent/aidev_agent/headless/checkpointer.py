"""TUI worker 使用的、与事件循环无关的 SQLite 检查点保存器。

设计要点
========

LangGraph 官方提供的两个 SQLite saver 都无法用于本 worker：

``langgraph.checkpoint.sqlite.SqliteSaver``（同步）
    只实现同步接口，``aget_tuple`` / ``aput`` / ``alist`` / ``aput_writes`` 抛出
    ``NotImplementedError``。而 SDK 通过 ``await graph.astream_events(...)`` 与
    ``await graph.aget_state(...)`` 驱动图执行——这些异步方法**确实会被调用**。

``langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver``（异步）
    实现了异步接口，但把自己绑定到创建它的那个事件循环：``__init__`` 里执行
    ``self.lock = asyncio.Lock()``，且每次数据库操作都由 ``async with self.lock``
    保护。SDK 并不在调用者的循环上运行图——其同步/异步桥接会在独立线程中为每次
    运行新建一个事件循环，于是 saver 的锁属于另一个循环，报
    "is bound to a different event loop"。

可用方案
========

同步内核 + 异步外壳：继承 :class:`SqliteSaver` 复用整套经过验证的同步实现
（相同的 schema 与 SQL、WAL、``threading.Lock``、``check_same_thread=False``），
只把抛 ``NotImplementedError`` 的异步方法替换为经 :func:`asyncio.to_thread`
调用同步方法的薄桥接。

:func:`asyncio.to_thread` 正是"与循环无关"的关键：它把阻塞的同步调用交给默认
执行器再 await 结果，在任何循环上表现一致。这里没有需要绑定的 ``asyncio.Lock``，
也没有缓存的循环引用，因此不会出现 "bound to a different event loop"。

注意事项
--------

* 继承来的 ``threading.Lock`` 串行化 worker 执行器线程与 SDK 每次运行循环线程对
  数据库的访问；它是真正的锁，可从任一线程获取。
* :meth:`from_conn_string` 以 ``check_same_thread=False`` 打开连接。连接由
  :class:`contextlib.ExitStack` 持有（进程级单例，创建与关闭都在
  ``SessionState`` 侧：:meth:`ensure_checkpointer
  <aidev_agent.headless.agent_runner.SessionState.ensure_checkpointer>` 惰性创建，
  ``close_exit_stack`` 在 ``Dispatcher.shutdown`` gather 完所有运行后关闭）。
* ``setup()`` 建表并设 ``PRAGMA journal_mode=WAL``，幂等；由创建方显式调用，
  不依赖它"自动发生"。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.sqlite import SqliteSaver


class TuiSqliteSaver(SqliteSaver):
    """具备可用异步方法、可在任意事件循环中使用的 :class:`SqliteSaver`。

    同步部分原样继承；只覆写 SDK 会调用的异步方法，每个都经
    :func:`asyncio.to_thread` 委托给对应的同步方法。

    请用 :meth:`from_conn_string` 而非裸构造函数创建实例——只有它保证连接以
    ``check_same_thread=False`` 打开（与 :meth:`SqliteSaver.from_conn_string`
    同一约定）。
    """

    @classmethod
    @contextmanager
    def from_conn_string(cls, conn_string: str) -> Iterator["TuiSqliteSaver"]:
        """打开（必要时创建）``conn_string`` 指定的数据库。

        ``@contextmanager`` 与 ``SqliteSaver.from_conn_string`` 保持签名一致；
        调用方（``SessionState.ensure_checkpointer``）把它交给进程级 ExitStack，
        ``conn.close()`` 由此时的 ``finally`` 执行。
        """
        conn = sqlite3.connect(conn_string, check_same_thread=False)
        try:
            yield cls(conn)
        finally:
            conn.close()

    # --------------------------------------------------------------- 异步外壳

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """对继承的同步 :meth:`get_tuple` 的异步封装。"""
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """对继承的同步 :meth:`list` 的异步封装。

        ``list`` 返回的惰性同步生成器在函数体内持锁并读取数据库，因此必须在同一个
        工作线程上完整消费完毕，而不能与事件循环逐块交错。先物化为列表可以保证
        所有数据库访问都发生在同一个执行器线程上。
        """
        rows = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )

        for row in rows:
            yield row

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """对继承的同步 :meth:`put` 的异步封装。"""
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """对继承的同步 :meth:`put_writes` 的异步封装。"""
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """对继承的同步 :meth:`delete_thread` 的异步封装。"""
        return await asyncio.to_thread(self.delete_thread, thread_id)
