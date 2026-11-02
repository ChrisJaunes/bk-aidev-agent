"""worker stdin 帧的方法路由器。

方法集合是固定的——没有动态分发，也没有 fallthrough。无法识别的方法会返回
``UNKNOWN_METHOD``，而不会执行任何东西（威胁模型：畸形帧绝不能触达任意代码）。

认证与配置两族方法：``auth.login`` / ``auth.logout`` 写入与清除会话 token；token
**只进不出**——回复帧、日志行、错误信息中都不会把它带回去（UI-SPEC §``/login``
掩码契约）。``config.info`` 提供缓存的配置快照，``config.set`` 写入一条本地声明。

并发模型
--------

stdin 循环对解析出的每个帧调用 :meth:`Dispatcher.handle`，并且在一次运行生成期间
**必须继续读取**。因此 ``session.start`` 不 await 该次运行——它在 ``self.runs`` 中登记
一个 ``asyncio.Task`` 后立即返回。如果它 await，两秒后到达的 ``session.cancel`` 会一直
躺在管道里无人读取，直到生成结束，这就让取消命令完全失去意义。

取消是本类负责的两步
--------------------

``session.cancel`` 把 runId 加入 :attr:`cancelled`——该状态的唯一权威——运行中的任务通过
传给 ``AgentRunner.run`` 的判定函数读取它。翻转它是让 SDK 的 ``cancel_checker`` 返回
True 的原因，从而产生一个*优雅的* ``RUN_FINISHED``，并让 TUI 的 ``isChatting`` 不会
卡住。

随后它安排一个后备手段：在短暂宽限期后硬取消该任务，用于运行卡在 ``await`` 里、
永远不会轮询该标志的情形。两半都是必需的；只用其中一半都会留下一条 TUI 卡死的路径。

:attr:`runs` 同样只在唯一一处清理——:meth:`_on_run_settled`，由 runner 在每条退出路径
上调用。这里有**故意不**并列 ``add_done_callback``：两套机制移除同一个键，正是一个
登记表开始自相矛盾的起点。
"""

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import Any
from uuid import uuid4

from .agent_runner import CANCEL_GRACE_SECONDS, AgentRunner, SessionState
from .frame_protocol import (
    FrameSink,
    error_frame,
    result_frame,
    started_frame,
)

# ============================================================================
# §3  帧路由
#     stdin 方法表。本节实现的并发与取消契约见模块 docstring。
# ============================================================================


#: 本模块所需的 ``FrameWriter`` 切片。见 :class:`aidev_agent.headless.frame_protocol.FrameSink`。
_Writer = FrameSink

logger = getLogger(__name__)


def new_run_id() -> str:
    """生成短的、防碰撞的运行标识符。"""
    return f"run-{uuid4().hex[:12]}"


