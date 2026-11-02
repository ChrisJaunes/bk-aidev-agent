"""worker 的资源管理器：在 SDK 基础上加一层本地覆盖和一个 token 槽位。

为何位于 worker 中
------------------
TypeScript 侧完全没有 ResourceManager 抽象——资源只以 RPC 载荷（``agent.info`` / ``agent.llms``）的形式到达 TUI。
``ResourceManager`` 是Python SDK 的概念：``BaseResourceManager`` 是一个 ABC，
唯一的抽象方法是 ``get_client()``，默认实现 ``AgentResourceManager`` 覆写的正是它。
``worker/agent_factory.py`` 原本内联了它的一个子类（``TuiAgentResourceManager``）；
本模块就是该类独立出来的归宿，因为它现在还多了两项职责。

不是对 SDK 的重新实现
---------------------
SDK 中与 manager 相关的功能全部保持继承：
``retrieve_agent_config``、``construct_tool``、``construct_mcp``、``knowledge_query``、会话方法族以及 ``get_client()`` 本身。
本类替换的只是配置与凭据的*来源*。

分层收拢（方案 B）
------------------
本模块曾是一个扁平模块：60+ 个模块级定义，配置读取、MCP 解析、工作区爬取各自散落在
模块作用域，而它们的**状态**却住在 ``SessionState`` 上（mcp mtime 缓存、快照输入、文件索引）。
每次使用都要由调用方把对象图重新拼一遍。

现在三个关注点各自由一个**同时持有状态与操作**的协作者拥有，全部为模块级纯 Python 类：

* :class:`LocalDeclarations`（§1）—— ``agents/${code}.json``（per-agent，用户/项目两级）的读/写与 :class:`LocalConfig`。
* :class:`McpResolver`（§2）—— ``mcp.json`` 的搜索、合并与 MCP 投影，**以及**逐文件 mtime 读缓存。跨来源的配置快照装配住在 :meth:`LocalResourceManager.get_config_snapshot`（§3），不归它管。
* :class:`WorkspaceIndex`—— 工作区第一层（文件 + 目录）的 ``os.scandir`` 实时读取。

:class:`LocalResourceManager`（§3）退化为编排者：它只暴露 ``get_agent_config()``、
``get_config_snapshot()``、``set_access_token()`` 与三个协作者属性。

SDK 基类在模块顶层导入（曾经的懒解析已移除）
------------------------------------------
:class:`LocalResourceManager` 是模块级 ``class``，基类 ``AgentResourceManager`` 在**本模块
顶层**导入。这意味着导入本模块会连带导入 ``aidev_agent``（及其 langchain / mcp / langsmith
依赖，实测约 1.2 s）——这是**刻意接受的代价**。

曾经这里用 ``exec`` + 字符串类体延迟基类解析，以守住 ``worker/__init__.py`` 的
“导入 worker 不导入 SDK”承诺。该机制已移除，原因是用户判定 **resource_manager 是对外接口，
必须能被正常导入**：``exec`` 出来的类没有 IDE 跳转与类型检查，门面与真类是不同对象
（``is`` 比较反直觉），且 ``exec globals`` 需手工维护名字清单。实测延迟导入只省下
worker 启动的首帧时间（54 ms vs 1236 ms），而代价是接口形态异常——两害相权，
**接口正常优先**。

三个协作者（:class:`LocalDeclarations` / :class:`McpResolver` / :class:`WorkspaceIndex`）
本就不导入 SDK 符号，与本次改动无关。

兼容层已删除
------------
被折进协作者的操作，其模块级转发壳**不存在**——用户明确选择“直接删除，同步改调用点”。
仍在模块作用域的只有**数据/类型契约**（dataclass、常量、异常）。此前还有两个有意的
monkeypatch 接缝（``resolve_ripgrep``、``shutil``），随 ripgrep 链一并删除（见 §4 的说明）。

三个配置来源，以及 MCP 为何全都是「追加」
--------------------------------------------
一轮对话的 MCP 集合由四层组成：平台自身的绑定作为 BASE（永不移除）是其中一层，另外三层
是**追加**（ADD）——用户空间 ``agents/${code}.json``、项目空间 ``agents/${code}.json``
（二者在 :class:`LocalDeclarations.read` 里按 per-agent 合并）、以及 ``mcp.json`` 的完整
DEFINITION（见 §2）。

**MCP 是追加，不是覆盖（用户裁决）。** 所有来源并集，同名条目**绝不互相替换**：
先出现者保留其身份与顺序。唯一的例外是「定义可以补全占位」——先出现者若只是 ``{}``
占位、而后出现者带完整定义，则用后者补全。旧模型里 ``mcp.json`` 对平台同名的整体
REPLACE 语义已经取消（见 :meth:`LocalResourceManager._merge_mcp_add_only`）。

token 与模型的取值为何都不住在本类
----------------------------------
manager 不是进程级单例——每次构建都会新建（``ensure_manager`` 一个；``build_agent`` 曾
又建一个），因此挂在本类实例上的可变槽位会**随 manager 分裂成多份**。

* ``current_model``（用户在 ``/models`` 选的模型）是**进程级**状态，因此住在
  ``SessionState`` 上，由 :class:`RuntimeInstanceLoader` 经注入的 ``runtime`` 读取
  （见 :class:`RuntimeStateSurface`）。
* token 经 ``/login`` 进入，随后经 :meth:`LocalResourceManager.set_access_token` 变更
  ——构造函数是入口，setter 是变更途径。它随 manager 携带，因此**调用方必须把同一个
  manager 交给所有需要它的路径**，而不是各自新建。

``mcp.json`` 遵循 token 的形态而非模型的形态：进入方式相同，但通过 :meth:`LocalResourceManager.refresh` 变更
因为用户编辑文件后不应需要重启 worker 才能生效。

刻意保留的两项既有行为
----------------------
清除审批设置；保留 ``chat_model`` 覆盖。两者现在各自多生长出一层，而不是被重写
——本地声明位于每轮显式选择*之下*、平台自身取值*之上*。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from logging import getLogger
from pathlib import Path
from typing import Any, Callable, Protocol

from aidev_agent.packages.resource_manager.agent import AgentResourceManager

from .utils import BKAI_HOME_DIRNAME

logger = getLogger(__name__)


# ============================================================================
# §1  本地声明层 —— <BKAI_HOME>/agents/${agent_code}.json（per-agent，用户空间与项目空间两级）
#     承载平台 AgentConfig 的**全量形状**，由 worker 读，部分由 TUI 写（/models /mcps 等）。
#     所有者：LocalDeclarations。
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

* **TUI 配置（进程级）** 迁往 :class:`aidev_agent.headless.config.TuiConfig`（``bkai_tui.json``）——
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
:func:`aidev_agent.headless.config.load_env_file` 属于同一类东西——一个小巧的、由人编辑、由
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
:meth:`LocalDeclarations.write` 只从白名单键构造写入载荷，因此 token 也无法被写回。
见 :attr:`LocalDeclarations.REJECTED_SECRET_KEYS` 与
:attr:`LocalDeclarations.DECLARATION_FIELDS`。

**261014-urj 的 D4 只放行了 ``bkai_tui.json`` 的 ``auth.access_token``**（见
:class:`aidev_agent.headless.config.TuiConfig`），**未触及本层**——agent 级配置仍结构性拒绝 token。

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


class LocalDeclarations:
    """per-agent ``agents/${agent_code}.json`` 的所有者：读取、合并、写入与
    :class:`LocalConfig` 的收窄。

    同时持有状态（可选显式路径）与操作，这正是方案 B 的要点：调用方不再需要在模块函数
    与 ``SessionState`` 之间重新拼装。

    **261014-urj：从"进程级单文件 ``agent.json``"收窄为"per-agent
    ``agents/${code}.json``"。** 父子 agent 各读自己的文件，路径隔离取代了原先的
    运行时门控（``_declaration_scope`` 已删除）。

    解析顺序由 :meth:`read` 实现：显式 ``path`` → ``AGENT_TUI_CONFIG_FILE`` →
    per-agent 两级作用域（用户空间 → 项目空间；给定时只读该路径）。五个常量
    （:attr:`CONFIG_FILE_OVERRIDE` / :attr:`AGENTS_DIRNAME` /
    :attr:`AGENT_CONFIG_SUFFIX` / :attr:`REJECTED_SECRET_KEYS` /
    :attr:`DECLARATION_FIELDS`）是**本节的配置面**，因此是类属性而非模块常量；测试
    相应按 ``LocalDeclarations.X`` monkeypatch。
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
    #: D4 只放行了 ``bkai_tui.json``（:class:`aidev_agent.headless.config.TuiConfig`），未触及此处。
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
            logger.info(
                f"config file not readable at {config_path}: {type(exc).__name__}; using platform config only"
            )
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
                merged[key] = LocalDeclarations._merge_dict(merged[key], value)
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

    @classmethod
    def resolve_agent_code(cls, config: LocalConfig, *, fallback: Callable[[], str] | None = None) -> str:
        """解析 ``agent_code``。顺序：环境变量 → 配置对象 → 平台默认。

        ``fallback`` 是最后一级（平台默认）的取值函数，由调用方提供——通常是
        manager 的 ``get_agent_code`` 或凭据里的 ``BKPAAS_APP_CODE``。缺省时返回
        ``""``。

        语义（261014-urj 后）：这是**入口 agent_code 解析的一级**——配置对象那一层
        现在由 :class:`aidev_agent.headless.config.TuiConfig` 提供（进程级决策），而不再是 per-agent
        的 ``agent_code`` 字段（门控随 per-agent 路径隔离一并删除，本方法不再被它
        调用）。环境变量（:attr:`AGENT_CODE_ENV`）仍优先于配置对象，配置对象仍优先于
        平台。配置对象层的缺失**不**立即失败——由最终合并结果的完整性保证“必填”语义。
        """
        env_value = os.environ.get(cls.AGENT_CODE_ENV, "").strip()

        if env_value:
            return env_value

        if config.agent_code:
            return config.agent_code

        return fallback() if fallback is not None else ""

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
        current = self.read(config_path)
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

与 :meth:`LocalDeclarations.read` 和 ``aidev_agent.headless.config.load_env_file`` 相同的约定：每个
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
    #: :class:`TuiConfig <aidev_agent.headless.config.TuiConfig>` 的两级作用域同构。
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
                logger.info(
                    f"dropped mcp server {name!r}: no 'command' and no 'url', so no transport can be derived"
                )

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
            logger.info(
                f"no {cls.SERVERS_KEY!r} key at {mcp_path}; reading the document itself as the server map"
            )

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

        stamp = self.stamp()

        if stamp is not None and stamp == self._cache_key:
            # 已缓存且文件未变——返回**同一个**对象（测试断言 ``is``）。
            return self._config if self._config is not None else McpConfig()

        config = self.load_all()
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


# ============================================================================
# §3  资源获取器 —— LocalResourceManager
#     消费 §1 与 §2。SDK 中与 manager 相关的功能全部保持继承。
# ============================================================================


