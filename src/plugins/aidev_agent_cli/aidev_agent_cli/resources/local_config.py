"""本地配置的**叶子**：per-agent 声明（``agents/${code}.json``）与 MCP 定义（``mcp.json``）。

本模块只装两件事，以及一个环境开关：

* :class:`LocalConfig` / :class:`BkAiAgentInfo` —— per-agent 声明式配置的读写；
* :class:`McpConfig` / :class:`McpResolver` —— ``mcp.json`` 的定位、读取、合并与 mtime 读缓存；
* :func:`platform_mcp_disabled` —— 丢弃平台 MCP 绑定的逃生开关（读环境变量，不属于解析）。

为什么是**叶子**（依赖方向）
----------------------------
``resources`` 层被拆成三个文件，是为了断开一个真实的循环：

::

    local_config.py（叶子）←  loaders.py  ←  platform.py

``LocalResourceManager``（在 ``platform.py``）在自己的 ``__init__`` 里装配 loader 链，而链首的
``PlatformConfigLoader`` 又必须回指那个 manager 取 per-call 的 ``agent_code`` / ``version``。
同一个文件时那只是模块内引用；拆包后就是模块级循环 import。因此按**被依赖方向**切：
本模块不 import ``loaders`` / ``platform`` / ``runtime``，只 import ``utils``。

这条纪律由 AST 断言强制（见 ``tests/test_layer_graph.py``），不是口头约定。

两个文件、两套词汇（为何 MCP 有两份）
--------------------------------------
``agents/${code}.json`` 的 ``mcp_server_config.mcpServers`` 是**声明**（名称，可为 ``{}`` 占位，
TUI 会写）；``mcp.json`` 的 ``mcpServers`` 是**定义**（手写，携带 transport/url/command）。
两者对合并而言都是「追加」：同名先到先得，唯一的例外是「完整定义可以补全 ``{}`` 占位」。

token 纪律（不变）
-----------------
``LocalConfig`` 的字段面**不含**任何 token 类键——因此「把 token 放进 per-agent 配置」在结构上
做不到（字段面由 :attr:`LocalConfig.FIELD_ALIASES` 的键集推导，写入载荷只由白名单构造）。
唯一放行 token 落盘的位置是 ``bkai_tui.json`` 的 ``auth.access_token``
（:class:`aidev_agent_cli.runtime.settings.BkAiConfig`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from logging import getLogger
from pathlib import Path
from typing import Any

from ..utils.logging import BKAI_HOME_DIRNAME

logger = getLogger(__name__)

# ============================================================================
# §1  本地声明层 —— <BKAI_HOME>/agents/${agent_code}.json（per-agent，用户空间与项目空间两级）
#     承载平台 AgentConfig 的**全量形状**，由 worker 读，部分由 TUI 写（/models /mcps 等）。
#     所有者：BkAiAgentInfo。
# ============================================================================

"""本地声明式配置：**每个 agent 一个**、由 worker 读写的 JSON 文件。

这是本地配置故事中的**声明**那一半。平台 ``retrieve_agent_config`` 的输出仍是
base，它知道应用实际绑定了什么。本文件只是让本地用户能够声明“使用*这个* agent
代码、*这个*模型，并添加*这些* MCP server / 知识库”，而无需修改 ``.env.tui``
或要求平台改动。

从“进程级单文件 ``agent.json``”到“per-agent ``agents/${code}.json``”
------------------------------------------------------------------
本层曾经只有一个固定文件名 ``agent.json``（用户/项目两级）。261014-urj 判定那混淆了
"进程级 TUI 配置"与"per-agent 配置"两个概念，于是：

* **TUI 配置（进程级）** 迁往 :class:`aidev_agent_cli.runtime.settings.BkAiConfig`（``bkai_tui.json``）——
  只管"主 Agent 是谁"（``agent_code``）与身份认证（``auth``）。
* **per-agent 配置（本层）** 改为按 ``agent_code`` 逐文件读取：
  ``~/.bkai/agents/${code}.json`` 与 ``<AGENT_TUI_CWD>/.bkai/agents/${code}.json``
  ——**用户空间与项目空间对称**，都用 ``agents/`` 子目录。``agent.json`` 这个固定
  文件名**退役**。

  这一收窄正是父子 agent 不串味的**结构性**保证：父子读的是**不同的文件**，物理上
  不可能互相污染，原先的运行时门控（``_declaration_scope``）因此**删除**。

* **平台全量形状**：字段名与结构对齐平台 ``AgentConfig``（``agent_id`` /
  ``tenant_id`` / ``space_id`` / ``prompt_setting`` / ``mcp_server_config`` …）。
  不是子集，也不是旧的窄 NAMES 形状。

编辑前三项值得先读的决定
------------------------

**1. 用 JSON，不用 YAML。**

选择 JSON 是有意为之：标准库 ``json`` 已经存在，YAML 会引入 PyYAML 依赖，而且
这个文件本来就是给同一批编辑 ``.env.tui`` 的人手工编辑的。它与
:func:`aidev_agent_cli.config.load_env_file` 属于同一类东西——一个小巧的、由人编辑、由
worker 自行解析的本地文件——因此并非本代码库中的新概念。

**2. 文件缺失或损坏不致命。**

这与 ``load_env_file`` 完全一致，也是本地文件读取方面最强的既有先例：文件不存在
或不可读时产生**空**结果并输出**一行** stderr。文件不存在是常态——多数用户从不
创建它——而平台配置本身完全可用。若抛异常，用户忘记的某个 JSON 文件中的笔误就会
让整个 TUI 崩溃。

容错是逐字段的，而非全有或全无：某个字段类型不对时只把它收窄为空值，其余字段照常
加载。一个 ``"agent_code": 123`` 不应该静默丢弃用户的 MCP 列表。

**3. token 类键在结构上被拒绝，而不仅是未在文档中提及。**

访问 token 只存在于进程内。为此不该在注释里写“不要在这里放 token”，而是让该文件
*无法*承载 token：:class:`LocalConfig` 没有 token 字段（其字段面完全由
:attr:`LocalConfig.FIELD_ALIASES` 的键集推导，因此“加 token 字段”会在
``TestLocalConfigShape`` 上立刻失败），因此文件中的 token 无法进入返回值；
:meth:`BkAiAgentInfo.write` 只从白名单键构造写入载荷，因此 token 也无法被写回。
见 :attr:`BkAiAgentInfo.REJECTED_SECRET_KEYS` 与
:attr:`BkAiAgentInfo.DECLARATION_FIELDS`。

**261014-urj 的 D4 只放行了 ``bkai_tui.json`` 的 ``auth.access_token``**（见
:class:`aidev_agent_cli.runtime.settings.BkAiConfig`），**未触及本层**——agent 级配置仍结构性拒绝 token。

这种非对称是有意为之：**读取**失败静默降级（平台配置继续可用），而**写入**失败
抛异常。因为用户输入 ``/models <code>`` 却得不到“什么也没保存”的反馈，正是应当
避免的。

