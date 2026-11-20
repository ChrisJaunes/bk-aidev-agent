"""worker 包的通用工具：日志配置与本机 BKAI 目录常量。

为什么日志独立成一个模块
------------------------
worker 的 stdout 是**协议通道**——父进程把它的每一行都当作 JSON 解析，数据帧按
``id``/``runId`` 关联，因此一条写到 stdout 的诊断行不会表现为报错，而是表现为 *TUI 一直
卡在等待一个被写到错误流上的回复*。本包的硬性规则由此而来：**stdout 只承载协议帧。**

把诊断落到日志文件（并保留一份 stderr），从根上避免了对任何流的污染。所有需要记录的模块
都用标准 ``logging`` 的模块级 logger，不得各自 ``print``、也不得各自配置 logging——
配置只应发生在一处（:func:`configure_logging`，由启动钩子显式调用），否则 handler 会
重复、级别会互相覆盖。

**本模块导入期没有任何副作用**：不建目录、不开文件。理由不是洁癖——"导入一个模块"若能
创建文件，就无法用静态断言守住启动次序（入口模块导入本身不得有日志副作用）。
在 :func:`configure_logging` 被显式调用之前就写日志的路径，会先落到
:func:`_log_without_config` 装的仅 stderr 兜底 handler 上（见其 docstring）。

其他模块如何使用
----------------
在本模块顶部拿一个模块级 logger，之后直接用它::

    from logging import getLogger

    logger = getLogger(__name__)

    logger.info("env file loaded")

来源标签由 logger 名推导：本包所有模块都在 ``aidev_agent_cli`` 命名空间之下，:class:`_PrefixFilter`
取 ``record.name`` 的最后一段，因此 ``aidev_agent_cli.resources.local_config`` 渲染为
``[local_config]``。无需手工传入前缀。
"""

from __future__ import annotations

import logging
import os
import sys
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)

#: 本包诊断输出的 logger 命名空间。本包所有模块的 logger 名都在它之下
#: （``aidev_agent_cli.resources.local_config``、``aidev_agent_cli.adapters.acp_dsh``……），
#: 因此 :func:`configure_logging` 只需配置这一个层级，就能覆盖本包全部诊断，而不触碰 root logger。
_LOG_NAMESPACE = "aidev_agent_cli"

#: 输出格式。``%(label)s`` 由 :class:`_PrefixFilter` 从 ``record.name`` 的最后一段填充，
#: 因此 ``aidev_agent_cli.resources.local_config`` 渲染为 ``[local_config]``——来源标签直接取自模块名，
#: 无需调用方手工传入。
_LOG_FORMAT = "[%(label)s] %(message)s"

#: 本机 BKAI 状态目录名（主目录下）。日志、配置等所有本机专属路径都从它派生——
#: 改名（``.bkai`` / ``.bkai-agent``）只需改这一处，避免出现“日志在 ``.bkai``、
#: 配置在 ``.bkai-agent``”的分裂。
BKAI_HOME_DIRNAME = ".bkai"

#: 日志文件路径的环境变量覆盖项。设置后写到该路径；未设置时用
#: :data:`DEFAULT_LOG_FILE`。
LOG_FILE_OVERRIDE = "BKAI_TUI_LOG_PATH"

#: 默认日志文件：``~/.bkai/logs/worker.log``。用户主目录下的固定位置，与 checkout
#: 位置无关——worker 可能被从任意 cwd 拉起，日志不应散落在各处。首次写日志时目录会被
#: 自动创建（见 :func:`configure_logging`）。目录名由 :data:`BKAI_HOME_DIRNAME` 派生。
DEFAULT_LOG_FILE = str(Path.home() / BKAI_HOME_DIRNAME / "logs" / "worker.log")