class ManagerSurface(Protocol):
    """worker 对资源管理器的最低要求。

    声明在实现旁边，这样该约定与满足它的类不会彼此脱节。它存在的原因是：
    曾经有两处调用点用 ``hasattr`` 探测这些方法，在方法缺失时跳过推送——
    这让缺失的能力看起来像可用的能力。
    """

    def get_client(self) -> Any: ...

    def get_agent_code(self) -> str: ...

    def set_access_token(self, token: str) -> None: ...

    def retrieve_agent_config(self, agent_code: str, version: str | None = ...) -> dict[str, Any]: ...


class LocalResourceManager(AgentResourceManager):
    """``AgentResourceManager`` + **per-agent** 本地声明式覆盖层 + 会话 token 槽位。

    这是 §3 的**编排者**：它把 §1/§2 的协作者组合起来，并把它们的成果装进
    ``AgentConfig``。本地状态分布在两个协作者上（261014-urj 后 ``self.declarations``
    与 ``self.local`` 已删除——per-agent 配置由 loader 现场读盘）：

      * ``self.mcp`` —— :class:`McpResolver`（``mcp.json`` + mtime 读缓存）
      * ``self.files`` —— :class:`WorkspaceIndex`（工作区第一层实时读取）

    **进程级 vs per-agent（261014-urj 的核心概念分离）**：本类负责**单个 agent** 的
    资源与配置，读 ``agents/${agent_code}.json``。**进程级**的 TUI 配置（"主 Agent 是谁"
    + 身份认证）属 :class:`aidev_agent.headless.config.TuiConfig`（``bkai_tui.json``）。两者唯一的
    耦合点：per-agent 配置的写入目标是 ``TuiConfig.agent_code`` 所指那个 agent 的文件。

    ``self.mcp`` 是**解析器**，不是解析结果：把它命名为 ``mcp`` 保持了对调用方的
    既有词汇，同时它内部用 ``.config`` 暴露解析出的 :class:`McpConfig`。
    """

    def __init__(
        self,
        access_token: str = "",
        *,
        runtime: RuntimeStateSurface | None = None,
        **kwargs: Any,
    ) -> None:
        # 父类 __init__ 接受 access_token 并保存为 self.access_token；
        # resolve_access_token 会优先使用它而不是用户名查询。此前从未传入该参数，
        # 导致整条用户 token 通道都是失效的。
        super().__init__(access_token=access_token, **kwargs)

        #: 进程级运行时状态（``SessionState``）的**注入**引用。
        #:
        #: ``current_model`` 这类运行时取值住在进程级状态上而非本类：
        #: manager 每次构建都会新建（``ensure_manager`` 一个、``build_agent`` 曾经又
        #: 建一个），挂在实例上的槽位会随 manager 一起分裂成多份，于是「哪个是真源」
        #: 变成未决问题。
        #:
        #: 注入而非导入：``agent_runner`` 导入本模块，反向导入会成环，因此本模块只
        #: 按 :class:`RuntimeStateSurface` 这一结构化契约使用传入对象，不知道它的具体
        #: 类型。``None`` 表示「无进程级运行时」，读取处自然降级为空取值。
        self.runtime = runtime

        # 261014-urj D1/D7：构造参数从六项瘦身到只剩 access_token（+ **kwargs）。
        # 旧的 local / mcp / declarations / resolver / files 五个注入缝**全部移除**：
        #   * ``local``（进程级单例）→ per-agent 模型下"哪个 agent 的 local"本身有歧义，
        #     改由 :class:`LocalAgentConfigLoader` 在 ``apply`` 时按 ``raw["agent_code"]``
        #     现场读盘（删除 ``self.local``，D8/A-4）。
        #   * ``declarations`` → per-agent 配置由 loader 现场读；无单例可挂（删除
        #     ``self.declarations``）。
        #   * ``mcp``（一次性种子）→ 构造后 ``rm.mcp.seed(...)``（``seed`` 是既有公开 API）。
        #   * ``resolver`` → 构造后 ``rm.mcp = McpResolver(...)``（属性替换，零成本）。
        #   * ``files``（D7 覆盖 planner 的"保留 files"裁决）→ rm 自建；测试要注入时
        #     构造后替换 ``rm.files`` 并调 :meth:`_rebuild_workspace_files` 同步重建
        #     ``rm.workspace_files``（保住对象同一性断言，见该方法）。

        #: 工作区 ``mcp.json`` 的所有者（含 mtime 读缓存）。消费方经 ``load()``
        #: lazy 取数；测试经 ``seed`` 注入显式覆盖。
        self.mcp = McpResolver()
        #: 工作区文件列表的所有者：工作区第一层（文件 + 目录）的实时读取。
        #: ``SessionState`` 曾另持一个 ``WorkspaceIndex``（双实例陷阱），现已收归此处。
        self.files = WorkspaceIndex()
        #: 工作区文件加载器（§6）：**无状态适配**，包装的正是上面的 ``self.files``。
        #: 见 :meth:`_rebuild_workspace_files`——``files`` 被替换后必须调用它重建，
        #: 否则同一性（``loader._index is rm.files``）会静默失效。
        self.workspace_files: Any = None
        self._rebuild_workspace_files()

        #: 本次 ``retrieve_agent_config`` 拉取的 ``(agent_code, version)`` 槽位。
        #:
        #: **这是链中传递 per-call 参数的唯一机制。** ``PlatformConfigLoader`` 是链首
        #: 成员（见下），它需要知道本次拉取是哪个 ``agent_code`` / ``version``，但
        #: loader 的构造参数在 ``__init__`` 时固定（无 per-call 构造参数可承载）。因此
        #: ``retrieve_agent_config`` 在遍历链之前置此槽位、``finally`` 清空（T-261014urj-06）。
        self._platform_request: tuple[str, str | None] | None = None

        #: 平台加载器：持有 TTL 缓存，:meth:`retrieve_agent_config` 与
        #: :meth:`_raw_platform_config` **共用同一实例**，因此两者看到同一份平台配置
        #: （rta 裁决 B）。**链首就是它**——只创建一次，链首引用同一对象，否则 TTL 缓存
        #: 会分裂。
        self.platform_loader = PlatformConfigLoader(self)

        # :meth:`retrieve_agent_config` 按序应用的加载器链。顺序即优先级（低 → 高）：
        #   链首：平台取数 + 逃生清空（fetch + apply；261014-urj 由"输入"改为"链成员"）
        #   → 审批裁剪（固定裁剪，不属于六源）
        #   → per-agent 本地声明 → MCP 定义 → 知识库 → 运行内存（model 最高）。
        #
        # **链首为何是平台（261014-urj 裁决 (a)）**：原先平台是链的**输入**——
        # ``retrieve_agent_config`` 先 ``fetch`` 再**显式**调 ``apply``，之后才遍历其余 5 个
        # loader。把它真放进链后，那层"链外特例"（"apply 必须在遍历前显式调用"）退化为
        # **链序本身**，消除"第二事实来源"。四条必须保持的性质靠此链序保证：
        #   (1) ``platform_mcp_disabled()`` 的逃生清空仍在本地来源叠加**之前**——因为链首
        #       的 ``apply`` 先于 ``LocalAgentConfigLoader`` 执行（从"靠显式调用顺序"变为
        #       "靠链序"，语义等价且更难破坏）；
        #   (2) TTL 缓存仍与快照路径共用同一实例——``platform_loader`` 只有一个实例，链首
        #       即它，``_raw_platform_config`` 仍经链（即经它的 ``fetch``）；
        #   (3) ``_ensure_platform_keys`` 仍在 ``retrieve_agent_config`` 返回处无条件调用；
        #   (4) 失败语义不变——平台失败仍在 ``fetch`` 内传播（不经 ``try/except``），穿过
        #       ``retrieve_agent_config`` 到 ``super().get_agent_config`` 被包成 ``ValueError``；
        #       ``_raw_platform_config`` 自己 ``except → {}``。
        #
        # :class:`WorkspaceFileLoader` 现在是**链成员**（即 ``self.workspace_files``）：
        # 本地文件行由链自己并入 ``retrieve_agent_config`` 的 raw dict，调用方不必再补。
        # 它排在 ``RuntimeInstanceLoader`` **之前**——末位必须是运行时那一源（它优先级
        # 最高，有测试断言 ``kinds[-1]``）。
        self._loaders: list[Any] = [
            self.platform_loader,
            ApprovalLoader(),
            LocalAgentConfigLoader(self),
            McpLoader(self),
            KnowledgebaseLoader(self),
            self.workspace_files,
            RuntimeInstanceLoader(runtime),
        ]

    def _replace_loader(self, kind: type, replacement: Any) -> None:
        """把链中该类（若有）的实例替换为 ``replacement``，没有则不动。

        ``_loaders`` 持有的是**对象**，因此任何"重建某个 loader"的入口都必须同步更新
        链里的那一项，否则链会一直用旧对象——这类失效是静默的。

        ``_loaders`` 尚不存在时（``__init__`` 里本方法先于链的装配被调用）直接返回：
        那时链还没建，它随后会取到当前的 ``self.workspace_files``。
        """
        loaders = getattr(self, "_loaders", None)

        if loaders is None:
            return

        for index, loader in enumerate(loaders):
            if isinstance(loader, kind):
                loaders[index] = replacement

                return

    def _rebuild_workspace_files(self) -> None:
        """（重）建 ``self.workspace_files``，包装当前的 ``self.files``。

        **注入缝（D7 的正式替代）。** ``files`` 不再作为构造参数（D7：五参数全删），
        因此测试要替换工作区索引时改为：``rm.files = WorkspaceIndex(...)`` 后调用本方法，
        使 ``workspace_files._index`` 重新指向新索引。这样对象同一性
        （``loader._index is rm.files``）仍是**可断言的真实性质**，而不是静默失效——
        计划明确要求"不得只删测试"。
        """
        self.workspace_files = WorkspaceFileLoader(self.files)
        # 链里也必须是**这一个**：本方法是 ``files`` 被替换后的重建入口，若只换属性
        # 而不换链中的成员，链会一直读旧索引（新文件永远不出现）。
        self._replace_loader(WorkspaceFileLoader, self.workspace_files)

        #: per-agent 声明式配置的**写入口**（``agents/${code}.json``）。
        #: 读由 loader 链现场进行（:class:`LocalAgentConfigLoader`）；本实例只用于写
        #: （:meth:`write_declaration`）。它是 **per-agent** 配置的所有者，因此归本
        #: 管理器——"谁是主 agent"是进程级 TUI 配置的事，不在本类。
        self.declarations = LocalDeclarations()

    def set_access_token(self, token: str) -> None:
        """替换会话 token。``/login`` / ``/logout`` 的落点。

        只变更实例属性。``get_client()`` 每次调用都会重新读取它，因此下一轮即可
        生效，无需重启 worker——而清空 token 会让 client 回到纯 app-state 模式。
        """
        self.access_token = token or ""
        # 仅记录是否设置，绝不记录取值。
        logger.info(f"access token {'set' if self.access_token else 'cleared'}")

    def write_declaration(self, kind: str, value: str, code: str) -> None:
        """写入**本 agent** 的一条声明式覆盖（``agents/${code}.json``）。

        per-agent 配置的**写入口**（261014-urj 后续：此职责从 ``SessionState`` 收归
        管理器）。``/models`` / ``/mcps`` / ``/knowledge`` 的 ``config.set`` 落点。

        ``code`` 由调用方给出（当前主 agent 的 ``agent_code``）——管理器不猜"当前是
        谁"，因为它对应的是**单个 agent**；"谁是主 agent"是进程级 TUI 配置的事
        （见 :class:`aidev_agent.headless.config.TuiConfig`）。

        同步且阻塞（写文件）。文件不可读/不可写、或 ``code`` 含路径穿越字符时
        **抛异常**：这是用户发起的操作，失败必须暴露（与读取路径的合法静默降级
        相反）。

        ``kind`` 的语义与 ``LocalDeclarations.write`` 一致（标量替换、集合追加），
        本项目只做转发，使 ``LocalDeclarations`` 的**归属**留在本模块这一侧。
        """
        self.declarations.write(kind, value, code=code)

    @staticmethod
    def _merge_mcp_add_only(declared: dict[str, dict], incoming: dict[str, dict]) -> dict[str, dict]:
        """MCP 合并：**追加**（ADD），同名条目绝不互相覆盖。

        规则落地（用户裁决，见任务 A2/MCP 语义）：

        * 先到先得——``declared`` 中已有的名称保留其**身份与顺序**，``incoming`` 中
          同名者不覆盖。
        * 例外（“定义可以补全，但从不用第二个定义覆盖第一个定义”）：先出现者只是
          ``{}`` 占位、而后出现者带完整定义时，用后者**补全**该占位。

        返回新 dict，绝不原地修改输入（``declared`` 可能来自平台 config）。
        """
        merged = dict(declared)

        for name, definition in incoming.items():
            if name not in merged:
                merged[name] = definition
            elif not merged[name] and definition:
                # 先出现者是占位（``{}``），后出现者带定义 → 补全。
                merged[name] = definition

        return merged

    @staticmethod
    def _ensure_platform_keys(raw: dict[str, Any]) -> dict[str, Any]:
        """补全 ``super().get_agent_config`` **直接下标访问**的两个键（只补不删）。

        **这是新增的防御，不是在保留既有行为。** ``super().get_agent_config`` 对
        ``res["agent_name"]`` 与 ``res["knowledgebase_settings"]["knowledgebases"]``
        做**无兜底的直接下标访问**（``.../packages/resource_manager/base.py:450`` 与
        ``:456``；``mcp_server_config`` 之类走 ``.get(...)`` 有兜底）。生产平台的
        ``knowledgebase_settings`` 可能是 ``{}`` 或整个缺失，那样合并 dict 就没有
        ``knowledgebases`` 键 → ``KeyError`` 会在第一轮对话就杀死 TUI。

        语义：``setdefault``——**只补不删**，绝不 ``pop``；形状不对时归一为可用的默认
        值。代号代价（T-261014rta-01）：平台 dict 缺 ``agent_name`` 从**响亮崩溃**
        变成**静默空串**；判定为值得（TUI 崩溃远更糟），且 ``AgentConfig.agent_name``
        本就是必填。

        放在这里（:class:`LocalResourceManager` 的私有静态方法）而非
        :class:`PlatformConfigLoader` 内：它守卫的是 ``super()`` 的**读取**契约（在下游
        映射时才炸），而不是平台取数器的形状——取数器只负责原样取回平台 dict。
        """
        raw.setdefault("agent_name", "")

        kb = raw.setdefault("knowledgebase_settings", {})

        if not isinstance(kb, dict):
            kb = raw["knowledgebase_settings"] = {}

        kb.setdefault("knowledgebases", [])

        if not isinstance(kb["knowledgebases"], list):
            kb["knowledgebases"] = []

        mcp = raw.setdefault("mcp_server_config", {})

        if not isinstance(mcp, dict):
            mcp = raw["mcp_server_config"] = {}

        mcp.setdefault("mcpServers", {})

        if not isinstance(mcp["mcpServers"], dict):
            mcp["mcpServers"] = {}

        return raw

    def retrieve_agent_config(
        self, agent_code: str, version: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """平台 dict → loader 链 → 合并后的 dict（六源合并流水线的**新家**）。

        链原先住在 :meth:`get_agent_config`（已装配的对象层），在 ``super()`` 已产出的
        ``AgentConfig`` 上做后置写入。但 ``super().get_agent_config`` 内部是
        ``res = self.retrieve_agent_config(...)``（**虚分派**，
        ``.../packages/resource_manager/base.py:361``），随后从平台 dict 映射十余个
        字段。因此覆写**本方法**后，``super().get_agent_config`` 会在**合并后的 dict**
        上做映射——无需手工重跑装配。见 §6 顶部说明。

        261014-urj 裁决 (a)：**平台加载器现在是链首成员**（原为链的"输入"）。因此本方法
        **不再预取数**——它只把本次拉取的 ``(agent_code, version)`` 放进
        ``self._platform_request`` 槽位，然后遍历整条链（含链首的
        :class:`PlatformConfigLoader`）。内容顺序即优先级（低 → 高）：

            env → <repo>/.bkai/agents/${code}.json → ~/.bkai/agents/${code}.json → 平台

        （``env`` 由各 loader 读环境变量叠加；两份 ``agents/${code}.json`` 由
        :class:`LocalAgentConfigLoader` 按 per-agent 两级读取；平台由链首 fetch。）

        语义（三条都**保持** base 语义）
        ------------------------------
        * 平台失败**不在本方法**包装：异常穿过到 ``super().get_agent_config`` 才被包成
          ``ValueError("Failed to retrieve agent config: ...")``；平台失败仍在
          :meth:`PlatformConfigLoader.fetch` 内传播（不经 ``try/except``）；
        * :meth:`_raw_platform_config`（快照路径）自己 ``try/except → {}`` 降级；
        * TTL 缓存保留——取数经 :meth:`PlatformConfigLoader.fetch`。

        步骤：
        1. 置 ``self._platform_request = (agent_code, version)``（链首据此 fetch）。
        2. 按序遍历 ``self._loaders``，每个 loader ``merge_to_raw(raw)``（链首既 fetch
           又 apply；其余按序并入）。
        3. ``finally`` 清空 ``self._platform_request``（T-261014urj-06：槽位不跨调用残留）。
        4. 返回 :meth:`_ensure_platform_keys` 守卫后的 dict。

        ``**kwargs`` 与 base 一样**不进入缓存键**，当前**无可观测效果**（``fetch``
        不接受它，也不为它新增缓存键）——但也不丢掉后让调用方误以为它生效了：这里
        显式声明为无效果。
        """
        # 链首的 PlatformConfigLoader 需要知道本次拉取的 agent_code / version；
        # loader 无 per-call 构造参数，故经这个槽位传递（T-261014urj-06：finally 清空）。
        self._platform_request = (agent_code, version)

        try:
            raw: dict[str, Any] = {}

            for loader in self._loaders:
                raw = loader.merge_to_raw(raw)

            return self._ensure_platform_keys(raw)
        finally:
            self._platform_request = None

    def get_config_snapshot(self) -> dict[str, Any]:
        """只读覆盖视图所渲染的载荷。``SessionState.describe_config_snapshot`` 的输入。

        261015 二次收拢：这段投影曾先后住在
        :meth:`McpResolver.build_config_snapshot <McpResolver>` 与模块级
        ``project_config_snapshot`` 上；如今唯一的调用方就是本方法，直接合并至此
        ——快照的装配与 manager 的其余职责同住，不再有第二处定义。

        来源（全部经 ``self``，无外部输入）：

        * **平台一半**——:meth:`_raw_platform_config`（loader 链 +
          ``PlatformConfigLoader`` 的 TTL 缓存，自带降级：失败时 ``{}``，快照
          允许降级而 ``agent.info`` 传播，见 :meth:`describe_agent_config`）。
          261014-urj：本地声明经链合并进 ``raw``，因此 ``local`` 固定为空
          :class:`LocalConfig`——投影的本地取值一律从 ``raw`` 落地。
        * **工作区定义**——:meth:`McpResolver.load`（mtime 门控读缓存，与链上
          :class:`McpLoader` 同一实例）经 :meth:`McpResolver.describe_servers`。
        * **入口 ``agent_code``**——:meth:`get_agent_code`，空时回退 ``raw`` 的
          ``agent_code``。

        线上纪律（不因搬家而改变）：``mcpServers`` 携带**完整**定义（含带凭据
        ``headers``，经审阅的决定——浮层要展示用户配置了什么）；它是线上载荷、
        不是脱敏边界，此处及 Node 侧都不得记录它。``localMcps`` 是
        ``agents/${code}.json`` 的**名称**列表、``mcpServers`` 是 ``mcp.json`` 的
        **完整定义**映射——不同来源，合并会让"已声明但无法解析"与"已配置"无法
        区分。字段名 camelCase（线上载荷），来源标注（``local`` / ``platform``）
        由 Node 侧推导，worker 只报告事实。
        """
        payload = self._raw_platform_config()
        payload = payload if isinstance(payload, dict) else {}
        local = LocalConfig()

        def text(value: Any) -> str:
            return value if isinstance(value, str) else ""

        # --- 平台“已绑定”——基础层 ------------------------------------------
        prompt_setting = payload.get("prompt_setting") or {}
        current_model = text(prompt_setting.get("llm_code")) if isinstance(prompt_setting, dict) else ""

        mcp_config = payload.get("mcp_server_config") or {}
        bound_mcps = sorted(mcp_config.get("mcpServers") or {}) if isinstance(mcp_config, dict) else []

        kb_settings = payload.get("knowledgebase_settings") or {}
        raw_kbs = kb_settings.get("knowledgebases") if isinstance(kb_settings, dict) else None
        bound_kbs = [each for each in raw_kbs if isinstance(each, str)] if isinstance(raw_kbs, list) else []

        agent_name = text(payload.get("agent_name"))
        platform_agent_code = self.get_agent_code() or text(payload.get("agent_code"))

        # --- 本地声明式覆盖 --------------------------------------------------
        # ``local`` 固定空 :class:`LocalConfig`：本地声明已经链合并进 ``raw``。
        # 保留这段读取（而非硬编码空值）是为了与投影语义逐字对齐——空实例让
        # 所有本地取值为空，生效值自然落回 ``raw``。
        local_code = getattr(local, "agent_code", "") or ""
        local_model = ""
        prompt_section = getattr(local, "prompt_setting", None)
        if isinstance(prompt_section, dict):
            local_model = prompt_section.get("llm_code") or ""
        local_mcps = []
        mcp_section = getattr(local, "mcp_server_config", None)
        if isinstance(mcp_section, dict):
            raw_servers = mcp_section.get("mcpServers")
            local_mcps = list(raw_servers) if isinstance(raw_servers, dict) else []
        local_kbs = []
        kb_section = getattr(local, "knowledgebase_settings", None)
        if isinstance(kb_section, dict):
            raw_local_kbs = kb_section.get("knowledgebases")
            local_kbs = [each for each in raw_local_kbs if isinstance(each, str)] if isinstance(raw_local_kbs, list) else []

        # --- 工作区 mcp.json 定义 --------------------------------------------
        servers = McpResolver.describe_servers(self.mcp.load())

        return {
            # 浮层第一组渲染的生效值（青色加粗）。``raw`` 已含链合并的本地覆盖。
            "currentModel": local_model or current_model,
            "currentModelSource": "local" if local_model else "platform",
            "currentAgent": local_code or platform_agent_code,
            "currentAgentSource": "local" if local_code else "platform",
            "agentName": agent_name,
            # 每个浮层的第 2 组：本地声明项。
            "localAgents": [local_code] if local_code else [],
            "localMcps": local_mcps,
            "localKbs": local_kbs,
            # `/mcps` 与 `/knowledge` 的第 1 组：平台已绑定的条目。
            "boundMcps": bound_mcps,
            "boundKbs": bound_kbs,
            # mcp.json 定义。`localMcps` 的含义不变，消费它的代码行为不变。
            "mcpServers": servers,
        }

    def describe_agent_config(self, agent_code: str) -> dict[str, Any]:
        """``agent.info`` 的载荷：平台配置 + 本地覆盖 + 本地文件行。

        与 :meth:`get_config_snapshot` 对称——两者都是"由 manager 自己拼装、调用方只管
        委派"，差别只在**降级语义**：``agent.info`` 的平台失败必须**可见**，而覆盖视图
        允许静默降级为 ``{}``（见 :meth:`_raw_platform_config`）。

        两半，两条刷新规则：

        * **平台一半**经 :meth:`retrieve_agent_config`（即 loader 链 +
          :class:`PlatformConfigLoader` 的 TTL 缓存）。连续读取同一 ``agent_code`` 由 TTL
          去重，因此"HTTP 请求只发一次"的约束仍成立，只是窗口从"每进程一次"变为 TTL。
        * **本地一半**由 :class:`WorkspaceFileLoader` 在链中贡献，基于**实时**读取的工作区
          第一层（``os.scandir``，无缓存），因此新文件在紧接着的 ``agent.info`` 上就可见。
          它以 ``resources[]`` 中 ``type == "file"`` 的行到达，没有单独的 ``local_files`` 通道。

        降级边界（有意的，不是意外）：平台拉取失败时**不**直接吞掉——若本地一半有内容
        可说则只返回本地行，平台抖动不该顺带吞掉用户自己的工作区文件；只有当本地一半
        也为空时错误才逃逸，因此"坏平台 + 没有本地文件"仍然是响亮的失败。
        """
        try:
            return self.retrieve_agent_config(agent_code)
        except Exception:
            rows = self.workspace_files.project()

            if not rows:
                raise

            logger.info("agent config: platform fetch failed; serving local files only")

            return {"resources": list(rows)}

    def _raw_platform_config(self) -> dict[str, Any]:
        """合并后的配置 dict，供快照投影使用；失败时降级为 ``{}``（快照允许降级）。

        经 :meth:`retrieve_agent_config`（即**经 loader 链**）取数，因此快照的
        ``platform`` 半与 agent 实际运行所用的配置**同源**——浮层不再报告一个从未生效
        的值（裁决 (b)）。它仍经 :class:`PlatformConfigLoader` 的 TTL 缓存，与构建路径
        **共用同一份**（歧义 b 的裁决 B）。

        自带的降级语义**完整保留**：``retrieve_agent_config`` 失败时 ``except`` → ``{}``
        （loader 契约是「永不抛异常」，两者一致；缓存只缓存成功，所以失败时根本没写
        缓存，重试仍会重新拉取）。
        """
        agent_code = getattr(self, "app_code", "") or self.get_agent_code()

        try:
            return self.retrieve_agent_config(agent_code)
        except Exception as exc:  # noqa: BLE001 - 快照失败不能杀死 worker
            logger.info(f"config snapshot: platform fetch failed: {type(exc).__name__}: {exc}")

            return {}

    # ``get_agent_config()`` 刻意**不**覆写（rulings (a)）。
    #
    # 审批清空（now :class:`ApprovalLoader`）移进链后，一个只调 ``super()`` 的薄覆写
    # 没有任何编排职责：空洞转发与 §6 顶部所述「第二个事实来源」的问题同类，且将来
    # 有人把逻辑加回来会**重新引入**同一个 bug。链住在 :meth:`retrieve_agent_config`
    # 里，``super().get_agent_config`` 会调用它（虚分派），因此本地覆盖自动对
    # ``get_agent_config`` 及其所有下游消费者生效。

    # ``get_client()`` 刻意**不**覆写。该覆写范围由继承满足：父类实现每次调用都会
    # 重新组装，而 ``resolve_access_token`` 优先读取 ``self.access_token``——
    # 因此在 ``set_access_token`` 之后取得的 client 自然携带新 token，无需额外
    # 处理。添加覆写只会逐字复制父类实现，并造成关于 client 如何认证的第二个
    # 事实来源。

    @classmethod
    def build(
        cls,
        creds: dict[str, str],
        username: str,
        *,
        access_token: str = "",
        runtime: RuntimeStateSurface | None = None,
    ) -> "LocalResourceManager":
        """唯一的构造入口。

        ``SessionState.ensure_manager`` 是唯一调用点；集合凭据与用户名。
        保持为 classmethod 而非让调用方直接构造，是为了让“凭据 dict 的键名”这一知识
        只存在于一处（``BKPAAS_APP_CODE`` 等）。

        ``runtime`` 是进程级运行时状态（``SessionState``），供
        :class:`RuntimeInstanceLoader` 读取运行时取值（如 ``current_model``）。
        见 :attr:`LocalResourceManager.runtime`。

        261014-urj：``local`` / ``mcp`` 转发**已删除**——per-agent 配置由 loader 现场读，
        工作区 ``mcp.json`` 由 :class:`McpLoader` 在链上经 ``rm.mcp.load()`` lazy 读取
        （mtime 门控），无需调用方推送。
        """
        return cls(
            access_token=access_token,
            app_code=creds.get("BKPAAS_APP_CODE", ""),
            app_secret=creds.get("BKPAAS_APP_SECRET", ""),
            username=username,
            runtime=runtime,
        )


# ============================================================================
# §4  工作区文件列表 —— WorkspaceIndex
#     工作区**第一层**（文件 + 目录）的实时读取；唯一所有者是 LocalResourceManager。
#     DirectoryListing 是它产出的值对象，留在模块级。
# ============================================================================

"""工作区文件列表，以及它为何这样读。

只列第一层，且每次投影都重读（D-28 已推翻）
-------------------------------------------
本类曾被要求「每个 worker 进程只爬取一次并永久缓存」（D-28）：递归 ``rg --files``
遍历约 8.1 万个路径开销很大，因此结果被缓存，并配了一套就绪/重试/截断机制。
**该决定已被用户推翻**，理由是实测与语义两条：

* 实测（本机，2026-10-14）：递归 ``rg --files`` = **75 ms / 81,170 条**；
  第一层 ``rg --files --max-depth 1`` = 5 ms / 38 条；
  **第一层 ``os.scandir`` = 0.02 ms / 63 条（25 目录 + 38 文件）**。
  ``os.scandir`` 比 ``rg`` 子进程快约 250 倍，且天然同时给出文件与目录。
* 语义：agent 运行过程中新建文件很常见，缓存会让用户看不到新文件。

因此本类现在**不缓存**：每次读 ``agent.info`` 时重新 ``os.scandir`` 一次第一层。
读取开销 0.02 ms，重复读取无妨，且各方法保持自洽。

为何没有 ripgrep
----------------
``rg`` 只能列**文件**，而用户裁决「文件与目录都要」，故改用 ``os.scandir``。
连带地整条 ``rg`` 解析/下载/子进程链（``resolve_ripgrep`` / ``_cached_binary`` /
``_download_ripgrep`` / ``RIPGREP_VERSION`` / ``CACHE_ENV_VAR`` / ``_which_rg``）
**已删除**——它在本用途上不再需要，且删除即缩小供应链面（不再运行时下载可执行文件）。

为何没有截断与拒绝清单
----------------------
第一层最多几千条，永远触及不到旧 ``MAX_PATHS = 30_000``，故 ``truncated`` 是死字段
（用户裁决删除）。旧 ``DENYLIST_DIRS``（``node_modules`` / ``.git`` 等）也随之删除：
第一层模式下没什么可过滤的，顶层若正好有 ``node_modules`` 目录就会出现在列表里
——这是用户明确接受的。

D-27 的 no-silent-degrade **保留**
----------------------------------
失败（root 缺失、权限错误）仍必须说明原因，由 ``reason`` 单独承载。空工作区与健康
读取都说不出话（``reason == ""``），两者在结构上不区分——因为确实没有区别。

读取是同步的
------------
``os.scandir`` 是单次 syscall 的迭代，无子进程、无网络，故 :meth:`read` 是**同步**方法。
旧设计需要 ``asyncio.to_thread`` 正是因为 ``rg`` 是**子进程**；删掉子进程后该理由消失。
其调用方 ``describe_agent_config`` 已被 ``asyncio.to_thread`` 包住，第一层读取自然落在
那个线程里。

条目形状
--------
:meth:`read` 的 ``entries`` 元素是**第一层的名字**：

* 文件：``"README.md"``（第一层下只有顶层文件，故不含 ``/``）。
* 目录：**带结尾 ``/``**，如 ``"src/"``。

:meth:`rows_for` 把它们变成 ``resources[]`` 的 ``{"code", "name", "type": "file"}`` 行，
其中 ``code`` 是工作区相对路径。前端据此渲染 ``@`` 菜单的「文件」分组。

排序：目录在前，其次按名不区分大小写，再次按原名
（确定性平局裁决，不依赖宿主机 locale）。
"""


@dataclass(frozen=True)
class DirectoryListing:
    """一次工作区第一层读取的结果。

    替代旧的 :class:`CrawlResult`：后者承载 ``truncated`` / ``source``（ripgrep 渠道），
    在本模式下都不存在。

    ``entries`` 是**已排序**的 ``names``——目录条目带结尾 ``/``。读取不可用时为空列表
    （root 缺失或 ``OSError``），因此文件分组为空，而不是让 ``agent.info`` 失败。
    """

    entries: list[str] = field(default_factory=list)


class WorkspaceIndex:
    """工作区**第一层**（文件 + 目录）的实时读取。

    无状态：没有缓存槽、没有就绪标志、没有重试常量。唯一的状态是
    :attr:`WORKSPACE_ENV_VAR` / :meth:`workspace_root` 决定「哪个目录」。

    它的所有者是 :class:`LocalResourceManager`（``self.files`` +
    ``self.workspace_files``）——``SessionState`` 不再持有第二个实例。
    """

    #: 待读取目录的环境变量覆盖项。默认为进程的 CWD。
    WORKSPACE_ENV_VAR = "AGENT_TUI_WORKSPACE"

    @staticmethod
    def workspace_root() -> str:
        """待读取的目录：TUI 的工作目录，或环境变量覆盖项。"""
        return os.environ.get(WorkspaceIndex.WORKSPACE_ENV_VAR) or os.getcwd()

    def read(self, root: str | None = None) -> DirectoryListing:
        """读取工作区**第一层**（文件 + 目录），**同步**。

        ``root`` 缺省取 :meth:`workspace_root`。目录条目在名字后追加 ``/``；
        文件用原名。**不递归**。

        对普通的环境问题**从不抛异常**：root 缺失或 ``OSError``（权限等）产生一个
        **空** :class:`DirectoryListing`，因为 TUI 必须仍能启动（文件分组只是 ``@``
        的一个来源，不是前置条件）。调用方因此只会看到一个空的文件分组。

        ``entry.is_dir()`` 默认**不跟随**符号链接，避免目录环与越界遍历。
        """
        resolved = Path(root if root is not None else self.workspace_root()).expanduser()

        if not resolved.is_dir():
            return DirectoryListing()

        try:
            with os.scandir(resolved) as scanner:
                # 只取 ``entry.name``（**不含路径**）：名字不参与任何 shell/子进程调用，
                # 且第一层不需要完整路径（T-t1s-01）。
                entries = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in scanner]
        except OSError:
            # 权限等错误降级为空列表，绝不抛——TUI 仍要能启动（T-t1s-05）。
            return DirectoryListing()

        entries.sort(key=lambda name: (not name.endswith("/"), name.removesuffix("/").lower(), name.removesuffix("/")))

        return DirectoryListing(entries=entries)

    def rows_for(self, directory: str, listing: DirectoryListing | None = None) -> list[dict[str, Any]]:
        """``directory`` 对应的 ``{"code", "name", "type": "file"}`` 行。

        经 :meth:`read` 实时派生（不缓存），并在每次调用时**重新折叠**。

        ``listing`` 可选：传入已读好的 :class:`DirectoryListing` 即**不再扫描**，
        使需要同一份读取派生多项的调用方只扫一遍。

        ``directory == ""`` 表示工作区根目录——这是生产路径唯一使用的取值。只出现
        ``directory`` **下一层**的条目（深层路径只贡献其第一段）。目录条目在 ``name`` 中
        带结尾 ``/``；``type`` 恒为 ``"file"``（``agent-info.ts`` 原样透传 ``type``）。

        一条条目是目录有**两个**来源：它以结尾 ``/`` 显式声明（:meth:`read` 对第一层
        目录这样做），或它作为更深条目的前缀出现。第一层模式下前者是常态，后者仍保留
        以支持传入更深层的 :class:`DirectoryListing`。
        """
        prefix = [segment for segment in directory.replace("\\", "/").split("/") if segment]
        by_name: dict[str, dict[str, Any]] = {}

        for path in (listing if listing is not None else self.read()).entries:
            # 结尾 ``/`` 是本条目“是目录”的显式声明（read 对第一层目录如此产出）。
            declares_directory = path.endswith("/")
            segments = [segment for segment in path.replace("\\", "/").split("/") if segment]

            # 跳过任何不在 `directory` 之下的路径，以及与之同级者（文件不能是自己的
            # 父级）。同时跳过共享前缀但更短的路径。
            if len(segments) <= len(prefix):
                continue

            if any(segments[index] != segment for index, segment in enumerate(prefix)):
                continue

            name = segments[len(prefix)]
            is_directory = declares_directory or len(segments) > len(prefix) + 1

            # 同时以直接条目和前缀形式出现的名称是目录——前缀胜出，因为目录与文件
            # 不能同名。
            existing = by_name.get(name)

            if existing is not None and existing["name"].endswith("/"):
                continue

            by_name[name] = {
                "code": "/".join([*prefix, name]),
                "name": f"{name}/" if is_directory else name,
                "type": "file",
                # 排序键携带在行本身（并在下面剥离）：目录在前，然后按名称不区分大小写排序。
                # 不留在线上形态中，因为该行被定义为 ``{code, name, type}``。
                "_is_directory": is_directory,
            }

        ordered = sorted(
            by_name.values(),
            key=lambda row: (
                not row["_is_directory"],
                row["name"].removesuffix("/").lower(),
                row["name"].removesuffix("/"),
            ),
        )

        return [{key: row[key] for key in ("code", "name", "type")} for row in ordered]


