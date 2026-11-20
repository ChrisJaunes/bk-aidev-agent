"""进程级设置 ``bkai_tui.json``：解析与写入。

文件结构::

    {
      "version": 1,
      "default_agent_code": "启动时的入口 agent",
      "auth": { "username": "平台用户名", "access_token": "用户的 access_token" },
      "env": { "BKPAAS_APP_CODE": "...", "BK_API_URL_TMPL": "..." }
    }

三层来源，按优先级从高到低：显式 ``path`` / ``$AGENT_TUI_TUI_CONFIG_FILE`` →
``<AGENT_TUI_CWD>/.bkai/bkai_tui.json``（项目）→ ``~/.bkai/bkai_tui.json``（用户）。
读取时从低到高逐层**深度合并**；写入落到 :attr:`BkAiConfig.storage_path`。

per-agent 配置（``agents/${code}.json``）由 ``LocalResourceManager`` 读取，与本文件
唯一的联系是 ``default_agent_code``。

token 纪律：``auth.access_token`` 是唯一放行 token 落盘的位置；日志与 ``repr``
永不回显 token 与 ``env`` 的取值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..utils.logging import BKAI_HOME_DIRNAME, configured_logger

logger = configured_logger(__name__)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _merge(base: dict, override: dict) -> dict:
    """深度合并：两边都是 dict 的键递归合并，其余以 ``override`` 为准。不修改入参。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(repr=False)
class BkAiConfig:
    """``bkai_tui.json`` 的读取（永不抛异常）与写入（逐字保留未知键）。"""

    CONFIG_FILENAME: ClassVar[str] = "bkai_tui.json"
    USER_CONFIG_FILE: ClassVar[str] = str(Path.home() / BKAI_HOME_DIRNAME / CONFIG_FILENAME)
    #: 用户启动 TUI 时所在目录；未设置时省略项目层（不回退到 ``os.getcwd()``）。
    CWD_ENV: ClassVar[str] = "AGENT_TUI_CWD"
    #: 指向配置文件，作为最高优先级的一层。
    CONFIG_FILE_OVERRIDE: ClassVar[str] = "AGENT_TUI_TUI_CONFIG_FILE"

    version: int = 1
    default_agent_code: str = ""
    auth: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    #: 写入目标：存在的最高优先级层；都不存在时为用户层。
    storage_path: str = USER_CONFIG_FILE

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
    def read(cls, path: str | None = None) -> BkAiConfig:
        """按 用户 → 项目 → 配置路径 逐层深度合并，并记录写入目标。"""
        project_root = os.environ.get(cls.CWD_ENV, "").strip()
        project = os.path.join(os.path.expanduser(project_root), BKAI_HOME_DIRNAME, cls.CONFIG_FILENAME)
        configured = path or os.environ.get(cls.CONFIG_FILE_OVERRIDE)

        merged: dict = {}
        storage_path = cls.USER_CONFIG_FILE
        for layer in (cls.USER_CONFIG_FILE, project if project_root else None):
            if layer and os.path.exists(layer):
                merged = _merge(merged, cls._read_one(layer))
                storage_path = layer
        # 显式配置路径即使尚不存在也是写入目标。
        if configured:
            merged = _merge(merged, cls._read_one(configured))
            storage_path = configured

        auth = merged.get("auth") if isinstance(merged.get("auth"), dict) else {}
        env = merged.get("env") if isinstance(merged.get("env"), dict) else {}
        version = merged.get("version")

        return cls(
            version=version if isinstance(version, int) else 1,
            default_agent_code=_str(merged.get("default_agent_code")),
            auth={k: v for k, v in auth.items() if k in ("username", "access_token") and isinstance(v, str)},
            env={k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)},
            storage_path=storage_path,
        )

    # ------------------------------------------------------------- 写入

    def write(self, key: str | list[str], value: Any) -> None:
        """把 ``value`` 写到 ``key`` 所指的位置并落盘；``list`` 表示逐级嵌套的键路径。

        读回 :attr:`storage_path` 的原始内容再修改，逐字保留未知键；同时更新内存中的同名字段。
        不创建父目录（缺失时抛异常上报）。
        """
        keys = [key] if isinstance(key, str) else list(key)
        if not keys:
            raise ValueError("config key path must not be empty")

        payload = self._read_one(self.storage_path) if os.path.exists(self.storage_path) else {}
        _assign(payload, keys, value)

        with open(self.storage_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        head, *rest = keys
        if hasattr(self, head) and head != "storage_path":
            if rest:
                _assign(getattr(self, head), rest, value)
            else:
                setattr(self, head, value)

        logger.info(f"wrote tui config: key={'.'.join(keys)}")

    def __repr__(self) -> str:
        """绝不回显 token / env 取值。"""
        return (
            f"BkAiConfig(version={self.version}, default_agent_code={self.default_agent_code!r}, "
            f"username={self.auth.get('username', '')!r}, "
            f"access_token={'set' if self.auth.get('access_token') else 'unset'}, "
            f"env={len(self.env)} key(s))"
        )


def _assign(target: dict, keys: list[str], value: Any) -> None:
    """沿 ``keys`` 逐级下钻（缺失或非 dict 的中间层替换为 ``{}``），在末级写入 ``value``。"""
    *parents, last = keys
    for key in parents:
        child = target.get(key)
        if not isinstance(child, dict):
            child = target[key] = {}
        target = child
    target[last] = value


__all__ = ["BkAiConfig"]
