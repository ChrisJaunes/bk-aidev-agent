"""worker 的进程环境：在 SDK import 之前把 ``bkai_tui.json`` 的 ``env`` 字段发布进进程。

环境变量只有两个来源：用户启动时已有的进程环境（优先，永不被覆盖），以及
``bkai_tui.json`` 的 ``env`` 字段（见 :mod:`.settings`）。

凭据**永不记日志、永不硬编码、永不进入错误信息**。
"""

from __future__ import annotations

import os

try:  # 可选依赖：``aidev_agent`` 总会带上它，但本模块不得因它缺失而导入失败。
    from bkapi_client_core import config as bkapi_config
except ImportError:  # pragma: no cover - SDK always ships this
    bkapi_config = None

#: 构建路径需要的凭据键（只有键名，取值在运行时读取）。
CREDENTIAL_KEYS = (
    "LLM_GATEWAY_URL",
    "LLM_GW_ENDPOINT",
    "MODEL_MANAGEMENT_NS_HOST",
    "BKPAAS_APP_CODE",
    "BKPAAS_APP_SECRET",
    "BKAIDEV_RESOURCE_STAGE",
    "AIDEV_SPACE_ID",
    "AIDEV_GATEWAY_NAME",
)


def _lookup(env: dict[str, str], key: str) -> str:
    return env.get(key) or os.environ.get(key) or ""


def get_credentials(env: dict[str, str]) -> dict[str, str]:
    """收集凭据键：先查配置 ``env``，再查进程环境；只返回有值的键。"""
    return {key: value for key in CREDENTIAL_KEYS if (value := _lookup(env, key))}


def publish_bkapi_aliases(env: dict[str, str]) -> None:
    """把 ``BKPAAS_APP_*`` 发布为 ``bkapi_client_core`` 查找的 ``BK_APP_*``（不覆盖已有值）。

    该库只为 secret 内置了 ``BKPAAS_*`` 别名，app code 没有；去掉 Django 后也没有
    settings 代理可回退，因此必须显式发布。
    """
    app_code = _lookup(env, "BKPAAS_APP_CODE")
    app_secret = _lookup(env, "BKPAAS_APP_SECRET")

    if app_code:
        os.environ.setdefault("BK_APP_CODE", app_code)
        # `aidev_agent.config.settings` 读的是 BKPAAS_APP_ID。
        os.environ.setdefault("BKPAAS_APP_ID", app_code)

    if app_secret:
        os.environ.setdefault("BK_APP_SECRET", app_secret)


def detach_bkapi_from_django() -> None:
    """解除 ``bkapi_client_core`` 的 Django settings 代理，让它只从 ``os.environ`` 解析。

    该库在 Django 可导入时绑定 ``django.conf.settings``；未配置的 ``LazySettings``
    在 ``hasattr`` 里抛 ``ImproperlyConfigured``，环境变量分支因此永远走不到。
    幂等；只解除 Django 对象，绝不覆盖显式注入的对象。
    """
    if bkapi_config is None:
        return

    settings_singleton = getattr(bkapi_config, "settings", None)
    bound = getattr(settings_singleton, "_settings", None)

    if bound is not None and type(bound).__module__.startswith("django."):
        settings_singleton._settings = None
        resolved = getattr(settings_singleton, "_resolved", None)

        if isinstance(resolved, dict):
            resolved.clear()


def apply_env_to_process(env: dict[str, str]) -> None:
    """把配置 ``env`` 非覆盖地发布进进程环境（SDK 在 import 时从环境读 settings）。"""
    for key, value in env.items():
        if value:
            os.environ.setdefault(key, value)


def bootstrap_environment(env: dict[str, str]) -> None:
    """为 SDK 准备进程环境；必须在 SDK import 之前运行，幂等。

    1. 移除 ``DJANGO_SETTINGS_MODULE``（部署 ``.env`` 里的值会拉起整套蓝鲸插件栈）；
    2. 发布 ``env``；
    3. 桥接 bkapi 凭据名并解除其 Django 代理。
    """
    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)

    apply_env_to_process(env)
    publish_bkapi_aliases(env)
    detach_bkapi_from_django()