# ============================================================================
# §6  加载器层 —— ConfigLoader 协议与六个实现
#     retrieve_agent_config 退化为「遍历加载器并按序 apply」。
#     每个加载器是对一个已有协作者（LocalDeclarations / McpResolver /
#     WorkspaceIndex / 平台 fetch / current_model）的适配器。
# ============================================================================

"""六源合并流水线的加载器层。

链住在哪里（本轮最关键的一处修正）
----------------------------------
链住在 :meth:`LocalResourceManager.retrieve_agent_config`——**平台原始 dict 层**，
而不是 ``get_agent_config``（已装配的 ``AgentConfig`` 对象层）。

原因是 SDK 的虚分派（``.../packages/resource_manager/base.py:361``）::

    def get_agent_config(self, agent_code, version=None, **kwargs) -> AgentConfig:
        res = self.retrieve_agent_config(agent_code, version=version, **kwargs)   # ← 虚分派
        ...
        return AgentConfig(
            agent_name=res["agent_name"],
            chat_model=prompt_setting.get("llm_code", ""),
            temperature=prompt_setting.get("temperature"),
            knowledgebase_ids=res["knowledgebase_settings"]["knowledgebases"],
            mcp_server_config=res.get("mcp_server_config", {}).get("mcpServers", {}),
            agent_info=dict(res),
            ...
        )

``super().get_agent_config`` **先**从平台 dict 映射十余个字段，**再**产出对象。
因此覆写 ``retrieve_agent_config`` 后，``super().get_agent_config`` 会在**合并后的
dict** 上做映射——**不需要**手工重跑装配。

为什么之前是错的：链原先住在 ``get_agent_config``，在 ``super()`` 已装配好的对象上做
后置写入。而下游消费者读的是 ``super()`` 从平台 dict 映射过去的那些字段，**对象层的
写入在下游无消费者**——12 个 ``LocalConfig`` 字段里只有 2 个碰巧生效
（``prompt_setting.llm_code`` → ``chat_model``，以及 ``mcp_server_config``）。

链的形状（261014-urj 裁决 (a)：平台加载器**真进链**）
-----------------------------------------------------
===========  =================================  ===================================
序号          加载器                              贡献
===========  =================================  ===================================
链首          :class:`PlatformConfigLoader`        ``fetch`` 取平台 dict（带 TTL 缓存）
                                                  + ``platform_mcp_disabled()`` 逃生清空
                                                  （``apply``）
第 1 步        :class:`ApprovalLoader`             固定裁剪：``approval_settings = {}``
第 2 步        :class:`LocalAgentConfigLoader`     ``agents/${code}.json`` 的全部字段
                                                  （per-agent 两级，平台词汇）
第 3 步        :class:`McpLoader`                  ``mcp.json`` 的**定义**（ADD-only）
第 4 步        :class:`KnowledgebaseLoader`        ``knowledgebase_settings.knowledgebases``
第 5 步        :class:`RuntimeInstanceLoader`      进程级运行时（注入的 ``runtime``）的
                                                  ``current_model`` → ``prompt_setting.llm_code``
===========  =================================  ===================================

**本轮机制变更（261014-urj）**：平台加载器原先是链的**输入**（``retrieve_agent_config``
先 ``fetch`` 再**显式** ``apply``，之后才遍历其余 5 个 loader）。现在它真进链——
``_loaders[0]`` 就是它，链完整遍历它的 ``merge_to_raw``。收益：那层"链外特例"
（"``apply`` 必须在遍历前显式调用"）退化为**链序本身**，消除"第二事实来源"。
四条必须保持的性质（原 (a)）：

1. ``platform_mcp_disabled()`` 的逃生清空仍在本地来源叠加**之前**——链首的 ``apply``
   先于 :class:`LocalAgentConfigLoader` 执行（靠链序保证，比靠显式调用顺序更难破坏）。
2. TTL 缓存仍与快照路径共用同一实例——``platform_loader`` 只有一个实例，链首即它。
3. :meth:`LocalResourceManager._ensure_platform_keys` 仍在 ``retrieve_agent_config``
   返回处无条件调用。
4. 失败语义不变——平台失败仍在 ``fetch`` 内传播，``_raw_platform_config`` 自己
   ``except → {}``。

``super().get_agent_config`` 随后在合并 dict 上做字段映射。链条本身**不含**
``super()``——它只产出一个 dict。

raw 层是 ENVELOPED 的 ``mcp_server_config``
-------------------------------------------
``retrieve_agent_config`` 返回的 dict 里 ``mcp_server_config`` 的形状是
``{"mcpServers": {...}}``（**ENVELOPED**），而 ``AgentConfig.mcp_server_config`` 是
**FLAT**（``super()`` 在装配时做 ``.get("mcpServers", {})`` 拆封，因为 SDK ``chat.py``
对每个具体 MCP 做 ``config.mcp_server_config.get(each)``）。**因此本层的所有 MCP
写入一律写 ENVELOPED**——写 FLAT 会让 ``super()`` 拆出空 dict，具体 MCP 被静默过滤。

为何需要 :meth:`LocalResourceManager._ensure_platform_keys` 守卫
----------------------------------------------------------------
``super().get_agent_config`` 对 ``res["agent_name"]`` 与
``res["knowledgebase_settings"]["knowledgebases"]`` 做**无兜底的直接下标访问**
（上面那段源码里的两处；``mcp_server_config`` 之类走 ``.get(...)`` 有兜底）。因此合并
dict **绝不能丢掉**这两个键，否则 TUI 在第一轮就 ``KeyError``。生产平台的
``knowledgebase_settings`` 可能是 ``{}`` 或整个缺失。守卫是**新增的防御**（把「平台
dict 缺键」从 ``KeyError`` 变成可用默认值），不是在保留既有行为。

分工边界（用户裁决）
--------------------
**``agents/${code}.json`` 的一切由 :class:`LocalAgentConfigLoader` 负责**——它读的就是
那个文件，MCP 声明（``mcp_server_config.mcpServers``）是其内容之一：**``McpLoader``
不再重复处理声明**，它只负责 ``mcp.json`` 的**定义**。
**``platform_mcp_disabled()`` 的逃生清空属于平台配置域**，由
:class:`PlatformConfigLoader` 在链首应用（丢弃的是平台绑定的 base，与本地声明无关），
不在 :class:`McpLoader` 里。

链序因此是「平台 → 审批裁剪 → per-agent 本地声明 → MCP 定义 → 知识库 → 运行内存」，
与重构前各来源的实际生效顺序逐字一致（声明先于定义合并，两者都 ADD-only）。

为何放在 ``resource_manager.py`` 的 §6（而非独立模块）
----------------------------------------------------
既有先例本身即理由：§1 ``LocalDeclarations`` / §2 ``McpResolver`` / §4+§5
``WorkspaceIndex`` 都是「同一关注点的状态与操作收进一个协作者类，放在本文件的 §N
分节」。六个加载器都是对这批协作者的适配器，它们的正确性只能对照那些协作者读；
拆到 ``worker/loaders.py`` 只会让「``retrieve_agent_config`` 到底读什么」散到两个
文件。``ConfigLoader`` 协议也是 :class:`LocalResourceManager` 的**私有编排契约**，
与实现同处一节才自洽。

契约（:class:`ConfigLoader`）
------------------------------
* ``apply(raw)`` **就地**修改并返回**同一个** raw dict（测试可断言 ``is``），便于
  链式应用；``merge_to_raw`` 是其契约别名（链遍历的是它，见下）；
* 永不抛异常——单个来源损坏只降级它自己（沿用 §1/§2 的容错约定）；
* 日志只记键名/数量，绝不记取值（沿用 §2 决定 4）。

链遍历 ``merge_to_raw``，不遍历 ``fetch``
-----------------------------------------
``merge_to_raw`` 是「把本 loader 的贡献并入平台 raw dict」这一动作的具名契约。261014-urj
后 :class:`PlatformConfigLoader` **也实现它**（既 fetch 又 merge），因此链的 6 个成员
全部实现 ``merge_to_raw``，链统一遍历它。"""


