"""Worker-local SQLite checkpoint storage with synchronous and loop-independent async access.

Each worker owns its connections; no saver crosses the spawn boundary.
The saver uses SqliteSaver's thread-safe synchronous core and asyncio.to_thread
for async methods, avoiding locks tied to a particular SDK event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from logging import getLogger
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple
from langgraph.checkpoint.sqlite import SqliteSaver

from ...infrastructure.logging import BKAI_HOME_DIRNAME

logger = getLogger(__name__)


class LoopIndependentSqliteSaver(SqliteSaver):
    """具备可用异步方法、可在任意事件循环中使用的 :class:`SqliteSaver`。

    同步部分原样继承；只覆写 SDK 会调用的异步方法，每个都经
    :func:`asyncio.to_thread` 委托给对应的同步方法。

    请用 :meth:`from_conn_string` 而非裸构造函数创建实例——只有它保证连接以
    ``check_same_thread=False`` 打开（与 :meth:`SqliteSaver.from_conn_string`
    同一约定）。
    """

    @classmethod
    @contextmanager
    def from_conn_string(cls, conn_string: str) -> Iterator["LoopIndependentSqliteSaver"]:
        """打开（必要时创建）``conn_string`` 指定的数据库。

        ``@contextmanager`` 与 ``SqliteSaver.from_conn_string`` 保持签名一致；
        调用方（``CheckpointStore.ensure``）把它交给进程级 ExitStack，
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
        rows = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))

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

class CheckpointStore:
    """惰性创建、只创建一次的进程级 :class:`LoopIndependentSqliteSaver`。"""

    def __init__(self) -> None:
        self._checkpointer: Any | None = None
        self._exit_stack: contextlib.ExitStack | None = None

    @property
    def saver(self) -> Any | None:
        """已创建的 saver（尚未创建时为 ``None``）。"""
        return self._checkpointer

    def ensure_sync(self) -> Any:
        """返回进程级 checkpoint saver，只惰性创建一次。

        :class:`aidev_agent_cli.runtime.sdk.checkpoints.LoopIndependentSqliteSaver`——一个内核为**同步**的 SQLite
        saver（``threading.Lock``、``sqlite3.connect(check_same_thread=False)``，无
        ``asyncio`` 状态），外层是一层异步皮，四个方法通过 ``asyncio.to_thread``
        委托给同步内核。它与两个打包 saver 的差异原因见该模块的 docstring。

        它有意做到**与事件循环无关**，因此——不同于 ``AsyncSqliteSaver``——不必在将要
        await 它的循环上构建。它可以从 worker 主循环、worker 线程自己的循环，或 SDK
        每次运行的一次性循环（``async_to_sync_generator``，
        ``aidev_agent/utils/async_utils.py``）中 await；这三种都由
        ``tests/worker/test_checkpointer.py`` 覆盖。**不要**把它"简化"成任何一种打包
        saver——两者在本场景下都会失败。

        数据库路径（:data:`aidev_agent_cli.utils.BKAI_HOME_DIRNAME`，即 ``~/.bkai/``）
        ------------------------------------------------------------------
        默认 ``~/.bkai/checkpoints.sqlite``——与 worker 日志
        （``~/.bkai/logs/``）和用户空间 TUI 配置（``~/.bkai/bkai_tui.json``）
        同一个家，按用户隔离，且一次检出不会带上数据库。环境变量
        ``BKAI_TUI_CHECKPOINT_FILE`` 整体替换路径（指定的是**文件**而非目录），
        测试用它把 DB 重定向到 ``tmp_path``。父目录不存在时创建之——
        ``aiosqlite.connect`` **不会**创建中间目录，干净的机器上首次运行会失败。

        ``from_conn_string`` 是 ``@contextmanager``：经
        :meth:`ExitStack.enter_context <contextlib.ExitStack.enter_context>` 进入，
        ExitStack 持有生成器（防 GC 触发其 ``finally`` 关连接）并记录 ``__exit__``；
        拆除发生在 :meth:`close_exit_stack`，不是这里——**不要**把它改写成
        ``with`` 语句（``with`` 会在语句结束时立即关闭连接）。

        ``setup()`` 创建表并设置 ``PRAGMA journal_mode=WAL``。它是幂等的
        （``self.is_setup``），但显式调用可以摆脱对它"自动发生"的依赖。

        这里的守卫只是普通的属性检查，不是锁。唯一的调用方
        （会话的首次装配）在会话锁
        下访问，因此实际不会出现两次并发的首次调用；竞态最坏的情况是对同一个
        文件多开一个冗余 saver，SQLite（WAL）可以容忍，且身份测试会捕获它。

        **调用方必须持有本 state**（生产中 dispatcher 持有）：临时实例一旦被
        GC，``_exit_stack`` 连同其栈帧持有的生成器会被终结、连接关闭，
        之后所有使用都以 ``sqlite3.ProgrammingError`` 失败。
        """
        if self._checkpointer is None:
            # 只有在 enter 成功后才赋值，这样 setup 期间的失败会让
            # ``_checkpointer`` 保持 None，下一轮可以重试，而不是留下半成品。
            override = os.environ.get("BKAI_TUI_CHECKPOINT_FILE")
            path = Path(override) if override else Path.home() / BKAI_HOME_DIRNAME / "checkpoints.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)

            stack = contextlib.ExitStack()
            try:
                saver = stack.enter_context(LoopIndependentSqliteSaver.from_conn_string(str(path)))
                saver.setup()  # CREATE TABLE + PRAGMA journal_mode=WAL
            except BaseException:
                stack.close()
                raise

            self._exit_stack = stack
            self._checkpointer = saver

            logger.info(f"checkpoint saver ready: {path}")

        return self._checkpointer

    async def ensure(self) -> Any:
        """:meth:`ensure_sync` 的协程形态（创建只是打开一个 SQLite 文件，不阻塞事件循环多久）。"""
        return self.ensure_sync()

    def close(self) -> None:
        """拆除进程级 ExitStack（当前即关闭 checkpoint saver）；幂等，空栈是 NO-OP。

        按记录的逆序执行 ``__exit__``——对 checkpoint 而言就是
        ``from_conn_string`` 的 ``conn.close()``。**必须在所有运行落定之后调用**
        ——:meth:`Dispatcher.shutdown <aidev_agent_cli.dispatch.Dispatcher.shutdown>` 正是
        这样做的（先 gather 完运行任务再碰它）；有运行仍在使用 saver 时关闭，
        等价于在它脚下抽走地板。

        SQLite（WAL）在每次写入时提交，因此不 close 也不丢数据——这里的关闭
        是**整洁性**（合并 ``-wal``、释放 fd），不是持久性要求。
        """
        stack = self._exit_stack
        self._exit_stack = None
        self._checkpointer = None

        if stack is not None:
            stack.close()


__all__ = ["CheckpointStore", "LoopIndependentSqliteSaver"]