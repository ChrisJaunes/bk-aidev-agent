"""Worker 入口：``python -u -m aidev_agent.headless --protocol tui``。

操作顺序不可随意调换：

1. **先装 stdout guard。** SDK 导入时可能打印（发生在后续首次运行，但仍在同一进程内），
   因此 guard 必须在本模块之后的一切更重的加载之前就位。
2. **信号。** ``SIGPIPE → SIG_IGN``（理由见下）；终端信号记日志；``SIGINT`` 忽略，
   因为 Ctrl+C 归父进程所有。
3. **宣告就绪**，随后增量读取帧直至 EOF。

读循环是不能天真实现的部分：一次 ``read()`` **不等于**一帧。数据块会在任意位置切分，
甚至切在 JSON 中间，因此必须维护跨读取的半行缓冲。

退出约定：stdin EOF 时取消所有未结束的运行任务，进程以 0 退出。绝不留下孤儿进程。

此处不校验环境，也不校验 SDK 来源。解释器解析到哪个 ``aidev_agent`` 就用哪个；
若其缺失或不完整，首次运行的导入会按名字报出来。
"""


from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import signal
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker  # noqa: F401 - 供守护执行器使用
from logging import getLogger
from typing import TYPE_CHECKING, Any, Iterator

from .config import TuiConfig, bootstrap_environment
from .frame_protocol import FrameWriter, error_frame, ready_frame
from .utils import configure_logging

if TYPE_CHECKING:
    from .dispatch import Dispatcher


# ============================================================================
# §1  stdout guard
#     在任何可能打印的导入之前安装：SDK 会在导入时打印，而 stdout 上的杂散行会破坏
#     NDJSON 分帧。
# ============================================================================

# 让 stdout 上只出现 NDJSON 协议帧。
#
# worker 会导入庞大的第三方 SDK（``aidev_agent`` → langchain → langgraph → ``ag_ui``）。
# 这些库中任何一个都可能 ``print()`` 出一段横幅、一条弃用提示或一行调试信息。在普通
# 终端上那是无害噪音；此处却是数据完整性故障——父进程把 stdout 的**每一行**都当 JSON
# 解析。一行杂散 ``print()`` 会产出非 JSON 行，破坏该帧的分帧；而由于帧是靠 ``id``/
# ``runId`` 关联而非行序，丢失或畸形的行不会以错误形式浮现，只会表现为「TUI 卡住等待
# 一个被写到错误流上的回复」。
#
# 因此 guard 不是防御性整洁，而是让「stdout 只承载协议」这一不变式真正成立的机制。
#
# 两层防御：
#   1. :func:`install_stdout_guard` 把 ``sys.stdout`` 换成转发到 ``sys.stderr`` 的代理，
#      捕获**导入时**执行的模块级 ``print()``——多数杂散输出都发生在那里。
#   2. :func:`captured_stdout` 是 ``contextlib.redirect_stdout(sys.stderr)`` 上下文管理器，
#      用于包裹 SDK **执行**——捕获运行期间产生的输出。
#
# 两者都不动 fd 1 本身，因此 :func:`protocol_out` 仍能在真正的通道上写帧。

#: 真正的 stdout 句柄，在 :func:`install_stdout_guard` 换掉 ``sys.stdout`` 之前捕获。
#: guard 安装前为 ``None``。
_real_stdout: Any = None

#: fd 1 的原始文本句柄，打开一次后复用。这是
#: :class:`~aidev_agent.headless.frame_protocol.FrameWriter` 写协议帧的目标。
_protocol_stream: Any = None

#: 哨兵，使 ``install_stdout_guard`` 幂等。
_installed = False