永不记录取值——只记录键名与 ``kind``。"""

#: 凭据族键名。结构上被 :class:`LocalConfig` 拒绝（无对应字段）+ 读取时上报 +
#: 写入白名单永不包含。模块级常量，使“字段面不得出现这些名字”这条性质可被测试按
#: 名字断言，而**不是** ``LocalConfig`` dataclass 的字段。
REJECTED_SECRET_KEYS = ("access_token", "bk_token", "bk_ticket")


@dataclass(frozen=True)
class LocalConfig:
    """本地声明。空实例表示“未声明任何内容”。

    承载平台 ``AgentConfig`` 的**全量形状**，字段名与平台保持一套词汇。**没有**任何
    token 类字段——这正是“token 不可能存在于此处”成为类的性质而非约定的原因。
    """

    agent_id: str = ""
    tenant_id: str = "system"
    agent_code: str = ""
    agent_name: str = ""
    space_id: str = ""
    agent_type: str = "single"
    updated_by: str = ""
    service_catalogue: str = ""
    conversation_settings: dict = field(default_factory=dict)
    prompt_setting: dict = field(default_factory=dict)
    knowledgebase_settings: dict = field(default_factory=dict)
    mcp_server_config: dict = field(default_factory=dict)

    #: 字段名 → 别名。仅用于读取时收窄：旧窄形状的名字（``default_model``、
    #: ``mcp_servers`` 等）继续被接受，但**规范名只有一套**（``prompt_setting`` 等）。
    #: 这是唯一的别名表，使嵌套字段“按字段逐键合并”的键集与标量字段名都从一处派生。
    FIELD_ALIASES = {
        "default_model": "prompt_setting",
        "mcp_servers": "mcp_server_config",
        "knowledgebases": "knowledgebase_settings",
    }

    @classmethod
    def _field_aliases(cls) -> dict[str, str]:
        """字段名 → 别名表。经类访问，使子类/测试可替换。"""
        return cls.FIELD_ALIASES

    def is_empty(self) -> bool:
        """本覆盖层是否未声明任何内容（所有字段均为空/默认）。"""
        for each in dataclass_fields(self):
            value = getattr(self, each.name)

            if each.name == "tenant_id" and value == "system":
                continue

            if each.name == "agent_type" and value == "single":
                continue

            if isinstance(value, dict):
                if value:
                    return False
            elif value:
                return False

        return True


class BkAiAgentInfo:
    """per-agent ``agents/${agent_code}.json`` 的所有者：读取、合并、写入与
    :class:`LocalConfig` 的收窄。

    同时持有状态（可选显式路径）与操作，这正是方案 B 的要点：调用方不再需要在模块函数
    与 ``Session`` 之间重新拼装。

    **261014-urj：从"进程级单文件 ``agent.json``"收窄为"per-agent
    ``agents/${code}.json``"。** 父子 agent 各读自己的文件，路径隔离取代了原先的
    运行时门控（``_declaration_scope`` 已删除）。

    解析顺序由 :meth:`read` 实现：显式 ``path`` → ``AGENT_TUI_CONFIG_FILE`` →
    per-agent 两级作用域（用户空间 → 项目空间；给定时只读该路径）。五个常量
    （:attr:`CONFIG_FILE_OVERRIDE` / :attr:`AGENTS_DIRNAME` /
    :attr:`AGENT_CONFIG_SUFFIX` / :attr:`REJECTED_SECRET_KEYS` /
    :attr:`DECLARATION_FIELDS`）是**本节的配置面**，因此是类属性而非模块常量；测试
    相应按 ``BkAiAgentInfo.X`` monkeypatch。
    """

    #: 指向备用配置文件的环境变量（单文件语义）。设置后只读该文件。
    CONFIG_FILE_OVERRIDE = "AGENT_TUI_CONFIG_FILE"

    #: per-agent 配置所在的子目录名（``<UserHome>/.bkai/agents``、
    #: ``<AGENT_TUI_CWD>/.bkai/agents``）。用户空间与项目空间**对称**，都用这个子目录。
    AGENTS_DIRNAME = "agents"

    #: per-agent 配置文件的扩展名。文件名是派生值 ``${agent_code}.json``——
    #: 旧的固定名 ``agent.json`` 已**退役**（261014-urj D3）。
    AGENT_CONFIG_SUFFIX = ".json"

    #: 携带用户启动 TUI 时所在目录的环境变量。与 ``McpResolver.CWD_ENV`` 同一语义
    #: ——项目空间那一层据此定位；未设置时省略该层，**不**回退到 ``os.getcwd()``。
    CWD_ENV = "AGENT_TUI_CWD"

    #: 该环境变量携带 ``agent_code``，解析顺序优先于配置文件（见
    #: :meth:`resolve_agent_code`）。
    AGENT_CODE_ENV = "AGENT_TUI_AGENT_CODE"

    #: 本地配置可承载的字段名（规范名集合）。**不含**任何 token 类键——由
    #: :class:`LocalConfig` 的字段面推导，因此两处不可能漂移。
    LOCAL_CONFIG_KEYS = tuple(each.name for each in dataclass_fields(LocalConfig))

    #: 看起来像凭据、因此即使用户写入也会被拒绝的键。见模块常量
    #: :data:`REJECTED_SECRET_KEYS` 的理由说明。**agent 级配置仍拒绝 token**——
    #: D4 只放行了 ``bkai_tui.json``（:class:`aidev_agent_cli.runtime.settings.BkAiConfig`），未触及此处。
    REJECTED_SECRET_KEYS = REJECTED_SECRET_KEYS

    #: 单数资源族 → （``LocalConfig`` 字段, 是否为集合）。写入入口的分发表——使用
    #: 映射而非 if/elif 链，使四个族及其标量/集合性质在一处可读。
    #:
    #: 键正是 ``config.set`` 白名单中的 ``kind`` 取值，也是 ``switchModel`` /
    #: ``switchAgent`` / ``switchMcp`` / ``switchKnowledge`` 回调所依据的名称。
    #: 四种拼写都是必需的。字段名已按新的全量形状更新：模型写进
    #: ``prompt_setting``（嵌套），知识库写进 ``knowledgebase_settings``（嵌套）。
    DECLARATION_FIELDS = {
        "models": ("prompt_setting", False),
        "agents": ("agent_code", False),
        "mcps": ("mcp_server_config", True),
        "knowledge": ("knowledgebase_settings", True),
    }

    def __init__(self, path: str | None = None) -> None:
        #: 显式路径，或 ``None`` 表示走 env / per-agent 两级作用域解析。``read()``
        #: 未传参时生效。
        self.path = path

    # ------------------------------------------------------------- 路径

    @staticmethod
    def _is_valid_agent_code(code: str) -> bool:
        """``agent_code`` 是否可作为**单个文件名**安全使用（T-261014urj-03）。

        路径穿越防护：含路径分隔符（``/`` / ``\\``）、``..``、空串或以 ``.`` 开头的值
        一律拒绝——否则 ``../`` 会越出 ``agents/`` 目录读写任意文件。校验放在路径派生
        之前，:meth:`read` / :meth:`write` 都经它。
        """
        if not isinstance(code, str) or not code:
            return False

        if os.sep in code or "/" in code or "\\" in code:
            return False

        return not (code in (".", "..") or code.startswith("."))

    @classmethod
    def user_agents_dir(cls) -> str:
        """用户空间 per-agent 目录 ``~/.bkai/agents``。"""
        return str(Path.home() / BKAI_HOME_DIRNAME / cls.AGENTS_DIRNAME)

    @classmethod
    def project_agents_dir(cls) -> str | None:
        """项目空间 per-agent 目录 ``<AGENT_TUI_CWD>/.bkai/agents``，未设 CWD 时为 ``None``。"""
        project_root = os.environ.get(cls.CWD_ENV, "").strip()

        if not project_root:
            return None

        return os.path.join(os.path.expanduser(project_root), BKAI_HOME_DIRNAME, cls.AGENTS_DIRNAME)

    @classmethod
    def user_path(cls, code: str) -> str:
        """用户空间 per-agent 配置路径 ``~/.bkai/agents/${code}.json``。"""
        return os.path.join(cls.user_agents_dir(), f"{code}{cls.AGENT_CONFIG_SUFFIX}")

    @classmethod
    def project_path(cls, code: str) -> str | None:
        """项目空间 per-agent 配置路径 ``<AGENT_TUI_CWD>/.bkai/agents/${code}.json``，
        未设 CWD 时为 ``None``。

        未设置 CWD 时**省略**这一层，而不是回退到 ``os.getcwd()``：worker 的 cwd 是
        ``agent_tui/`` 模块根，回退会把模块目录当作用户项目来读（见 §2
        ``McpResolver.search_paths`` 中的同一坑）。``expanduser`` 容忍 ``~`` 写法。
        """
        agents_dir = cls.project_agents_dir()

        if agents_dir is None:
            return None

        return os.path.join(agents_dir, f"{code}{cls.AGENT_CONFIG_SUFFIX}")

    # ------------------------------------------------------------- 收窄

    @staticmethod
    def _text(value: object) -> str:
        """把单个字段收窄为字符串，若不是字符串则返回 ``""``。"""
        return value if isinstance(value, str) else ""

    @classmethod
    def _narrow_section(cls, value: object) -> dict:
        """收窄一个嵌套 section（``prompt_setting`` 等）的**内容**为 dict。

        规范键（``prompt_setting``）携带的就是 section 内容本身，因此直接收窄为 dict。
        非 dict 时返回 ``{}``——单字段降级，不影响其他字段。
        """
        return dict(value) if isinstance(value, dict) else {}

    @classmethod
    def _section_keys(cls) -> dict[str, frozenset[str]]:
        """嵌套 section 的已知子键集合，用于判断一个 dict 是否为裸 section。

        这是**唯一的**嵌套字段知识表：新增 section 只需在此加一项。
        """
        return {
            "prompt_setting": frozenset(
                {
                    "llm_code",
                    "fallback_model",
                    "non_thinking_llm",
                    "fallback_vision_model",
                    "temperature",
                    "llm_token_limit",
                    "max_tokens",
                    "tool_output_compress_thrd",
                }
            ),
            "conversation_settings": frozenset(),
            "knowledgebase_settings": frozenset({"knowledgebases"}),
            "mcp_server_config": frozenset({"mcpServers"}),
        }

    def _text_field_names(self) -> frozenset[str]:
        """值为标量的字段名集合（其余为 dict 字段）。"""
        return frozenset(
            each.name
            for each in dataclass_fields(LocalConfig)
            if not isinstance(getattr(LocalConfig(), each.name), dict)
        )

    def _narrow(self, raw: dict) -> LocalConfig:
        """把一份 JSON 对象逐字段收窄为 :class:`LocalConfig`。

        规范键优先；缺省时回退到该字段的别名（旧窄形状，如 ``default_model`` →
        ``prompt_setting``）。别名的值对该字段的**嵌套位置**有两种写法：标量
        （``"default_model": "m"`` → ``{"llm_code": "m"}``）或该 section 的裸内容
        （``"knowledgebases": ["k"]`` → ``{"knowledgebases": ["k"]}``）。两者都据此
        归一化到规范形状，因此**规范名只有一套**。
        """
        text_names = self._text_field_names()
        kwargs: dict[str, Any] = {}

        for name in text_names:
            if name in raw:
                kwargs[name] = self._text(raw[name])
            else:
                kwargs[name] = self._text(self._alias_value(name, raw))

        for name in self._section_keys():
            if name in raw:
                # 规范键：内容即 section 本身。
                kwargs[name] = self._narrow_section(raw[name])
            else:
                alias = self._alias_value(name, raw, present_only=True)
                kwargs[name] = self._alias_section(name, alias) if alias is not None else {}

        return LocalConfig(**kwargs)

    @staticmethod
    def _alias_value(field_name: str, raw: dict, *, present_only: bool = False) -> object:
        """取该字段别名的原始值；``present_only`` 时缺失返回 ``None``。"""
        for alias, canonical in LocalConfig.FIELD_ALIASES.items():
            if canonical == field_name and alias in raw:
                return raw[alias]

        return None if present_only else raw.get(field_name)

    @classmethod
    def _alias_section(cls, field_name: str, value: object) -> dict:
        """把别名的值归一化为规范 section 形状。"""
        if field_name == "prompt_setting":
            # 旧形状 ``default_model`` 是标量，落到该 section 的主键 ``llm_code``。
            text = cls._text(value)
            return {"llm_code": text} if text else {}

        if field_name == "knowledgebase_settings":
            items = [each for each in value if isinstance(each, str) and each] if isinstance(value, list) else []
            return {"knowledgebases": items} if items else {}

        if field_name == "mcp_server_config":
            # 旧形状 ``mcp_servers`` 是名称列表，无法表达定义——归一化为
            # ``{"mcpServers": {名称: {}}}`` 占位（定义由 mcp.json 提供）。
            names = [each for each in value if isinstance(each, str) and each] if isinstance(value, list) else []
            return {"mcpServers": {name: {} for name in names}} if names else {}

        return cls._narrow_section(value)

    @classmethod
    def _mcp_servers(cls, config: LocalConfig) -> dict[str, dict]:
        """从声明配置中取出 ``mcpServers`` 映射（拆开 ``mcp_server_config`` 信封）。

        命名沿用 SDK 对 FLAT ``AgentConfig.mcp_server_config`` 的约定：返回
        ``名称 → server 字典``。信封缺失或格式错误时返回 ``{}``。
        """
        envelope = config.mcp_server_config if isinstance(config.mcp_server_config, dict) else {}
        servers = envelope.get("mcpServers")

        return dict(servers) if isinstance(servers, dict) else {}

    # ------------------------------------------------------------- 读取

    def _read_one(self, config_path: str) -> LocalConfig:
        """读取**一个**配置文件。永不抛异常；失败时返回空配置 + 一行日志。"""
        try:
            with open(config_path, encoding="utf-8") as handle:
                raw_text = handle.read()
        except OSError as exc:
            # 平台配置是 base，继续可用。
            logger.info(f"config file not readable at {config_path}: {type(exc).__name__}; using platform config only")
            return LocalConfig()

        try:
            raw = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            # 只记录异常*名称*——JSONDecodeError 的消息会回显文件片段，
            # 而文件损坏正是我们不知道那个片段是什么内容的情形。
            logger.info(
                f"config file is not valid JSON at {config_path}: {type(exc).__name__}; using platform config only"
            )
            return LocalConfig()

        if not isinstance(raw, dict):
            logger.info(f"config file must hold a JSON object at {config_path}; using platform config only")
            return LocalConfig()

        for key in type(self).REJECTED_SECRET_KEYS:
            if key in raw:
                # 仅键名。取值是凭据，而这行是日志。
                logger.info(f"ignoring {key!r} in the local config: the access token is session-only (D-12)")

        # 逐字段收窄：单个坏字段只降级它自己。
        return self._narrow(raw)

    @staticmethod
    def _merge_dict(base: dict, overlay: dict) -> dict:
        """递归合并两个 dict：``overlay`` 的值覆盖 ``base`` 的标量，dict 逐层递归。"""
        merged = dict(base)

        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = BkAiAgentInfo._merge_dict(merged[key], value)
            else:
                merged[key] = value

        return merged

    def _merge(self, base: LocalConfig, overlay: LocalConfig) -> LocalConfig:
        """把 ``overlay`` 合并到 ``base`` 上：标量覆盖，dict 字段逐键（递归）合并。"""
        kwargs: dict[str, Any] = {}

        for each in dataclass_fields(LocalConfig):
            name = each.name
            lower = getattr(base, name)
            upper = getattr(overlay, name)

            if isinstance(lower, dict):
                kwargs[name] = self._merge_dict(lower, upper)
            else:
                kwargs[name] = upper if upper not in ("", None) else lower

        return LocalConfig(**kwargs)

    def read(self, code: str | None = None, path: str | None = None) -> LocalConfig:
        """读取 per-agent 本地声明式配置。永不抛异常；见模块 docstring。

        解析顺序：显式 ``path`` → :attr:`AGENT_TUI_CONFIG_FILE` →
        per-agent 两级作用域（用户空间 ``~/.bkai/agents/${code}.json``，再叠加项目空间
        ``<AGENT_TUI_CWD>/.bkai/agents/${code}.json``，**项目覆盖用户**）。

        ``code`` 缺省是**合法**的（快照/测试路径给出显式 ``path``，或调用方只关心空配置）：
        既无 ``code`` 又无 ``path``/override 时返回空 :class:`LocalConfig`——**不再**
        静默回退到别处（旧的"进程级 ``agent.json``"这一层已随 per-agent 化删除）。

        ``agent_code`` 含路径穿越字符（``/`` / ``..`` 等）时拒绝读取并返回空配置 + 一行
        日志（T-261014urj-03）。

        文件缺失、不可读或无法解析为 JSON 对象时，该层贡献空配置并输出一行 stderr，
        仅指明失败*类别*——绝不包含其内容，因为损坏文件中可能含有敏感信息的片段。

        标量字段：项目非空值覆盖用户值。嵌套 dict 字段（``prompt_setting`` /
        ``conversation_settings`` / ``knowledgebase_settings`` / ``mcp_server_config``）：
        **逐键递归合并**，因此项目可以只覆盖用户的一个子键，其余子键保留。
        """
        config_path = path or self.path or os.environ.get(type(self).CONFIG_FILE_OVERRIDE)

        if config_path:
            # 显式单文件语义：只读该文件，不叠加 per-agent 两级作用域。
            return self._read_one(config_path)

        if code is None:
            # 无 code、无 path/override：per-agent 模型下没有"默认文件"可读。
            return LocalConfig()

        if not type(self)._is_valid_agent_code(code):
            logger.info(f"rejecting unsafe agent_code for per-agent config path: {code!r}")
            return LocalConfig()

        # 用户空间 → 项目空间。项目覆盖用户。
        merged = self._read_one(type(self).user_path(code))
        project_path = type(self).project_path(code)

        if project_path:
            merged = self._merge(merged, self._read_one(project_path))

        return merged

    # ------------------------------------------------------------- 写入

    def write(self, kind: str, value: str, path: str | None = None, code: str | None = None) -> None:
        """写入一项 per-agent 声明式覆盖，保留磁盘上已有的其他所有键。

        ``kind`` 是单数资源族（``"models"`` / ``"agents"`` / ``"mcps"`` /
        ``"knowledge"``），经 :attr:`DECLARATION_FIELDS` 映射到 :class:`LocalConfig`
        字段。标量 REPLACE——模型与 agent code 各只有一个，第二次选择覆盖第一次。
        集合 APPEND 并去重——本地列表只增不减，因为 UI 没有“移除”入口，而列表的
        读-改-写不该由一条单行命令承担。

        路径解析顺序：显式 ``path`` → ``self.path`` → :attr:`AGENT_TUI_CONFIG_FILE` →
        ``<user_agents_dir>/${code}.json``。**261014-urj：``code`` 与 ``path`` 缺一不可**——
        per-agent 模型下没有"进程级单文件"可写；两者皆缺时**响亮失败**（见下）。

        与 :meth:`read`（把缺失文件视为空配置）不同，无法完成的写入会 RAISE。读取失败会
        降级到平台配置，用户甚至可能察觉不到；而写入失败意味着一次显式切换静默地没有
        生效，这是不被允许的。调用方（worker RPC → 状态行）负责呈现该错误。

        永不写入 token 类键：下面的载荷仅由白名单字段名构建，因此 token 在结构上
        无法进入该文件。日志行只携带 ``kind``——绝不包含取值，绝不包含路径。
        """
        if kind not in type(self).DECLARATION_FIELDS:
            raise ValueError(f"unknown declaration kind: {kind!r}")

        config_path = path or self.path or os.environ.get(type(self).CONFIG_FILE_OVERRIDE)

        if not config_path:
            if code is None:
                # per-agent 模型：写入必须知道是哪个 agent，否则只能是静默的错文件。
                raise ValueError("writing a per-agent declaration requires an agent_code")

            if not type(self)._is_valid_agent_code(code):
                raise ValueError(f"unsafe agent_code for per-agent config path: {code!r}")

            config_path = type(self).user_path(code)

        field_name, is_collection = type(self).DECLARATION_FIELDS[kind]

        # 从磁盘上已有内容开始，这样无关的键不会被丢弃；文件缺失或损坏时从空开始，
        # 与读取路径的容错一致。
        current = self.read(path=config_path)
        payload = self._payload(current)

        if is_collection:
            # 集合族：追加到 section 内的集合子键。``mcp_server_config`` 的
            # ``mcpServers`` 是**映射**（名称 → 定义，可携带完整定义），因此新名称以
            # ``{}`` 占位；``knowledgebase_settings`` 的 ``knowledgebases`` 是列表。
            nested_key = type(self)._collection_key(field_name)
            section = payload[field_name]
            existing = section.get(nested_key)
            existing_map = dict(existing) if isinstance(existing, dict) else {}
            existing_list = list(existing) if isinstance(existing, list) else []

            if type(self)._collection_is_map(field_name):
                updated = existing_map if value in existing_map else {**existing_map, value: {}}
            else:
                updated = existing_list if value in existing_list else [*existing_list, value]

            payload[field_name] = {**section, nested_key: updated}
        elif (sub_key := type(self)._scalar_section_key(field_name)) is not None:
            # 标量族但落在嵌套 section 内（``models`` → ``prompt_setting.llm_code``）。
            payload[field_name] = {**payload[field_name], sub_key: value}
        else:
            # 顶层标量族（``agents`` → ``agent_code``）。
            payload[field_name] = value

        # 创建父目录（``~/.bkai/agents/`` 等）：**per-agent 化之后必须这样做**。
        # 原注释写「配置路径是显式的，为它创建目录树会是一种意外」——那在路径只有
        # 一个显式 ``.agent_tui.json`` 时成立；现在路径是**推导**出来的
        # （``~/.bkai/agents/${code}.json``），新机器上该目录必然不存在，于是**首次**
        # ``/models`` 写入必定 ``FileNotFoundError``。日志文件路径（``utils``）早已
        # 采用同样的 ``mkdir(parents=True, exist_ok=True)`` 约定。用户发起的写入失败
        # 必须暴露——这里的选择是**让它成功**，而不是让一个缺失的目录变成用户可见的错。
        parent = os.path.dirname(config_path)

        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        # 仅 ``kind``——取值是用户输入，遵循与 token 相同的日志纪律。
        logger.info(f"wrote local declaration: kind={kind}")

    @staticmethod
    def _collection_key(field_name: str) -> str:
        """集合族写入嵌套 dict 时使用的子键（``mcp_server_config`` → ``mcpServers``）。"""
        return {
            "mcp_server_config": "mcpServers",
            "knowledgebase_settings": "knowledgebases",
        }[field_name]

    @staticmethod
    def _collection_is_map(field_name: str) -> bool:
        """该集合族是否为**映射**（名称 → 定义）而非列表。

        ``mcpServers`` 是映射，因此新名称以 ``{}`` 占位、可被完整定义补全；其余集合
        是纯名称列表。
        """
        return field_name == "mcp_server_config"

    @classmethod
    def _scalar_section_key(cls, field_name: str) -> str | None:
        """标量族落入嵌套 section 时使用的子键；非嵌套字段返回 ``None``。

        ``models`` 现在是 ``prompt_setting`` 里的一个子键（``llm_code``），不再是
        顶层标量——这是字段名从 ``default_model`` 变成 ``prompt_setting.llm_code``
        的落点。``agents`` 仍是顶层标量（``agent_code``）。
        """
        return {"prompt_setting": "llm_code"}.get(field_name)

    @staticmethod
    def _payload(config: LocalConfig) -> dict[str, Any]:
        """从 :class:`LocalConfig` 构造**仅含白名单字段名**的写入载荷。

        这是结构性拒绝凭据的落点：键集完全由 :class:`LocalConfig` 的字段面推导，
        因此 token 类键在结构上无法出现在写入载荷中。
        """
        return {
            "agent_id": config.agent_id,
            "tenant_id": config.tenant_id,
            "agent_code": config.agent_code,
            "agent_name": config.agent_name,
            "space_id": config.space_id,
            "agent_type": config.agent_type,
            "updated_by": config.updated_by,
            "service_catalogue": config.service_catalogue,
            "conversation_settings": dict(config.conversation_settings),
            "prompt_setting": dict(config.prompt_setting),
            "knowledgebase_settings": dict(config.knowledgebase_settings),
            "mcp_server_config": dict(config.mcp_server_config),
        }


# ============================================================================
# §2  MCP 定义层 —— mcp.json
#     DEFINITION 那一半。完整的分 server 定义，手工编写，TUI 从不写入。
#     所有者：McpResolver（含逐文件 mtime 读缓存）。
# ============================================================================

"""本地 ``mcp.json``：标准 MCP server 定义，由 worker 读取。

