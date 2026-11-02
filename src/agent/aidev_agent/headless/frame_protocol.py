"""Worker NDJSON 帧协议：帧信封与串行化输出。

本模块有两个关注点，都以**帧**为核心：

1. **帧信封**——线上协议版本、六个帧构造函数、``ready`` 帧的能力集。这些是
   ``dispatch`` 与 ``agent_runner`` 共同依赖的协议词汇。
2. **串行化输出**——:class:`FrameWriter`，把帧按 NDJSON 逐行写出且不会交错。

把两者放在一起是因为它们本就是一件事的两面：帧长什么样，以及帧怎么不打架地写出去。
模块不导入包内其他任何内容，因此 ``dispatch`` 与 ``agent_runner`` 都不必依赖对方才能
获得协议词汇。

命名说明：本模块原名 ``writer.py``，那个名字只覆盖了关注点 2，而**关注点 1 才是其他
模块依赖它的主因**（只有 ``__main__`` 用 :class:`FrameWriter`）。改名为
``frame_protocol`` 以覆盖两者。TS 侧的对应模块是 ``src/transport/worker-protocol.ts``——
那份文件描述同一套线格式，其注释中的字段名以本模块为权威来源。

要解决的问题
------------

每个 Agent 运行都是一个独立的 ``asyncio.Task``，因此多个任务可能同时产生数据帧——
快速 token 流、工具进度更新、``session.cancel`` 确认。如果每个任务都直接写 stdout，两条
JSON 行可能在行中间交错，父进程读到的内容在拼接点两侧都不是合法 JSON。这种故障在 TUI
里表现为“卡住不动”，极难排查。

方案
----

所有数据帧都经过同一个队列，由唯一一个 worker 消费。因为只有一个消费者，**完全不需要
加锁**——这正是采用此结构的意义所在。不要在这样一个天生串行的结构外面再加锁，那只会带
来纯开销。

写入失败语义
------------

数据帧未成功写出时 ``send`` 返回 ``False``；而编程错误——无法 JSON 序列化的 payload、
``errno.ENOSPC`` 的 ``OSError``——必须作为异常抛出，而不是被折叠成 ``False``。否则真实
的 bug 将与正常的对端断连无法区分，traceback 也永远不会被记录。

由于 flush 运行在独立线程上，上述致命异常无法再从 ``await writer.send()`` 中向外传播
（它们在线程中抛出，生产者并未 join 该线程）。因此它们会被记录到
:attr:`FrameWriter.failure`，并由 :meth:`FrameWriter.close` 重新抛出，使 traceback 仍能
进入日志而不是消失。

``False`` 的第二种来源
----------------------
:attr:`_ENQUEUE_TIMEOUT_SECONDS` 增加了一种新的含义：``False`` 也可以表示“背压超过了
等待期限”。

这一行为是刻意的最后手段，值得明确说明其取舍。此前的规则是“协议帧绝不允许丢弃”，但旧
设计之所以能承受该规则，是因为它没有办法从卡死的队列中脱身——而这恰恰导致它会永久挂起。
边界总要在某处让步，选择是：丢弃一帧、让本轮以一条显眼的日志结束，还是保留这一帧、把用
户会话永久挂在转圈上。丢弃是可恢复的（运行结束、TUI 恢复正常，只是记录少一个事件并对此
有明确日志）；挂起则不可恢复。

该期限设为“队列持续 30s 不消费”，健康的管道永远不会接近这一数值——实测每帧消费耗时为
微秒级。
"""

from __future__ import annotations

import asyncio
import errno
import json
import threading
from logging import getLogger
from typing import Any, Protocol

logger = getLogger(__name__)


class FrameSink(Protocol):
    """本包其余部分所需的 :class:`FrameWriter` 子集。

    ``peer_gone`` 刻意不在此声明。``AgentRunner`` 通过
    ``getattr(..., "peer_gone", False)`` 探测它，并将属性缺失视为“假定对端存活”——最小
    的测试替身无需实现它，而缺少属性不应被误判为运行失败。在 Protocol 上声明为可选无法
    表达这一点，``getattr`` 的默认值可以。
    """

    async def send(self, frame: dict[str, Any]) -> bool: ...


