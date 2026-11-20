# -*- coding: utf-8 -*-
"""进程启动钩子：stdout guard → 环境发布 → 出网限制 → sys.path 加固 → 埋点。

两个入口共用同一组步骤（:func:`_install`），差异只在各自的前置：

* :func:`install_startup_hooks` —— CLI 入口用：先装日志，env 来自入口解析好的配置。
* :func:`install_worker_startup` —— spawned worker 用：日志已由父进程建立；env 与 cwd 来自
  JSON 快照，**整体替换**进程环境后再走同一组步骤。

两者都是**显式调用**（不靠 import 副作用）。本模块顶部只 import 本包 utils 与 bootstrap 内模块，
它们导入期都没有文件副作用；"日志文件何时打开"只由这里两个函数决定。

环境变量只有两个来源：用户启动时已有的进程环境（优先，永不被覆盖），以及
``bkai_tui.json`` 的 ``env`` 字段。凭据**永不记日志、永不进入错误信息**。
"""

from __future__ import annotations

import importlib
import os

from ..utils.logging import configure_logging
from .network_guard import install_from_env
from .open_telemetry import install_otel
from ..utils.stdio import harden_sys_path, install_stdout_guard


def _bkapi_config():
    """``bkapi_client_core.config``；可选依赖，未安装时返回 ``None``。"""
    try:
        return importlib.import_module("bkapi_client_core.config")
    except ImportError:
        return None


def _publish_env(env: dict[str, str]) -> None:
    """为 SDK 准备进程环境；必须在 SDK import 之前运行，幂等。

    1. 移除 ``DJANGO_SETTINGS_MODULE``（部署 ``.env`` 里的值会拉起整套蓝鲸插件栈）；
    2. 把 ``env`` 非覆盖地发布进进程环境（SDK 在 import 时从环境读 settings）；
    3. 把 ``BKPAAS_APP_*`` 发布为 ``bkapi_client_core`` 查找的 ``BK_APP_*``，以及
       ``aidev_agent.config`` 读取的 ``BKPAAS_APP_ID``（不覆盖已有值）。
    """
    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)

    for key, value in env.items():
        if value:
            os.environ.setdefault(key, value)

    app_code = env.get("BKPAAS_APP_CODE") or os.environ.get("BKPAAS_APP_CODE")
    app_secret = env.get("BKPAAS_APP_SECRET") or os.environ.get("BKPAAS_APP_SECRET")
    if app_code:
        os.environ.setdefault("BK_APP_CODE", app_code)
        os.environ.setdefault("BKPAAS_APP_ID", app_code)
    if app_secret:
        os.environ.setdefault("BK_APP_SECRET", app_secret)


def _detach_bkapi_from_django() -> None:
    """解除 ``bkapi_client_core`` 绑定的 Django settings 代理，让它只从进程环境解析；幂等。

    未配置的 ``LazySettings`` 会在 ``hasattr`` 里抛错，环境变量分支因此永远走不到。
    只解除 Django 对象，绝不覆盖显式注入的对象。
    """
    singleton = getattr(_bkapi_config(), "settings", None)
    bound = getattr(singleton, "_settings", None)
    if bound is not None and type(bound).__module__.startswith("django."):
        singleton._settings = None
        resolved = getattr(singleton, "_resolved", None)
        if isinstance(resolved, dict):
            resolved.clear()


def _install(env: dict[str, str]) -> None:
    """两个入口共用的启动顺序；每一步都只碰标准库或本包 utils / bootstrap。

    1. stdout guard —— 保护整段导入窗口（SDK 导入期的 print 不得落到 fd 1）；
    2. 环境发布并解除 bkapi 的 Django 代理 —— SDK 在 import 期读取自己的 settings；
    3. 出网限制 —— 评测 / 离线场景；
    4. sys.path 加固 —— 剔除 cwd 条目，之后的装载（含 OTel）不会被本地同名模块遮蔽；
    5. OTel —— 必须在**首次** ``aidev_agent`` import 之前 patch 模块属性。
    """
    install_stdout_guard()
    _publish_env(env)
    _detach_bkapi_from_django()
    install_from_env()
    harden_sys_path()
    install_otel(env)


def install_startup_hooks(env: dict[str, str]) -> None:
    """CLI 入口：装日志后走共用启动顺序。**不导入任何 adapter。**

    ``env`` 是入口解析好的 ``bkai_tui.json`` 的 ``env`` 段（配置文件只由 ``__main__`` 读一次）。
    """
    configure_logging()
    _install(env)


def install_worker_startup(env: dict[str, str], cwd: str, *, egress_fd: int | None = None) -> None:
    """spawned worker：以快照 env **整体替换**进程环境、切到快照 cwd，再走共用启动顺序。

    ``egress_fd`` 是父进程交下来的出网 broker 描述符，在替换环境之后登记，供出网限制一步读取。
    幂等：执行体以同一快照重放时得到同样的进程状态。
    """
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)
    if egress_fd is not None:
        os.environ["AGENT_TUI_EGRESS_FD"] = str(egress_fd)
    _install(env)


__all__ = ["install_startup_hooks", "install_worker_startup"]