class Dispatcher:
    """把解析后的帧路由到各处理器，并跟踪进行中的运行。

    属性：
        runs: ``runId`` → 生成该次运行的独立任务。
        state: 进程级身份 / 配置状态（``SessionState``）。**本类是它唯一的持有者**；
            每次 ``runner.run(state=self.state)`` 传的是同一个实例，runner 不存它。
        runner: ``AidevAGUIAgent`` 驱动器。在此构建一次；它昂贵的图构建在首次运行时
            才惰性发生。它只"一次一个地跑"，不持有进程级状态。
        writer: 被每个任务共享的串行化帧写入器。
    """

    def __init__(
        self,
        writer: _Writer,
        *,
        shutdown_cb: Any = None,
        state: SessionState | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.writer = writer
        self.runs: dict[str, asyncio.Task[None]] = {}
        #: ``cancel_checker`` 必须返回 ``True`` 的 runId 集合，保存在
        #: dispatcher 上使取消路径无需穿透 runner 即可被观察。
        self.cancelled: set[str] = set()
        # 整个进程一个 SessionState：传入即用，否则自建。runner 不接收它——
        # 它只在每次 run() 时被传进去。
        self.state = state if state is not None else SessionState(env=env)
        self._shutdown_cb = shutdown_cb
        self._shutting_down = False

        # 整个进程一个 runner，驱动真实 agent。进程级状态归本类持有，runner
        # 不存；测试替身经覆写 ``AgentRunner._run`` 注入（见 agent_runner 模块
        # docstring），生产与测试同一条路径。
        self.runner = AgentRunner(
            writer=writer,
            on_run_settled=self._on_run_settled,
        )

    # ---------------------------------------------------------------- 路由

    async def handle(self, method: str | None, params: dict[str, Any] | None, req_id: str | None) -> None:
        """分发一个请求。**从不抛异常**——见下。

        生产者/消费者边界（与 stdin 循环的契约）
        ----------------------------------------
        本方法是每个请求的处理入口，而调用它的 stdin 读循环以「一条坏请求绝不
        能打断整条循环」为前提（``__main__._process_line`` 同样承诺 "Never
        raises"）。因此这里必须吞掉所有异常：handler 抛出的任何东西都只应让
        **这一个请求**失败并回一帧 error，而不能穿透到读循环去终止整个会话——
        那会让此后的每一次交互都无人处理。

        ``INTERNAL_ERROR`` 与 ``BAD_REQUEST``/``UNKNOWN_METHOD`` 区分开：后者是
        调用方可以纠正的协议误用，前者是 worker 自身的处理失败——调用方看到
        error 帧就够了，具体异常只进日志（且只记类型与消息，不记 ``params``，
        因为其中可能含有用户输入）。
        """
        try:
            await self._dispatch(method, params, req_id)
        except Exception as exc:  # noqa: BLE001 - 一条请求的失败绝不上抛到读循环
            logger.info(f"request handler failed for method={method!r}: {exc!r}")
            await self.writer.send(
                error_frame(req_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
            )

    async def _dispatch(self, method: str | None, params: dict[str, Any] | None, req_id: str | None) -> None:
        """按 ``method`` 路由到具体 handler。可能抛异常，由 :meth:`handle` 兜底。"""
        params = params if isinstance(params, dict) else {}

        if method == "session.start":
            await self._handle_session_start(params, req_id)
        elif method == "session.cancel":
            await self._handle_session_cancel(params, req_id)
        elif method == "session.get":
            await self._handle_session_get(params, req_id)
        elif method == "session.list":
            await self._handle_session_list(req_id)
        elif method == "session.create":
            await self._handle_session_create(req_id)
        elif method == "agent.info":
            await self._handle_agent_info(req_id)
        elif method == "agent.llms":
            await self._handle_agent_llms(req_id)
        elif method == "config.info":
            await self._handle_config_info(req_id)
        elif method == "config.set":
            await self._handle_config_set(params, req_id)
        elif method == "auth.login":
            await self._handle_auth_login(params, req_id)
        elif method == "auth.logout":
            await self._handle_auth_logout(req_id)
        elif method == "process.shutdown":
            await self._handle_shutdown(req_id)
        else:
            await self.writer.send(
                error_frame(req_id, "UNKNOWN_METHOD", f"unknown method: {method!r}")
            )

    # ------------------------------------------------------------- 处理器

    def _on_run_settled(self, run_id: str) -> None:
        """把已结束的运行从登记表与取消集合中移除。

        作为 runner 的 ``on_run_settled`` 回调安装，使清理在每条退出路径上都会发生
        ——正常、取消或出错——而不是在每一处重复实现。
        """
        self.runs.pop(run_id, None)
        self.cancelled.discard(run_id)

    async def _handle_session_start(self, params: dict[str, Any], req_id: str | None) -> None:
        """``session.start``：启动一次运行并立即返回。

        ``req_id`` 缺失时无法把流关联到调用方，回 ``BAD_REQUEST``。登记任务后
        **不** await——stdin 循环必须保持可响应，这样 ``session.cancel`` 才能被
        接收（并发契约见模块 docstring）。
        """
        if req_id is None:
            await self.writer.send(
                error_frame(None, "BAD_REQUEST", "session.start requires an id")
            )
            return

        run_id = new_run_id()
        await self.writer.send(started_frame(req_id, run_id))

        task = asyncio.create_task(
            self._run(req_id, run_id, params),
            name=f"run-{run_id}",
        )
        self.runs[run_id] = task

    async def _run(self, req_id: str, run_id: str, params: dict[str, Any]) -> None:
        """通过真实的 agent runner 驱动一次运行。

        取消判定函数在**此处**构建，来自本 dispatcher 自己的集合，然后传入——runner
        不持有自己的取消状态。这样"哪些运行已被取消"只有一个权威，而不是两个需要
        保持同步的集合。

        ``model`` 刻意**不**转发：模型是用户在 ``/models`` 里做出的持久选择，走
        ``config.set``（kind=models）落盘并由 ``SessionState.current_model`` 在当前
        进程内立即生效，经 loader 链落到 ``AgentConfig.chat_model``。把它作为每轮
        输入会再造一条并行的覆盖路径——而两条路径不一致时无法判断谁生效。

        ``executeKwargs`` 普通轮次为 ``None``；chat-helper 借它携带嵌套
        UserQuestion 的 ``resume``，此处原样转发。
        """
        await self.runner.run(
            req_id=req_id,
            run_id=run_id,
            session_code=params.get("sessionCode"),
            input_text=params.get("input", ""),
            state=self.state,
            is_cancelled=lambda: run_id in self.cancelled,
            execute_kwargs=params.get("executeKwargs"),
        )

    async def _handle_session_cancel(self, params: dict[str, Any], req_id: str | None) -> None:
        """``session.cancel``：两步取消——优雅标志 + 宽限期后的硬取消后备。

        ``runId`` 缺失或类型不对回 ``BAD_REQUEST``。未知或已结束的运行**上报**
        ``cancelled: False`` 而不是静默成功，这样父方不会误以为自己取消了某个
        仍在运行的东西。两步各自的理由见 :meth:`_cancel_backstop` 与模块
        docstring。
        """
        run_id = params.get("runId")
        if not isinstance(run_id, str) or not run_id:
            await self.writer.send(
                error_frame(req_id, "BAD_REQUEST", "session.cancel requires params.runId")
            )
            return

        task = self.runs.get(run_id)

        if task is None or task.done():
            await self.writer.send(
                result_frame(req_id, {"runId": run_id, "cancelled": False, "reason": "no active run"})
            )
            return

        # 第 1 步——优雅路径。翻转该标志让 SDK 的 ``cancel_checker`` 返回 True，
        # agent 发出一个正常的 ``RUN_FINISHED``。正是那个终止事件让 TUI 的
        # ``isChatting`` 能落定；单纯取消任务会中途中断、没有终止事件，UI 永远卡住。
        self.cancelled.add(run_id)

        # 第 2 步——后备手段。卡在 ``await`` 里的运行永远不会到达下一次
        # ``cancel_checker`` 轮询。**不** await 它：``handle`` 必须返回，读循环
        # 才能继续服务其他帧。
        asyncio.create_task(
            self._cancel_backstop(run_id, task),
            name=f"cancel-backstop-{run_id}",
        )

        await self.writer.send(result_frame(req_id, {"runId": run_id, "cancelled": True}))

    async def _cancel_backstop(self, run_id: str, task: asyncio.Task[None]) -> None:
        """若优雅路径未能及时让 ``task`` 结束，则硬取消它。

        宽限期的存在是为了让 SDK 能在任务被拆除之前发出它的 ``RUN_FINISHED``；立即
        取消会与那个事件竞争并丢掉它，而这正是 ``isChatting`` 卡死这一失败场景所要
        防范的。
        """
        await asyncio.sleep(CANCEL_GRACE_SECONDS)
        if not task.done():
            logger.info(
                f"run {run_id} still running {CANCEL_GRACE_SECONDS}s after cancel; "
                "hard-cancelling (graceful RUN_FINISHED was not observed)"
            )
            task.cancel()

    async def _handle_session_get(self, params: dict[str, Any], req_id: str | None) -> None:
        """``session.get``：无副作用地查一条会话记录。

        这只是身份记账——不存消息。对话记录住在 SDK 的远端存储里，以此处返回的
        ``sessionCode`` 为键（它就是 LangGraph 的 thread key）。
        """
        session_code = params.get("sessionCode")
        payload: dict[str, Any] = {
            "session": self.state.get(session_code if isinstance(session_code, str) else None),
            "sessionCode": session_code,
        }

        await self.writer.send(result_frame(req_id, payload))

    async def _handle_session_list(self, req_id: str | None) -> None:
        """``session.list``：所有已登记会话，最新在最后。"""
        await self.writer.send(result_frame(req_id, {"items": self.state.session_list()}))

    async def _handle_session_create(self, req_id: str | None) -> None:
        """``session.create``：新建一条会话并返回其身份记录。"""
        await self.writer.send(result_frame(req_id, self.state.session_new()))

    async def _handle_agent_info(self, req_id: str | None) -> None:
        """``agent.info``：``/`` 与 ``@`` 菜单所需的三个列表。

        阻塞（先解析凭据，再做平台 HTTP 拉取），用 ``asyncio.to_thread`` 卸载
        ——与 ``AgentRunner._run`` 使用的纪律一致。直接在循环上
        await，会在平台往返期间冻住每一次进行中的运行。
        """
        config = await asyncio.to_thread(self.state.describe_agent_config)

        await self.writer.send(result_frame(req_id, {"agentInfo": config}))

    async def _handle_agent_llms(self, req_id: str | None) -> None:
        """``agent.llms``：``/models`` 所需的模型列表。

        非流式且幂等。阻塞（平台 HTTP 拉取），同样用 ``asyncio.to_thread`` 卸载
        ——理由同 :meth:`_handle_agent_info`。
        """
        llms = await asyncio.to_thread(self.state.list_llms)

        await self.writer.send(result_frame(req_id, {"items": llms}))

    async def _handle_config_info(self, req_id: str | None) -> None:
        """``config.info``：四个只读配置浮层（``/models`` / ``/agents`` / ``/mcps``
        / ``/knowledge``）的启动快照。

        刻意与 ``_handle_agent_info`` **分离**：``agent.info`` 执行**实时**平台拉取
        （它的数据供给 ``/`` 与 ``@`` 菜单，必须是最新的），而这里读取快照。把它们
        保留为两个方法，正是让"缓存 vs 实时"这一区分体现在代码结构里，而不是住在
        一句会被后续修改悄悄推翻的注释里。

        用 ``asyncio.to_thread`` 卸载，因为首次调用会拉取平台并读本地文件——
        全部阻塞。不记录取值。
        """
        snapshot = await asyncio.to_thread(self.state.describe_config_snapshot)

        await self.writer.send(result_frame(req_id, {"config": snapshot}))

    async def _handle_config_set(self, params: dict[str, Any], req_id: str | None) -> None:
        """``config.set``：写入一条声明式覆盖。对错误参数从不抛异常。

        唯一的写 RPC。它写入一条本地声明，从而使状态侧的快照与 manager 都失效
        （``set_local_declaration``）——因此一次切换的刷新方式与 ``/login`` /
        ``/logout`` 完全一致：刷新集合是 ``login / logout / set``。

        ``kind`` 是判别式，不是方法名的一部分：四个资源族
        （``("models", "agents", "mcps", "knowledge")``）共享同一套校验、同一条
        错误路径和同一种回复形状，四个方法名只会让白名单、能力表面与 TS 包装器
        为四个完全相同的行为各翻四倍。写入落点：``kind == "agents"`` 写**进程级**
        TUI 配置（``bkai_tui.json`` 的 ``agent_code``）；其余三个写**当前主 Agent**
        的 per-agent 配置（``agents/${TuiConfig.agent_code}.json``）。分派发生在
        ``SessionState.set_local_declaration`` 内，本处理器只需转发 kind/value。

        未知的 ``kind`` 或缺失/空白的 ``value`` 以 ``{"ok": False}`` 作答，
        而不是错误帧：调用方是本仓库自己的 UI，而这里要求**可见**失败，状态行从
        ``ok: false`` 渲染它。把 ``error`` 帧类型留给协议故障。被拒绝的写入
        **不得**触碰文件系统——kind 名里有个拼写错误，不是重写用户配置文件的
        理由。日志只记 ``kind``（不记 value）。

        用 ``asyncio.to_thread`` 卸载，因为每次写入会写本地文件并强制重建——
        阻塞。
        """
        kind = params.get("kind")
        value = params.get("value")
        kind = kind if isinstance(kind, str) else ""
        value = value if isinstance(value, str) else ""

        if kind not in ("models", "agents", "mcps", "knowledge") or not value:
            await self.writer.send(result_frame(req_id, {"ok": False}))

            return

        await asyncio.to_thread(self.state.set_local_declaration, kind, value)

        await self.writer.send(
            result_frame(req_id, {"ok": True, "kind": kind, "value": value})
        )

    async def _handle_auth_login(self, params: dict[str, Any], req_id: str | None) -> None:
        """``auth.login``：写入会话 token。

        token **只进不出**。此处没有任何地方回显它——不进入回复载荷、不进入日志行、
        不进入错误信息（UI-SPEC §``/login`` 掩码契约使它成为安全要求）。

        没有校验步骤：``/login`` 的职责是**写入**凭据。无效的 token 会在下一轮作为
        平台自身的错误暴露出来，这就是可见错误规则。在此探测既会新增一次平台调用，
        又会新增一种失败模式，却没有用户可见的收益。

        manager 重建是阻塞的那一半（SDK import + HTTP 客户端），因此 setter 在循环外
        执行；重建本身在下次 ``SessionState.ensure_manager``——也就是下一轮——惰性发生。

        空提交是 NO-OP，不是清除：把它当作清除会静默丢弃一个有效的会话 token
        ——Node 侧 ``resolveLoginKey`` 契约防范同一情形。
        """
        token = params.get("token")
        token = token if isinstance(token, str) else ""

        if not token:
            await self.writer.send(
                result_frame(req_id, {"ok": False, "hasToken": self.state.has_access_token()})
            )

            return

        await asyncio.to_thread(self.state.set_access_token, token)
        # `hasToken`（一个 bool）是唯一回传的东西——绝不是 token 本身。
        await self.writer.send(result_frame(req_id, {"ok": True, "hasToken": True}))

    async def _handle_auth_logout(self, req_id: str | None) -> None:
        """``auth.logout``：丢弃会话 token。token 纪律同 :meth:`_handle_auth_login`。"""
        await asyncio.to_thread(self.state.clear_access_token)
        await self.writer.send(result_frame(req_id, {"ok": True, "hasToken": False}))

    async def _handle_shutdown(self, req_id: str | None) -> None:
        """``process.shutdown``：先回 ``ok`` 再触发关闭回调（可以是协程）。"""
        await self.writer.send(result_frame(req_id, {"ok": True}))
        if self._shutdown_cb is not None:
            result = self._shutdown_cb()
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------- 关闭

    async def shutdown(self) -> None:
        """取消所有未结束的运行，拆除进程级 ExitStack，然后关闭 writer。

        这是父子进程退出耦合中"先取消所有任务再退出"的那一半。从 stdin EOF 路径与
        ``process.shutdown`` 两处调用。

        拆除 ExitStack 放在 **gather 之后**：必须等所有运行落定，才能动它们仍在
        使用的资源——有运行在用就关，等价于在它脚下抽走地板。
        """
        if self._shutting_down:
            return
        self._shutting_down = True

        tasks = [t for t in self.runs.values() if not t.done()]
        for task in tasks:
            task.cancel()

        if tasks:
            logger.info(f"shutdown: cancelling {len(tasks)} outstanding run task(s)")
            await asyncio.gather(*tasks, return_exceptions=True)

        self.runs.clear()
        self.state.close_exit_stack()
        await self.writer.close()
