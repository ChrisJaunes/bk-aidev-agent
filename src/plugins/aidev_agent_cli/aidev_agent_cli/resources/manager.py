# -*- coding: utf-8 -*-
"""唯一的资源管理器 LocalResourceManager（+ 会话内存账本 HeadlessLedger）。

LocalResourceManager 由一组**子资源加载器**叠加而成（aidev_agent_cli.resources.loaders），
叠加顺序即 --resources 的顺序（低 → 高优先级），见 aidev_agent_cli.resources.sources。
headless / agui / acp 三个协议用的都是它；差别只在加载器组合：

* 含 platform 底座：AgentResourceManager 的平台能力全部可用（HTTP 客户端、凭据、会话登记）；
* 不含 platform：任何平台 API 入口统一抛 HeadlessUnsupportedError（既有 try/except 兜底把
  平台写入降级为日志），事件只走 writer，凭据为空。

会话历史：给了 ledger 就读 ledger（headless 的内存账本）；否则平台模式读平台、
无平台模式为空（续轮状态由 checkpointer 承载）。
"""

from __future__ import annotations

import copy
from logging import getLogger
from typing import Any, Iterable

from aidev_agent.packages.langchain_core.tools.base import McpToolsResult
from aidev_agent.packages.resource_manager.agent import AgentResourceManager

from .loaders import ResourceOverrides, WorkspaceFileLoader, build_loaders, get_loader_class
from .local_config import LocalDeclarations, McpResolver
from .sources import parse_resources
from .workspace import WorkspaceIndex
from ..infrastructure.errors import HeadlessUnsupportedError

logger = getLogger(__name__)


class HeadlessLedger:
    """会话内存账本：以平台 ``chat_session_contents`` 记录形状保存本轮历史。"""

    def __init__(self) -> None:
        self._records: list[dict] = []

    def append(self, record: dict) -> None:
        self._records.append(copy.deepcopy(record))

    def drop(self, message_id: str) -> None:
        """删除指定记录（轮次失败时回滚预写的 user 记录）。"""
        self._records = [r for r in self._records if r.get("message_id") != message_id]

    def records(self) -> list[dict]:
        """返回账本副本，调用方修改不会污染会话历史。"""
        return copy.deepcopy(self._records)

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


# ============================================================================
# §3  资源获取器 —— LocalResourceManager
#     消费 §1 与 §2。SDK 中与 manager 相关的功能全部保持继承。
# ============================================================================


