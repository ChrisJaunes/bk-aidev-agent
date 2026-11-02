"""worker 的进程环境：``bkai_tui.json`` 的 ``env`` 字段与凭据。

本模块只回答一个问题：*这台机器的环境说了什么？* 环境变量只有**两个来源**：

1. **用户启动时已经导入的进程环境**（shell 里 export 过、或平台注入的）——它们
   天然在 ``os.environ`` 里，本模块不覆盖它们；
2. **配置文件 ``bkai_tui.json`` 的 ``env`` 字段**（固定值的键值对，经
   :class:`TuiConfig` 读取）——它们被**非覆盖地**发布进进程（供 SDK 自己的
   settings 对象读取），并参与凭据解析。

不再有独立的 ``.env.tui`` 文件：那套 shell 风格解析（``export`` 前缀、注释块、
``${VAR}`` 展开）已随本改动删除，固定值直接写 JSON。

agent 从哪里来
--------------
不在这里。agent 由 :mod:`aidev_agent.headless.agent_factory` 经 SDK 自己的
``AgentInstanceFactory`` 拼装——应用平台角色提示词、知识库、技能、MCP server
与回写接线都在那一侧。用凭据手工建图会造出一个并非平台所描述的 agent（见
那个模块的 docstring），因此「凭据 → 环境」的关切住在这里，「凭据 → agent」
的关切住在那里。

凭据从配置的 ``env`` 字段或进程环境读取，**永不记日志、永不硬编码、永不进入
错误信息**。

TuiConfig：进程级 TUI 配置
-------------------------
本模块还承载 :class:`TuiConfig`——**进程级**的 TUI 配置，住在
``~/.bkai/bkai_tui.json``（用户空间）与 ``<AGENT_TUI_CWD>/.bkai/bkai_tui.json``
（项目空间，项目覆盖用户）。它与 per-agent 配置（``agents/${code}.json``，由
:class:`aidev_agent.headless.local_resources.LocalResourceManager` 读取）是**两个不同层**：

* :class:`TuiConfig` 只管**三件事**——"主 Agent 是谁"（``agent_code``）、身份
  认证（``auth.username`` / ``auth.access_token``）与固定值环境变量（``env``）。
  ``/agents`` 写它的 ``agent_code``。
* per-agent 配置（``/models`` / ``/mcps`` / ``/knowledge`` / 未来的 ``/skills``）
  写的是**当前主 Agent**（即 ``TuiConfig.agent_code`` 所指那个 agent）的
  ``agents/${code}.json``。这一耦合是两份配置之间**唯一**的联系。

token 纪律：``auth.access_token`` 是**唯一**放行 token 落盘的位置；agent 级
配置（``LocalDeclarations``）仍结构性拒绝 token。日志纪律不受影响：本类日志
与 ``__repr__`` **永不**回显 token 与 ``env`` 的取值（见 :meth:`TuiConfig.__repr__`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Callable

from .utils import BKAI_HOME_DIRNAME

logger = getLogger(__name__)

#: 构建路径需要的凭据键。只有**键名**——取值在运行时读取，
#: 且永不记日志。
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


def get_credentials(env: dict[str, str]) -> dict[str, str]:
    """收集构建所需的凭据键。

    取值先查配置文件的 ``env`` 字段（``env`` dict），再查进程环境（用户启动时
    导入的值）。只返回真正解析到值的键，调用方因此能区分"已配置"与"配置成了
    空串"。

    返回值永不记日志——只记键名。
    """
    creds: dict[str, str] = {}
    for key in CREDENTIAL_KEYS:
        value = env.get(key) or os.environ.get(key) or ""
        if value:
            creds[key] = value
    return creds


def publish_bkapi_aliases(env: dict[str, str]) -> None:
    """把凭据以 ``bkapi_client_core`` 查找的名字发布出去。

    该库从 ``BK_APP_CODE`` / ``BK_APP_SECRET`` 解析客户端凭据（见
    ``bkapi_client_core.config.SettingKeys``），而蓝鲸 PaaS 环境导出的是
    ``BKPAAS_APP_CODE`` / ``BKPAAS_APP_SECRET``。它为 *secret* 内置了别名
    （``BKPAAS_APP_SECRET`` 在列表里），但**没有**为 app code 内置。

    去掉 ``DJANGO_SETTINGS_MODULE`` 后不再有 Django settings 代理这条回退，
    别名必须显式提供——否则每次 API 调用都报 "settings are not configured"。

    已存在的变量永不覆盖。
    """
    app_code = env.get("BKPAAS_APP_CODE") or os.environ.get("BKPAAS_APP_CODE") or ""
    app_secret = env.get("BKPAAS_APP_SECRET") or os.environ.get("BKPAAS_APP_SECRET") or ""

    if app_code:
        os.environ.setdefault("BK_APP_CODE", app_code)
        # `aidev_agent.config.settings` 读的是 APP_CODE；两种拼法都发布，代价极小。
        os.environ.setdefault("BKPAAS_APP_ID", app_code)

    if app_secret:
        os.environ.setdefault("BK_APP_SECRET", app_secret)


def detach_bkapi_from_django() -> None:
    """阻止 ``bkapi_client_core`` 向 Django 咨询它的 settings。

    ``bkapi_client_core.config`` 在 import 时只要 Django 可导入就把
    ``self._settings = django.conf.settings`` 绑上。它的 ``get()`` 先对那个对象
    ``hasattr(key)``，而未配置的 ``LazySettings`` 会在 ``hasattr`` 里抛
    ``ImproperlyConfigured``——下面的环境变量分支因此永远走不到，整条分支
    顺序塌掉。

    本 worker 是无 Django 应用的独立进程，这个代理没有意义。解除它（把
    ``_settings`` 置 ``None``）让该库纯粹从 ``os.environ`` 解析——
    :func:`publish_bkapi_aliases` 刚把凭据放进去的地方。

    幂等；库未安装或 Django 从来不可用时是 NO-OP（``_settings`` 已是 ``None``）。
    """
    try:
        from bkapi_client_core import config as bkapi_config
    except ImportError:  # pragma: no cover - SDK always ships this
        return

    settings_singleton = getattr(bkapi_config, "settings", None)

    if settings_singleton is None:
        return

    bound = getattr(settings_singleton, "_settings", None)

    # 只解除 Django 代理；绝不覆盖显式注入的对象。
    if bound is not None and type(bound).__module__.startswith("django."):
        settings_singleton._settings = None
        # 丢弃可能经代理取过的缓存查询结果。
        resolved = getattr(settings_singleton, "_resolved", None)

        if isinstance(resolved, dict):
            resolved.clear()


def apply_env_to_process(env: dict[str, str]) -> None:
    """把配置 ``env`` 字段的值发布进进程环境。

    SDK 在 import 时从环境读取它自己的 settings（``aidev_agent.config.settings``），
    只放在 dict 里的值到不了 ``ChatModel.get_setup_instance``。已存在的环境变量
    **不**被覆盖——用户启动时导入的值（shell export / 平台注入）必须胜过配置
    文件里的固定值。
    """
    for key, value in env.items():
        if value:
            os.environ.setdefault(key, value)


def bootstrap_environment(env: dict[str, str]) -> None:
    """为 SDK 准备进程环境。

    必须在任何会在模块级读取配置的 SDK import 之前运行。依次做三件事：

    1. 移除 ``DJANGO_SETTINGS_MODULE``。仓库部署用的 ``.env`` 把它设为
       ``bk_plugin.patch.plugin``，其 settings 模块会导入整套蓝鲸插件栈——包括
       本组件不得依赖的 ``aidev_bkplugin``。
    2. 把解析后的 env 发布到进程环境中（SDK 在 import 时读取）。
    3. 桥接 ``bkapi_client_core`` 查找的凭据名，并解除它的 Django settings 代理。

    幂等；可安全地多次调用。
    """
    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)

    apply_env_to_process(env)
    publish_bkapi_aliases(env)
    detach_bkapi_from_django()


@dataclass
class TuiConfig:
    """进程级 TUI 配置：``bkai_tui.json`` 的两级读取、写入与 token 白名单。

    与 :class:`aidev_agent.headless.local_resources.LocalResourceManager`（per-agent 配置）的
    关系见模块 docstring：本类只管"主 Agent 是谁"（``agent_code``）、身份认证
    （``auth.username`` / ``auth.access_token``）与固定值环境变量（``env``）三件事。

    形状（用户给定）：:

        {
          "version": 1,
          "agent_code": "启动时的入口 agent",
          "auth": { "username": "平台用户名", "access_token": "用户的 access_token" },
          "env": { "BKPAAS_APP_CODE": "...", "BK_API_URL_TMPL": "..." }
        }

    ``env`` 是**固定值**环境变量的家：worker 不再解析独立的 ``.env.tui``——
    环境变量要么来自用户启动时已导入的进程环境（优先、不被覆盖），要么来自
    此字段。写入路径不写 ``env``（它由用户手工维护；``_write_payload`` 的
    "保留未知键"机制保证 ``/agents`` / ``/login`` 的写入不会丢掉它）。

    ``access_token`` 是**独立字段**而非 dict，便于结构性控制（与 :class:`LocalConfig`
    的"无 token 字段"是相反的设计目标——此处 token 是合法内容，故有字段）。
    ``username`` 同住 ``auth`` 段：平台网关拒绝匿名调用，worker 的用户名与 token
    同源，都从本文件读取（不再读 ``BK_INIT_SUPERUSER`` 环境变量）。

    读取纪律（与 ``LocalDeclarations`` 一致）
    ---------------------------------------
    * 文件缺失、不可读、JSON 损坏或非对象 → 该层贡献空配置 + **一行**日志（只记异常
      *类别名*与路径，**绝不**记文件片段——JSONDecodeError 的消息会回显内容）。
    * ``read()`` 永不抛异常。
    * 路径解析顺序：显式 ``path`` → :attr:`CONFIG_FILE_OVERRIDE` →
      两级作用域（用户空间 → 项目空间，**项目覆盖用户**）。

    token 纪律
    ----------
    ``auth.access_token`` 是**唯一**放行 token 落盘的位置；agent 级配置
    （``agents/${code}.json``）仍由 :class:`LocalDeclarations` 结构性拒绝
    token——本类不改该纪律。

    日志纪律：:meth:`write_agent_code` / :meth:`write_access_token` 只记 ``kind``
    与键名；:meth:`__repr__` 显式覆盖为 ``'set'`` / ``'unset'``，**绝不**回显
    token 取值（有守护测试）。
    """

    version: int = 1
    agent_code: str = ""
    username: str = ""
    access_token: str = ""

    #: 用户空间配置文件：``~/.bkai/bkai_tui.json``。目录名由
    #: :data:`aidev_agent.headless.utils.BKAI_HOME_DIRNAME` 派生——与日志路径共享同一父目录。
    USER_CONFIG_FILE = str(Path.home() / BKAI_HOME_DIRNAME / "bkai_tui.json")

    #: 项目空间所在的子目录名（``<AGENT_TUI_CWD>/.bkai/``）。
    PROJECT_DIRNAME = BKAI_HOME_DIRNAME

    #: 两级作用域内查找的文件名。**不是** ``tui.json``——文件名是 ``bkai_tui.json``。
    CONFIG_FILENAME = "bkai_tui.json"

    #: 携带用户启动 TUI 时所在目录的环境变量。与 §1 / §2 同一语义——项目空间那一层
    #: 据此定位；未设置时省略该层，**不**回退到 ``os.getcwd()``。
    CWD_ENV = "AGENT_TUI_CWD"

    #: 指向备用配置文件的环境变量（单文件语义）。设置后只读该文件。
    CONFIG_FILE_OVERRIDE = "AGENT_TUI_TUI_CONFIG_FILE"

    def __init__(
        self,
        version: int = 1,
        agent_code: str = "",
        username: str = "",
        access_token: str = "",
        env: dict[str, str] | None = None,
        path: str | None = None,
    ) -> None:
        #: 配置文件版本号。仅接受 ``int``，否则回落到 ``1``。
        self.version = version if isinstance(version, int) else 1
        #: 启动时的入口 agent。仅接受 ``str``，否则回落到 ``""``。
        self.agent_code = agent_code if isinstance(agent_code, str) else ""
        #: 平台用户名（网关拒绝匿名调用）。仅接受 ``str``。
        self.username = username if isinstance(username, str) else ""
        #: 用户的 access token（唯一放行落盘的位置）。仅接受 ``str``。
        self.access_token = access_token if isinstance(access_token, str) else ""
        #: 固定值环境变量（``env`` 字段）。仅收 **str → str** 的条目——其余形状
        #: （非 dict、非 str 键、非 str 值）逐条丢弃，永不抛异常。**永不记日志、
        #: repr 永不回显取值**（可能携带凭据）。
        self.env: dict[str, str] = {
            str(key): value
            for key, value in (env or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        #: 显式路径，或 ``None`` 表示走 env / 两级作用域解析。``read()`` 未传参时生效。
        self.path = path

    # ------------------------------------------------------------- 路径

    @classmethod
    def user_path(cls) -> str:
        """用户空间配置路径 ``~/.bkai/bkai_tui.json``。"""
        return cls.USER_CONFIG_FILE

    @classmethod
    def project_path(cls) -> str | None:
        """项目空间配置路径 ``<AGENT_TUI_CWD>/.bkai/bkai_tui.json``，未设 CWD 时为 ``None``。

        未设置 CWD 时**省略**这一层，而不是回退到 ``os.getcwd()``：worker 的 cwd 是
        ``agent_tui/`` 模块根，回退会把模块目录当作用户项目来读（与 §1 / §2 同一坑）。
        ``expanduser`` 容忍 ``~`` 写法。
        """
        project_root = os.environ.get(cls.CWD_ENV, "").strip()

        if not project_root:
            return None

        return os.path.join(
            os.path.expanduser(project_root), cls.PROJECT_DIRNAME, cls.CONFIG_FILENAME
        )

    # ------------------------------------------------------------- 收窄

    @staticmethod
    def _read_one(config_path: str) -> dict:
        """读取**一个** ``bkai_tui.json`` 的原始 JSON 对象。永不抛异常。

        失败时返回 ``{}`` 并输出一行 ``logger.info``——**只记异常类别名与路径，绝不记
        文件片段**（JSONDecodeError 的消息会回显内容）。返回**原始
        dict** 而非 :class:`TuiConfig`，因为写入路径需要保留未知键。
        """
        try:
            with open(config_path, encoding="utf-8") as handle:
                raw_text = handle.read()
        except OSError as exc:
            logger.info(f"tui config file not readable at {config_path}: {type(exc).__name__}")
            return {}

        try:
            raw = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.info(f"tui config file is not valid JSON at {config_path}: {type(exc).__name__}")
            return {}

        if not isinstance(raw, dict):
            logger.info(f"tui config file must hold a JSON object at {config_path}")
            return {}

        return raw

    @classmethod
    def _narrow(cls, raw: dict) -> "TuiConfig":
        """把一份 JSON 对象逐字段收窄为 :class:`TuiConfig`（永不抛异常）。"""
        auth = raw.get("auth")
        username = auth.get("username") if isinstance(auth, dict) else None
        access_token = auth.get("access_token") if isinstance(auth, dict) else None
        env = raw.get("env")

        return cls(
            version=raw.get("version"),
            agent_code=raw.get("agent_code"),
            username=username,
            access_token=access_token,
            env=env if isinstance(env, dict) else None,
        )

    @classmethod
    def _resolve_path(cls, path: str | None) -> str | None:
        """解析读取/写入所用的**单文件**路径；``None`` 表示走两级作用域。"""
        return path or os.environ.get(cls.CONFIG_FILE_OVERRIDE) or None

    @classmethod
    def read(cls, path: str | None = None) -> "TuiConfig":
        """读取进程级 TUI 配置。永不抛异常；见类 docstring。

        解析顺序：显式 ``path`` → :attr:`CONFIG_FILE_OVERRIDE` → 两级作用域
        （用户空间 ``~/.bkai/bkai_tui.json``，再叠加项目空间
        ``<AGENT_TUI_CWD>/.bkai/bkai_tui.json``，**项目覆盖用户**）。

        给定显式 ``path`` / override 时只读该文件（单文件语义，不叠加两级作用域）。
        """
        config_path = cls._resolve_path(path)

        if config_path:
            return cls._narrow(cls._read_one(config_path))

        # 用户空间 → 项目空间。项目覆盖用户：以项目那层的原始 dict 覆盖用户那层。
        merged = dict(cls._read_one(cls.user_path()))
        project_path = cls.project_path()

        if project_path:
            merged.update(cls._read_one(project_path))

        return cls._narrow(merged)

    # ------------------------------------------------------------- 写入

    @classmethod
    def _write_payload(cls, mutate: "Callable[[dict], None]", path: str | None) -> None:
        """读现有原始 payload → 交给 ``mutate`` 修改 → 写回磁盘。

        **逐字保留未知键**：从磁盘上已有的原始 dict 开始（不是 :class:`TuiConfig`
        实例），因此本类只认识的三个键之外的键不会被丢弃。

        与 §1 的 ``LocalDeclarations.write`` 同一约定：**不调用** ``os.makedirs``——
        配置路径是显式的，为它创建目录树会是一种意外；父目录缺失时抛异常并上报。
        """
        config_path = cls._resolve_path(path) or cls.user_path()
        payload = dict(cls._read_one(config_path))

        mutate(payload)

        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def write_agent_code(self, code: str, path: str | None = None) -> None:
        """写入入口 ``agent_code``，保留磁盘上已有的其他所有键。

        只写 ``agent_code`` 一个键——绝不触碰/新增其他键。日志只记 ``kind``，绝不记
        取值（value 是用户输入，与 token 同纪律）。
        """
        payload_path = path if path is not None else self.path

        def _mutate(payload: dict) -> None:
            payload["agent_code"] = code

        self._write_payload(_mutate, payload_path)
        logger.info("wrote tui config: kind=agent_code")

    def write_access_token(self, token: str, path: str | None = None) -> None:
        """写入 ``auth.access_token``（**唯一放行 token 落盘的位置**），保留其他所有键。

        日志只记 ``kind``，**绝不**记 token 取值。
        """
        payload_path = path if path is not None else self.path

        def _mutate(payload: dict) -> None:
            auth = payload.get("auth")

            if not isinstance(auth, dict):
                auth = payload["auth"] = {}

            auth["access_token"] = token

        self._write_payload(_mutate, payload_path)
        logger.info("wrote tui config: kind=access_token")

    # ------------------------------------------------------------- repr

    def __repr__(self) -> str:
        """显式覆盖：**绝不回显 token / env 取值**——token 只报 ``set`` /
        ``unset``，``env`` 只报键数（``env`` 可能携带凭据，逐键列出也不行）。

        dataclass 默认 repr 会把 ``access_token`` 全文打出，因此此处必须覆盖。
        """
        return (
            f"TuiConfig(version={self.version}, agent_code={self.agent_code!r}, "
            f"username={self.username!r}, "
            f"access_token={'set' if self.access_token else 'unset'}, "
            f"env={len(self.env)} key(s))"
        )