#: 压入队列以停止 flush 的哨兵对象。使用身份比较（``is``），因此任何对象都可以——这个
#: 对象是私有且唯一的。
_DRAIN_STOP = object()

#: 队列深度上限。以背压替代无限制的内存增长：淹没队列的运行会等待，而不是把多兆字节的
#: 工具结果缓冲在内存中。
_QUEUE_MAX = 256

#: 单帧写入在发送方放弃等待空位前允许耗费的时长。用于界定最坏情况：没有它，卡死的
#: flush 会让 agent 循环永久挂起，而不是以可处理的方式让本轮失败。
#:
#: 取值宽松，因为它唯一需要胜过的目标是“永远”。实测消费速率为每帧微秒级，健康的管道
#: 远不会接近该值。
_ENQUEUE_TIMEOUT_SECONDS = 30.0

#: 表示“对端已消失”而非“主机存在真实 I/O 问题”的 errno 集合。集合之外的值会重新抛出，
#: 从而在 traceback 中暴露，而不是看起来像一次正常的断连。
_PEER_GONE_ERRNOS = frozenset(
    {
        errno.EPIPE,  # 向已关闭的管道写入（POSIX）
        errno.ECONNRESET,  # 对端重置了连接
        errno.EBADF,  # 文件描述符被关闭
        errno.ESHUTDOWN,  # 传输端点已关闭
        getattr(errno, "WSAECONNRESET", -1),  # win32 映射（POSIX 上为空操作）
        getattr(errno, "WSAESHUTDOWN", -1),
    }
) - {-1}


