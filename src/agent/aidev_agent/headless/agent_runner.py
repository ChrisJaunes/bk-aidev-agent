"""驱动真实的 ``AidevAGUIAgent``，并把它的 AG-UI 事件作为帧转发出去。

本模块负责的契约
----------------

**每次运行一个独立任务。** 一次运行是它自己的 ``asyncio.Task``，绝不是 stdin 循环里
的同步迭代。正是这一点让 ``session.cancel`` 在生成进行中仍能被送达——如果循环被阻塞在
迭代一个生成器上，取消帧会一直躺在管道里无人读取，直到该次运行结束，而那时取消已经
没有意义。

**转发，不重新建模。** ``AidevAGUIAgent.run()`` 产出的是**已编码的 JSON 字符串**
（``EventEncoder`` 的输出，形如 ``data: {...}\\n\\n`` 的 SSE 格式）。每一个都会被
去掉外壳、解析，然后原样放进 ``payload``。这里不做过滤、改名或特例处理——尤其是
``MESSAGES_SNAPSHOT`` 与 ``RUN_FINISHED`` 原样透传。worker 是分帧层，不是第二套消息
协议。

**取消是协作式且分两段。** SDK 轮询 ``cancel_checker``，当它返回 ``True`` 时发出
一个正常的 ``RUN_FINISHED``，而不是中途强行中断。正是这条优雅路径让 TUI 的
``isChatting`` 不会卡住。因此取消**先**设置每轮标志；dispatcher 随后在宽限期之后
硬取消该任务，作为对卡在 ``await`` 里、永远不会观察该标志的运行的后备手段。

标志本身**不**保存在这里。``run()`` 把判定函数作为参数接收，因为“这次运行是否已取消”
是调用的输入——与 :meth:`_run` 的签名一致——而 dispatcher 是它的唯一权威。自己
保留一份副本的运行会成为 dispatcher 已跟踪状态的第二个事实来源。

**agent 失败会降级，不会杀死 worker。** 任何异常都会变成一个 ``kind=error`` 帧，
错误码为 ``AGENT_ERROR``。唯一会传播的异常是 ``asyncio.CancelledError``，它必须传播，
这样任务取消才能继续工作。

测试替身
--------

``AidevAGUIAgent`` 的驱动需要 SDK、环境和可用的模型，而测试不需要。测试经
覆写/monkeypatch :meth:`AgentRunner._run`（单个方法、单一签名
``(params, is_cancelled, state)``）替换实现；生产环境零注入缝。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import sys
import threading
import traceback
from contextvars import copy_context
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

from aidev_agent.pydantic_models import ExecuteKwargs

from . import agent_factory
from .checkpointer import TuiSqliteSaver
from .config import TuiConfig, get_credentials
from .frame_protocol import FrameSink, completed_frame, error_frame, event_frame
from .local_resources import LocalDeclarations, LocalResourceManager
from .utils import BKAI_HOME_DIRNAME

logger = getLogger(__name__)


#: 一次被取消的运行在 dispatcher 硬取消其任务之前，被给予多久来发出自己的优雅终止
#: 事件。见 ``dispatch._cancel_backstop``。
CANCEL_GRACE_SECONDS = 2.0

#: agent 构建允许花费多久，超出则以可见错误使本轮失败。
#:
#: 构建过程会导入 SDK、拉取平台配置，并枚举每个已绑定 MCP server 的工具列表。MCP
#: 那一半在 SDK 里**没有**单 server 超时（``BaseResourceManager.construct_mcp``，
#: 用 ``asyncio.gather`` 走两次尝试），因此一个无响应的 server 会无限期挂住构建——
#: 连带挂住 TUI。
#:
#: 相对健康构建（实测亚秒级）而言给得很宽，因为在负载高的机器上冷启动 SDK import
#: 合理地慢，而误报失败比慢一点的成功更糟。它要战胜的是“永远”，不是“慢”。
AGENT_BUILD_TIMEOUT_SECONDS = 180.0

#: 任何 agent 侧失败上报给 TUI 时使用的错误码。
AGENT_ERROR_CODE = "AGENT_ERROR"

#: 经生产者队列传递的流结束标记，见 :meth:`_aiter_agent`。
#: 用模块级唯一对象，这样合法的 ``None``/``""`` 分片永远不会被误认为终止符。
_STREAM_END = object()

#: 告诉循环：生产者从 ``agent.execute`` 拿到的是一个异步生成器（测试替身），必须在该
#: 循环上 await，而不是从队列里取。见 :meth:`_aiter_agent`。
_ASYNC_RESULT = object()


class _Writer(FrameSink):
    """本模块所需的 ``FrameWriter`` 切片。

    ``peer_gone`` 有意声明为可选：真实 writer 总是有它，但最小化的测试替身不必有，
    而属性缺失绝不能被误判为运行失败。见 :class:`aidev_agent.headless.frame_protocol.FrameSink`。
    """

    async def send(self, frame: dict[str, Any]) -> bool: ...


def _never_cancelled() -> bool:
    """``is_cancelled`` 的默认值：不支持取消的运行永远不会被取消。"""
    return False


def _new_session_code() -> str:
    """生成防碰撞的会话标识符（会话登记表用）。

    这就是 TUI 持有的不透明句柄，也是 LangGraph checkpointer 的关联键——SDK 从
    ``ExecuteKwargs.session_code`` 推导它自己的 ``thread_id``（
    ``agent.session.session_code → agent.thread_id → configurable.thread_id``）。
    worker 不另立 ``thread_id``：那只会是 ``session_code`` 的恒等副本，既不改变
    行为，又暗示"改它能换线程"（实际不能）。
    """
    return f"thread-{uuid4().hex[:12]}"


def _now_iso() -> str:
    """ISO-8601 UTC 时间戳，秒级精度（会话登记表用）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_sse_shell(chunk: str) -> str:
    """去掉 SSE 帧的 ``data: `` 前缀与结尾空行。

    ``EventEncoder.encode`` 产出 ``data: {json}\\n\\n``。worker 扮演的正是原本会由
    HTTP SSE 连接承担的传输角色，因此它**只**去掉这层外壳，别的什么都不动。多行
    ``data:`` 块会被拼接，这是 SSE 规范要求的；以 ``:`` 开头的注释与心跳行则无论
    出现在哪里都被丢弃。

    完全没有 ``data:`` 行的分片会返回去空格后的原文——它不是 SSE，为它臆造一种
    变换只会破坏载荷。
    """
    text = chunk.strip()

    payload_lines: list[str] = []
    saw_data = False
    for line in text.splitlines():
        if line.startswith("data:"):
            saw_data = True
            payload_lines.append(line[len("data:") :].lstrip())
        elif line.startswith(":"):
            # SSE 注释 / 心跳——永远不属于载荷。
            continue
        elif saw_data:
            # data 块内的续行（SSE 会把这些拼接起来）。
            payload_lines.append(line)

    if not saw_data:
        # 不是 SSE 帧；返回去空格后的原文而非空字符串，这样直接喂原始 JSON 的
        # 调用方仍然可用。
        return text

    return "\n".join(payload_lines)