class _StderrProxy:
    """``sys.stdout`` 的替身，静默转发到 stderr。

    库可能探测的每个方法都做委托而非抛异常。向本对象 ``print()`` 本身绝不能抛异常，
    否则我们就用「导入崩溃」换掉了「流被污染」。
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
        # 返回 False：TUI 场景下 stdout 是管道，返回 True 会诱使库输出颜色转义 / 进度条。
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
        # 未显式建模的一切都委托给 stderr，使属性探测（``stdout.name``、
        # ``stdout.newlines`` 等）表现正常。
        return getattr(self._stderr, name)


def install_stdout_guard(protocol_stream: Any = None) -> None:
    """把 ``sys.stdout`` 的写入改道到 stderr，并把 fd 1 留给协议使用。

    必须在导入 ``aidev_agent`` 或任何其他重 SDK **之前**调用，因为本 guard 防范的
    杂散输出最常发生在导入时。

    参数：
        protocol_stream：协议帧的可选显式文本流。省略时，围绕 fd 1 打开一个新的文本包装。
    """
    global _real_stdout, _protocol_stream, _installed

    if _installed:
        return

    _real_stdout = sys.stdout

    if protocol_stream is not None:
        _protocol_stream = protocol_stream
    else:
        # os.fdopen 不复制任何东西——它直接包装 fd 1。行缓冲会破坏 writer 自身的队列
        # 语义，因此保持为普通缓冲文本流，由 writer 显式 flush。
        _protocol_stream = os.fdopen(
            os.dup(1), "w", encoding="utf-8", errors="replace", buffering=1
        )

    sys.stdout = _StderrProxy(sys.stderr)
    _installed = True


def is_stdout_guard_installed() -> bool:
    """返回 guard 是否已安装（测试/诊断用）。"""
    return _installed


def protocol_out() -> Any:
    """返回协议帧必须写入的流（fd 1）。

    若 guard 从未安装，则回退到保存的原始 stdout，使测试无需启动整个入口即可使用 writer。
    """
    if _protocol_stream is not None:
        return _protocol_stream
    if _real_stdout is not None:
        return _real_stdout
    return sys.stdout


@contextlib.contextmanager
def captured_stdout() -> Iterator[None]:
    """第二层：在被包裹的代码块内把 ``print()`` 改道到 stderr。

    用在 SDK 执行外围，使运行**期间**产生的输出也留在 stdout 之外——即便某个库在
    :func:`install_stdout_guard` 替换 ``sys.stdout`` 之前就保存了对原对象的引用。
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


# ============================================================================
# §2  sys.path 加固
#     启动第 2 步。只关心一件事：把任意的 cwd 挡在导入路径之外。此处不检查也不校验 SDK。
# ============================================================================


def _harden_sys_path() -> None:
    """从 ``sys.path`` 中剔除 CWD 条目。

    ``""`` 与 ``"."`` 在导入时都解析为当前工作目录，因此本地一个杂散的 ``utils/``
    或同名目录可能遮蔽已安装的包。worker 是以任意 ``cwd`` 启动的——TUI 传的是
    `agent_tui/` 模块根——所以这一点在此处的影响不同于普通脚本。

    ``sys.path`` 没有需要**添加**的东西。SDK 由解释器已有的东西解析：``.pth`` 文件、
    可编辑安装或 ``PYTHONPATH``。

    本函数不导入 ``aidev_agent``。SDK 在首次运行时才被拉入，若其缺失或不完整，
    ``ImportError`` 会指名无法解析的模块。
    """
    sys.path = [p for p in sys.path if p not in {"", "."}]


# ============================================================================
# §3  Agent 追踪
#     启动第 3 步：必须在环境已发布**之后**、首次 SDK 导入**之前**运行，
#     因为它会 monkey-patch SDK 的模块属性。
# ============================================================================