这是本地配置故事中的**定义**那一半，与 §1 的**声明**那一半并列。
它们刻意是两个文件、两套词汇：

=====================================================  =====================================
``agents/${code}.json`` → ``mcp_server_config.mcpServers``  ``mcp.json`` → ``mcpServers``
=====================================================  =====================================
本地（per-agent，用户/项目两级）的 MCP server 映射             完整的逐 server 定义（手写）
可由 ``/mcps`` 追加名称（以 ``{}`` 占位）                手工编写，TUI 从不写入
也可携带完整定义                                        携带 SDK 需要的一切
=====================================================  =====================================

为何两者并存：``agents/${code}.json`` 里由 ``/mcps`` 追加的名称以 ``{}`` 占位——足以让名称
成立，却从不足以真正连接到任何东西。``mcp.json`` 承载真实 MCP 客户端所需的
transport/url/command，既不必要求平台改动，也不必把该结构放进一个 TUI 会在每次
``/mcps`` 切换时重写的文件。**两者对合并而言都是「追加」**（用户裁决）：同名条目
先到先得，只有先出现的 ``{}`` 占位可被后出现的完整定义补全。

路径，以及为何不是“用户当前所在目录”
------------------------------------
``mcp.json`` 从多个位置发现并合并，优先级由低到高（:meth:`McpResolver.search_paths`）：