class AgentRunner:
    """一次一个地运行 ``AidevAGUIAgent`` 会话轮次并以帧输出。

    本类**只**负责"跑一轮"。它不持有任何进程级状态——会话身份
    登记表、凭据、缓存的 resource manager、TUI 配置全部住在
    :class:`SessionState` 上，由调用方（:class:`aidev_agent.headless.dispatch.Dispatcher`）持有并在
    每次 ``run(state=...)`` 时传进来。runner 在一次运行期间借用那个实例，但**不**把
    它存成实例属性。

    这样"哪些会话已登记""谁登录了"就只有一个权威（dispatcher 持有的那一个
    ``SessionState``），而不是 runner 与 dispatcher 各持一份、需要保持同步。

    agent 与图在**首次运行时惰性构建**，使 worker 启动保持快速、``process.ready``
    不被图构建延迟（图构建会导入 SDK 并打开 HTTP 客户端）。
    """

    def __init__(
        self,
        *,
        writer: _Writer,
        on_run_settled: Callable[[str], None] | None = None,
    ) -> None:
        self.writer = writer
        #: 运行结束时以 ``run_id`` 调用，使 dispatcher 能把它从任务登记表中移除。
        self._on_run_settled = on_run_settled
        #: 串行化惰性构建，使两个并发运行不会重复构建。**构造于 ``__init__``**：
        #: 惰性创建（"为 None 则建"）在两个并发协程同时通过判空时会各自得到一把
        #: 锁，随后的 ``async with`` 互斥即告失效——正是这把锁要防的并发问题。
        self._build_lock = asyncio.Lock()

    def _build_session_agent(
        self,
        state: SessionState,
        session_code: str,
        *,
        checkpointer: Any = None,
    ) -> Any:
        """为 ``session_code`` 组装由平台描述的 agent。

        按会话构建而非跨会话缓存：agent 携带该会话的写回处理器与线程绑定。

        ``state`` 是调用方（dispatcher）持有的进程级状态，本次运行经它取凭据、解析
        agent_code，并读取/更新"已向平台登记过的会话"集合。

        本函数**不**接收模型：``chat_model`` 由 loader 链从进程级的
        ``SessionState.current_model``（``/models`` 的持久选择）与落盘的 per-agent 声明
        得出，不是一次构建的输入。

        ``checkpointer`` 是 worker 的进程级
        :class:`aidev_agent.headless.checkpointer.TuiSqliteSaver`，由**异步**调用方
        （:meth:`_run` 的构建段）解析，此处只做保存。该 saver 有意做到与事件
        循环无关，因此在本线程里构造也能工作——但解析仍留在异步调用方，这样单例在
        构建线程被派发之前就已创建，且构建失败永远不会取决于 saver 的构造。
        """
        rm = state.ensure_manager()

        if session_code not in state._known_sessions:
            # 一轮运行开始前平台必须已知该会话——在 SESSION 模式下 SDK 会自行拉取
            # 它的已存历史，而未知的 code 会 404。
            agent_factory.ensure_session(rm, session_code)
            state._known_sessions.add(session_code)

        # ``rm`` 由本类提供（``state.ensure_manager()``），而不是让 ``build_agent`` 另建
        # 一个：agent 因此拿到的是**同一个** manager，会话 token（``/login``）与
        # ``current_model`` 都与本进程级状态同源。
        agent = agent_factory.build_agent(
            rm,
            session_code,
            state._resolve_agent_code(rm),
            checkpointer=checkpointer,
        )

        logger.info(agent_factory.describe_agent(agent))

        return agent

    # ------------------------------------------------------------------- 运行

    async def run(
        self,
        *,
        req_id: str,
        run_id: str,
        session_code: str | None,
        input_text: str,
        state: SessionState,
        is_cancelled: Callable[[], bool] | None = None,
        execute_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """执行一轮，持续输出帧直到完成或被取消。

        发出的帧，按顺序：

        1. 每个 SDK 产出的 AG-UI 事件一个 ``event`` 帧，
        2. 正常结束时 ``completed``，
        3. 运行抛出异常时 ``error``（错误码 ``AGENT_ERROR``）。

        除 :class:`asyncio.CancelledError` 外从不抛出异常，而它必须传播，这样任务
        取消才能工作。失败的运行会降级为一个帧，而不是杀死 worker。

        ``state`` 是调用方（dispatcher）持有的进程级 :class:`SessionState`，每次运行
        经它取会话身份（``state.resolve``）、凭据与 agent_code。runner **不**把它存成
        属性——它只是本次运行的**输入**。

        ``is_cancelled`` 是 SDK 轮询的协作式取消判定函数；它由调用方提供而不是从
        runner 状态推导，因为它确实是本次调用的**输入**（与 :meth:`_run` 的签名
        一致）。dispatcher 持有已取消运行的集合，运行只需向它询问。``None`` 表示
        “永不取消”，这正是没有取消支持的调用方——脚本、测试替身——合理想要的。

        仅靠标志并不总是足够（卡在 ``await`` 里的运行永远不会轮询），这也是
        dispatcher **还**会在 :data:`CANCEL_GRACE_SECONDS` 后硬取消该任务的原因。
        两半都是必需的。
        """
        try:
            # ``resolve`` 确保该会话已登记（未知 code 会新建），返回值仍是同一个
            # ``session_code``——它同时就是 LangGraph 的 thread key，不再另立
            # ``thread_id``。因此下面只传 ``sessionCode``，不再传同义的 ``threadId``。
            session_code = state.resolve(session_code)
            logger.info(f"run {run_id} session_code={session_code!r}")

            params = {
                "reqId": req_id,
                "runId": run_id,
                "input": input_text,
                "sessionCode": session_code,
                # chat-helper 的 `execute_kwargs` 原样传递（携带嵌套的 UserQuestion
                # `resume`）。普通轮次时缺席——此时运行实现合并 `{}`，因此 SDK 仍能
                # 拿到它的基础键。
                "executeKwargs": execute_kwargs,
            }

            # 单一签名 ``(params, is_cancelled, state)``：真实实现与测试替身同形
            # （替身经覆写/monkeypatch :meth:`_run` 注入，见模块 docstring）。
            gen = self._run(params, is_cancelled or _never_cancelled, state)

            async for chunk in gen:
                # ``peer_gone`` 在 writer 协议上是可选的：真实的 ``FrameWriter``
                # 暴露它，但最小化的替身可能没有，而属性缺失绝不能把一次健康的运行
                # 变成错误帧。缺席意味着“假定对端存活”——随后 ``send`` 返回 False
                # 才是权威信号。
                if getattr(self.writer, "peer_gone", False):
                    logger.info(f"run {run_id} stopping: peer gone")
                    break

                raw = strip_sse_shell(chunk) if isinstance(chunk, str) else chunk
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    # 非 JSON 分片是 bug，不是致命状况：丢弃一个畸形事件不能杀死整次
                    # 运行。
                    logger.info(f"run {run_id} produced non-JSON event, dropped: {str(chunk)[:200]!r}")
                    continue

                ok = await self.writer.send(event_frame(req_id, run_id, payload))
                if not ok:
                    logger.info(f"run {run_id} stopping: frame write reported peer gone")
                    break

        except asyncio.CancelledError:
            # 来自 dispatcher 后备手段的任务级取消。重新抛出以保持取消语义完整，但
            # 先报告完成，这样父方能让 ``isChatting`` 落定而不是挂住。
            logger.info(f"run {run_id} hard-cancelled")
            await self.writer.send(completed_frame(req_id, run_id))
            raise
        except Exception as exc:  # noqa: BLE001 - 永不杀死 worker
            logger.info(f"run {run_id} failed: {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
            await self.writer.send(
                error_frame(req_id, AGENT_ERROR_CODE, f"{type(exc).__name__}: {exc}"[:500])
            )
            return
        finally:
            if self._on_run_settled is not None:
                self._on_run_settled(run_id)

        await self.writer.send(completed_frame(req_id, run_id))

    # --------------------------------------------------------- 真实运行实现

    async def _run(
        self,
        params: dict[str, Any],
        is_cancelled: Callable[[], bool],
        state: SessionState,
    ) -> AsyncIterator[str]:
        """驱动由平台描述的 agent，产出它已编码的事件字符串。

        这是“驱动**真实** ``aidev_agent``”的实现。所有 SDK 导入都延迟到首次使用时，
        这样仅仅导入本模块（并因此运行分帧测试）永远不会触碰 SDK。

        ``state`` 是由 :meth:`run` 逐层透传进来的进程级 :class:`SessionState`。
        它只被构建段使用，用于取凭据 / 解析 agent_code / 登记会话 / 解析
        checkpointer——runner 自身不持有它。

        agent 来自 :mod:`aidev_agent.headless.agent_factory`，即 SDK 自己的
        ``AgentInstanceFactory``，而不是手工拼装的 LangGraph。这一点不止关乎工具：
        工厂才是应用平台的角色提示词、知识库、技能、MCP server、模型上下文选项与
        会话回写接线的地方。手工构建会造出一个并非平台所描述的 agent——缺失的工具
        列表只是可见的症状。
        """
        # --- 1. 惰性、卸载到线程、带截止时间的构建 ----------------------------
        # ``_build_lock`` 于 ``__init__`` 构造（见其注释：惰性创建会引入两把锁的
        # 竞态），串行化并发首次构建。
        #
        # **为什么构建要有截止时间**：``AgentInstanceFactory.build_agent`` 最终走到
        # SDK 的 ``BaseResourceManager.construct_mcp``，它用 ``asyncio.gather``
        # 拉取每个 MCP server 的工具列表，且**没有单 server 超时**（base.py:621-690，
        # 尝试两次，无 ``wait_for``）。一个接受了 TCP 连接却永不响应的 MCP server
        # 会阻塞整个 gather——连带阻塞构建与整轮；TUI 永远显示“streaming…”，
        # 与崩溃无法区分。这是真实情形而非假设（端点被移除的 MCP server 失败模式
        # 是静默挂起而不是 4xx）。超时上报为一次普通轮次错误，至少告诉用户哪个
        # 东西慢。该线程无法被取消（``to_thread`` 无中断手段），超时后会在后台
        # 继续跑到底层 socket 超时——给**等待**设界才是这一层能做的事。
        run_id = params["runId"]
        input_text = params["input"]
        # `sessionCode` 是唯一的会话身份——它同时就是 LangGraph 的 thread key。
        session_code = params.get("sessionCode") or ""
        # chat-helper 的 execute_kwargs，普通轮次时为 `{}`。这个 `or {}` 是让缺席
        # 情形不至于破坏 `model_validate` 的原因——下面的五个基础键仍然会被提供。
        carried = params.get("executeKwargs") or {}

        if not session_code:
            raise ValueError("session.start requires a sessionCode")

        async with self._build_lock:
            # checkpointer 在共享的异步路径上解析，早于 ``to_thread`` 派发：saver
            # 单例（:class:`aidev_agent.headless.checkpointer.TuiSqliteSaver`）只构建一次、不与
            # 构建线程竞争，且构造失败表现为构建失败而不是轮次内部某个神秘的首次
            # 使用错误。``ensure_checkpointer`` 幂等，后续轮次无代价。
            checkpointer = await state.ensure_checkpointer()

            try:
                agent = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._build_session_agent,
                        state,
                        session_code,
                        checkpointer=checkpointer,
                    ),
                    timeout=AGENT_BUILD_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"agent build exceeded {AGENT_BUILD_TIMEOUT_SECONDS}s. A configured MCP "
                    "server most likely accepted the connection and then never answered — "
                    "the SDK fetches MCP tools with no per-server timeout. Check the MCP "
                    f"servers bound to this agent, and ~/.bkai/logs/worker.log for which one "
                    f"did not respond. (session_code={session_code})"
                ) from None

        # --- 2. agent 消费的输入 ---------------------------------------------
        # ``ChatCompletionAgent.execute`` 接收 SDK 自己的 ``ExecuteKwargs``。
        # ``stream_mode`` 只接受 'start' / 'attach'。
        #
        # ``persist_input`` 为 True，是因为在工厂之下，记录用户轮次的是 SDK。
        # worker 自己不保留任何消息存储：TUI 侧的 `plusMessage` 是一次交接，而不是
        # 第二个权威。
        #
        # chat-helper 的 `execute_kwargs` **最后**展开，这样嵌套的 `resume`（或
        # 它未来携带的任何键）能到达 SDK。上面五个基础键是 worker 自己的默认值，
        # 而 chat-helper 总是发送相同的 `stream` / `stream_mode` / `persist_input`
        # 取值——因此展开对它们而言是可观测地中性的，`resume` 是它唯一能真正新增的键。
        execute_kwargs = ExecuteKwargs.model_validate(
            {
                "stream": True,
                "stream_mode": "start",
                "input": input_text,
                "session_code": session_code,
                "persist_input": True,
                **carried,
            }
        )

        # --- 3. 取消 ----------------------------------------------------------
        # SDK 轮询这个判定函数；dispatcher 另外会在宽限期后对卡在 await 里的运行硬
        # 取消该任务。
        self._bind_cancel_checker(agent, is_cancelled)

        # --- 4. 原样流式输出 --------------------------------------------------
        # ``execute`` 产出的是已编码的 SSE 字符串。原样 yield 出去；调用方去掉外壳
        # 并把每个作为一个帧转发。
        retries = int(os.environ.get("AGENT_TUI_LLM_RATE_LIMIT_RETRIES", "0"))
        if retries <= 0:
            async for chunk in self._aiter_agent(agent, execute_kwargs):
                yield chunk
            return

        from aidev_agent.packages.langchain_core.models.llm_retry import RetryState

        loop = asyncio.get_running_loop()
        retry_state = None

        def report_retry(event: dict) -> None:
            if retry_state is None or loop.is_closed():
                return
            if is_cancelled() or getattr(self.writer, "peer_gone", False):
                retry_state.cancel()
            # A model can wait without producing SDK chunks. Publish heartbeats
            # directly on the writer's loop so the parent can account for that wait.
            asyncio.run_coroutine_threadsafe(
                self.writer.send(event_frame(params["reqId"], run_id, event)), loop
            )

        retry_state = RetryState(
            max_retries=retries,
            wait_budget=float(os.environ.get("AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET", "0")),
            cancel_checker=lambda: is_cancelled() or getattr(self.writer, "peer_gone", False),
            on_event=report_retry,
        )
        try:
            with retry_state.bind():
                async for chunk in self._aiter_agent(agent, execute_kwargs, retry_state=retry_state):
                    yield chunk
        finally:
            retry_state.cancel()

    @staticmethod
    def _bind_cancel_checker(agent: Any, is_cancelled: Callable[[], bool]) -> None:
        """挂上 SDK 会查询的协作式取消判定函数。

        工厂构建的 agent 在事件处理器存在时会从它上面读取 ``cancel_checker``；
        否则该标志只会被 dispatcher 的硬取消观察到。在支持时设置它，可以保持优雅的
        ``RUN_FINISHED`` 路径工作。
        """
        handler = getattr(agent, "event_handler", None)

        if handler is not None and hasattr(handler, "cancel_checker"):
            try:
                handler.cancel_checker = is_cancelled
            except Exception:  # noqa: BLE001 - 可选钩子，永不致命
                logger.info("event handler rejected cancel_checker; relying on hard cancel")

    @staticmethod
    async def _aiter_agent(agent: Any, execute_kwargs: Any, *, retry_state: Any = None) -> AsyncIterator[str]:
        """迭代 ``agent.execute``，且**不**阻塞主事件循环。

        为什么整次调用都跑在工作线程里
        ------------------------------
        ``agent.execute`` 是**完全同步**的。仅这次调用就很昂贵——
        ``_execute`` -> ``_stream`` 会构建运行图、做一次 checkpointer 的
        ``get_state``（``chat.py:1182``）并为模型阻塞——而随后的迭代还会按分片继续
        阻塞。把任一半放在事件循环上跑，都会为整轮挂住 worker 中的其他每次运行
        （并发要求）。

        把**整件事**（调用 + 迭代）放在一个工作线程里跑，就能让循环保持自由。这对
        checkpointer 也很重要：SDK 的异步图运行发生在 SDK **自己的**每轮循环上
        （``async_to_sync_generator``，``aidev_agent/utils/async_utils.py``），因此
        saver 是从生产者线程的上下文而非本循环被触碰的。worker 的
        :class:`aidev_agent.headless.checkpointer.TuiSqliteSaver` 特意做到与循环无关（同步内核 +
        ``asyncio.to_thread`` 异步皮），正是为了让两侧都安全。

        分片经一个有界的 ``queue.Queue`` 回传：生产者线程在 ``put`` 上阻塞（天然的
        背压），循环用 ``asyncio.to_thread(queue.get)`` 取走，因此两侧都不会空转。
        异常会在循环上重新抛出；``_STREAM_END`` 终止该流。
        """
        # 给队列设界，使快速的生产者无法把整轮缓冲在内存里；当循环忙于取走时，
        # 工作线程会在 `put` 上阻塞。
        chunks: queue.Queue[Any] = queue.Queue(maxsize=64)
        stream = {"result": None}

        def _produce() -> None:
            try:
                # 调用与迭代**两者**都必须在循环之外：`execute()` 是 eager 的，且在
                # yield 任何东西之前就触碰 checkpointer。
                result = agent.execute(execute_kwargs)
                stream["result"] = result

                if hasattr(result, "__aiter__"):  # pragma: no cover - 仅测试替身
                    chunks.put(_ASYNC_RESULT)

                    return

                for chunk in result:
                    chunks.put(chunk)
            except BaseException as exc:  # noqa: BLE001 - 转发到下面的循环
                chunks.put(exc)
            finally:
                chunks.put(_STREAM_END)

        context = copy_context()
        producer = threading.Thread(target=context.run, args=(_produce,), name="agent-execute", daemon=True)
        producer.start()

        try:
            while True:
                # `to_thread` 让循环保持自由，以便服务来自生产者线程的 saver 的
                # `run_coroutine_threadsafe` 调用。
                item = await asyncio.to_thread(chunks.get)

                if item is _STREAM_END:
                    return

                if item is _ASYNC_RESULT:
                    result = stream["result"]
                    async for chunk in result:
                        yield chunk
                    return

                if isinstance(item, BaseException):
                    raise item

                yield item
        finally:
            if retry_state is not None:
                retry_state.cancel()
            # 运行可能提前结束（对端消失、取消）。关闭该同步生成器，使 SDK 自己的
            # `finally` 块得以执行；生产者线程随后自行结束（它是守护线程，因此绝无
            # 可能卡住退出）。
            result = stream["result"]
            close = getattr(result, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:  # noqa: BLE001 - 拆解不得掩盖真正的错误
                    logger.info("agent.execute teardown raised; ignored")


class SessionState:
    """worker 的进程级身份 / manager / 配置快照状态。

    与 :class:`AgentRunner` 同处一个模块，因为两者是同一个关注面的两半：runner 驱动
    单个轮次的执行，本类持有那些轮次运行时所依据的身份——"我们在和哪个平台对话、以
    谁的身份"。**进程级的唯一宿主是 :class:`Dispatcher`**：dispatcher
    构造一个实例并持有它，在每次 ``runner.run(state=...)`` 时把同一个实例传进去。
    runner 在一次运行期间借用它，但**不**存为实例属性（它回到"一次一个地跑"的定位）。

    manager 的构造就内置在 :meth:`ensure_manager` 里：只有本类构造 manager、
    也只有它决定用哪个身份（用户名与 token 同源，都读 ``bkai_tui.json`` 的
    ``auth`` 段）。模型枚举就是 :meth:`list_llms` 本身。
    :mod:`aidev_agent.headless.agent_factory` 只剩 SDK 拼装（``build_agent`` 及其辅助）。
    进程环境的准备（``aidev_agent.headless.config.bootstrap_environment``）不在此列——它由
    ``__main__`` 在启动时完成一次，见 :meth:`ensure_manager`。

    每进程一个实例。这里存几样东西，它们有意共用同一条刷新路径：

    * 凭据字典，以及缓存的平台 resource manager，
    * checkpoint saver 单例（:meth:`ensure_checkpointer`）——进程级状态，但**刻意
      不走**那条刷新路径，见 :meth:`invalidate`，
    * 会话级 access token（``/login`` / ``/logout``），
    * 会话身份登记表（``_sessions``）与已向平台登记过的会话集合
      （``_known_sessions``）——都是跨运行存活的进程级状态，
    * 进程级 TUI 配置的写入端（``_tui_config``，``bkai_tui.json`` 的 ``agent_code``
      / ``auth``）；per-agent 配置的写入经 manager（``rm.write_declaration``），
      本类只负责给出"当前主 agent"的 ``code``。

    会话身份登记表只存*身份，别的什么都不存*（``sessionCode`` / ``createdAt``）：
    消息与对话状态住在 ``aidev_agent``（经 LangGraph checkpointer 的远端持久化），
    ``session_code`` 是关联键——SDK 由它推导自己的 ``thread_id``（= LangGraph 的
    ``configurable.thread_id``）。任何一侧出现第二个权威存储都明确不在范围内：
    让登记表长成"只是为了 UI"的对话缓存，会立即造出第二个事实来源——TUI 从本地
    登记表渲染，SDK 从它的 checkpointer 渲染，两者会在某次运行被中断、恢复或重放
    的那一刻开始漂移。

    **进程级与 per-agent 的概念分离**（``config.set`` 的归属）：``kind == "agents"`` 写**进程级**
    TUI 配置；``kind`` ∈ ``{models, mcps, knowledge}`` 写**当前主 Agent** 的 per-agent
    配置。二者唯一的耦合点：per-agent 写入的目标是 ``TuiConfig.agent_code`` 所指那个
    agent 的文件。

    ``invalidate()`` 会一并丢弃 manager、快照以及 ``agent.info`` 中缓存的那一半平台
    数据，这正是让 ``login / logout / switch`` 这组刷新成为同一套机制、而不是三份
    近似拷贝的原因。工作区文件列表归 manager（``rm.files``）所有，本类不持有它——
    它也不是平台数据，其读取与谁登录无关（见 :meth:`describe_agent_config`）。

    ``mcp.json`` 的逐文件 mtime 读缓存**不在**本类上——它已下沉到 manager 的
    :class:`resource_manager.McpResolver`（``rm.mcp``），并由 loader 链上的
    :class:`resource_manager.McpLoader` 经 ``load()`` lazy 读取；本类不推送，
    “文件未变则不重读”的记账由 resolver 拥有。

    工作区文件列表（``agent.info`` 的本地那一半）同样**不在**本类上——它的唯一所有者
    是 manager（``rm.files`` + ``rm.workspace_files``）。:meth:`describe_agent_config`
    只委派给 ``rm``，行由链中的 :class:`WorkspaceFileLoader` 并入 ``resources[]``，因此
    不存在「第二个 ``WorkspaceIndex`` 实例」，也不会被读第二遍。
    """

    def __init__(self, *, env: dict[str, str] | None = None) -> None:
        #: 启动时解析一次的凭据（每次会话构建共享）。
        self._creds: dict[str, str] | None = None
        #: 平台 resource manager，复用以便每进程只拉取一次配置。
        self._resource_manager: Any | None = None
        #: 会话级 access token。**必须**存在 rm 之外：``invalidate()``
        #: （``/login`` / ``/logout`` / ``config.set``）会把 ``_resource_manager`` 置
        #: ``None``，下一轮重建一个全新 rm（其 ``access_token`` 是空的），因此"最后
        #: 登录的那个 token"要跨重建存活——每次 ``ensure_manager`` 时重新推入新 rm
        #: （见该方法）。
        #:
        #: **跨重启**：启动时从 ``TuiConfig.auth.access_token``
        #: （``bkai_tui.json``，用户空间）读回；``/login`` 落盘、``/logout`` 清盘，
        #: 因此本字段与磁盘文件始终是同一个事实。读盘永不抛（缺失 = 无 token）。
        self._access_token: str = self._read_tui_config().access_token
        #: 用户在 ``/models`` 里选中的模型，``None`` 表示保留平台自身的选择。
        #:
        #: 这是**当前生效的模型**，不是"某一次构建期间临时生效"的覆盖：切换之后它就
        #: 一直生效，直到再次切换。
        #:
        #: **为什么在进程级状态上而不是在 manager 上**：manager 每次构建都会新建，挂在
        #: 实例上的槽位会随 manager 分裂成多份，于是「哪个是真源」变成未决问题——经另一
        #: 份走的路径（``agent.info``、``config.info`` 快照）永远看到 ``None``。
        #:
        #: 由 :meth:`set_local_declaration` 在 ``kind == "models"`` 时设置（它与落盘的
        #: per-agent 声明同步：声明负责跨进程持久，本字段负责当前进程内**立即**生效）。
        self.current_model: str | None = None
        #: 进程级 TUI 配置（``bkai_tui.json``）：管“主 Agent 是谁”（``agent_code``）
        #: 与身份认证（``auth``）。**按需读盘**（:meth:`_read_tui_config`），不缓存——
        #: 它是进程级配置，用户可能在外部编辑文件，且 ``/agents`` 写入后必须立即可见。
        #: 见 :class:`aidev_agent.headless.config.TuiConfig`。
        self._tui_config = TuiConfig()
        #: 凭据解析所依据的环境字典。
        self._env = env or {}
        #: session_code -> 记录的会话 dict。仅内存；持久化的对话记录是 SDK 的
        #: checkpointer，以 ``session_code``（即它的 LangGraph thread key）为键。
        self._sessions: dict[str, dict[str, Any]] = {}
        #: 本进程内已向平台登记过的会话 code。跨运行存活——同一会话的第二次运行
        #: 不再重复 ``ensure_session``。
        self._known_sessions: set[str] = set()
        #: worker 进程级的 checkpoint saver（:class:`aidev_agent.headless.checkpointer.TuiSqliteSaver`）。
        #: 惰性创建于 :meth:`ensure_checkpointer`，此后每次构建都复用；靠
        #: ``session_code`` 隔离（SDK 由它推导 LangGraph 的
        #: ``configurable.thread_id``，按会话稳定）来区分两个会话的状态，saver 本身
        #: 是有意共享的。**不随 :meth:`invalidate` 丢弃**——checkpoint 承载跨会话
        #: 持久状态，丢了等于丢历史；有序关闭发生在 :meth:`close_exit_stack`
        #: （``Dispatcher.shutdown`` 在 gather 完所有运行后调用）。
        self._checkpointer: Any | None = None
        #: 进程级 :class:`contextlib.ExitStack`：**整个进程生命周期**使用、需要有序
        #: 拆除的资源都从它进入（当前唯一成员：checkpoint 的
        #: ``from_conn_string`` 上下文；后续同性质的资源也应挂到这里，而不是
        #: 各自另立"永不关闭"的裸引用）。
        #:
        #: ``from_conn_string`` 是 ``@contextmanager``，``.__enter__()`` 把生成器
        #: 推进到 ``yield`` 并返回 saver；ExitStack 记住其 ``__exit__`` 回调，同时
        #: 经栈帧持有该生成器——若没有任何东西持有它，CPython 会在表达式结束的
        #: 瞬间将其引用计数归零并执行其 ``finally``（``conn.close()``），连接立即
        #: 关闭。持有方式因此是 ExitStack 而不是裸属性：拆除有明确的落点
        #: （:meth:`close_exit_stack`），不是"永不 ``__exit__``"。
        self._exit_stack: contextlib.ExitStack | None = None

    # ------------------------------------------------------- 会话身份登记表

    def session_new(self) -> dict:
        """创建一个新会话并返回其身份记录。

        返回带 ``sessionCode`` 与 ``createdAt`` 的 dict。**没有** ``threadId``：
        ``session_code`` 就是 LangGraph 的 thread key（SDK 由它推导 ``thread_id``），
        二者不是两个身份，因此不记两份。TUI 侧如仍读到 ``threadId`` 只是因为它对
        该字段做了回退渲染（``session-picker``），缺失时回退到 ``sessionCode``。
        """
        session_code = _new_session_code()
        record = {
            "sessionCode": session_code,
            "createdAt": _now_iso(),
        }
        self._sessions[session_code] = record
        return record

    def resolve(self, session_code: str | None) -> str:
        """确保 ``session_code`` 对应的会话已登记，返回该 ``session_code``。

        返回的就是传入的 code（未知/缺失时返回新建的 code）——因为 ``session_code``
        本身就是 LangGraph 的 thread key，不存在"另一个 id"可解析。保留本方法而非让
        调用方内联，是为了保留"未知 code 不报错、而是新建"这条语义（见下）。

        未知或缺失的 code 会产生一个**新**会话而不是报错：调用方想要对话却没有可用
        句柄，唯一有用的响应就是一个能用的。静默复用无关线程才是危险的替代方案——
        这正是未知情形选择新建而不是猜测的原因。
        """
        if session_code and session_code in self._sessions:
            return session_code
        return self.session_new()["sessionCode"]

    def get(self, session_code: str | None) -> dict | None:
        """无副作用地查找一条会话记录。

        与 :meth:`resolve` 不同，它从不创建——``session.get`` 是一次读取，而一次
        凭空造数据的读取正是登记表开始说谎的起点。
        """
        if not session_code:
            return None
        return self._sessions.get(session_code)

    def session_list(self) -> list[dict]:
        """返回所有已记录的会话，最新的在最后。

        给 TUI 的顺序说明：一个会话只有在其运行*经* :meth:`resolve` *登记*之后才会
        出现在这里。``session.start`` 立即返回，随后运行任务才调用 :meth:`resolve`，因此
        在这两点之间发出的 ``session.list`` 合理地会返回一个还不包含这个刚启动会话的
        列表。那是读取一个真实登记表的结果，不是会话丢失。
        """
        return list(self._sessions.values())

    # ----------------------------------------------------------------- auth

    def invalidate(self) -> None:
        """丢弃缓存的 manager，使下一次 ``ensure_manager`` 重建它。

        ``/login`` 通过**重建**生效，而不是重启 worker。与 ``_build_lock``
        （在 ``_run`` 内惰性创建）不同，从 RPC 处理器调用它是安全的：
        它只清除引用，随后的重建由调用方的 ``asyncio.to_thread`` 串行化。

        刷新集合是 ``login / logout / switch``，而`config.set` 一侧的刷新由
        ``set_local_declaration`` 调用本方法完成——三条路径共用一套机制，而不是三份
        近似拷贝。

        **配置缓存不住在这里**：快照（只读覆盖视图）与
        ``agent.info`` 的平台半都由 manager 自己经 :class:`PlatformConfigLoader`
        的 TTL 缓存提供（``rm.get_config_snapshot()`` / ``rm.retrieve_agent_config()``）。
        重建 manager 即丢掉那些 TTL 缓存，因此一次改变身份的 ``/login`` 不会返回
        上一个身份的资源列表——这正是这里要清缓存的目的。**本地**工作区文件列表
        有意保留不动——它住在 manager 上（``rm.files``），是对本机工作区的**实时**
        读取，登录或切换模型并不会改变存在哪些文件。

        **checkpoint saver 同样刻意不在丢弃之列**（:attr:`_checkpointer`）：它承载
        跨会话的持久对话状态，login/logout/switch 都不是丢它的理由——丢了等于丢
        历史。刷新的是"身份与配置"，不是"对话记录"。
        """
        self._resource_manager = None

    def set_access_token(self, token: str) -> None:
        """保存会话级 access token 并排队一次 manager 重建。

        同步方法；调用方用 ``asyncio.to_thread`` 包住它，因为随后的重建是阻塞的
        （SDK import + HTTP 客户端构造）。

        token 在此有意**不**做校验：``/login`` 的职责是**写入**凭据，而不是证明它
        有效。无效 token 会作为平台自身的错误在下一轮暴露出来，这就是可见错误规则。
        预先校验探测既会多一次平台调用，又会引入新的失败模式，却没有任何用户可见的
        收益。

        从不记录 token 取值：只记录其存在与否。

        **落盘**：先写 ``bkai_tui.json`` 的 ``auth.access_token``
        （``TuiConfig.write_access_token``，唯一放行 token 落盘的位置），成功后才
        更新内存——写失败（用户发起的操作）响亮抛出，且内存与磁盘不会分叉。
        """
        self._tui_config.write_access_token(token)
        self._access_token = token or ""
        self.invalidate()
        logger.info(f"access token {'set' if self._access_token else 'cleared'}; manager will rebuild")

    def clear_access_token(self) -> None:
        """丢弃 token，回到纯应用凭据状态。

        不退出进程，也不拆解会话：对话活在平台上，而 token 从来不是会话的身份——
        它只是在存在期间提升了客户端的权限。

        落盘侧同步清空（写空串）：否则重启后 :meth:`__init__` 的读回会让
        "已登出"的 token 复活。
        """
        self._tui_config.write_access_token("")
        self._access_token = ""
        self.invalidate()
        logger.info("access token cleared; manager will rebuild")

    def has_access_token(self) -> bool:
        """当前是否持有会话 token。供登录状态行使用。"""
        return bool(self._access_token)

    def set_local_declaration(self, kind: str, value: str) -> None:
        """写入**一条**声明式覆盖，然后刷新（按 kind 分派到两份配置）。

        **归属划分**（两份配置、两条规则）：

        * ``kind == "agents"``（选哪个主 Agent）→ **进程级 TUI 配置**
          （``bkai_tui.json`` 的 ``agent_code``），经 :attr:`_tui_config` 写。切换它会
          改变入口 agent_code → 重建。
        * ``kind`` ∈ ``{"models", "mcps", "knowledge"}``（对当前主 Agent 进行配置）→
          **per-Agent 配置**，经 manager 的 :meth:`LocalResourceManager.write_declaration`
          写 ``agents/${当前主 agent}.json``（``TuiConfig.agent_code`` 所指）。写
          `/models` 时写的就是**当前主 agent** 那个文件——这是两份配置之间**唯一**的
          耦合点，也是"谁是主 agent"（TUI 级）与"该 agent 怎么配"（agent 级）交界处。

        标量是**替换**（模型和 agent code 各只有一个）；集合是**追加**
        （``config.set`` 的语义）。追加而非替换，使覆盖可以在单个 RPC 内完成：UI 永远
        不必对整份列表做读-改-写。

        这次写入正是把“只在 login/logout 刷新”变成“login / logout / switch”的
        原因：切换会改变声明，因此缓存的平台+本地快照已过期，**必须**重建。

        同步且阻塞（写文件）；调用方用 ``asyncio.to_thread`` 包住它。文件不可读/
        不可写会**抛异常**：这是用户发起的操作，失败必须暴露，而不能像读取路径
        那样合法地静默降级。写 per-agent 配置但当前无主 agent_code 时也会 raise
        （``LocalDeclarations.write`` 的响亮失败）。

        ``kind == "models"`` 会同时更新 :attr:`current_model`：落盘的声明负责跨进程
        持久，而本字段让切换在**当前进程内立即**生效——否则要等平台 TTL 缓存过期才能
        看到新模型。两者写的是同一个事实，不是两个事实来源。
        """
        if kind == "agents":
            self._tui_config.write_agent_code(value)
        else:
            # per-agent 配置的写入口归**管理器**（它是 per-agent 配置的所有者）；
            # ``code`` 由本类给出（当前主 agent），管理器不猜"当前是谁"。
            # 用**读路径同一个**解析器（``_resolve_agent_code``），而不是另写一条链：
            # 两者回答的是同一个问题（"我们在配置哪个 agent"）。
            rm = self.ensure_manager()
            code = self._resolve_agent_code(rm)
            rm.write_declaration(kind, value, code)

            if kind == "models":
                self.current_model = value or None

        self.invalidate()
        # 只记 ``kind``——value 是用户输入，遵循与 token 相同的日志纪律。
        logger.info(f"local declaration written: kind={kind}")

    def _read_tui_config(self) -> TuiConfig:
        """读取**当前**进程级 TUI 配置（每次读盘）。

        不缓存：``bkai_tui.json`` 是进程级配置，用户可能在外部编辑，且 ``/agents`` 写入
        后必须立即可见。读盘成本极低（单文件 JSON），且 ``TuiConfig.read`` 永不抛。
        """
        return TuiConfig.read()

    # ------------------------------------------------------------------ build

    async def ensure_checkpointer(self) -> Any:
        """返回进程级 checkpoint saver，只惰性创建一次。

        :class:`aidev_agent.headless.checkpointer.TuiSqliteSaver`——一个内核为**同步**的 SQLite
        saver（``threading.Lock``、``sqlite3.connect(check_same_thread=False)``，无
        ``asyncio`` 状态），外层是一层异步皮，四个方法通过 ``asyncio.to_thread``
        委托给同步内核。它与两个打包 saver 的差异原因见该模块的 docstring。

        它有意做到**与事件循环无关**，因此——不同于 ``AsyncSqliteSaver``——不必在将要
        await 它的循环上构建。它可以从 worker 主循环、worker 线程自己的循环，或 SDK
        每次运行的一次性循环（``async_to_sync_generator``，
        ``aidev_agent/utils/async_utils.py``）中 await；这三种都由
        ``tests/worker/test_checkpointer.py`` 覆盖。**不要**把它"简化"成任何一种打包
        saver——两者在本场景下都会失败。

        数据库路径（:data:`aidev_agent.headless.utils.BKAI_HOME_DIRNAME`，即 ``~/.bkai/``）
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
        （:meth:`AgentRunner._run` 的构建段）在 runner 的 ``_build_lock``
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
            saver = stack.enter_context(TuiSqliteSaver.from_conn_string(str(path)))
            saver.setup()                   # CREATE TABLE + PRAGMA journal_mode=WAL

            self._exit_stack = stack
            self._checkpointer = saver

            logger.info(f"checkpoint saver ready: {path}")

        return self._checkpointer

    def close_exit_stack(self) -> None:
        """拆除进程级 ExitStack（当前即关闭 checkpoint saver）；幂等，空栈是 NO-OP。

        按记录的逆序执行 ``__exit__``——对 checkpoint 而言就是
        ``from_conn_string`` 的 ``conn.close()``。**必须在所有运行落定之后调用**
        ——:meth:`Dispatcher.shutdown <aidev_agent.headless.dispatch.Dispatcher.shutdown>` 正是
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

    def ensure_manager(self) -> Any:
        """解析凭据与平台 resource manager，仅一次。

        同步方法，因此由调用方放在 ``asyncio.to_thread`` 中执行：import SDK 和构造
        HTTP 客户端是阻塞工作，不能为其他运行阻塞事件循环。

        这是本 worker **唯一**构建 manager 的地方——``agent_factory.build_agent`` 不
        再自建（由 :meth:`AgentRunner._build_session_agent` 把这里的实例传给它），因此
        token 与运行时取值都与本进程级状态同源。

        实例状态的推送
        --------------
        凭据是构造参数（它不会变）；token 则是**推送**的——manager 每次
        重建都从这里重新获得它，否则用户 ``/login`` 后必须重启 worker 才能生效。
        工作区 ``mcp.json`` **不推送**：由 loader 链 lazy 读取（见方法体末尾的说明）。
        """
        if self._resource_manager is None:
            # 环境准备（``aidev_agent.headless.config.bootstrap_environment``）**不**在此重跑：它由
            # ``__main__`` 在启动时、首个 SDK import 之前完成一次。直接驱动
            # ``SessionState`` 的调用方（测试、脚本）需自行先调它——那是一个 config 层
            # 的公开函数，而非本类的职责。
            creds = get_credentials(self._env)
            self._creds = creds
            # 用户名与 token 同源：都读 ``bkai_tui.json`` 的 ``auth`` 段（平台网关
            # 拒绝匿名调用，工具构建按身份限定作用域）。每次构建现读——配置按需
            # 读盘，不缓存（用户可能在外部编辑文件）。
            username = self._read_tui_config().username
            # ``runtime=self``：把本进程级状态注入 manager，使它的 loader 链能读到
            # ``current_model`` 等运行时取值。必须**始终**注入——否则 manager 上那份
            # 运行时信息是空的，经它走的每一次配置拉取都看不到本轮模型。
            rm = LocalResourceManager.build(creds, username, runtime=self)
            # token 是对应用凭据的**追加**。工厂用空默认值构建（其签名不变）；
            # 会话 token 在此推送到**实例**上，使重建自然带上 ``/login`` 最后写入的
            # 值。从不记录日志、从不持久化。
            #
            # 无条件执行，这正是重点：:class:`ManagerSurface` 就是契约，因此无法
            # 接受 token 的 manager 是一个坏替身，应当响亮失败而非被静默跳过。
            if self._access_token:
                rm.set_access_token(self._access_token)

            # 工作区 ``mcp.json`` **不在此推送**：:class:`resource_manager.McpLoader`
            # 在配置链上经 ``rm.mcp.load()`` lazy 读取（mtime 门控），任何 manager
            # ——包括 ``build_agent`` 另建的——都会自己读到文件，编辑后无需重启。

            self._resource_manager = rm

        return self._resource_manager

    # ------------------------------------------------------- read-only queries

    @property
    def credentials(self) -> dict[str, str]:
        """凭据字典，首次构建 manager 之前为 ``{}``。

        平台 app code 就在其中（``BKPAAS_APP_CODE``），:func:`build_agent` 需要它来
        指定正在组装的 agent——因此该值必须在 manager 被缓存之后仍然存活，而不只是
        在构建期间。
        """
        return self._creds or {}

    def _resolve_agent_code(self, rm: Any) -> str:
        """解析**入口** ``agent_code``（env → TuiConfig → 平台默认）。

        本方法是**唯一**的解析点：两条读平台配置的路径都经它——
        生成 ``agent.info`` 与**构建 agent**（:meth:`AgentRunner._build_session_agent`
        先算好再传给 :func:`agent_factory.build_agent`，后者只接收、不自行解析）。

        入口 ``agent_code`` 的来源是 :class:`aidev_agent.headless.config.TuiConfig`（进程级
        ``bkai_tui.json`` 的 ``agent_code`` 字段）。这是**语义正确**的：入口 agent 是
        进程级决策（"启动时跑哪个 agent"），本就该属 TUI 配置。

        **关键区分（不同层，不要合并）**：入口 agent_code **只决定"跑哪个 agent"**；
        一旦确定 ``agent_code``，per-agent 配置由 ``agents/${agent_code}.json`` 提供
        （由 manager 的 loader 现场读）。二者是**不同层**。

        ``rm`` 参数保留（签名不变），但仅用于最后的平台兜底（``rm.get_agent_code()``）。
        """
        env_value = os.environ.get(LocalDeclarations.AGENT_CODE_ENV, "").strip()

        if env_value:
            return env_value

        tui_agent_code = self._read_tui_config().agent_code

        if tui_agent_code:
            return tui_agent_code

        return (self._creds or {}).get("BKPAAS_APP_CODE", "") or rm.get_agent_code()

    def describe_agent_config(self) -> dict[str, Any]:
        """``agent.info`` 的 payload：平台配置 + 本地覆盖 + 本地文件行。

        只做委派。载荷的三部分怎么拼、平台失败时如何降级，全部由
        :meth:`LocalResourceManager.describe_agent_config` 承担——与
        :meth:`describe_config_snapshot` 委派给 ``rm.get_config_snapshot()`` 同构。
        投影与 ``resources`` 的键形状是 resource_manager 的职责，本类不伸手进
        ``rm.workspace_files``。

        同步且阻塞（见 ``ensure_manager``）：调用方用 ``asyncio.to_thread`` 包住它，
        否则事件循环会为其他每次运行卡住。
        """
        rm = self.ensure_manager()

        return rm.describe_agent_config(self._resolve_agent_code(rm))

    def list_llms(self) -> list[dict[str, Any]]:
        """通过缓存的 manager 枚举平台可用模型。

        返回原始的 ``ILlmItem`` dict（``llm_code`` / ``llm_name`` / ``property``）
        ——归一化是 Node 侧的职责
        （``src/bootstrap/modelSelection.ts::normaliseModels``），那里已经拥有这套
        归约逻辑并有单元测试覆盖。

        枚举的客户端方法细节
        --------------------
        ``ILlmListQuery`` 传空：TUI 自己对完整列表做子串过滤，而平台的 ``fuzzy``
        匹配既不等于子串匹配，也不保序。

        SDK 客户端方法是 ``list_agents_v1_llms``
        （``api/bk_aidev.py:200``，``GET /openapi/aidev/resource/v1/agents/llms/``），
        **不是** ``retrieve_llms``。它返回标准信封
        （``{code,data,message,request_id,result,trace_id}``）；模型列表位于 ``data``
        之下，因此这里需要拆封。出于防御，也兼容直接返回裸列表的情况，以防未来 SDK
        版本直接返回 payload。

        尽力而为：平台失败（含 401/403/40103）会**传播**，而不是退化为空列表——权限类
        场景需要**可见**的错误，在这里吞掉会在 Node 看到之前就抹掉这个区别。
        """
        rm = self.ensure_manager()
        result = rm.get_client().api.list_agents_v1_llms()

        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            data = result.get("data")

            return data if isinstance(data, list) else []

        return []

    def describe_config_snapshot(self) -> dict[str, Any]:
        """只读配置覆盖视图的载荷，**每次都向 manager 现取**。

        本类**不**缓存快照：快照由 manager 自己的
        :meth:`LocalResourceManager.get_config_snapshot` 提供，而它内部经
        :class:`PlatformConfigLoader` 的 TTL 缓存——因此"平台已绑定配置只读一次"的
        效果仍成立（TTL 窗口内去重），只是缓存归 manager（平台配置的所有者）。

        刷新集合是 ``login / logout / switch``：``set_local_declaration`` 与
        ``set_access_token`` 都走 :meth:`invalidate`，而重建 manager 会丢掉它上面的
        TTL 缓存，因此切换后紧接着的 ``/models`` 不会看到切换前的值。

        与本类 :meth:`describe_agent_config` 的差异在**降级语义**，那才是两者
        刻意分开的原因：``agent.info`` 的平台失败必须**传播**（可见错误），而覆盖
        视图允许**降级**（仍带本地一半，平台抖动时不会整块空白）。该差异由 manager
        侧两个方法各自承担，不需要本类再加一层缓存来表达。

        快照**只有一个来源**——``rm.get_config_snapshot()``：多个来源意味着"同一份
        载荷有两种拼法"，替身与真 manager 的行为漂移无从发现。

        阻塞（首次或 TTL 过期时含 SDK import + HTTP 拉取）：调用方用
        ``asyncio.to_thread`` 包住它。
        """
        return self.ensure_manager().get_config_snapshot()
