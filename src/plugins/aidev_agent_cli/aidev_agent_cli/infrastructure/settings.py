"""进程级设置 ``bkai_tui.json``：主 Agent、身份认证与固定值环境变量。

文件结构::

    {
      "version": 1,
      "agent_code": "启动时的入口 agent",
      "auth": { "username": "平台用户名", "access_token": "用户的 access_token" },
      "env": { "BKPAAS_APP_CODE": "...", "BK_API_URL_TMPL": "..." }
    }

读取顺序：显式 ``path`` → ``$AGENT_TUI_TUI_CONFIG_FILE``（单文件）→ 两级作用域
``~/.bkai/bkai_tui.json`` 叠加 ``<AGENT_TUI_CWD>/.bkai/bkai_tui.json``（项目覆盖用户）。

per-agent 配置（``agents/${code}.json``）由 ``LocalResourceManager`` 读取，与本文件
唯一的联系是 ``agent_code``。

token 纪律：``auth.access_token`` 是唯一放行 token 落盘的位置；日志与 ``repr``
永不回显 token 与 ``env`` 的取值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar

from .logging import BKAI_HOME_DIRNAME, configured_logger

logger = configured_logger(__name__)

#: ACP 会话 token 的环境变量名。ACP 由客户端拉起并声明身份，因此 env 优先于磁盘。
ACP_ACCESS_TOKEN_ENV = "BKAI_TUI_ACCESS_TOKEN"


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass(repr=False)
class TuiConfig:
    """``bkai_tui.json`` 的读取（永不抛异常）与写入（逐字保留未知键）。"""

    USER_CONFIG_FILE: ClassVar[str] = str(Path.home() / BKAI_HOME_DIRNAME / "bkai_tui.json")
    CONFIG_FILENAME: ClassVar[str] = "bkai_tui.json"
    #: 用户启动 TUI 时所在目录；未设置时省略项目层（不回退到 ``os.getcwd()``）。
    CWD_ENV: ClassVar[str] = "AGENT_TUI_CWD"
    #: 指向备用配置文件（单文件语义）。
    CONFIG_FILE_OVERRIDE: ClassVar[str] = "AGENT_TUI_TUI_CONFIG_FILE"

    version: int = 1
    agent_code: str = ""
    username: str = ""
    access_token: str = ""
    env: dict[str, str] = field(default_factory=dict)
    path: str | None = None

    # ------------------------------------------------------------- 路径

    @classmethod
    def user_path(cls) -> str:
        return cls.USER_CONFIG_FILE

    @classmethod
    def project_path(cls) -> str | None:
        project_root = os.environ.get(cls.CWD_ENV, "").strip()

        if not project_root:
            return None

        return os.path.join(os.path.expanduser(project_root), BKAI_HOME_DIRNAME, cls.CONFIG_FILENAME)

    @classmethod
    def _resolve_path(cls, path: str | None) -> str | None:
        return path or os.environ.get(cls.CONFIG_FILE_OVERRIDE) or None

    # ------------------------------------------------------------- 读取

    @staticmethod
    def _read_one(config_path: str) -> dict:
        """读一个文件的原始 JSON 对象；失败返回 ``{}``，日志只记异常类别名与路径。"""
        try:
            with open(config_path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.info(f"tui config file unusable at {config_path}: {type(exc).__name__}")
            return {}

        if not isinstance(raw, dict):
            logger.info(f"tui config file must hold a JSON object at {config_path}")
            return {}

        return raw

    @classmethod
    def _narrow(cls, raw: dict) -> "TuiConfig":
        """把原始 JSON 逐字段收窄为 :class:`TuiConfig`；形状不对的字段回落为默认值。"""
        auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        version = raw.get("version")

        return cls(
            version=version if isinstance(version, int) else 1,
            agent_code=_str(raw.get("agent_code")),
            username=_str(auth.get("username")),
            access_token=_str(auth.get("access_token")),
            env={k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)},
        )

    @classmethod
    def read(cls, path: str | None = None) -> "TuiConfig":
        config_path = cls._resolve_path(path)

        if config_path:
            return cls._narrow(cls._read_one(config_path))

        merged = cls._read_one(cls.user_path())
        project_path = cls.project_path()

        if project_path:
            merged.update(cls._read_one(project_path))

        return cls._narrow(merged)

    # ------------------------------------------------------------- 写入

    def _write(self, mutate: Callable[[dict], None], path: str | None, kind: str) -> None:
        """读原始 payload → ``mutate`` → 写回。不创建父目录（缺失时抛异常上报）。"""
        config_path = self._resolve_path(path if path is not None else self.path) or self.user_path()
        payload = self._read_one(config_path)

        mutate(payload)

        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        logger.info(f"wrote tui config: kind={kind}")

    def write_agent_code(self, code: str, path: str | None = None) -> None:
        self._write(lambda payload: payload.update(agent_code=code), path, "agent_code")

    def write_access_token(self, token: str, path: str | None = None) -> None:
        """写入 ``auth.access_token``。ACP 启动路径不调用（客户端 token 只在内存生效）。"""

        def mutate(payload: dict) -> None:
            if not isinstance(payload.get("auth"), dict):
                payload["auth"] = {}
            payload["auth"]["access_token"] = token

        self._write(mutate, path, "access_token")

    def __repr__(self) -> str:
        """绝不回显 token / env 取值。"""
        return (
            f"TuiConfig(version={self.version}, agent_code={self.agent_code!r}, "
            f"username={self.username!r}, "
            f"access_token={'set' if self.access_token else 'unset'}, "
            f"env={len(self.env)} key(s))"
        )


def resolve_acp_access_token(config: TuiConfig | None = None) -> str:
    """ACP 启动所需的 access token：env 优先、磁盘兜底；都为空时抛 RuntimeError（不含取值）。"""
    env_token = os.environ.get(ACP_ACCESS_TOKEN_ENV, "").strip()
    if env_token:
        logger.info("ACP access token resolved from env")
        return env_token

    token = (config if config is not None else TuiConfig.read()).access_token
    if token:
        logger.info("ACP access token resolved from the config file")
        return token

    raise RuntimeError(
        f"ACP 模式需要一个 access token：请设置环境变量 {ACP_ACCESS_TOKEN_ENV}，"
        "或在 bkai_tui.json 的 auth.access_token 中登录。"
    )