1. ``~/.bkai/mcp.json``——用户空间，与 ``bkai_tui.json`` / 日志 / checkpoints
   同一个家。机器本地状态不锚在安装位置上（安装后的 worker 没有可用的
   "仓库根"）。
2. ``<AGENT_TUI_CWD>/.bkai/mcp.json``——用户启动 TUI 时所在的项目。优先级
   最高，因此项目可以覆盖用户全局定义中同名的 server。

它刻意不是 ``os.getcwd()/mcp.json``。worker 启动时的 ``cwd`` 是 ``agent_tui/``，
所以 worker 内的 ``os.getcwd()`` 是模块根目录，而非用户的工作区——相对路径读取会
静默地查找错误的目录。用户真正的工作区通过 ``McpResolver.CWD_ENV`` 传给我们。

编辑前四项值得先读的决定
------------------------

**1. 文件缺失或损坏不致命。**

与 :meth:`BkAiAgentInfo.read` 和 ``aidev_agent_cli.config.load_env_file`` 相同的约定：每个
候选路径被独立读取，缺失、不可读或无法解析的文件只贡献**一行** stderr 而不贡献
任何内容，其他候选照常加载。文件不存在是常态——多数用户从不创建这些文件，而平台
自身的 MCP 绑定没有它们也完全可用。若抛异常，用户忘记的某个 JSON 文件中的笔误
就会让整个 TUI 崩溃。

