# -*- coding: utf-8 -*-
"""agent 追踪埋点（OTel）：与协议无关的进程设施，headless / agui / acp 共用。

必须在环境已发布**之后**、首次 SDK 导入**之前**运行（它会 monkey-patch SDK 的模块属性），
因此由入口的启动钩子统一调用，而不是由某个 adapter 调用。
"""

from __future__ import annotations

import importlib
import os

from ..utils.logging import configured_logger

logger = configured_logger(__name__)


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
#    从 ``bootstrap.startup.install_startup_hooks`` 在环境发布之后、导入任何 adapter 之前调用。
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


class _OtelComponents:
    """SDK 的 OTel 组件集合（**可选依赖**）：装载一次，按名取用。"""

    __slots__ = ("config", "get_otel_endpoint_by_env", "get_otel_endpoint_by_json_str", "instrumentor")

    def __init__(self, sdk, instrumentor, utils) -> None:
        self.config = sdk.OTelConfig
        self.instrumentor = instrumentor.BkAidevAgentInstrumentor
        self.get_otel_endpoint_by_env = utils.get_otel_endpoint_by_env
        self.get_otel_endpoint_by_json_str = utils.get_otel_endpoint_by_json_str


def _sdk_otel() -> _OtelComponents:
    """装载 SDK 的 OTel 组件。未安装时抛 ``ImportError``，由 :func:`install_otel` 降级。

    这里是本包**唯一**的 OTel 可选装载点，且刻意用 ``importlib.import_module`` 而不是
    ``from ... import ...``：可选依赖的装载是一条**显式的动态边界**，不是"把 import 藏进
    函数体"——后者会让 AST 审计无法区分「真可选」与「为绕环而延迟」，也就无法守住本包
    "函数内 import 为零" 的契约。

    装载时机不变：调用它的是 :func:`install_otel`，而后者仍必须在首次 ``aidev_agent``
    导入之前运行（见上文约束 1、约束 2）。
    """
    sdk = importlib.import_module("aidev_agent.packages.opentelemetry")
    instrumentor = importlib.import_module("aidev_agent.packages.opentelemetry.instrumentor")
    utils = importlib.import_module("aidev_agent.packages.opentelemetry.utils")

    return _OtelComponents(sdk, instrumentor, utils)


def install_otel(env: dict[str, str] | None = None) -> bool:
    """安装 SDK 的 agent 埋点。返回其是否生效。

    本函数自包含：开关判定、端点收集（含去重）与安装都在此一处完成，模块只保留这一个
    otel 入口。

    只调用一次，从 ``bootstrap.startup.install_startup_hooks``，在环境准备好之后、任何 ``aidev_agent`` 导入之前
    ——见上文约束 1。

    ``env`` 是解析后的 ``.env.tui``。它仅用于报告看到了哪些变量；进程环境才是 SDK 的
    事实来源，而运行到此处时启动钩子已把 ``env`` 发布进去。
    """
    # --- 1. 开关：默认开启，假值退出 ---
    if os.environ.get(ENABLED_ENV, "").strip().lower() in _FALSY:
        logger.info(f"[otel] disabled via {ENABLED_ENV}; no traces will be exported")

        return False

    # --- 2. 收集端点（镜像 ``aidev_bkplugin`` 的 ``apps.py``）---------------
    endpoints: list[dict] = []
    try:
        components = _sdk_otel()

        # 来源 1：BKAI_AGENT_OTEL_ENDPOINTS（多地址，JSON）。
        # 畸形取值**不得**丢弃其他来源——某个可选来源里的一个拼写错误不应静默地禁用整个
        # 开发者路径。
        try:
            endpoints.extend(components.get_otel_endpoint_by_json_str())
        except ValueError as exc:
            logger.info(f"[otel] ignoring malformed {ENDPOINTS_ENV}: {exc}")

        # 来源 2：OTEL_GRPC_URL + OTEL_BK_DATA_TOKEN（单地址；bk-report）。
        # 由 SDK 依 BKAI_AGENT_APM_OTEL_ENABLED 门控，因此除非运维打开它，否则是空操作。
        endpoints.extend(components.get_otel_endpoint_by_env())

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
        config = components.config(otel_endpoints=endpoints)
        components.instrumentor(config).instrument()
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[otel] instrumentation failed ({type(exc).__name__}: {exc}); continuing without traces")

        return False

    # 只记 URL——绝不记 token，也绝不记 config 的 repr（每次启动都很吵）。
    logger.info(f"[otel] instrumentation installed; exporting to {[ep.get('url') for ep in endpoints]}")

    return True


__all__ = [
    "DATA_TOKEN_ENV",
    "ENABLED_ENV",
    "ENDPOINTS_ENV",
    "GRPC_URL_ENV",
    "install_otel",
]