class RuntimeStateSurface(Protocol):
    """进程级运行时状态的表面——由 :class:`aidev_agent.headless.agent_runner.SessionState` 实现。

    为何是 Protocol 而非导入那个类：``agent_runner`` 导入本模块，反向导入会成环。
    因此本模块只声明它**用到的**那部分形状，由调用方把真实对象注入进来
    （见 :attr:`LocalResourceManager.runtime`）。这与 :class:`ManagerSurface` 的惯例一致：
    用结构化契约表达依赖，而不是用 import 表达。

    ``current_model`` 为何属于进程级状态而不属于 manager：manager 每次构建都会新建，
    挂在实例上的槽位会随 manager 分裂；而「当前用哪个模型」是用户在 ``/models`` 里
    做出的**持久**选择，只有一个进程级槽位才能保证所有路径（含 ``agent.info`` 快照）
    看到同一个值。
    """

    current_model: str | None


class ConfigLoader(Protocol):
    """一个配置来源。``merge_to_raw`` 就地把本源的贡献写进 ``raw``，或原样返回。

    契约：

    * ``apply(raw)`` **就地**修改并返回**同一个** raw dict（测试可断言 ``is``），
      便于链式应用；``merge_to_raw`` 是其契约别名，链遍历的是 ``merge_to_raw``；
    * 永不抛异常——单个来源损坏只降级它自己（沿用 §1/§2 的容错约定）；
    * 日志只记键名/数量，绝不记取值（沿用 §2 决定 4）。

    少数加载器在「本轮无事可做」时原样返回 ``raw``（如 ``mcp.json`` 不存在时的
    :class:`McpLoader`），这是性能选择而非契约例外——返回**同一个**对象仍是要求。
    """

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    def merge_to_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把本 loader 的贡献并入平台 raw dict（就地）。

        链遍历本方法而非 ``apply``：``merge_to_raw`` 是「贡献」这一动作的具名契约。
        261014-urj 后连 :class:`PlatformConfigLoader` 也实现它（既 fetch 又 merge），
        因此六个链成员统一经它遍历。默认实现委托给 ``apply``，因此多数 loader 只需
        实现 ``apply``。
        """
        return self.apply(raw)


class RawContributor:
    """贡献者的**具体**基类：为链中加载器提供 ``merge_to_raw`` 默认实现。

    :class:`ConfigLoader` 是 Protocol（结构化契约，不提供运行时基类），因此协议里的
    ``merge_to_raw`` 只是**类型层面**的声明——实际对象需要一个真实的方法。把默认实现
    放在这里而不是每个加载器里：链遍历 ``merge_to_raw``，因此每个贡献者都必须**真正
    拥有**这个方法；一处实现、多个继承，避免多份逐字复制。

    :class:`PlatformConfigLoader` **不**继承它——它刻意自带 ``merge_to_raw``（因为要
    从 ``rm._platform_request`` 取 per-call 参数，不能简单委托 ``apply``）。
    """

    def merge_to_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        """链遍历入口：委托给 ``apply``（各贡献者实现的即贡献动作）。"""
        return self.apply(raw)


class PlatformConfigLoader:
    """平台配置的加载器：带 **TTL 实例内存缓存** 的拉取。

    适配协作者：:meth:`LocalResourceManager.retrieve_agent_config`（平台 HTTP）。

    **261014-urj 裁决 (a)：本类由"链的输入"改为"链首成员"。** 它既 fetch 又 merge
    （:meth:`merge_to_raw`）——原先它是链的**输入**（``retrieve_agent_config`` 先
    ``fetch`` 再**显式** ``apply``，之后才遍历其余 loader），因此**刻意不实现**
    ``merge_to_raw``；现在真进链（``_loaders[0]``），链完整遍历它的 ``merge_to_raw``。
    收益见 §6 顶部：消除"链外特例"这一第二事实来源。

    :meth:`apply` **不是**恒等变换：它把 ``platform_mcp_disabled()`` 的逃生清空作用在
    ``raw["mcp_server_config"]["mcpServers"]``（**enveloped**）上，并兜底归一信封形状。
    它现在是链首执行的第一个动作，因此逃生清空**先于**任何本地来源叠加。
    :meth:`fetch` 负责带缓存地取回平台 dict。

    缓存语义（用户裁决 D1/D2，以及歧义 b 的裁决 B）
    --------------------------------------------
    * **实例内存**（``self._cache``）——进程内，不做磁盘持久化、不跨进程共享。
    * 缓存键是 ``agent_code``：两条取数路径（:meth:`LocalResourceManager.
      retrieve_agent_config` 与 :meth:`LocalResourceManager._raw_platform_config`）
      是**同一份**平台配置（同一个 ``agent_code`` 的 ``retrieve_agent_config``
      结果），因此共用一份缓存，避免「同一次进程内两份可能不同的平台配置」这一第二
      事实来源。
    * **只缓存成功**：``retrieve_agent_config`` 抛异常时向上传播且**不写缓存**，
      因此失败语义与重构前一致（``get_agent_config`` 失败传播、
      ``_raw_platform_config`` 自行降级为 ``{}``），只是成功的拉取被复用。
    * 时间戳用 :func:`time.monotonic` ——不受系统时钟回拨影响。
    * 缓存键**不含** ``version``：多版本并存是既有属性（同一进程通常只服务一个
      版本），本类不新增要求——首次拉取的版本被复用至 TTL 过期。
    """

    #: 平台配置缓存的存活秒数。默认 300（5 分钟）。环境变量可覆盖。
    #: 命名参照 ``aidev_agent.headless.utils.LOG_FILE_OVERRIDE``（``BKAI_TUI_LOG_PATH``）的
    #: BKAI_* 先例。
    TTL_ENV = "BKAI_AGENT_CONFIG_TTL"

    #: :attr:`TTL_ENV` 未设置或非法时的默认存活秒数。
    #: “未设置”用**默认值**，不是“无缓存”——这是用户裁决 D2 的明确要求。
    DEFAULT_TTL_SECONDS = 300.0

    def __init__(self, rm: Any) -> None:
        #: 取数所依据的**平台 HTTP** 方法。
        #:
        #: ⚠️ 对 :class:`LocalResourceManager` 必须绑定**基类**的
        #: ``retrieve_agent_config``，**不能**用 ``rm.retrieve_agent_config``：后者现在是
        #: :class:`LocalResourceManager` 的**覆写**（链的家），若绑定它，``fetch`` → 覆写
        #: → ``fetch`` 会**无限递归**（261014-rta 引入的真实崩溃，单测因 stub 掉
        #: ``platform_loader.retrieve_agent_config`` 而漏掉——真实 worker 在启动一轮 run
        #: 时才炸，表现为 SIGTERM 后不退出）。
        #:
        #: 对其余对象（测试替身）沿用 ``rm.retrieve_agent_config``——它们是显式注入的
        #: 取数接缝，没有覆写递归风险。
        if isinstance(rm, LocalResourceManager):
            from aidev_agent.packages.resource_manager.base import BaseResourceManager

            self.retrieve_agent_config = BaseResourceManager.retrieve_agent_config.__get__(rm, type(rm))
        else:
            self.retrieve_agent_config = rm.retrieve_agent_config

        #: 所属 manager。:meth:`merge_to_raw` 经它读 ``_platform_request`` 槽位（261014-urj
        #: 新增——链中传递 per-call 的 agent_code / version）。
        self._rm = rm

        #: ``agent_code`` → ``(平台配置, 拉取时刻的 monotonic 秒)``。
        self._cache: dict[str, tuple[Any, float]] = {}
        #: 缓存注入口（测试用）。默认 :func:`time.monotonic`。
        self._clock: Callable[[], float] = time.monotonic

    def ttl_seconds(self) -> float:
        """当前生效的 TTL 秒数。

        **每次调用时**对照 :data:`os.environ` 读取（不缓存到导入期——与
        ``platform_mcp_disabled()``「调用时读取」的既有实践一致，测试才能切换它）。

        * 未设置 / 空白 → :attr:`DEFAULT_TTL_SECONDS`（不是 0 / 无缓存）。
        * 非法值（非数字、负数）→ 回退默认 + **一行**日志（只记异常类别或“负值”，
          绝不记取值），不得抛异常——TTL 的排版错误不该让 TUI 起不来。
        """
        raw = os.environ.get(type(self).TTL_ENV, "").strip()

        if not raw:
            return type(self).DEFAULT_TTL_SECONDS

        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            logger.info(f"{type(self).TTL_ENV} is not a number: {type(exc).__name__}; using default TTL")

            return type(self).DEFAULT_TTL_SECONDS

        if value < 0:
            logger.info(f"{type(self).TTL_ENV} is negative; using default TTL")

            return type(self).DEFAULT_TTL_SECONDS

        return value

    def fetch(self, agent_code: str, version: str | None = None) -> Any:
        """带 TTL 缓存地取回平台配置。

        未过期则复用缓存值（返回**同一**对象，测试可断言 ``is``）；过期或缺失时
        重新拉取。``retrieve_agent_config`` 的异常向上传播且**不写缓存**。

        ``version`` 透传给 ``self.retrieve_agent_config``——**否则**子 agent 的
        ``version=None`` 语义与工厂的 ``version`` 路由会被静默丢弃。缓存键仍是
        ``agent_code``（不含 ``version``：多版本并存是既有属性，本类不新增要求）。
        """
        now = self._clock()
        cached = self._cache.get(agent_code)

        if cached is not None and (now - cached[1]) < self.ttl_seconds():
            return cached[0]

        # 异常向上传播：调用方各自处理（retrieve_agent_config 传播，快照路径降级）。
        value = self.retrieve_agent_config(agent_code, version=version)
        self._cache[agent_code] = (value, now)

        return value

    def merge_to_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        """链首成员的贡献：``fetch`` 平台 dict → 并入入参 raw → ``apply``。

        261014-urj 新增——这是本任务**唯一新增的 loader 方法**。它把原先散在
        ``retrieve_agent_config`` 里的"取数 + 浅拷贝 + 信封拷贝 + apply"整段搬进链首，
        使平台加载器真正成为链的成员。

        per-call 参数（``agent_code`` / ``version``）从 ``self._rm._platform_request``
        取——loader 无 per-call 构造参数可承载，这是链中传递它们的唯一机制。
        ``retrieve_agent_config`` 遍历链前置位、``finally`` 清空（T-261014urj-06）。

        拷贝语义（T-261014rta-05，原样搬运）：``fetch`` 返回的可能是 TTL 缓存里的
        **那个**对象；链**就地**修改它，若直接入链会跨调用累积本地覆盖。顶层浅拷贝保护
        顶层键，``mcp_server_config`` 的信封再拷一层，保护链就地写入的嵌套 ``mcpServers``
        子 dict——两者合起来才真正保护缓存对象与调用方传入的 payload。
        """
        request = getattr(self._rm, "_platform_request", None)

        if request is None:
            # 不应发生（正常路径经 retrieve_agent_config 置位）。遵守 loader 契约：
            # 记一行日志并原样返回 raw，**永不抛**。
            logger.info("platform loader ran without a _platform_request slot; returning raw unchanged")
            return raw

        agent_code, version = request
        return self.apply(self._fetch_into(raw, agent_code, version))

    def _fetch_into(self, raw: dict[str, Any], agent_code: str, version: str | None) -> dict[str, Any]:
        """取平台 dict 并**并入**入参 ``raw``（就地），返回该 raw。

        ``setdefault`` 语义：平台值只在 raw 尚无该键时补入。由于本 loader 是链首，raw
        此刻为空——因此等价于"把平台 dict 作为 base"。浅拷贝 + 信封拷贝见
        :meth:`merge_to_raw` 的说明。
        """
        fetched = self.fetch(agent_code, version)

        if isinstance(fetched, dict):
            for key, value in fetched.items():
                raw.setdefault(key, value)

            mcp = raw.get("mcp_server_config")

            if isinstance(mcp, dict):
                raw["mcp_server_config"] = {**mcp, "mcpServers": dict(mcp.get("mcpServers") or {})}

        return raw

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """处理 ``platform_mcp_disabled()`` 逃生通道，并兜底归一化信封形状。

        **``platform_mcp_disabled()`` 逃生通道**：这是本地配置唯一可以 **REMOVE**
        而非添加的地方，且刻意全有或全无——完整理由见 :func:`platform_mcp_disabled`。
        它属于**平台配置域**：丢弃的是平台绑定的 base，与本地声明的 server 无关，
        因此在平台层就清掉，后续加载器仍会照常把本地来源并进来。

        作用点是 ``raw["mcp_server_config"]["mcpServers"]``（**enveloped**）。清空
        平台 base 时**保留外层信封**（置 ``mcpServers = {}`` 而非置整个
        ``mcp_server_config = {}``）：两者对 ``super()`` 等价，但保留信封让后续
        :class:`McpLoader` 的写入形状一致。

        另加一条**兜底**：``raw.get("mcp_server_config")`` 不是 dict 时归一为
        ``{"mcpServers": {}}``——raw 层不存在 pydantic 的 ``None`` 字段默认值问题
        （那是对象层的事），故此处只做形状守卫，不做 ``None`` 归一化。
        """
        mcp = raw.get("mcp_server_config")

        if not isinstance(mcp, dict):
            mcp = raw["mcp_server_config"] = {}

        servers = mcp.get("mcpServers")

        if not isinstance(servers, dict):
            servers = mcp["mcpServers"] = {}

        if platform_mcp_disabled():
            if servers:
                logger.info(
                    f"dropping {len(servers)} platform-bound mcp server(s) "
                    f"(platform MCP disabled): {sorted(servers)}"
                )

            mcp["mcpServers"] = {}

        return raw


class ApprovalLoader(RawContributor):
    """审批清空：TUI 的固定裁剪，置空 ``raw["approval_settings"]``。

    它**不属于**六源之一，而是 TUI 对平台配置的固定裁剪（用户裁决 D3）。原先这段
    逻辑在 ``get_agent_config`` 的编排层（``cfg.approval_settings = {}``）；D3 把它
    移进加载器链：

    * 审批不在 TUI 的范围内——其副作用 worker 是 SDK 指向插件方向
      （``aidev_bkplugin``）的最后一处反向引用；清空绑定意味着该 interrupt
      **永远不会被触发**。完整理由见 ``agent_factory`` 中同一处说明。
    * 进链后，清空对**所有**读平台配置的路径都生效（``agent.info`` 的两半、快照
      路径）——这比留在编排层更好（那里只有 ``get_agent_config`` 那一条清）。

    链序位置：**链首**（裁决 (c)）。它是**置空**而非合并，唯一正确性要求是「最终为
    空」。除本类外没有任何 loader 写 ``approval_settings``，所以顺序本身无实质影响；
    放链首让「裁剪」在任何来源叠加之前就完成，可读性最好，且与「审批不属于六源」的
    定位一致。

    ``or {}`` 语义保留：``approval_settings=None`` 时 ``super()`` 的映射结果是
    ``{}``（``approval_settings=res.get("approval_settings") or {}``），置空为 ``{}``
    与之等价——因此 ``test_tolerates_absent_approval_settings`` 继续通过。
    """

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw.get("approval_settings"):
            logger.info("clearing approval_settings (approval is out of scope for the TUI)")

        raw["approval_settings"] = {}

        return raw


class LocalAgentConfigLoader(RawContributor):
    """per-agent 本地声明的加载器：``agents/${agent_code}.json`` 的全部字段 → 平台 raw dict。

    适配协作者：:class:`LocalDeclarations`——**复用**它读出的
    :class:`LocalConfig`，不重实现 per-agent 两级读取。

    命名（261014-urj D3）：原名 ``LocalConfigLoader``，改名 ``LocalAgentConfigLoader``
    以体现它读的是**单个 agent** 的配置（``agents/${code}.json``），与进程级的 TUI 配置
    （:class:`aidev_agent.headless.config.TuiConfig`）区分。``agent.json`` 这个固定文件名已退役。

    本加载器拥有 ``agents/${code}.json`` 贡献的一切，因为它就是那个文件的内容：

    * 顶层文本字段（``agent_id`` / ``tenant_id`` / ``agent_code`` /
      ``agent_name`` / ``space_id`` / ``agent_type`` / ``updated_by`` /
      ``service_catalogue``）按**平台词汇**写入 raw。规则是「**只增不删**」：
      文本字段一律**仅当本地值非空**时才写（不能把平台的 ``agent_name`` 清成
      ``""``）。⚠️ ``tenant_id`` / ``agent_type`` 的 dataclass 默认是
      ``"system"`` / ``"single"``，因此它们**总是**非空——写它们会盖掉平台值，这是
      **有意的**：用户声明了就是用户赢。
    * ``prompt_setting`` / ``conversation_settings``：**逐键并集**（本地键优先），
      仅当本地 section 非空时才写。注意 ``conversation_settings.commands`` 会被
      ``super()`` 读去构造 ``command_agent_mapping``——逐键并集对嵌套的 ``commands``
      列表是**整体替换**（本地那份覆盖平台那份），与旧行为一致。
    * **不碰** ``knowledgebase_settings``：``knowledgebases`` 的 ADD-only union 归
      :class:`KnowledgebaseLoader`（D2 的分工表），本加载器不处理该子键。
      **这是必须逐字保留的分工边界**：``LocalAgentConfigLoader`` 与
      :class:`KnowledgebaseLoader` 都读同一个 ``${code}.json``，若本加载器也处理
      ``knowledgebases`` 就会**双写**。
    * ``mcp_server_config``：**ENVELOPED 直通**（§6 顶部 E 节）。取
      :meth:`LocalDeclarations._mcp_servers` 得到 FLAT 映射，与
      ``raw["mcp_server_config"]["mcpServers"]`` 做 :meth:`LocalResourceManager.
      _merge_mcp_add_only`（ADD-only，用户裁决、mw9 定案，**不得回退为 REPLACE
      或普通 dict 覆盖**），包回 ``{"mcpServers": ...}`` 写回。

    读取（:meth:`read_config`，**注入缝**）
    --------------------------------------
    默认经 :meth:`LocalDeclarations.read` 按 per-agent 两级读取。这是 D1 的**正式注入缝**
    （代替已删除的构造参数）：``__init__`` 仍只收 ``rm``，测试可
    ``monkeypatch.setattr(type(loader), "read_config", lambda self, code: LocalConfig(...))``
    或对实例打补丁来注入声明，而不新增构造参数。

    子 agent 串味防护已由**路径隔离**取代（261014-urj 裁决 (b)）
    ---------------------------------------------------------
    ``_declaration_scope`` 门控与四段门控代码**已删除**。原因：``read_config`` 按
    ``agent_code`` 读**不同的文件**，父子 agent 物理上不可能互相串味——这是**结构性**
    保证（比运行时门控更强）。**空 ``agent_code``（``raw`` 无该键，少见）时读不到
    per-agent 文件，因此贡献空**——不再有"未限定作用域对任何拉取生效"的兜底（该兜底随
    门控一并删除）。
    """

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    def read_config(self, agent_code: str) -> LocalConfig:
        """读取该 ``agent_code`` 的 per-agent 本地声明（注入缝）。

        默认经 :meth:`LocalDeclarations.read` 的两级读取（用户空间 → 项目空间，项目覆盖
        用户）。``agent_code`` 为空或含路径穿越字符时返回空 :class:`LocalConfig`（由
        :meth:`LocalDeclarations.read` 处理）。
        """
        if not agent_code:
            return LocalConfig()

        return LocalDeclarations().read(code=agent_code)

    def declared_prompt_settings(self, local: LocalConfig) -> dict:
        """本地 ``prompt_setting`` section 的内容（空 section 时为 ``{}``）。

        收 ``local`` 参数（而非读实例状态），避免实例状态——这是本任务对原
        ``declared_prompt_settings()`` 的收参化。
        """
        section = local.prompt_setting

        return dict(section) if isinstance(section, dict) else {}

    def declared_servers(self, local: LocalConfig) -> dict[str, dict]:
        """``agents/${code}.json`` 的 ``mcp_server_config.mcpServers``（两级已合并）。"""
        return LocalDeclarations._mcp_servers(local)

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # per-agent 读取（裁决 (b) 后无门控）：raw 自带本次拉取的 agent_code，据此读
        # 对应的 agents/${code}.json。空 agent_code 读不到文件 → 贡献空。
        agent_code = raw.get("agent_code") or ""
        local = self.read_config(agent_code)

        if local.is_empty():
            return raw

        # 顶层文本字段：仅当本地值非空才写（不清空平台值）。``agent_id`` 只在 raw
        # 尚无该键时补（它是"新键"语义：平台通常不带 agent_id 时才由本地提供）。
        scalar_fields: tuple[tuple[str, Any], ...] = (
            ("tenant_id", local.tenant_id),
            ("agent_code", local.agent_code),
            ("agent_name", local.agent_name),
            ("space_id", local.space_id),
            ("agent_type", local.agent_type),
            ("updated_by", local.updated_by),
            ("service_catalogue", local.service_catalogue),
        )

        raw.update({key: value for key, value in scalar_fields if value})

        if local.agent_id and "agent_id" not in raw:
            raw["agent_id"] = local.agent_id

        # 嵌套 section：逐键并集（本地键优先），仅当本地 section 非空。
        for key, section in (
            ("prompt_setting", local.prompt_setting),
            ("conversation_settings", local.conversation_settings),
        ):
            if isinstance(section, dict) and section:
                raw[key] = {**(raw.get(key) or {}), **section}

        # MCP 声明：ENVELOPED 直通（§6 顶部 E 节）——与平台的 ``mcpServers`` 做
        # ADD-only 合并后包回信封。
        declared_servers = self.declared_servers(local)

        if declared_servers:
            mcp = raw.get("mcp_server_config")

            if not isinstance(mcp, dict):
                mcp = raw["mcp_server_config"] = {}

            servers = mcp.get("mcpServers")

            if not isinstance(servers, dict):
                servers = {}

            merged = self._rm._merge_mcp_add_only(dict(servers), declared_servers)
            added = sorted(name for name in merged if name not in servers)

            # 只有值真的变化才记；日志只记键名/数量，绝不记 server dict 内容。
            if added:
                logger.info(f"local declaration adds {len(added)} mcp server(s): {added}")

            mcp["mcpServers"] = merged

        # 主模型覆盖：记录与旧 ``apply(cfg)`` 逐字一致的语义（只覆盖主模型，
        # 仅当声明值非空、且与现值不同才记录——但实际生效由 ``super()`` 读
        # ``prompt_setting.llm_code`` 完成，这里只记日志）。
        declared_model = self.declared_prompt_settings(local).get("llm_code") or ""
        current_model = (raw.get("prompt_setting") or {}).get("llm_code") or ""

        if declared_model and declared_model != current_model:
            logger.info("overriding chat_model (local declaration)")

        return raw


class McpLoader(RawContributor):
    """MCP 的加载器：**仅** ``mcp.json`` 的完整定义 → 平台 raw dict，ADD-only。

    适配协作者：:class:`McpResolver`（``mcp.json`` 的完整定义）。

    分工（用户裁决）：``agents/${code}.json`` 的 MCP **声明**由
    :class:`LocalAgentConfigLoader` 负责（那是本地配置文件的内容）；本加载器只处理
    ``mcp.json`` 的**定义**，不再重复处理声明。

    raw 层形状是 **ENVELOPED**
    -------------------------
    ``AgentConfig.mcp_server_config`` 是 **FLAT**（``名称 → 定义``：SDK ``chat.py`` 对
    每个具体 MCP 做 ``config.mcp_server_config.get(each)``），但**平台 raw dict** 里是
    **ENVELOPED**（``{"mcpServers": {...}}``）——``super().get_agent_config`` 正是在装配
    时做 ``.get("mcpServers", {})`` 拆封。因此本加载器在 raw 层写入 ``raw
    ["mcp_server_config"]["mcpServers"]``（ENVELOPED），**不**写 FLAT。在 raw 层写 FLAT
    会让 ``super()`` 拆出空 dict，具体 MCP 被静默过滤。§2 在读取 ``mcp.json`` 时完成
    拆封，本加载器拿到的是 FLAT 定义，包回信封后入库。

    合并规则（用户裁决，mw9 定案，**不得回退为 REPLACE**）：所有来源并集，同名条目
    先到先得、绝不互相替换；唯一例外是「定义可以补全占位」——先出现者只是 ``{}``
    占位、而后出现者带完整定义时，用后者补全。经
    :meth:`LocalResourceManager._merge_mcp_add_only`。

    平台绑定的取舍（``platform_mcp_disabled()`` 全有或全无）**不在此处**：那是
    平台配置域的事，由 :class:`PlatformConfigLoader` 在取数后应用。
    """

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    def definitions(self) -> dict[str, dict]:
        """``mcp.json`` 的完整定义（经 :class:`McpResolver`，lazy + mtime 门控）。"""
        return self._rm.mcp.load().server_definitions()

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # 无门控（裁决 (b) 已删除）：``mcp.json`` 是**工作区**级全局来源（不是 per-agent
        # 配置），因此对它不做 per-agent 读取——它对该 workspace 的每次拉取都生效。
        definitions = self.definitions()

        if not definitions:
            return raw

        mcp = raw.get("mcp_server_config")

        if not isinstance(mcp, dict):
            mcp = raw["mcp_server_config"] = {}

        servers = mcp.get("mcpServers")

        if not isinstance(servers, dict):
            servers = {}

        before = set(servers)
        merged = self._rm._merge_mcp_add_only(dict(servers), definitions)
        added = [name for name in definitions if name not in before]

        if added:
            logger.info(f"mcp.json adds {len(added)} server(s): {sorted(added)}")

        mcp["mcpServers"] = merged

        return raw


class KnowledgebaseLoader(RawContributor):
    """知识库的加载器：per-agent 声明增量追加到
    ``raw["knowledgebase_settings"]["knowledgebases"]``。

    适配协作者：:class:`LocalDeclarations`（**per-agent**，``agents/${code}.json``）。
    只追加平台尚无的名称、保持平台顺序在前——这是「本地资源叠加在 agent 配置之上」的
    读法，也是重构前的逐字语义。

    本地声明来源（261014-urj）：与 :class:`LocalAgentConfigLoader` 同法读
    ``read_config(agent_code)``（**同一个** ``${code}.json``）。**分工边界必须保留**：
    :class:`LocalAgentConfigLoader` **不碰** ``knowledgebase_settings`` 的
    ``knowledgebases`` 子键，由本加载器负责——否则会**双写**。

    写入位置：raw 层的 ``raw["knowledgebase_settings"]["knowledgebases"]``。语义逐字
    保留：平台在前、只追加本地新增、去重。``super()`` 随后用
    ``res["knowledgebase_settings"]["knowledgebases"]`` 装配 ``knowledgebase_ids``，
    因此断言的端到端结果不变。

    无子 agent 门控（裁决 (b) 已删除）：读取按 ``agent_code`` 逐文件，路径隔离即防护。
    """

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    def read_config(self, agent_code: str) -> LocalConfig:
        """读取该 ``agent_code`` 的 per-agent 本地声明（与 :class:`LocalAgentConfigLoader`
        同法的注入缝）。"""
        if not agent_code:
            return LocalConfig()

        return LocalDeclarations().read(code=agent_code)

    def declared_knowledgebases(self, agent_code: str) -> list[str]:
        """per-agent ``knowledgebase_settings.knowledgebases`` 名称列表。"""
        section = self.read_config(agent_code).knowledgebase_settings

        if not isinstance(section, dict):
            return []

        names = section.get("knowledgebases")

        return [each for each in names if isinstance(each, str) and each] if isinstance(names, list) else []

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # per-agent 读取（裁决 (b) 后无门控）：按本次拉取的 agent_code 读对应文件。
        agent_code = raw.get("agent_code") or ""
        kbs = self.declared_knowledgebases(agent_code)

        if not kbs:
            return raw

        kb = raw.get("knowledgebase_settings")

        if not isinstance(kb, dict):
            kb = raw["knowledgebase_settings"] = {}

        existing = kb.get("knowledgebases")

        if not isinstance(existing, list):
            existing = []

        added = [name for name in kbs if name not in existing]

        if added:
            logger.info(f"local config adds {len(added)} knowledgebase(s): {added}")

        kb["knowledgebases"] = list(existing) + added

        return raw


class WorkspaceFileLoader(RawContributor):
    """工作空间文件的加载器：本地文件资源行。

    适配协作者：:class:`WorkspaceIndex`——**无状态适配**，包装由
    :class:`LocalResourceManager` 持有的那一个索引（``rm.files``）。
    在 rm 侧装配为 ``self.workspace_files``，因此全程**只有一个** ``WorkspaceIndex``
    实例（旧的「SessionState 与 rm 各持一个」双实例陷阱已消除）。

    它是**默认链的成员**（§3 装配），因此 ``retrieve_agent_config`` 自己就带着本地
    文件行，调用方不必再补。

    ``resources`` 为何可以进链
    --------------------------
    链的产物确实会流进 SDK 的 ``AgentConfig``（pydantic 模型），而 ``resources`` 不是它的
    字段——但这一点**不构成**排除的理由：``retrieve_agent_config`` 返回的是合并后的
    **raw dict**（`super().get_agent_config` 从中挑它认识的十余个键映射），多出的键只是
    不被映射，不会污染平台契约。真正会被"焊进契约"的是把 Node 侧载荷概念写进
    ``AgentConfig`` 的字段定义，而本加载器写的是 raw dict 上的额外键，两者不同。

    单次读取
    --------
    :meth:`project` 只 ``read()`` 一次，由同一次扫描派生全部文件行。
    """

    def __init__(self, index: WorkspaceIndex) -> None:
        #: 被包装的工作区索引——rm 的 ``self.files``。
        self._index = index

    def project(self) -> list[dict[str, Any]]:
        """工作区根目录的文件资源行——**只读一次**工作区。"""
        return self._index.rows_for("")

    def file_resources(self) -> list[dict[str, Any]]:
        """工作区根目录的文件资源行——与 ``WorkspaceIndex.rows_for("")`` 逐字等价。"""
        return self._index.rows_for("")

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把本地文件行并入 raw：**追加**到现有 ``resources`` 之后。

        追加而非覆盖：``resources`` 是平台自己的字段（工具行，如 ``weather_query``），
        本地工作区行排在它后面——与原先 ``describe_agent_config`` 的合并语义逐字一致。

        本地文件行**只**走 ``resources``（``type == "file"``）。此前同一份扫描还会额外写入
        一个 ``local_files`` 兄弟键，导致前端把同一个文件同时投影进「资源」与「文件」两个
        分组；该通道已删除。
        """
        raw["resources"] = [*self._existing_resources(raw), *self.project()]

        return raw

    @staticmethod
    def _existing_resources(raw: dict[str, Any]) -> list[dict[str, Any]]:
        """raw 里已有的 ``resources``（平台行）；缺失或非列表时为空。"""
        existing = raw.get("resources")

        return existing if isinstance(existing, list) else []