容错是逐字段的，而非全有或全无：一个格式错误的 server 条目只丢弃它自己，其余照常
加载。

**2. ``transport`` 是推导出来的，因为标准 ``mcp.json`` 并没有这个键。**

``langchain_mcp_adapters`` **要求** ``transport``，而约定俗成的 ``mcp.json`` 形态
对 stdio server 使用 ``command``、对 HTTP server 只使用 ``url``。本仓库钉住的
版本也没有 ``transportType`` 别名，因此 :meth:`McpResolver.normalise_server` 直接
丢弃该键而非翻译它——保留客户端会忽略的键就是在数据里说谎。

因此映射关系为：``command`` → ``stdio``，否则 ``url`` → ``streamable_http``，
两者都没有的 server 被丢弃并输出一行指明其名称。若没有最后这个分支，SDK 会在之后
加载工具时、在某一轮对话中才抛异常——离用户真正要修的那个文件远得多。

**3. 定义是合并 *追加*（ADD），不是覆盖。**

``mcp.json`` 与 ``agents/${code}.json``、平台绑定三者对合并而言同属「追加」：同名条目先到先得，
后出现者绝不覆盖先出现者的定义。唯一的例外是「定义可以补全占位」——先出现者若只是
``{}``，后出现的完整定义会补全它。这是用户对该语义的裁决（见模块 docstring 与
:meth:`LocalResourceManager._merge_mcp_add_only`）。

**4. 机密就在本文件中，因此日志纪律是防线。**

真实的 ``mcp.json`` 会在 ``headers`` 里携带 ``bk_app_secret`` / ``access_token``
——本仓库的 ``tests/mcp.json`` 正是如此。此处绝不可记录 server 字典：只记录名称与
数量。这并非表面功夫：stderr 是 TUI 会读取的日志汇，凭据一旦到达那里就等同于
已被公开。