class LocalResourceManager(AgentResourceManager):
    """``AgentResourceManager`` + **per-agent** 本地声明式覆盖层 + 会话 token 槽位。

    这是 §3 的**编排者**：它把 §1/§2 的协作者组合起来，并把它们的成果装进
    ``AgentConfig``。本地状态分布在两个协作者上（261014-urj 后 ``self.declarations``
    与 ``self.local`` 已删除——per-agent 配置由 loader 现场读盘）：

      * ``self.mcp`` —— :class:`McpResolver`（``mcp.json`` + mtime 读缓存）
      * ``self.files`` —— :class:`WorkspaceIndex`（工作区第一层实时读取）

    **进程级 vs per-agent（261014-urj 的核心概念分离）**：本类负责**单个 agent** 的
    资源与配置，读 ``agents/${agent_code}.json``。**进程级**的 TUI 配置（"主 Agent 是谁"
    + 身份认证）属 :class:`aidev_agent_cli.config.TuiConfig`（``bkai_tui.json``）。两者唯一的
    耦合点：per-agent 配置的写入目标是 ``TuiConfig.agent_code`` 所指那个 agent 的文件。

    ``self.mcp`` 是**解析器**，不是解析结果：把它命名为 ``mcp`` 保持了对调用方的
    既有词汇，同时它内部用 ``.config`` 暴露解析出的 :class:`McpConfig`。
    """

    def __init__(
        self,
        access_token: str = "",
        *,
        overrides: ResourceOverrides | None = None,
        sources: Iterable[str],
        ledger: HeadlessLedger | None = None,
        agent_code: str = "",
        model: str = "",
        **kwargs: Any,
    ) -> None:
        # 父类 __init__ 接受 access_token 并保存为 self.access_token；
        # resolve_access_token 会优先使用它而不是用户名查询。此前从未传入该参数，
        # 导致整条用户 token 通道都是失效的。
        super().__init__(access_token=access_token, **kwargs)

        # Accepted explicit values, never a Session or mutable runtime reference.
        self.overrides = overrides or ResourceOverrides()

        #: 叠加的来源名（已校验、已展开预设；调用方必须给出，这里不做兜底）。
        self.sources: tuple[str, ...] = parse_resources(sources)
        #: 是否带平台底座：决定平台 API 是否可用。
        self.uses_platform: bool = "platform" in self.sources
        #: 会话内存账本（headless）；None 表示历史由平台 / checkpointer 承载。
        self.ledger = ledger
        #: 注入底座使用的 agent_code / 模型（也作为 get_agent_code 的首选值）。
        self.default_agent_code = agent_code
        self.default_model = model
        if not self.uses_platform:
            # 无平台：不得带出任何可用凭据（覆盖父类对 settings.APP_CODE 的回退）。
            self.access_token = ""
            self.app_code = ""
            self.app_secret = ""

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
        #: ``Session`` 曾另持一个 ``WorkspaceIndex``（双实例陷阱），现已收归此处。
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

        # :meth:`retrieve_agent_config` 按序应用的加载器链（顺序即优先级，低 → 高）。
        # 底座（platform / injected）若存在必在链首；platform 底座的 TTL 缓存由
        # retrieve_agent_config 与 _raw_platform_config 共用（同一实例 = self.platform_loader）。
        self._loaders: list[Any] = build_loaders(self, self.sources)
        platform_class = get_loader_class("platform")
        self.platform_loader: Any | None = next(
            (loader for loader in self._loaders if isinstance(loader, platform_class)), None
        )

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

        per-agent 配置的**写入口**（261014-urj 后续：此职责从 ``Session`` 收归
        管理器）。``/models`` / ``/mcps`` / ``/knowledge`` 的 ``config.set`` 落点。

        ``code`` 由调用方给出（当前主 agent 的 ``agent_code``）——管理器不猜"当前是
        谁"，因为它对应的是**单个 agent**；"谁是主 agent"是进程级 TUI 配置的事
        （见 :class:`aidev_agent_cli.config.TuiConfig`）。

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

    def retrieve_agent_config(self, agent_code: str, version: str | None = None, **kwargs: Any) -> dict[str, Any]:
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
        self._platform_request = (agent_code or self.default_agent_code, version)

        try:
            raw: dict[str, Any] = {}

            for loader in self._loaders:
                raw = loader.merge_to_raw(raw)

            return self._ensure_platform_keys(raw)
        finally:
            self._platform_request = None

    def get_config_snapshot(self) -> dict[str, Any]:
        """Project effective and locally declared configuration from the same resource chain."""
        payload = self._raw_platform_config()
        payload = payload if isinstance(payload, dict) else {}
        local = self.declarations.read(code=self.get_agent_code())

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
        local_code = self.overrides.agent_code or local.agent_code
        local_model = self.overrides.model
        prompt_section = getattr(local, "prompt_setting", None)
        if isinstance(prompt_section, dict):
            local_model = local_model or prompt_section.get("llm_code") or ""
        local_mcps = []
        mcp_section = getattr(local, "mcp_server_config", None)
        if isinstance(mcp_section, dict):
            raw_servers = mcp_section.get("mcpServers")
            local_mcps = list(raw_servers) if isinstance(raw_servers, dict) else []
        local_kbs = []
        kb_section = getattr(local, "knowledgebase_settings", None)
        if isinstance(kb_section, dict):
            raw_local_kbs = kb_section.get("knowledgebases")
            local_kbs = (
                [each for each in raw_local_kbs if isinstance(each, str)] if isinstance(raw_local_kbs, list) else []
            )

        # --- 工作区 mcp.json 定义 --------------------------------------------
        servers = McpResolver.describe_servers(self.mcp.load())

        return {
            # 浮层第一组渲染的生效值（青色加粗）。``raw`` 已含链合并的本地覆盖。
            "currentModel": current_model,
            "currentModelSource": "local"
            if not self.uses_platform
            or (local_model == current_model and any(source in self.sources for source in ("local", "runtime")))
            else "platform",
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
        agent_code = self.get_agent_code()

        try:
            return self.retrieve_agent_config(agent_code)
        except Exception as exc:  # noqa: BLE001 - 快照失败不能杀死 worker
            logger.info(f"config snapshot: platform fetch failed: {type(exc).__name__}: {exc}")

            return {}

    # ``get_client()`` 平台模式下不覆写（注入模式由 ``_PLATFORM_ONLY`` 守卫）。该覆写范围由继承满足：父类实现每次调用都会
    # 重新组装，而 ``resolve_access_token`` 优先读取 ``self.access_token``——
    # 因此在 ``set_access_token`` 之后取得的 client 自然携带新 token，无需额外
    # 处理。添加覆写只会逐字复制父类实现，并造成关于 client 如何认证的第二个
    # 事实来源。

    # ------------------------------------------------------------------ 无平台降级

    def get_agent_code(self, **kwargs: Any) -> str:
        if self.default_agent_code or not self.uses_platform:
            return self.default_agent_code
        return super().get_agent_code(**kwargs)

    def get_chat_session_contents(self, session_code: str, **kwargs: Any) -> list[dict]:
        if self.ledger is not None:
            return self.ledger.records()
        if not self.uses_platform:
            return []
        return super().get_chat_session_contents(session_code, **kwargs)

    def get_chat_session_context(self, session_code: str, **kwargs: Any) -> list[dict]:
        if self.ledger is not None:
            return self.ledger.records()
        if not self.uses_platform:
            return []
        return super().get_chat_session_context(session_code, **kwargs)

    def publish_event(self, event: Any) -> None:
        if not self.uses_platform:
            return None
        return super().publish_event(event)

    def event_publishing_enabled(self) -> bool:
        if not self.uses_platform:
            return False
        return super().event_publishing_enabled()

    def resolve_access_token(self, username: str = None) -> str:
        """无平台时返回空凭据：``build_executor_info`` 装配期无条件调用，但不得产生可用凭据。"""
        if not self.uses_platform:
            return ""
        return super().resolve_access_token(username)

    def retrieve_chat_session(self, session_code: str, **kwargs: Any) -> dict:
        """无平台时会话详情为空（事实源归宿主），只用于装配期兜底读取。"""
        if not self.uses_platform:
            return {}
        return super().retrieve_chat_session(session_code, **kwargs)

    def construct_mcp(self, mcp_config: dict, username: str = None, **kwargs: Any):
        """无 MCP 配置时返回空结果（装配期无条件调用，不触达平台）。"""
        if not mcp_config:
            return McpToolsResult(tools=[], fetch_failures=[])
        return super().construct_mcp(mcp_config, username=username, **kwargs)

    @classmethod
    def build(
        cls,
        creds: dict[str, str] | None = None,
        username: str = "",
        *,
        sources: Iterable[str],
        access_token: str = "",
        overrides: ResourceOverrides | None = None,
        ledger: HeadlessLedger | None = None,
        agent_code: str = "",
        model: str = "",
    ) -> "LocalResourceManager":
        """唯一的构造入口（三个协议共用）。

        creds 只在 sources 含 platform 时有意义；「凭据 dict 的键名」这一知识只在这里。
        overrides 是接纳时固定的配置值，不含会话对象、连接或传输出口。
        """
        creds = creds or {}
        return cls(
            access_token=access_token,
            app_code=creds.get("BKPAAS_APP_CODE", ""),
            app_secret=creds.get("BKPAAS_APP_SECRET", ""),
            username=username,
            overrides=overrides,
            sources=sources,
            ledger=ledger,
            agent_code=agent_code,
            model=model,
        )


#: 只有平台能提供的能力：无 platform 来源时统一抛 :class:`HeadlessUnsupportedError`。
_PLATFORM_ONLY = (
    "get_client",
    "retrieve_knowledgebase",
    "retrieve_knowledge",
    "is_resume_session",
    "create_tool_approval",
    "update_chat_session_sandbox_pv_id",
    "update_chat_session_caller_context",
    "get_or_create_session",
    "update_session_status",
    "save_session_content",
    "retrieve_skill",
    "check_agent_call_permission",
    "construct_tool",
    "get_paas_sbx_client",
    "knowledge_query",
    "start_flow_agent",
    "get_flow_agent_task_info",
    "retry_flow_agent_node",
    "skip_flow_agent_node",
    "stop_flow_agent_task",
    "pause_flow_agent_task",
    "resume_flow_agent_task",
    "get_flow_agent_task_node_info",
)


def _platform_only(name: str):
    def method(self, *args: Any, **kwargs: Any):
        if not self.uses_platform:
            raise HeadlessUnsupportedError(name)
        return getattr(super(LocalResourceManager, self), name)(*args, **kwargs)

    method.__name__ = method.__qualname__ = name
    return method


for _name in _PLATFORM_ONLY:
    setattr(LocalResourceManager, _name, _platform_only(_name))
del _name


__all__ = ["HeadlessLedger", "LocalResourceManager"]
