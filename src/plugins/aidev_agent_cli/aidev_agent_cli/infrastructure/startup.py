# -*- coding: utf-8 -*-
"""进程启动钩子：日志 → stdout guard → 环境发布 → 埋点 → 出网限制 → sys.path 加固。

两个入口，**共享同一组底层步骤**：

* :func:`install_startup_hooks` —— CLI 入口用（``__main__`` / 各 adapter 的嵌入式入口）：
  会读配置文件并装日志。
* :func:`install_worker_startup` —— spawned worker 用：env 来自 JSON 快照（不是配置文件）、
  日志已由父进程建立，因此只做「环境 → guard → 出网 → sys.path → OTel」这五步。

两者都是**显式调用**（不靠 import 副作用），顺序一致。本模块顶部只 import 本包
infrastructure 层（``logging`` / ``stdio`` / ``network_guard`` / ``environment`` /
``open_telemetry``），它们导入期都没有文件副作用，因此模块可以安全地放在入口顶部——
"日志文件何时打开"只由这里两个函数决定。

worker 的调用点是 ``runtime.worker._worker_entry``：它在 ``import_module`` 装载执行体
**之前**完成 env 发布与 ``install_worker_startup``，所以执行体顶部的 SDK 依赖落在
guard / 环境 / OTel 之后就位。
"""

from __future__ import annotations

import os

from .environment import bootstrap_environment
from .logging import configure_logging
from .network_guard import install_from_env
from .open_telemetry import install_otel
from .settings import TuiConfig
from .stdio import harden_sys_path, install_stdout_guard


def install_startup_hooks() -> None:
    """装日志 / stdout guard / 发布环境 / 出网限制 / 加固 sys.path。**不导入任何 adapter。**

    每一步都只碰标准库或本包 infrastructure 层，因此可以安全地在 SDK import 之前执行。
    读配置文件（``TuiConfig.read()``）在这里完成——env 来源只有这一处，不给调用方开第二
    条"直接给 env"的旁路。
    """
    # --- 1. 诊断先接到日志文件（后面每一步都会记日志） ---------------------
    configure_logging()

    # --- 2. guard：保护整段导入窗口（SDK import 期的 print 不得落到 fd 1）--
    install_stdout_guard()

    # --- 3. 进程环境（SDK 在 import 期读取自己的 settings） ----------------
    env = TuiConfig.read().env
    bootstrap_environment(env)

    # --- 3b. agent 追踪埋点（所有协议）：环境已发布、SDK 尚未导入 ----------
    install_otel(env)

    # --- 4. 可选出网限制（评测 / 离线场景） -------------------------------
    install_from_env()

    # --- 5. 剔除 sys.path 里的 cwd 条目 ----------------------------------
    harden_sys_path()


def install_worker_startup(env: dict[str, str]) -> None:
    """spawned worker 的启动顺序：环境 → stdout guard → 出网 → sys.path 加固 → OTel。

    worker 的每一步都是**契约**，不是风格：

    1. ``bootstrap_environment`` —— ``aidev_agent`` 在 import 期读自己的 settings，环境必须
       先发布（``os.environ.clear()`` + 快照 env 由调用方 ``_worker_entry`` 在更早处完成，
       本步是同一快照的幂等重放）；
    2. ``install_stdout_guard`` —— SDK（``ag_ui``）导入期的 print 不得落到 fd 1，那是我方
       协议通道；
    3. ``install_from_env`` —— 评测 / 离线场景的出网限制；
    4. ``harden_sys_path`` —— 剔除 sys.path 里的 cwd 条目（导入搜索路径固定）；
    5. ``install_otel`` —— 必须在**首次** ``aidev_agent`` import 之前 patch 模块属性。

    与 :func:`install_startup_hooks` 的差异只在少了 ``configure_logging()`` 与
    ``TuiConfig.read()``：worker 的 env 来自 JSON 快照而非配置文件，日志已由父进程建立。
    """
    bootstrap_environment(env)
    install_stdout_guard()
    install_from_env()
    harden_sys_path()
    install_otel(env)


#: 两个入口函数是模块的公开面；:func:`publish_environment` / :func:`worker_environment`
#: 是 worker 侧 env 快照的显式发布点（``_worker_entry`` 用）。
__all__ = [
    "install_startup_hooks",
    "install_worker_startup",
    "publish_environment",
    "worker_cwd",
    "worker_environment",
]


def _session_settings(snapshot: dict) -> dict:
    """快照里的 ``session.settings``；缺失或形状不对时返回空 dict（调用方各自兜底）。"""
    session = snapshot.get("session") if isinstance(snapshot, dict) else None
    settings = session.get("settings") if isinstance(session, dict) else None

    return settings if isinstance(settings, dict) else {}


def worker_environment(snapshot: dict) -> dict[str, str]:
    """从 worker 快照里取环境：``session.settings.env``。省略时用**当前**进程环境。

    ``env`` 可省略：生产快照总是带着它（父进程按配置解析好），而只关心 IPC 契约的 worker
    测试可以不带——那时"发布的环境"就是父进程已经有的那份，语义上没有区别。
    """
    env = _session_settings(snapshot).get("env")
    if env is None:
        return dict(os.environ)
    if not isinstance(env, dict):
        raise ValueError("worker snapshot env must be an object")

    return dict(env)


def worker_cwd(snapshot: dict) -> str:
    """从 worker 快照里取工作目录：``session.settings.cwd``。省略时用当前 cwd。"""
    cwd = _session_settings(snapshot).get("cwd")
    if cwd is None:
        return os.getcwd()
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("worker snapshot cwd must be a non-empty string")

    return cwd


def publish_environment(env: dict[str, str]) -> dict[str, str]:
    """把快照 env 原样发布进 ``os.environ``，并返回**同一个** dict（可继续交给后续步骤）。

    返回入参而不是 ``None``：``_worker_entry`` 的读法是 ``env = publish_environment(...)``
    然后 ``install_worker_startup(env)``——``bootstrap_environment`` 会在这个 dict 上
    ``pop``（移除 ``DJANGO_SETTINGS_MODULE``），所以它必须是一个真实 dict。
    """
    os.environ.clear()
    os.environ.update(env)

    return env