# worker 进程的 agent 追踪埋点。
#
# SDK 已自带所需的一切——``BkAidevAgentInstrumentor``、``BkAgentOTelService`` 与
# ``OTelConfig``，都在 ``aidev_agent.packages.opentelemetry`` 之下——因此本模块只是
# 接线，不是可观测性逻辑。不要在此重新实现 span 构建。
#
# 端点的收集方式（镜像 ``aidev_bkplugin`` 的 ``apps.py``）
# --------------------------------------------------------
# 参考实现是 ``aidev_bkplugin/apps.py`` 中的 ``init_bk_aidev_agent_otel``，由 Django 的
# ``ready()`` 运行。它从若干独立来源收集端点，并把并集传给**一个** ``OTelConfig``。
# 本模块沿用同一形态，只省略一个来源：
#
#   ``BKAI_AGENT_OTEL_ENDPOINTS``  JSON 多地址来源   采用
#   ``agent_info.otel_info``       平台会话信息        **不用**——见下
#   ``OTEL_GRPC_URL`` + token      环境变量单地址来源 采用
#
# **刻意跳过 ``agent_info``**：``apps.py`` 能调它，是因为 Django 的 ``ready()`` 运行在
# 已完整初始化、且持有活跃平台客户端的应用里。本模块运行在首次 ``aidev_agent`` 导入
# **之前**（见约束 1），没有可用来取 ``agent_info`` 的客户端；在此取它还意味着在
# worker 启动路径上做一次 HTTP 往返，而那条路径正是我们要测量的关键路径。另外两个
# 来源在本部署下覆盖了同样的范围：``OTEL_GRPC_URL`` 正是 ``.env.tui`` 所配置的。
#
# 决定其运行位置的两条 SDK 约束
# -----------------------------
#
# 1. ``_instrument`` 会 monkey-patch，因此必须在 SDK 被导入前运行。
#    ``BkAidevAgentInstrumentor._instrument`` 用 ``wrap_function_wrapper`` 包裹
#    ``ChatCompletionAgent._get_agent`` 与 ``AgentKnowledgeNode.__call__``。那是模块属性
#    补丁，只有在目标模块「已导入、但尚无任何东西捕获原始引用」时打上才生效。
#    从 ``__main__`` 在 ``bootstrap_environment`` 之后立即调用。
#
# 2. ``BaseInstrumentor`` 是单例，本身已幂等。
#    ``BaseInstrumentor.instrument()`` 会检查 ``_is_instrumented_by_opentelemetry``，
#    第二次调用时记录 ``Attempting to instrument while already instrumented`` 并返回
#    ``None``。因此本模块不保留自己的守卫标志——单例就是守卫，私有标志会成为「埋点是否
#    已运行」的第二个事实来源。
#
# 失败即降级，不阻断
# ------------------
# 埋点是诊断性的。缺包、端点字符串畸形或 collector 没在跑，都**不得**让 TUI 无法工作
# ——可观测性功能绝不能变成可用性依赖。此处每次失败都是一行 stderr 加 ``False`` 返回。
#
# 注意：端点不可达甚至不会抛异常——OTLP exporter 会在后台重试，于是轮次照常运行、trace
# 直接丢失。因此「TUI 还能用」**不是** trace 已送达的证据。

#: SDK 读取的多地址 JSON 来源所用的环境变量。
ENDPOINTS_ENV = "BKAI_AGENT_OTEL_ENDPOINTS"

#: SDK 读取的单地址来源（bk-report）所用的环境变量。在 ``.env.tui`` 中设置；
#: 本模块只报告它们是否存在。
GRPC_URL_ENV = "OTEL_GRPC_URL"
DATA_TOKEN_ENV = "OTEL_BK_DATA_TOKEN"

#: 是否尝试埋点。默认开启：整件事的意义就是看清首个 token 去了哪里，而默认关闭的诊断
#: 永远不会被打开。用假值退出。
ENABLED_ENV = "AGENT_TUI_OTEL"

_FALSY = ("0", "false", "no", "off")