class _DrainFailure:
    """一种致命的 flush 异常，从 flush 线程传递到 ``close()``。

    包装原始异常，使 ``close()`` 能在保留其自身 traceback 的情况下重新抛出，而不是只
    报告一行摘要。
    """

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class FrameWriter:
    """基于有界队列的单消费者串行 NDJSON 写入器。

    生命周期::

        writer = FrameWriter(stream)
        await writer.start()                     # 启动唯一的 flush 线程
        await writer.send(frame)                 # 入队并序列化
        await writer.close()                     # 发送哨兵并 join flush 线程

    ``send`` 可从任意数量的并发任务中安全调用。

    队列是 ``asyncio.Queue``，而 flush 是一个 ``threading.Thread``，通过
    ``run_coroutine_threadsafe`` 桥接到队列。这一组合是刻意的：生产者是 asyncio 任务
    （因此需要非阻塞事件循环的异步背压），而 flush 执行阻塞式 I/O（因此不能是 asyncio
    任务）。参见模块文档字符串。
    """

    def __init__(self, stream: Any, *, maxsize: int = _QUEUE_MAX) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize must be positive, got {maxsize}")

        self._stream = stream
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_thread: threading.Thread | None = None
        self._peer_gone = False
        self._closed = False
        #: 致命的 flush 异常，由 ``close()`` 重新抛出。对端消失不记录在此——它是一种
        #: 正常结果，通过 ``send`` 返回。
        self._failure: _DrainFailure | None = None

    # ------------------------------------------------------------------ state

    @property
    def peer_gone(self) -> bool:
        """当一次写入因父进程停止读取而失败后为 True。"""
        return self._peer_gone

    @property
    def queue(self) -> asyncio.Queue[Any]:
        """为测试暴露队列（例如断言 ``maxsize``）。"""
        return self._queue

    @property
    def failure(self) -> BaseException | None:
        """若 flush 因真实 I/O 问题终止，返回该致命异常。"""
        return self._failure.exc if self._failure is not None else None

    # ------------------------------------------------------------------ start

    async def start(self) -> None:
        """启动唯一的 flush 线程。

        恰好一个消费者，因此恰好零把锁。重复调用会抛出异常，而不是悄悄遗留一个消费者。

        在此捕获正在运行的事件循环，因为 flush 线程需要它来桥接回 ``asyncio.Queue``；
        线程自身无法发现其调用方的事件循环。
        """
        if self._drain_thread is not None:
            raise RuntimeError("FrameWriter.start() called twice")

        self._loop = asyncio.get_running_loop()
        # 构造上即为唯一消费者——这是本类中不出现任何锁的原因。不要添加锁。
        self._drain_thread = threading.Thread(
            target=self._drain_in_thread,
            name="worker-drain",
            daemon=True,
        )
        self._drain_thread.start()

    # ------------------------------------------------------------------ send

    async def send(self, frame: dict[str, Any]) -> bool:
        """序列化并入队一帧。

        帧被接受时返回 ``True``。无法序列化的 payload 会抛出 ``TypeError``——那是编程
        错误，而不是断连，不应被伪装成断连。

        ``False`` 表示“该帧未能写出”，原因恰好有两种：对端已消失（常见情况），或背压
        超过了 :data:`_ENQUEUE_TIMEOUT_SECONDS`。第二种并非按设计静默丢弃协议帧，而是
        从不再消费的队列中脱身的最后手段，发生时会有显眼的日志。两种含义都意味着“停止
        发送”，这也是每个调用方的处理方式；区别体现在日志里，而非返回值中。
        """
        if self._peer_gone:
            return False
        if self._closed:
            return False

        # 入队前先序列化。多兆字节工具输出的耗时步骤是 json.dumps，在此完成（不涉及任何
        # 共享状态）可避免一个巨大的帧在消费其他生产者的帧时拖住唯一的消费者。
        #
        # ensure_ascii=False 因为协议需要承载中文文本；转义所有 CJK 码点会显著增大
        # payload。
        #
        # 此处的 TypeError 会向外传播：调用方必须看到真实的 bug。
        line = json.dumps(frame, ensure_ascii=False) + "\n"

        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # 背压。协议帧不允许丢弃——那会静默丢失一个事件并使 TUI 失去同步——因此这里
            # 等待空位以保持顺序。
            #
            # 等待是有界的。既然 flush 已不会阻塞事件循环，队列满就意味着 flush 确实很
            # 慢，而非死锁——但“确实很慢”在对端不读取又不关闭的情况下可能变成“永远”。
            # 超时会把这种情况变成可见的失败，而不是卡死本轮。
            logger.info(f"queue full (maxsize={self._queue.maxsize}); applying back-pressure")
            try:
                await asyncio.wait_for(
                    self._queue.put(line), timeout=_ENQUEUE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.info(
                    f"queue still full after {_ENQUEUE_TIMEOUT_SECONDS}s; "
                    "dropping this frame to unblock the run"
                )

                return False

        return True

    async def send_log(self, message: str) -> bool:
        """尽力而为的非协议输出。

        队列满时直接丢弃而不阻塞 agent 循环。永远不要用它发送协议帧。
        """
        if self._peer_gone or self._closed:
            return False
        try:
            self._queue.put_nowait(_LogLine(message))
        except asyncio.QueueFull:
            return False
        return True

    # ------------------------------------------------------------------ drain

    def _drain_in_thread(self) -> None:
        """唯一的消费者。运行在自己的线程上，直到收到哨兵。

        所有阻塞式工作都在这里，位于事件循环之外。通过把 ``get()`` 提交给捕获的循环并
        等待返回的 future 来从 ``asyncio.Queue`` 取项——这正是
        ``asyncio.run_coroutine_threadsafe`` 存在的用途。
        """
        loop = self._loop

        if loop is None:
            # start() 从未被 await。没有可消费的内容，也无需上报。
            return

        while True:
            try:
                item = asyncio.run_coroutine_threadsafe(self._queue.get(), loop).result()
            except Exception as exc:  # noqa: BLE001
                # 事件循环已消失（关闭）。没有需要服务的对象了。
                logger.info(f"drain could not read the queue, consumer stopping: {exc!r}")

                return

            if item is _DRAIN_STOP:
                return

            line = item.text if isinstance(item, _LogLine) else item

            try:
                self._stream.write(line)
                self._stream.flush()
            except BrokenPipeError:
                self._handle_peer_gone("BrokenPipeError")

                return
            except ValueError as exc:
                # ValueError("I/O operation on closed file") 是唯一表示“对端消失”的
                # ValueError。UnicodeEncodeError 是 ValueError 的子类，表示 locale 配置
                # 错误，属于真实 bug；记录它，使 ``close()`` 能带 traceback 重新抛出。
                if isinstance(exc, UnicodeEncodeError) or "closed file" not in str(exc):
                    self._record_failure(exc, "fatal ValueError")

                    return

                self._handle_peer_gone(str(exc))

                return
            except OSError as exc:
                if exc.errno not in _PEER_GONE_ERRNOS:
                    # ENOSPC、EACCES 等是主机问题，不是断连。
                    self._record_failure(exc, f"fatal OSError errno={exc.errno}")

                    return

                self._handle_peer_gone(f"OSError errno={exc.errno}")

                return
            except Exception as exc:  # noqa: BLE001 - 绝不让消费者静默终止
                # 写入失败绝不能静默杀死消费者。记录它（供 ``close()`` 重新抛出）并结束
                # 循环——进程会通过 __main__ 中的 peer_gone 检查退出，而不是卡死。
                self._record_failure(exc, "unexpected error")

                return

    def _handle_peer_gone(self, detail: str) -> None:
        self._peer_gone = True
        logger.info(f"peer gone ({detail}); writer marked dead")

    def _record_failure(self, exc: BaseException, label: str) -> None:
        """记录致命的 flush 异常，使其不会在线程中丢失。

        flush 线程没有可向其抛出的调用方。没有此机制，traceback 只会存在于该线程的临终
        遗言中；``close()`` 会重新抛出它，使带原始 traceback 的异常进入日志。
        """
        self._peer_gone = True
        self._failure = _DrainFailure(exc)
        logger.info(f"drain {label}, consumer stopping: {exc!r}")

    # ------------------------------------------------------------------ close

    async def close(self, *, timeout: float = 3.0) -> None:
        """以哨兵清空队列，然后 join flush 线程。

        重新抛出致命的 flush 异常（非对端消失的写入失败）；在 flush 不再与生产者共享栈
        的情况下，这是它到达调用方的唯一剩余途径。
        """
        if self._closed:
            return
        self._closed = True

        thread = self._drain_thread

        if thread is None:
            return

        try:
            self._queue.put_nowait(_DRAIN_STOP)
        except asyncio.QueueFull:
            # 队列卡死。不要为了等待空位而挂起关闭流程：该线程是守护线程，因此进程仍可
            # 退出。
            logger.info("close(): queue full; not waiting for the drain to finish")

        # 在事件循环之外 join：flush 可能正在阻塞式管道上写入，在此 await 会重新引入本类
        # 正是要避免的那种停顿。
        await asyncio.to_thread(thread.join, timeout)

        if thread.is_alive():
            logger.info(f"close(): drain thread did not finish within {timeout}s")

        self._drain_thread = None

        if self._failure is not None:
            failure = self._failure.exc

            logger.info(f"close(): drain had failed with {failure!r}; re-raising")

            raise failure


class _LogLine:
    """包装类，使日志行在队列中可与协议行区分。"""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text + ("" if text.endswith("\n") else "\n")


# ============================================================================

"""Worker NDJSON 协议的帧信封构造函数。

协议形态：

Node → Python::

    {"v":1,"id":"req-1","method":"session.start","params":{"sessionCode":"s1","input":"你好"}}
    {"v":1,"id":"req-2","method":"session.cancel","params":{"runId":"run-1"}}
    {"v":1,"id":"req-4","method":"process.shutdown","params":{}}

Python → Node::

    {"v":1,"id":"boot","kind":"ready","capabilities":["chat","cancel","session"]}
    {"v":1,"id":"req-1","kind":"started","runId":"run-1"}
    {"v":1,"id":"req-1","kind":"event","runId":"run-1","payload":{...AG-UI event...}}
    {"v":1,"id":"req-1","kind":"completed","runId":"run-1"}
    {"v":1,"id":"req-3","kind":"result","payload":{"items":[]}}
    {"v":1,"id":"req-1","kind":"error","error":{"code":"AGENT_ERROR","message":"..."}}

关联规则
--------

普通请求与流事件可以交错；归属判定必须使用 ``id``/``runId``，绝不能依赖行顺序。

``payload`` 字段按 Python SDK 的原样承载 AG-UI 事件。此处不做任何重新建模——这是刻意
的，以便 worker 保持在帧封装层，而不成为第二套消息协议。"""

# ============================================================================
# 线上协议信封
#     六个帧构造函数与版本常量。它们与 FrameWriter 同属本模块，因为 `dispatch` 与
#     `agent_runner` 都需要它们，而本模块不导入包内其他任何内容——因此二者都不必依赖对方
#     才能获得线上协议词汇。
# ============================================================================

#: 线上协议版本。每一帧都在其 ``v`` 字段中携带它；构造函数中永远不要硬编码字面量
#: ``1``——应改为递增此常量。
PROTOCOL_VERSION = 1

#: 启动时主动发出的 ``ready`` 帧所使用的请求 id。
READY_ID = "boot"

#: ``ready`` 帧中声明的能力集。``"auth"`` 声明 ``auth.login`` / ``auth.logout``，
#: ``"config"`` 声明 ``config.info`` / ``config.set``。二者都是增量字段：与旧版 worker
#: 通信的 TUI 只会看到标志缺失，从而可以降级而不是半可用。
#:
#: 刻意不再有 ``"files"`` 项。本地文件列表现在通过 ``agent.info`` 的 ``resources[]``
#: （``type == "file"`` 的行）传递，与所有其他资源一致。为它保留能力标志会声明一个 worker
#: 并不提供的方法。
DEFAULT_CAPABILITIES = ["chat", "cancel", "session", "agent", "auth", "config"]


def ready_frame(
    capabilities: list[str] | None = None,
    *,
    sdk_file: str | None = None,
) -> dict[str, Any]:
    """构造唯一一个启动帧，声明 worker 已可接受工作。

    ``sdk_file`` 是 worker 将要 patch 的、已解析的 ``aidev_agent.__file__``。它是一个
    平级键，而不是 ``capabilities`` 的条目：能力列表是固定的方法集合，而文件系统路径
    不是方法。该字段同样是增量字段——与旧版 worker 通信的 TUI 看到键缺失后降级，与新版
    worker 通信的 TUI 则可直接报告被 patch 的是哪份代码，而无需推断。

    未提供时该键被省略，而不是以 ``None`` 发出：键缺失明确表示“此 worker 不上报该项”，
    而出现 ``None`` 则需要另一条规则才能表达同样含义。
    """
    frame: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "id": READY_ID,
        "kind": "ready",
        "capabilities": list(
            DEFAULT_CAPABILITIES if capabilities is None else capabilities
        ),
    }

    if sdk_file is not None:
        frame["sdk_file"] = sdk_file

    return frame


def started_frame(req_id: str, run_id: str) -> dict[str, Any]:
    """确认 ``session.start``，并将用于关联的 ``runId`` 交给调用方。"""
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "kind": "started",
        "runId": run_id,
    }


def event_frame(req_id: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """承载运行中的 agent 产生的一个原始 AG-UI 事件。"""
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "kind": "event",
        "runId": run_id,
        "payload": payload,
    }


def completed_frame(req_id: str, run_id: str) -> dict[str, Any]:
    """表示一次运行正常结束。"""
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "kind": "completed",
        "runId": run_id,
    }


def result_frame(req_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """回应非流式请求（``session.*``、``process.shutdown`` 等）。"""
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "kind": "result",
        "payload": payload,
    }


def error_frame(req_id: str | None, code: str, message: str) -> dict[str, Any]:
    """针对 ``req_id`` 上报一次失败。

    对于无法归因到单个请求的失败，``req_id`` 可以为 ``None``——例如无法读取的行上的
    解析错误，或在读循环开始前就检测到的导入失败。
    """
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "kind": "error",
        "error": {"code": code, "message": message},
    }
