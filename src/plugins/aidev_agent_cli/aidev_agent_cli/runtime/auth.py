# -*- coding: utf-8 -*-
"""Process authentication and platform application configuration.

AuthManager owns user identity; ProcessConfig resolves application credentials
and launch defaults. Successful login/logout increments identity revision.

RuntimeService captures identity per admission; Sessions do not subscribe or hold clients.
Interactive login persists the token; ACP startup identity is memory-only. Values are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from logging import getLogger

from .contracts import LaunchOptions
from .models import IdentitySnapshot
from ..infrastructure.environment import get_credentials
from ..infrastructure.settings import TuiConfig
from ..resources.local_config import LocalDeclarations

logger = getLogger(__name__)


class AuthManager:
    """进程级用户身份。

    :param access_token: 显式 token（如 acp 启动参数）；``None`` 时从 ``bkai_tui.json`` 读回。
    :param username: 显式用户名；``None`` 时每次从 ``bkai_tui.json`` 现读（用户可能在外部编辑）。
    """

    def __init__(self, *, access_token: str | None = None, username: str | None = None) -> None:
        self._access_token = TuiConfig.read().access_token if access_token is None else access_token
        self._username = username
        self.revision = 0
        self._last_username = self.username

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def username(self) -> str:
        return self._username if self._username is not None else TuiConfig.read().username

    def snapshot(self):
        username = self.username
        if username != self._last_username:
            self._last_username = username
            self.revision += 1
        return IdentitySnapshot(username, self._access_token, self.revision)

    def has_token(self) -> bool:
        return bool(self._access_token)

    def login(self, token: str, *, persist: bool = True) -> None:
        """替换 token。``persist`` 时先落盘、成功后才更新内存（两者不分叉）。

        token 在此不做校验：无效 token 会在下一轮作为平台自身的错误暴露。
        """
        if persist:
            TuiConfig().write_access_token(token)
        self._access_token = token or ""
        logger.info(f"access token {'set' if self._access_token else 'cleared'} (persist={persist})")
        self.revision += 1

    def logout(self, *, persist: bool = True) -> None:
        """丢弃 token，回到纯应用凭据状态；落盘侧同步清空，避免重启后复活。"""
        self.login("", persist=persist)


@dataclass
class ProcessConfig:
    """进程级配置。``env`` 是 ``bkai_tui.json`` 的 ``env`` 段（入口启动时已发布到进程环境）。"""

    env: dict[str, str] = field(default_factory=dict)
    defaults: LaunchOptions = field(default_factory=LaunchOptions)

    def credentials(self) -> dict[str, str]:
        """平台应用凭据；只返回真正解析到值的键。从不记日志。"""
        return get_credentials(self.env)

    def default_agent_code(self) -> str:
        """进程级入口 agent_code：用户显式 export 的环境变量优先，其次 ``bkai_tui.json``。"""
        env_value = os.environ.get(LocalDeclarations.AGENT_CODE_ENV, "").strip()
        if env_value:
            return env_value

        return TuiConfig.read().agent_code

    def write_agent_code(self, agent_code: str) -> None:
        """切换主 Agent（写 ``bkai_tui.json`` 的 ``agent_code``）。"""
        TuiConfig().write_agent_code(agent_code)


__all__ = ["AuthManager", "ProcessConfig"]