class RuntimeInstanceLoader(RawContributor):
    """**进程级运行时状态**的加载器：``current_model`` → ``raw["prompt_setting"]["llm_code"]``
    （最高优先级）。

    适配协作者：:class:`RuntimeStateSurface`——即进程级的 ``SessionState``，**注入**进来
    （本模块不 import ``agent_runner``，否则成环）。本加载器是本模块的**运行时取值入口**：
    「当前用哪个模型」这类信息住在进程级状态上，不在 manager 上——manager 每次构建都会
    新建，挂在它上面的槽位会分裂成多份。

    ``current_model`` 是用户在 ``/models`` 里做出的**持久**选择：切换之后一直生效，直到
    再次切换。它**没有**作用域上下文管理器——此前那个「构建前设、构建后还原」的写法把
    一件持久的事做成了临时的事。

    优先级（低 → 高）：平台 ``<`` 本地声明（``prompt_setting.llm_code``）
    ``<`` ``current_model``。它是六源中**最后（最高）**一源，因此必须排在其他加载器
    之后应用（链中最后一项）。落盘声明与它写的是同一个事实（前者跨进程持久，后者让
    切换在当前进程内立即生效），因此两者一致时本加载器什么也不做。

    ``retrieve_agent_config`` 的每一次调用都经本加载器——包括 SDK
    ``factory.get_agent_config`` 与 ``agent_runner`` 直接调用的那一处，因此
    ``current_model`` 对所有路径一致生效。
    """

    def __init__(self, runtime: RuntimeStateSurface | None) -> None:
        self.runtime = runtime

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # ``runtime`` 为 None（无进程级状态时）``getattr`` 默认 None，天然是 no-op。
        model = getattr(self.runtime, "current_model", None)

        if model:
            section = raw.get("prompt_setting")

            if not isinstance(section, dict):
                section = raw["prompt_setting"] = {}

            if model != section.get("llm_code"):
                logger.info("overriding chat_model (selected model)")
                section["llm_code"] = model

        return raw
