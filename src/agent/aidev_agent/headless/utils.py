"""worker 包的通用工具：日志配置与本机 BKAI 目录常量。

为什么日志独立成一个模块
------------------------
worker 的 stdout 是**协议通道**——父进程把它的每一行都当作 JSON 解析，数据帧按
``id``/``runId`` 关联，因此一条写到 stdout 的诊断行不会表现为报错，而是表现为 *TUI 一直
卡在等待一个被写到错误流上的回复*。本包的硬性规则由此而来：**stdout 只承载协议帧。**

把诊断落到日志文件（并保留一份 stderr），从根上避免了对任何流的污染。所有需要记录的模块
都用标准 ``logging`` 的模块级 logger，不得各自 ``print``、也不得各自配置 logging——
配置只应发生在一处（:func:`configure_logging`，本模块导入时自动执行），否则 handler 会
重复、级别会互相覆盖。

其他模块如何使用
----------------
在本模块顶部拿一个模块级 logger，之后直接用它::

    from logging import getLogger

    logger = getLogger(__name__)

    logger.info("env file loaded")

来源标签由 logger 名推导：本包所有模块都在 ``worker`` 命名空间之下，:class:`_PrefixFilter`
取 ``record.name`` 的最后一段，因此 ``aidev_agent.headless.local_resources`` 渲染为
``[resource_manager]``。无需手工传入前缀。
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
#: （``aidev_agent.headless.local_resources``），因此 :func:`configure_logging` 只需配置这一个层级，
#: 就能覆盖本包全部诊断，而不触碰 root logger。
_LOG_NAMESPACE = "aidev_agent.headless"

#: 输出格式。``%(label)s`` 由 :class:`_PrefixFilter` 从 ``record.name`` 的最后一段填充，
#: 因此 ``aidev_agent.headless.local_resources`` 渲染为 ``[resource_manager]``——来源标签直接取自模块名，
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


def configure_logging() -> None:
    """把本包的诊断输出接到**日志文件**（同时保留一份 stderr）。

    为什么是文件优先
    ----------------
    本包的硬性规则是：**stdout 只承载协议帧。** 一个写到 stdout 的诊断行会被父进程当作
    JSON 解析，而数据帧按 ``id``/``runId`` 关联，因此该错误不表现为报错，而是 *TUI 一直
    卡在等待一个被写到错误流上的回复*。把诊断落到文件里，从根上避免了对任何流的污染。

    stderr 仍然保留一份
    -------------------
    不是可选装饰。TUI 靠 worker 的 stderr 做启动诊断（``aidev_agent.headless.stderr`` 事件与启动超时
    时的日志尾部），只写文件会让这些信息在最需要的时候消失。文件用于事后排查，stderr 用于
    当下可见。

    配置对象是 :data:`_LOG_NAMESPACE`（``worker``）这个层级，**不是** root logger。
    这一区别是刻意的：``root.setLevel(INFO)`` 会连带打开整个第三方 SDK（langchain、
    langgraph、ag_ui……）的 INFO 级日志，把日志文件淹没在噪音里。本包只负责自己的日志。

    文件不可写（目录不可创建、磁盘只读）时降级为仅 stderr，绝不因为日志而让 worker 起不来。

    幂等：重复调用不会叠加 handler。
    """
    namespace = logging.getLogger(_LOG_NAMESPACE)

    if namespace.handlers:
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


# 在导入时即完成配置。若干调用方（``install_otel``、``resource_manager``）会直接
# 记录诊断，而不经过 ``main()``——一个“先调 __main__ 才有日志”的约定会让这些路径静默
# 丢日志。此调用幂等，``main()`` 中的再次调用只是保持局部显式。
configure_logging()