注意与 :meth:`McpResolver.describe_servers` 的非对称性：它*确实*会把完整定义传递出去。
那是为线上协议做出的、经过审阅的明确决定（TUI 要展示用户配置了什么）；
它绝不是放宽上述日志规则的许可。"""


#: 该环境变量会丢弃**所有**平台绑定的 MCP server。
#:
#: ``1`` / ``true`` / ``yes``（不区分大小写）表示开启；其他任何取值——包括未设置
#: ——都保留平台的绑定。
#:
#: 该开关为何存在，以及为何是环境变量
#: ---------------------------------
#: 平台自身的绑定通常经 ``retrieve_agent_config`` 到达，并构成合并的 base。
#: 本地的任何文件都无法移除其中一项，因为本地配置只做「追加」，从不表达「不要这个」：
#: 名称列表（它无法表达“不要这个”），也因为当某轮对话不带 ``specific_resources``
#: 时（本 worker 正是如此）会加载 ``config.mcp_server_config`` 的**全部**内容。
#: 因此，端点已失效的平台绑定 MCP server（被移除的网关路由、过期的空间代理）在
#: 客户端侧无从规避，而代价是整轮对话：SDK 用 ``asyncio.gather`` 抓取 MCP 工具
#: 列表且没有逐 server 超时，因此一个无响应的 server 会阻塞 agent 构建，直到其
#: socket 放弃为止。
#:
#: 这个开关正是为这种情况准备的逃生通道。它是一种刻意的 STOMP 而非过滤器：
#: 全有或全无，不持久化，且设在环境变量而非配置文件中，因此不会被误认为用户已
#: 保存设置的一部分。
DISABLE_PLATFORM_MCP_ENV = "AGENT_TUI_DISABLE_PLATFORM_MCP"

#: :data:`DISABLE_PLATFORM_MCP_ENV` 中表示“开启”的取值。
_TRUTHY = ("1", "true", "yes", "on")


def platform_mcp_disabled() -> bool:
    """是否应完全丢弃平台自身的 MCP 绑定。

    在调用时读取而非导入时读取，因此进程可以重新配置而无需重启
    （测试也因此可以切换它）。

    刻意保持为**模块级函数**：测试按 ``resource_manager.platform_mcp_disabled()``
    调用它，且 §3 的 ``get_agent_config`` 也按这个模块级名字引用。
    """
    return os.environ.get(DISABLE_PLATFORM_MCP_ENV, "").strip().lower() in _TRUTHY


#: 标准 ``mcp.json`` 嵌套其 server 所用的键。
# ============================================================================
# §2  MCP 定义层 —— McpResolver
#     ``mcp.json`` 的定位、读取、合并、mtime 读缓存与投影，全部属于一个对象。
#     McpConfig 是它产出的值对象，保留在模块级（被 §3 与调用方按类型引用）。
# ============================================================================

"""``mcp.json`` 的全部逻辑集中在一个类里。

为何收拢
--------
此前本节是 14 个模块级名字、约 460 行：常量、5 个 ``_`` 前缀的实现函数、
3 个公开纯函数，外加 ``McpResolver`` 本身。读一条配置要跨 5 个函数跳转，而
“谁持有状态”只能从注释里读出来。现在全部收进 :class:`McpResolver`：

* **定位**（``search_paths`` / ``default_path``）—— 类属性与 staticmethod
* **读单个文件**（``read_one_file`` → ``extract_servers`` → ``narrow_servers``
  → ``normalise_server``）—— 私有 staticmethod，只在本类内被调用
* **合并**（``load_all``）—— 私有 staticmethod
* **状态**（``_config`` / ``_cache_key`` / ``stamp``）—— 实例属性与方法
* **投影**（``describe_servers``）—— staticmethod。跨来源的配置快照投影
  （``build_config_snapshot``）曾也在此列，几经搬家（模块级
  ``project_config_snapshot``）后于 261015 **合并进**
  :meth:`LocalResourceManager.get_config_snapshot`（§3）——快照是
  平台/本地/mcp.json 三个来源的装配，归编排者管，不归“mcp.json 所有者”管。

``McpConfig`` 刻意留在模块级：它是**值**而非行为，被 ``AgentConfig`` 的装配
（§3）与调用方按类型标注引用，塞进 resolver 只会让类型引用绕路。