def install_otel(env: dict[str, str] | None = None) -> bool:
    """安装 SDK 的 agent 埋点。返回其是否生效。

    本函数自包含：开关判定、端点收集（含去重）与安装都在此一处完成，模块只保留这一个
    otel 入口。

    只调用一次，从 ``__main__``，在环境准备好之后、任何 ``aidev_agent`` 导入之前
    ——见上文约束 1。

    ``env`` 是解析后的 ``.env.tui``。它仅用于报告看到了哪些变量；进程环境才是 SDK 的
    事实来源，而运行到此处时 ``bootstrap_environment`` 已把 ``env`` 发布进去。
    """
    # --- 1. 开关：默认开启，假值退出 ---
    if os.environ.get(ENABLED_ENV, "").strip().lower() in _FALSY:
        logger.info(f"[otel] disabled via {ENABLED_ENV}; no traces will be exported")

        return False

    # --- 2. 收集端点（镜像 ``aidev_bkplugin`` 的 ``apps.py``）---------------
    try:
        from aidev_agent.packages.opentelemetry import OTelConfig
        from aidev_agent.packages.opentelemetry.instrumentor import BkAidevAgentInstrumentor
        from aidev_agent.packages.opentelemetry.utils import (
            get_otel_endpoint_by_env,
            get_otel_endpoint_by_json_str,
        )

        endpoints: list[dict] = []

        # 来源 1：BKAI_AGENT_OTEL_ENDPOINTS（多地址，JSON）。
        # 畸形取值**不得**丢弃其他来源——某个可选来源里的一个拼写错误不应静默地禁用整个
        # 开发者路径。
        try:
            endpoints.extend(get_otel_endpoint_by_json_str())
        except ValueError as exc:
            logger.info(f"[otel] ignoring malformed {ENDPOINTS_ENV}: {exc}")

        # 来源 2：OTEL_GRPC_URL + OTEL_BK_DATA_TOKEN（单地址；bk-report）。
        # 由 SDK 依 BKAI_AGENT_APM_OTEL_ENABLED 门控，因此除非运维打开它，否则是空操作。
        endpoints.extend(get_otel_endpoint_by_env())

        # 来源 3（agent_info）刻意缺席——见上文。
    except Exception as exc:  # noqa: BLE001 - 诊断绝不能弄坏 worker
        logger.info(f"[otel] instrumentation failed ({type(exc).__name__}: {exc}); continuing without traces")

        return False

    # --- 3. 去重：每个有效目的地只保留第一份配置 ---------------------------
    # 比较前先把枚举 ``exporter_type`` 归一化为其取值，因为各 builder 返回枚举，而 JSON
    # 提供的那个本就是字符串。URL 去掉尾斜杠，使 "http://x:4317/" 与 "http://x:4317"
    # 视为同一端点。
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []

    for endpoint in endpoints:
        exporter_type = endpoint.get("exporter_type")
        exporter_type = getattr(exporter_type, "value", exporter_type)
        identity = (
            str(endpoint.get("url", "")).rstrip("/"),
            str(exporter_type),
            str(endpoint.get("token", "")),
        )

        if identity in seen:
            continue

        seen.add(identity)
        deduped.append(endpoint)

    endpoints = deduped

    # --- 4. 已启用却无处可发 ---------------------------------------------
    # 点明**哪些**变量缺失，而不是静默地什么都不发——这是最耗时的失败，因为一切看起来
    # 都像成功了。
    if not endpoints:
        seen_env = env if env is not None else os.environ
        missing = [key for key in (ENDPOINTS_ENV, GRPC_URL_ENV, DATA_TOKEN_ENV) if not seen_env.get(key)]
        logger.info(f"[otel] no OTel endpoint resolved; continuing without traces. Unset: {missing}")

        return False

    # --- 5. 安装 ----------------------------------------------------------
    try:
        config = OTelConfig(otel_endpoints=endpoints)
        BkAidevAgentInstrumentor(config).instrument()
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[otel] instrumentation failed ({type(exc).__name__}: {exc}); continuing without traces")

        return False

    # 只记 URL——绝不记 token，也绝不记 config 的 repr（每次启动都很吵）。
    logger.info(f"[otel] instrumentation installed; exporting to {[ep.get('url') for ep in endpoints]}")

    return True


# ============================================================================
# §4  入口
#     操作顺序：guard -> 加固 sys.path -> 信号 -> 就绪 -> 读循环。
# ============================================================================
#: 读取块大小。大到足以摊薄系统调用，小到半行会被缓冲而不是以数兆字节的块留存。
_CHUNK_SIZE = 65536

#: 显式命名，而非 ``getLogger(__name__)``。本模块以 ``python -m worker`` 运行时
#: ``__name__`` 是 ``"__main__"``——那是顶层名，**不在** ``worker`` 命名空间之下，因此
#: 不会继承 :func:`aidev_agent.headless.utils.configure_logging` 装在 ``worker`` 上的 handler，日志会
#: 静默丢失。固定成 ``aidev_agent.headless.runtime`` 使其与本包其余模块同源（标签渲染为 ``[__main__]``）。
logger = getLogger("aidev_agent.headless.runtime")


def _log_exit(reason: str) -> None:
    """记录 worker 为何退出。

    没有任何理由的「worker exited」是最难诊断的失败——这是每条退出路径的取证线索。
    """
    logger.info(reason)


#: 由信号处理器设置，使读循环能尽快离开。声明在模块作用域，因为处理器运行在任何协程
#: 帧之外。
_shutdown_requested: asyncio.Event | None = None

#: 运行中的事件循环，在 ``main`` 中捕获，使处理器能向它交接。
_loop: asyncio.AbstractEventLoop | None = None


