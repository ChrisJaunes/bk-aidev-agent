# -*- coding: utf-8 -*-
"""进程级统一配置：本地配置、智能体配置、启动选项与身份。

``__main__`` 解析 ``~/.bkai/bkai_tui.json`` 得到 :class:`BkAiConfig`，经 ``build_runtime`` 组装成
:class:`ProcessConfig` 交给 ``RuntimeService``；worker 侧由快照重建（不读配置文件）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..utils.runs import LaunchOptions
from .settings import BkAiConfig
from ..resources.local_config import BkAiAgentInfo
from .auth import AuthManager
from ..utils.logging import BKAI_HOME_DIRNAME

#: 覆盖本机持久化文件位置的环境变量（整体替换为指定**文件**；测试用它重定向到临时目录）。
HISTORY_FILE_ENV = "BKAI_TUI_HISTORY_FILE"
CHECKPOINT_FILE_ENV = "BKAI_TUI_CHECKPOINT_FILE"


def _storage_file(env_name: str, default_name: str) -> str:
    return os.environ.get(env_name) or str(Path.home() / BKAI_HOME_DIRNAME / default_name)


#: 构建路径需要的平台凭据键（只有键名，取值在运行时读取）。
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


@dataclass
class ProcessConfig:
    """进程级配置。

    - ``bkai``：本地配置（``bkai_tui.json``；其 ``env`` 段已由入口发布到进程环境）；省略时现读；
    - ``agents``：运行时智能体配置（per-agent ``agents/${code}.json``），按 agent_code 缓存；
    - ``launch``：本次启动的选项（默认模型 / 会话 / 资源 / 工作区根）；
    - ``auth``：本次启动的身份管理器；省略时绑定 ``bkai``；
    - ``history_path`` / ``checkpoint_path``：本机持久化文件，构造时（即入口装配时）一次解析定死，
      之后只从这里取；checkpoint 路径经快照交给 worker（sdk 不认识 ``~/.bkai``）。
    """

    bkai: BkAiConfig = field(default_factory=lambda: BkAiConfig.read())
    agents: dict[str, BkAiAgentInfo] = field(default_factory=dict)
    launch: LaunchOptions = field(default_factory=LaunchOptions)
    auth: AuthManager | None = None
    history_path: str = field(default_factory=lambda: _storage_file(HISTORY_FILE_ENV, "history.sqlite"))
    checkpoint_path: str = field(default_factory=lambda: _storage_file(CHECKPOINT_FILE_ENV, "checkpoints.sqlite"))

    def __post_init__(self) -> None:
        if self.auth is None:
            self.auth = AuthManager(self.bkai)

    @property
    def env(self) -> dict[str, str]:
        return self.bkai.env

    def agent_info(self, agent_code: str) -> BkAiAgentInfo:
        """``agent_code`` 对应的智能体配置（同一进程内复用同一实例）。"""
        return self.agents.setdefault(agent_code, BkAiAgentInfo())

    def credentials(self) -> dict[str, str]:
        """平台应用凭据：先查配置 ``env``，再查进程环境；只返回有值的键。从不记日志。"""
        env = self.env
        return {key: value for key in CREDENTIAL_KEYS if (value := env.get(key) or os.environ.get(key))}

    def default_agent_code(self) -> str:
        """进程级入口 agent_code：用户显式 export 的环境变量优先，其次 ``bkai_tui.json``。"""
        env_value = os.environ.get(BkAiAgentInfo.AGENT_CODE_ENV, "").strip()
        if env_value:
            return env_value

        return self.bkai.default_agent_code

    def write_agent_code(self, agent_code: str) -> None:
        """切换主 Agent（写 ``bkai_tui.json`` 的 ``default_agent_code``）。"""
        self.bkai.write("default_agent_code", agent_code)


__all__ = ["CHECKPOINT_FILE_ENV", "HISTORY_FILE_ENV", "ProcessConfig"]