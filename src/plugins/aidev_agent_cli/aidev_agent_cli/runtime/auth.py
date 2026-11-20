# -*- coding: utf-8 -*-
"""进程级用户身份。

AuthManager 持有 ``bkai_tui.json`` 的 :class:`BkAiConfig`，身份即其 ``auth`` 段；成功的
login / logout 递增身份版本号。RuntimeService 在每次 admission 捕获身份快照，Session 不订阅。
交互式登录落盘；ACP 启动身份只在内存生效。取值永不记日志。
"""

from __future__ import annotations

from logging import getLogger

from ..utils.runs import IdentitySnapshot
from .settings import BkAiConfig

logger = getLogger(__name__)


class AuthManager:
    """进程级用户身份，取自（并写回）给定的 :class:`BkAiConfig`。"""

    def __init__(self, config: BkAiConfig) -> None:
        self._config = config
        self.revision = 0
        self._last_username = self.username

    @property
    def access_token(self) -> str:
        return self._config.auth.get("access_token", "")

    @property
    def username(self) -> str:
        return self._config.auth.get("username", "")

    def snapshot(self) -> IdentitySnapshot:
        """当前身份；用户名与上次快照不同（配置被改写）时递增版本号。"""
        username = self.username
        if username != self._last_username:
            self._last_username = username
            self.revision += 1
        return IdentitySnapshot(username, self.access_token, self.revision)

    def has_token(self) -> bool:
        return bool(self.access_token)

    def login(self, token: str, *, persist: bool = True) -> None:
        """替换 token。``persist`` 时先落盘、成功后才更新内存（两者不分叉）。

        token 在此不做校验：无效 token 会在下一轮作为平台自身的错误暴露。
        """
        token = token or ""
        if persist:
            self._config.write(["auth", "access_token"], token)
        else:
            self._config.auth["access_token"] = token
        logger.info(f"access token {'set' if token else 'cleared'} (persist={persist})")
        self.revision += 1

    def logout(self, *, persist: bool = True) -> None:
        """丢弃 token，回到纯应用凭据状态；落盘侧同步清空，避免重启后复活。"""
        self.login("", persist=persist)


__all__ = ["AuthManager"]