def _log_signal(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
    """请求读循环停止，然后让它自然展开。

    为何此处**不再**抛 ``SystemExit``
    ---------------------------------
    读循环停在 ``await asyncio.to_thread(os.read, 0, ...)`` 上；处理器里抛出的
    ``SystemExit`` 会递交给主线程，被 ``asyncio.run`` 转成**任务**上的
    ``CancelledError``，``finally`` 块随之运行——但执行器线程仍阻塞在 ``os.read`` 上，
    那是条再也不会有人写的管道，于是 ``asyncio.run`` 无法关闭循环，解释器永久存活。
    那正是本模块要防止的孤儿进程。

    因此处理器只**发信号**。读循环把阻塞读取与该事件赛跑，从而在同一次迭代内醒来并走
    普通的 EOF 路径：取消所有运行、flush、以 0 退出。与 stdin EOF 结果相同。
    """
    names = {}
    for attr in ("SIGPIPE", "SIGTERM", "SIGHUP", "SIGINT", "SIGBREAK"):
        value = getattr(signal, attr, None)
        if value is not None:
            names[int(value)] = attr
    logger.info(f"received signal {names.get(signum, signum)}; requesting shutdown")

    _request_shutdown_threadsafe()


def _request_shutdown_threadsafe() -> None:
    """在循环线程上设置关闭事件。

    ``asyncio.Event.set`` 不是线程安全的，而信号处理器可能在主线程运行、此时循环正处于
    回调中——因此用 ``call_soon_threadsafe`` 编组这次写入。若尚未记录循环（启动期间的
    信号），则无可打断之物，下一次读取会看到 stdin EOF。
    """
    loop = _loop
    event = _shutdown_requested

    if loop is None or event is None or loop.is_closed():
        return

    # 循环在检查与调用之间关闭时，进程已经在退出途中。
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(event.set)


def _install_signal_handlers() -> None:
    """安装处理器，每次调用都用 hasattr 守卫。

    SIGPIPE/SIGHUP 在 Windows 上不存在，因此不加守卫的 ``signal.signal`` 会让
    ``python -m`` 在那些平台上失败。

    ``SIGPIPE → SIG_IGN`` 是重要的那个。默认处置下，任何任务一写向父进程已停止读取的
    管道，进程就会被**静默**杀死——而 worker 会从许多 asyncio 任务写。忽略它把该事件
    变成可捕获的 ``BrokenPipeError``，writer 会把它报告为干净的 peer-gone，于是 worker
    可以主动退出而不是毫无痕迹地消失。
    """
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _log_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _log_signal)
    elif hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _log_signal)
    if hasattr(signal, "SIGINT"):
        # Ctrl+C 归父进程所有；它会显式发送 shutdown/cancel。
        signal.signal(signal.SIGINT, signal.SIG_IGN)


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
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
_stdin_executor: ThreadPoolExecutor | None = None


def _get_stdin_executor() -> ThreadPoolExecutor:
    """惰性创建的、用于 stdin 读取的守护执行器。"""
    global _stdin_executor

    if _stdin_executor is None:
        # 一个 worker 就够：循环内读取严格串行。
        _stdin_executor = _DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-stdin")

    return _stdin_executor


async def _read_chunk() -> bytes:
    """从 fd 0 读取一块，且不阻塞事件循环。"""
    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(_get_stdin_executor(), os.read, 0, _CHUNK_SIZE)


async def _mirror_stop_event(source: asyncio.Event, target: asyncio.Event) -> None:
    """一旦 ``source`` 被设置，就把它复制到 ``target``。

    读循环里用一个微型桥接而非直接 ``await source.wait()``，因为循环本就在监听
    ``target``（``process.shutdown`` 设置的那个同一事件），且两个来源都必须汇聚到同一条
    关闭路径。
    """
    await source.wait()
    target.set()