常量为何是类属性
----------------
``DEFAULT_MCP_FILE`` / ``CWD_ENV`` 等是**本节的配置面**，不是全局常量。
放进类后 ``McpResolver.DEFAULT_MCP_FILE`` 明确表达了归属，测试的 monkeypatch
目标也从模块改为类（``monkeypatch.setattr(McpResolver, "DEFAULT_MCP_FILE", ...)``）。
``platform_mcp_disabled`` 留在模块级——它读的是环境变量，不属于 mcp.json 解析。
"""


@dataclass(frozen=True)
class McpConfig:
    """``mcp.json`` 中的 server。空实例表示“未声明任何内容”。

    ``servers`` 是 ``名称 → server 字典``——FLAT 结构，且该形态在本模块之外也有
    意义：``AgentConfig.mcp_server_config`` 必须保持扁平，因为 ``chat.py`` 用
    ``config.mcp_server_config.get(each)`` 解析具体的 MCP 资源。若那里是
    ``{"mcpServers": {...}}`` 载荷，会把每个具体 MCP 静默地过滤为 ``None``，
    因此拆封在这里、在读取时一次性完成。
    """

    servers: dict[str, dict] = field(default_factory=dict)

    def names(self) -> list[str]:
        """server 名称，按文件顺序。用于日志与计数。"""
        return list(self.servers)

    def is_empty(self) -> bool:
        """文件是否未声明任何可用的 server。"""
        return not self.servers

    def server_definitions(self) -> dict[str, dict]:
        """扁平的 ``名称 → server 字典`` 映射，供配置合并使用。"""
        return dict(self.servers)


class McpResolver:
    """``mcp.json`` 的所有者：定位、读取、合并、投影，以及逐文件 mtime 读缓存。

    唯一的公开入口是 :meth:`load`（带缓存）与 :meth:`seed`（直接推入）。
    其余方法都是私有实现——它们曾是模块级函数，收拢后不再暴露。

    mtime 键的语义（由测试钉住）：

    * ``stamp is not None and stamp == cache_key`` 短路，返回**同一个**对象实例
      （``test_ensure_resource_manager_reloads_only_when_the_file_changed`` 断言 ``is``）。
    * 缺失文件的 stamp 全为 ``None``，与任何真实 mtime 都不同，因此文件的出现或消失
      会在**两个方向**上使缓存失效。
    * ``stamp is not None`` 守卫防止“无文件”在首次调用时短路（``_cache_key`` 初值也是
      ``None``，没有守卫时缺失文件会被当作已缓存返回）。
    """

    #: 用户空间的 ``mcp.json``（``~/.bkai/mcp.json``）。优先级最低的候选。
    #:
    #: 与 ``bkai_tui.json``、日志、checkpoints 同一个家——机器本地状态不锚在
    #: 安装位置上（安装后的 worker 没有可用的"仓库根"）。
    #:
    #: 类属性而非模块常量：这是本节可被替换的配置面，测试按
    #: ``McpResolver.DEFAULT_MCP_FILE`` monkeypatch 它。
    DEFAULT_MCP_FILE = str(Path.home() / BKAI_HOME_DIRNAME / "mcp.json")

    #: 携带用户启动 TUI 时所在目录的环境变量。
    #:
    #: 这是*用户*的工作区到达 worker 的途径。worker 自己无法发现它
    #: （``os.getcwd()`` 是模块根目录），因此该值由此环境变量发布。worker 在其后
    #: 追加 :attr:`PROJECT_DIRNAME` 与 :attr:`MCP_FILENAME` 来定位项目级 ``mcp.json``。
    CWD_ENV = "AGENT_TUI_CWD"

    #: 项目空间内存放 ``mcp.json`` 的子目录（``<project>/.bkai/``）——与
    #: :class:`BkAiConfig <aidev_agent_cli.runtime.settings.BkAiConfig>` 的两级作用域同构。
    PROJECT_DIRNAME = BKAI_HOME_DIRNAME

    #: 在 :attr:`PROJECT_DIRNAME` 内查找的文件名。
    MCP_FILENAME = "mcp.json"

    #: 标准 ``mcp.json`` 嵌套其 server 所用的键。
    SERVERS_KEY = "mcpServers"

    #: 本模块合成或移除的键。``transport`` 是 ``langchain_mcp_adapters`` 坚持要求的
    #: 那个；``transportType`` 是约定俗成的 ``mcp.json`` 写法所用的键，而该适配器会
    #: 忽略它。
    TRANSPORT_KEY = "transport"
    TRANSPORT_TYPE_KEY = "transportType"

    #: 可推导的 ``transport`` 取值，以及暗示每一者的键。
    #: 有序：第一个匹配的判别键生效，因此 ``command``（stdio）先于 ``url`` 被检查。
    #: 同时携带两者的 server 是格式错误的；将其视为 stdio 与所有 MCP 客户端的优先级
    #: 一致。
    _TRANSPORT_BY_DISCRIMINATOR = (("command", "stdio"), ("url", "streamable_http"))

    def __init__(self, path: str | None = None) -> None:
        #: 显式路径：给出时 :meth:`load` 只读该文件，否则合并 :meth:`search_paths`。
        self.path = path
        #: 最近一次解析结果，或 ``None``（尚未读取）。
        self._config: McpConfig | None = None
        #: 读取时所用的逐文件 ``(path, mtime_ns | None)`` 键。
        self._cache_key: tuple[tuple[str, int | None], ...] | None = None
        #: :meth:`seed` 已置位。置位后 :meth:`load` 直接返回种子、绕过 mtime 缓存
        #: ——这是注入缝（测试/显式覆盖）的既有语义，防止 lazy 读取把种子冲掉。
        self._seeded: bool = False

    # ------------------------------------------------------------- 定位

    @classmethod
    def default_path(cls) -> str:
        """用户空间的默认 ``mcp.json`` 路径（``~/.bkai/mcp.json``）。

        没有备选路径：该文件只属于我们，缺失就只是空配置。这是
        :meth:`search_paths` 中优先级最低的一项，而不是"mcp.json 在哪里"的
        全部答案。
        """
        return cls.DEFAULT_MCP_FILE

    @classmethod
    def search_paths(cls) -> list[str]:
        """所有 ``mcp.json`` 候选路径，按优先级由低到高排列。

        同名 server 由靠后的条目胜出，因为 :meth:`load_all` 按顺序合并它们：

        1. :attr:`DEFAULT_MCP_FILE`——用户空间 ``~/.bkai/mcp.json``。
        2. ``<AGENT_TUI_CWD>/.bkai/mcp.json``——用户启动时的项目。

        当 :attr:`CWD_ENV` 未设置时，项目级候选被省略。此时回退到 ``os.getcwd()``
        不只是缺失，而是错误的：worker 的 cwd 是 ``agent_tui/`` 模块根目录，
        那会静默地把模块自身目录当作用户的项目来读取。

        即使文件不存在也会返回路径；如何处理缺失是读取方的职责，而需要标记该列表
        （:meth:`stamp` 的 mtime 缓存）的调用方同样需要缺失的那几项。
        """
        paths = [cls.DEFAULT_MCP_FILE]

        project_root = os.environ.get(cls.CWD_ENV, "").strip()

        if project_root:
            paths.append(os.path.join(os.path.expanduser(project_root), cls.PROJECT_DIRNAME, cls.MCP_FILENAME))

        return paths

    # --------------------------------------------------------- 读单个文件

    @classmethod
    def normalise_server(cls, name: str, raw: object) -> dict | None:
        """推导出 ``langchain_mcp_adapters`` 所需的键，或返回 ``None`` 表示丢弃它。

        返回一个浅拷贝，其中 ``transport`` 已解析、``transportType`` 已移除；
        server 自身的键（``url``、``command``、``args``、``env``、``headers``、
        ``selected_tools``、``credential_type`` 等）原样透传。由 SDK 的
        ``construct_mcp`` 解释这些键，它才是决定消费哪些键的权威。

        ``None`` 表示“该条目无法连接”——要么它不是 JSON 对象，要么既没有
        ``command``（stdio）也没有 ``url``（HTTP）。调用方记录其名称并丢弃它；
        这里用返回值而非抛异常，是本模块容错约定的一部分。
        """
        if not isinstance(raw, dict):
            return None

        # 复制一份，这样调用方修改结果也触及不到 ``McpConfig.servers``
        # ——dataclass 是 frozen 的，但其中作为取值的 dict 不是。
        server = dict(raw)

        # 丢弃而非翻译：该适配器版本没有它的别名，留下它只会在数据里放入一个
        # 客户端会忽略的键。
        server.pop(cls.TRANSPORT_TYPE_KEY, None)

        if not isinstance(server.get(cls.TRANSPORT_KEY), str) or not server[cls.TRANSPORT_KEY]:
            derived = next(
                (
                    transport
                    for discriminator, transport in cls._TRANSPORT_BY_DISCRIMINATOR
                    if server.get(discriminator)
                ),
                None,
            )

            if derived is None:
                # 仅 NAME。``raw`` 可能含有带凭据的 headers，而这行是日志
                # （见 §2 决定 4）。
                logger.info(f"dropped mcp server {name!r}: no 'command' and no 'url', so no transport can be derived")

                return None

            server[cls.TRANSPORT_KEY] = derived

        return server

    @classmethod
    def extract_servers(cls, raw: dict) -> dict | None:
        """拆开标准的 ``{"mcpServers": {...}}`` 外层信封。

        返回内层映射，信封缺失或格式错误时返回 ``None``——此时调用方改为把文档本身
        当作映射，因此裸的 ``{name: {...}}`` 文件仍能加载。

        ``None`` 这个答案正是让调用方能够记录非标准情形的原因：用户从别处复制、
        却没有加外层信封的文件，是“我的 server 全都不见了”最可能的原因，
        而此处的沉默会让它无从诊断。
        """
        nested = raw.get(cls.SERVERS_KEY)

        return nested if isinstance(nested, dict) else None

    @classmethod
    def narrow_servers(cls, raw_servers: dict) -> dict[str, dict]:
        """收窄每个条目，丢弃无法连接的那些。"""
        servers: dict[str, dict] = {}

        for name, value in raw_servers.items():
            if not isinstance(name, str) or not name:
                continue

            normalised = cls.normalise_server(name, value)

            if normalised is not None:
                servers[name] = normalised

        return servers

    @classmethod
    def read_one_file(cls, mcp_path: str) -> dict[str, dict]:
        """读取**一个** ``mcp.json``。永不抛异常；返回 ``名称 → server`` 映射。

        缺失、不可读或格式错误的文件贡献一个空映射和至多一行 stderr，仅指明失败
        *类别*——绝不包含其内容，对于损坏或含凭据的文件而言，那正是绝不能被回显的
        东西。这种单文件级的容错，使 :meth:`load_all` 能够合并多个候选，
        而不会因一个坏文件压制其余内容。
        """
        try:
            with open(mcp_path, encoding="utf-8") as handle:
                raw_text = handle.read()
        except OSError as exc:
            # 不存在是常态：平台自身的绑定继续可用。
            logger.info(f"mcp file not readable at {mcp_path}: {type(exc).__name__}; skipping")

            return {}

        try:
            raw = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            # 只记录异常*名称*——JSONDecodeError 的消息会回显文件片段，
            # 而文件损坏正是我们不知道那个片段是什么内容的情形。
            logger.info(f"mcp file is not valid JSON at {mcp_path}: {type(exc).__name__}; skipping")

            return {}

        if not isinstance(raw, dict):
            logger.info(f"mcp file must hold a JSON object at {mcp_path}; skipping")

            return {}

        raw_servers = cls.extract_servers(raw)

        if raw_servers is None:
            # 可以容忍，但绝不静默：没有 ``mcpServers`` 键的文档要么是裸映射文件
            # （没问题），要么是用户写错了（有问题），而从这里无法区分两者。
            logger.info(f"no {cls.SERVERS_KEY!r} key at {mcp_path}; reading the document itself as the server map")

            raw_servers = {key: value for key, value in raw.items() if key != cls.SERVERS_KEY}

        servers = cls.narrow_servers(raw_servers)

        if servers:
            # 仅名称——server 字典携带含凭据的 headers。
            logger.info(f"loaded {len(servers)} mcp server definition(s) from {mcp_path}: {sorted(servers)}")

        return servers

    # ------------------------------------------------------------- 合并

    @classmethod
    def load_all(cls, path: str | None = None) -> McpConfig:
        """读取并合并所有 ``mcp.json`` 候选。永不抛异常。

        给出显式 ``path`` 时只读取该文件。否则读取 :meth:`search_paths` 的每一项
        并按优先级由低到高合并，因此优先级更高的文件中同名的 server REPLACE 较低者
        ——与本模块对平台绑定所采用的“完整定义赢得替换”规则一致。

        每个候选由 :meth:`read_one_file` 独立读取，因此缺失或损坏的文件只移除
        它自己的贡献，其余照常加载。
        """
        if path is not None:
            return McpConfig(cls.read_one_file(path))

        merged: dict[str, dict] = {}

        for candidate in cls.search_paths():
            # 靠后的候选胜出：``update`` 覆盖先前同名的条目。
            merged.update(cls.read_one_file(candidate))

        return McpConfig(merged)

    # --------------------------------------------------- 缓存与读取入口

    def load(self, path: str | None = None) -> McpConfig:
        """读取工作区 ``mcp.json``，仅在某一个发生变化时重读。

        lazy 读取的统一入口：链上的 :class:`McpLoader` 与快照投影都经它取数，
        因此**消费即读取**——调用方（``ensure_manager`` 等）不再需要显式推送。
        mtime 门控让无关的重复拉取只付两次 ``stat`` 的代价，而不是重新解析。

        显式种子（:meth:`seed`）优先：置位后直接返回种子，不读盘。这是注入缝
        的既有语义（"绕过 mtime 缓存"），也防止 lazy 读取把测试注入的定义冲掉。

        ``stamp is not None`` 这个守卫的作用，是防止“无文件”在第一次调用时短路：
        ``self._cache_key`` 的初值也是 ``None``，没有这个守卫，缺失的文件会被
        当作 ``self._config`` 返回——仍然是 ``None``，即根本不是 ``McpConfig``，
        随后消费方会出于正确的结果、但错误的理由把它归一化为空配置。

        从不抛异常：底层的 :meth:`load_all` 降级为空配置并输出一行 stderr，
        因此不可读的文件不会让某一轮失败。
        """
        if path is not None:
            # 显式路径：不参与 mtime 缓存（一次性读取语义）。
            return self.load_all(path)

        if self._seeded:
            return self._config if self._config is not None else McpConfig()

        if self.path is not None:
            try:
                modified = os.stat(self.path).st_mtime_ns
            except OSError:
                modified = None
            stamp = ((self.path, modified),)
        else:
            stamp = self.stamp()

        if stamp is not None and stamp == self._cache_key:
            # 已缓存且文件未变——返回**同一个**对象（测试断言 ``is``）。
            return self._config if self._config is not None else McpConfig()

        config = self.load_all(self.path)
        self._config = config
        self._cache_key = stamp

        return config

    def seed(self, config: McpConfig | None) -> None:
        """直接设置已解析的 :class:`McpConfig`，绕过 mtime 缓存。

        显式覆盖/注入缝（测试用它替代落盘）。``None`` 表示“文件消失了”，
        重置为空配置而不是保留先前的定义。

        置位后 :meth:`load` 返回种子而非读盘（见该方法），因此注入的定义对
        lazy 读取同样可见——种子与 mtime 缓存不会互相覆盖。

        ``.config`` 属性是解析结果的读取口；快照投影经它。
        """
        self._config = config if config is not None else McpConfig()
        self._seeded = True

    @property
    def config(self) -> McpConfig:
        """当前已解析的定义。未种子/未读取时为空配置（永不为 ``None``）。"""
        return self._config if self._config is not None else McpConfig()

    @classmethod
    def stamp(cls) -> tuple[tuple[str, int | None], ...]:
        """:meth:`load` 所读每个文件的 ``(path, mtime_ns)`` 身份。

        在此解析而非从加载器传出，这样加载器保持单一返回值及其“从不抛异常”的契约。

        :meth:`search_paths` 的每个候选各一项，顺序相同，文件缺失时 mtime 为
        ``None``。``None`` mtime 是合法的 stamp——与任何真实 mtime 都不同——因此
        文件的出现或消失会在两个方向上使缓存失效。
        """
        stamp: list[tuple[str, int | None]] = []

        for path in cls.search_paths():
            try:
                stamp.append((path, os.stat(path).st_mtime_ns))
            except OSError:
                stamp.append((path, None))

        return tuple(stamp)

    # ------------------------------------------------------------- 投影

    @staticmethod
    def describe_servers(config: McpConfig | None) -> dict[str, dict]:
        """用于 ``config.info`` 快照的 ``mcpServers`` 投影。

        返回**完整**定义，包含 ``headers`` 与 ``env``。这是经过审阅的决定而非疏漏：
        TUI 的职责是向用户展示他们配置了什么，而一个被裁剪成 ``{transport, url}``
        的投影无法回答“我选了哪些工具”或“我的 header 生效了吗”。

        有两个必然后果，两者都必须成立：

        1. 这是**线上载荷**。其输出会离开 worker 进程，因此本函数**不是**脱敏边界，
           绝不能被误当作脱敏边界。
        2. 日志规则不变。任何调用本函数的地方都不得打印其结果（§2 决定 4）——
           只记录名称与数量。

        ``None`` 配置（无人推送过）产生 ``{}``，因此该线上字段始终是 JSON 对象，
        Node 侧的收窄不会遇到 ``undefined``。
        """
        return dict(config.servers) if config is not None else {}