class _PrefixFilter(logging.Filter):
    """把 ``record.name`` 的最后一段写入 ``record.label``，供 :data:`_LOG_FORMAT` 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.label = record.name.rsplit(".", 1)[-1]

        return True


class _CurrentStderr:
    """一个每次写入时都重新解析 ``sys.stderr`` 的文本流。

    ``logging.StreamHandler`` 在**构造时**就绑定了它拿到的流对象。这会在两种情况下出错：
    pytest 的 ``capsys`` 每个用例都会替换 ``sys.stderr``，而本 handler 是在进程启动时构造
    一次、此后一直持有旧引用——于是被捕获的输出消失，测试看不见诊断信息。直接传
    ``sys.stderr`` 会让“诊断可见”这件事依赖于启动时机。

    这个代理把解析推迟到写入时刻，因此 handler 永远写到*当前*的 stderr；同时保留
    ``flush`` 语义，是 ``print(..., file=sys.stderr, flush=True)`` 的直接替代品。
    """

    def write(self, data: str) -> int:
        return sys.stderr.write(data)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return sys.stderr.isatty()

    def __getattr__(self, name: str) -> Any:
        # 未显式建模的属性（``encoding``、``errors``、``fileno``……）委托给当前 stderr，
        # 使库的探测行为与直接持有 stderr 句柄一致。
        return getattr(sys.stderr, name)


#: 兜底 handler 的名字标记：只有 stderr、没有文件（:func:`_log_without_config` 装，
#: :func:`configure_logging` 摘）。
_FALLBACK_HANDLER = "aidev_agent_cli.fallback"

#: 是否已发出过「尚未配置日志」的提示（:func:`_log_without_config` 用，见其 docstring）。
_hinted = False


def _handlers_are_configured() -> bool:
    """本包的 logger 命名空间是否已经有**真实** handler（文件或 stderr 的那两条）。"""
    return any(handler.get_name() != _FALLBACK_HANDLER for handler in logging.getLogger(_LOG_NAMESPACE).handlers)


def _log_without_config() -> logging.Logger:
    """未配置日志时的兜底入口：装一份纯 stderr handler，并只提示一次。

    本模块**不在导入期配置日志**——导入 ``aidev_agent_cli.infrastructure.logging`` 不得创建
    目录、不得打开日志文件（那是真实副作用，会让"仅仅 import 一个模块"改变进程的文件描述
    符视图，也让启动次序无法被静态断言）。配置只由两个显式调用点发起：

    * :func:`aidev_agent_cli.infrastructure.startup.install_startup_hooks`（CLI 入口）；
    * :func:`aidev_agent_cli.infrastructure.startup.install_worker_startup`（spawned worker）。

    若干路径会在那之前就记录诊断（``install_otel`` 的降级、资源解析的早期信息、嵌入式
    调用方）。为了不丢这些行，这里在**首次真正要写日志**时装一份仅 stderr 的 handler：
    不建目录、不开文件，因此仍然没有文件副作用；真正的配置随后会接管同一命名空间
    （``configure_logging`` 见到已有 handler 会让位，故这里标记 ``_log_without_config`` 装过，
    由 ``configure_logging`` 替换掉它）。
    """
    global _hinted

    if _handlers_are_configured():
        return logging.getLogger(__name__)

    namespace = logging.getLogger(_LOG_NAMESPACE)
    if namespace.handlers:
        return logging.getLogger(__name__)

    namespace.propagate = False
    handler = logging.StreamHandler(_CurrentStderr())
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_PrefixFilter())
    handler.set_name(_FALLBACK_HANDLER)
    namespace.addHandler(handler)
    namespace.setLevel(logging.INFO)
    if not _hinted:
        _hinted = True
        logging.getLogger(__name__).warning("logging not configured yet; diagnostics go to stderr only")

    return logging.getLogger(__name__)


class _DeferredLogger:
    """延迟解析的 logger 代理：**构造与取用都不产生副作用**。

    为什么需要它
    ------------
    本包的每条诊断都必须在启动钩子配置好 logging 之后才落到日志文件，但"模块级
    ``logger = getLogger(__name__)``"这个惯用法本身是安全的——而"在 import 期顺手把日志
    配上"才是副作用。为了让**两者都成立**（既不在导入期配置日志，又不让早期诊断丢失），
    各模块通过 :func:`configured_logger` 取本代理：

    * 构造时什么也不做；
    * 第一次真正写日志时才向 :func:`_log_without_config` 要一个 logger——那条路径会装一份
      仅 stderr 的兜底 handler（不建目录、不开文件），随后 ``configure_logging`` 接管。

    代理只实现 ``logging.Logger`` 上本包实际用到的接口；``getattr`` 兜住其余（这样
    ``logger.isEnabledFor``、``logger.name`` 之类探测行为与真 logger 一致）。
    """

    __slots__ = ("_name", "_logger")

    def __init__(self, name: str) -> None:
        self._name = name
        self._logger: logging.Logger | None = None

    def _target(self) -> logging.Logger:
        if self._logger is None:
            self._logger = _log_without_config()

        return self._logger

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().debug(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().error(msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().exception(msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().critical(msg, *args, **kwargs)

    def log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        self._target().log(level, msg, *args, **kwargs)

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - 与 logging.Logger 同名
        return self._target().isEnabledFor(level)

    def getChild(self, suffix: str) -> logging.Logger:  # noqa: N802 - 与 logging.Logger 同名
        return self._target().getChild(suffix)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target(), item)

    def __repr__(self) -> str:
        return f"<DeferredLogger {self._name}>"


def configured_logger(name: str) -> _DeferredLogger:
    """模块级 logger 的**无副作用**取法：返回 :class:`_DeferredLogger`。

    用 ``logger = configured_logger(__name__)`` 取代 ``logger = getLogger(__name__)``。
    区别只有一个，但很关键：后者在当前解释器里若触发了任何 logger 级配置（例如某些第三方
    库在 import 期就 ``basicConfig``），本包会在启动钩子之前就开始写文件；前者一律把
    "日志落到哪里" 推迟到真正发出记录的那一刻。
    """

    return _DeferredLogger(name)


def configure_logging() -> None:
    """把本包的诊断输出接到**日志文件**（同时保留一份 stderr）。

    为什么是文件优先
    ----------------
    本包的硬性规则是：**stdout 只承载协议帧。** 一个写到 stdout 的诊断行会被父进程当作
    JSON 解析，而数据帧按 ``id``/``runId`` 关联，因此该错误不表现为报错，而是 *TUI 一直
    卡在等待一个被写到错误流上的回复*。把诊断落到文件里，从根上避免了对任何流的污染。

    stderr 仍然保留一份
    -------------------
    不是可选装饰。TUI 靠 worker 的 stderr 做启动诊断（``aidev_agent_cli.stderr`` 事件与启动超时
    时的日志尾部），只写文件会让这些信息在最需要的时候消失。文件用于事后排查，stderr 用于
    当下可见。

    配置对象是 :data:`_LOG_NAMESPACE`（``worker``）这个层级，**不是** root logger。
    这一区别是刻意的：``root.setLevel(INFO)`` 会连带打开整个第三方 SDK（langchain、
    langgraph、ag_ui……）的 INFO 级日志，把日志文件淹没在噪音里。本包只负责自己的日志。

    文件不可写（目录不可创建、磁盘只读）时降级为仅 stderr，绝不因为日志而让 worker 起不来。

    幂等：重复调用不会叠加 handler；本函数会先摘掉 :func:`_log_without_config` 装的兜底
    handler（那份只有 stderr，没有文件）。
    """
    namespace = logging.getLogger(_LOG_NAMESPACE)

    for existing in [handler for handler in namespace.handlers if handler.get_name() == _FALLBACK_HANDLER]:
        namespace.removeHandler(existing)
        existing.close()

    if any(handler.get_name() != _FALLBACK_HANDLER for handler in namespace.handlers):
        return

    # ``propagate = False``：本 handler 已经负责输出，再向 root 冒泡会在宿主也配置了
    # root handler 时产生重复行。
    namespace.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    try:
        path = os.environ.get(LOG_FILE_OVERRIDE) or DEFAULT_LOG_FILE
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler | None = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        # 日志是诊断，不是可用性依赖：文件写不了就只留 stderr。
        file_handler = None

    for handler in (file_handler, logging.StreamHandler(_CurrentStderr())):
        if handler is None:
            continue
        handler.setFormatter(formatter)
        handler.addFilter(_PrefixFilter())
        namespace.addHandler(handler)

    namespace.setLevel(logging.INFO)


__all__ = [
    "BKAI_HOME_DIRNAME",
    "DEFAULT_LOG_FILE",
    "LOG_FILE_OVERRIDE",
    "configure_logging",
    "configured_logger",
]