async def _read_chunk_or_stop(stop: asyncio.Event) -> bytes:
    """读取一块；一旦请求关闭就立即返回 ``b""``。

    阻塞的 ``os.read`` 停驻在执行器线程中时无法被信号打断，因此这次读取与 stop 事件**赛跑**。
    信号设置该事件，本函数即刻返回；调用方随后走与 stdin EOF 相同的路径。

    停驻的读取被刻意留下——它跑在专用执行器的守护线程上（见 ``_get_stdin_executor``），
    因此不会拖延解释器关闭。
    """
    reader = asyncio.ensure_future(_read_chunk())
    waiter = asyncio.ensure_future(stop.wait())

    try:
        done, _pending = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # 取消落败的一方。reader 底层的线程仍在执行器里跑；future 被取消，因此无人再 await 它。
        for task in (reader, waiter):
            if not task.done():
                task.cancel()

    if waiter in done and stop.is_set() and reader not in done:
        # 关闭赢下赛跑——报告 EOF 让调用方离开循环。
        return b""

    try:
        return reader.result()
    except asyncio.CancelledError:
        return b""
    except (OSError, ValueError):
        raise


async def main() -> int:
    global _loop, _shutdown_requested

    # 信号处理器会向本循环交接工作，因此在任何处理器可能触发之前先把两者记下。
    #
    # 顺序重要：先创建**事件**，再发布循环。SIGTERM 处理器跑在主线程，可能恰好在这两条
    # 语句之间触发。若 `_loop` 已设而 `_shutdown_requested` 仍为 None，
    # `_request_shutdown_threadsafe` 会看到 `event is None` 而**返回**——丢弃该信号——
    # 随后镜像安装处的守卫又看到事件未设，于是送达的 SIGTERM 永不传播，worker 变成孤儿
    # 直到 stdin EOF。把事件放在前面，则即使在记录循环之前到达也会找到事件并设置它；
    # 下面的 `is_set()` 检查随后走关闭路径。
    _shutdown_requested = asyncio.Event()
    _loop = asyncio.get_running_loop()
    _loop.set_default_executor(_DaemonThreadPoolExecutor(thread_name_prefix="headless-sdk"))

    _install_signal_handlers()

    # --- 0. 把诊断路由到日志文件 ------------------------------------------
    # 最优先：下面的一切都会记日志，而日志文件必须已经配置好。诊断写入
    # `~/.bkai/logs/worker.log`（用 BKAI_TUI_LOG_PATH 覆盖）与 stderr，因此 stdout 只
    # 承载协议帧，同时 TUI 保留其基于 stderr 的启动诊断。
    configure_logging()

    # --- 1. 安装 stdout guard ---------------------------------------------
    # 最优先，在任何东西可能打印之前。
    #
    # `sys.stdout` 是**协议**通道：TUI 读到的每一帧都是 fd 1 上的一行 NDJSON。没有它，
    # SDK 一行杂散的 `print()` 就会落进那个流，TUI 无法解析。guard 把 `sys.stdout` 换成
    # 转发到 stderr 的代理，把 fd 1 留给真正的帧。
    install_stdout_guard()
    bootstrap_environment(TuiConfig.read().env)

    from .network_guard import install_from_env

    install_from_env()

    from .dispatch import Dispatcher

    # --- 2. 加固 sys.path --------------------------------------------------
    _harden_sys_path()

    # Report the package running this process; there is no patch target to resolve.
    import aidev_agent

    _resolved_sdk_file = aidev_agent.__file__

    writer = FrameWriter(protocol_out())
    await writer.start()

    # --- 3. 宣告就绪 ------------------------------------------------------
    if not await writer.send(ready_frame(sdk_file=_resolved_sdk_file)):
        _log_exit("startup write failed (broken stdout pipe before first frame)")
        await writer.close()
        return 0

    # 同时出现在日志 tee 与线路上，使解析到的树无需解析握手即可确认。
    logger.info(f"SDK runtime: {_resolved_sdk_file}")

    stop = asyncio.Event()

    def _request_shutdown() -> None:
        stop.set()

    # 把信号事件桥接进本循环的 stop 事件。`_shutdown_requested` 由信号处理器经
    # `call_soon_threadsafe` 设置；在此监听它意味着 SIGTERM 走与 stdin EOF **相同**的路径：
    # 取消所有运行、关闭 writer、以 0 退出。这正是让被信号通知的 worker 不变成孤儿的机制。
    if _shutdown_requested is not None:
        if _shutdown_requested.is_set():
            stop.set()
        else:
            asyncio.ensure_future(_mirror_stop_event(_shutdown_requested, stop))

    # 读取进程级配置供 Dispatcher 使用。环境发布已在**模块顶层**完成（见顶部
    # 1b 段：必须先于 SDK import，晚了的值到不了 ``api.domains`` 的 import 期
    # 求值）。此处不记录任何取值——只记键名。
    env_values = TuiConfig.read().env
    logger.info(f"config env loaded: {len(env_values)} key(s)")

    # --- 3a. 注入 TUI 的默认通用 agent ------------------------------------
    # 经 SDK 官方扩展点（common_agent_factory.replace_defaults）把本地文件
    # runtime（限定 AGENT_TUI_CWD 项目内）叠加到每个会话的 executor。在
    # bootstrap_environment 之后调用一次；详见 agent_factory.TuiFileAgent。
    from aidev_agent.services.common_agent import common_agent_factory  # noqa: E402

    from .agent_factory import TuiFileAgent  # noqa: E402

    common_agent_factory.replace_defaults(TuiFileAgent())
    logger.info("default common agent replaced: TuiFileAgent (local file runtime)")

    # --- 3b. 安装 agent 追踪埋点 ------------------------------------------
    # 紧接环境发布之后、任何 `aidev_agent` 导入之前——§3 约束 1 要求的次序。
    # `BkAidevAgentInstrumentor` 会 monkey-patch `ChatCompletionAgent._get_agent`，因此目标
    # 模块必须尚未被导入（其原始引用也未被捕获）；这是启动路径上该条件仍成立的最后时刻。
    # 该调用从不抛异常，且每条失败路径都返回 False，因此端点坏了或缺 SDK 只会降级为
    # 「没有 trace」，而不会让 worker 停下。
    install_otel(env_values)

    dispatcher = Dispatcher(
        writer,
        shutdown_cb=_request_shutdown,
        env=env_values,
    )

    # --- 工作区文件列表在启动路径上不再做任何 I/O -------------------------
    # 工作区第一层（文件 + 目录）现在由链中的 `WorkspaceFileLoader` 在每次
    # `retrieve_agent_config`（即每次 `agent.info` 投影）时**实时**读取。那是对第一层的
    # 一次同步 `os.scandir`（约 0.02 ms，文件与目录都含），因此此处无物可爬、无物可缓存、
    # 无物可调度。

    # --- 4. 带持久半行缓冲的增量读循环 -----------------------------------
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""

    try:
        while not stop.is_set():
            try:
                # 与 `stop` 赛跑，使信号能唤醒循环，尽管底层 `os.read` 无法被打断。
                chunk = await _read_chunk_or_stop(stop)
            except (OSError, ValueError) as exc:
                logger.info(f"stdin read failed: {exc!r}")
                break

            if not chunk:
                # EOF：父进程关闭了命令管道。
                logger.info("stdin EOF")
                break

            buffer += decoder.decode(chunk)

            # 一次 read() **不是**一帧。只有完整的行（以 "\n" 结尾）可被解析；尾部余量留在
            # 缓冲中等待下一次读取。
            *complete, buffer = buffer.split("\n")

            for raw in complete:
                line = raw.strip()
                if not line:
                    continue
                await _process_line(line, dispatcher)
    except Exception as exc:  # noqa: BLE001 - 崩掉的循环仍必须走到关闭
        # 崩掉的 stdin 循环若留下仍在运行的任务，恰好会产生本阶段禁止的孤儿——因此记日志，
        # 并落到关闭流程。
        logger.info(f"stdin loop crashed: {exc!r}")
    finally:
        # --- EOF / stop：取消所有运行、关闭 writer、以 0 退出 ---------------
        await dispatcher.shutdown()
        _log_exit("stdin EOF (TUI closed the command pipe)" if not stop.is_set() else "process.shutdown requested")

    return 0


async def _process_line(line: str, dispatcher: Dispatcher) -> None:
    """解析一行协议并路由。从不抛异常。"""
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        # 绝不在畸形输入上弄崩循环。
        preview = line[:120]
        await dispatcher.writer.send(error_frame(None, "PARSE_ERROR", preview))
        return

    if not isinstance(req, dict):
        await dispatcher.writer.send(error_frame(None, "PARSE_ERROR", line[:120]))
        return

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params")

    await dispatcher.handle(
        method if isinstance(method, str) else None,
        params,
        req_id if isinstance(req_id, str) else None,
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
