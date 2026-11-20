# -*- coding: utf-8 -*-
"""平台来源（底座）：带 TTL 缓存地拉取平台 agent 配置。"""

from __future__ import annotations

import copy
import os
import time
from logging import getLogger
from typing import Any, Callable

from aidev_agent.packages.resource_manager.agent import AgentResourceManager
from aidev_agent.packages.resource_manager.base import BaseResourceManager

from .base import ResourceLoader
from ..local_config import platform_mcp_disabled

logger = getLogger(__name__)


def _chains_through_retrieve_agent_config(rm: Any) -> bool:
    """该对象是否是 ``LocalResourceManager``（判定"链的家"而不是具体类型）。

    为什么不用 ``isinstance(rm, LocalResourceManager)``：那个类住在 ``platform.py``，而本模块
    必须**先于**它被导入（``platform`` 装配链），于是模块级 import 会成环。这里用**结构化**
    判据——"``retrieve_agent_config`` 的覆写就在这个对象的类上"——它精确表达了那条守卫
    真正关心的事：覆写存在 → 必须绑基类实现，否则 ``fetch`` → 覆写 → ``fetch`` 无限递归。

    替身（测试注入的取数接缝）在自己的类上定义 ``retrieve_agent_config``，但它们的类是
    测试类、不是 ``AgentResourceManager`` 子类；因此再加一条"是 SDK manager 子类"的判据，
    使替身沿用原路径（``rm.retrieve_agent_config``）而不被误绑基类。
    """
    return isinstance(rm, AgentResourceManager) and "retrieve_agent_config" in type(rm).__dict__


class PlatformConfigLoader(ResourceLoader):
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

    name = "platform"
    is_base = True

    @classmethod
    def create(cls, rm: Any) -> "PlatformConfigLoader":
        return cls(rm)

    #: 平台配置缓存的存活秒数。默认 300（5 分钟）。环境变量可覆盖。
    #: 命名参照 ``aidev_agent_cli.utils.LOG_FILE_OVERRIDE``（``BKAI_TUI_LOG_PATH``）的
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
        if _chains_through_retrieve_agent_config(rm):
            self.retrieve_agent_config = BaseResourceManager.retrieve_agent_config.__get__(rm, type(rm))
        else:
            self.retrieve_agent_config = rm.retrieve_agent_config

        #: 所属 manager。:meth:`merge_to_raw` 经它读 ``_platform_request`` 槽位（261014-urj
        #: 新增——链中传递 per-call 的 agent_code / version）。
        self._rm = rm

        #: ``agent_code`` → ``(平台配置, 拉取时刻的 monotonic 秒)``。
        self._cache: dict[tuple[str, str | None], tuple[Any, float]] = {}
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
        cached = self._cache.get((agent_code, version))

        if cached is not None and (now - cached[1]) < self.ttl_seconds():
            return cached[0]

        # 异常向上传播：调用方各自处理（retrieve_agent_config 传播，快照路径降级）。
        value = self.retrieve_agent_config(agent_code, version=version)
        self._cache[(agent_code, version)] = (value, now)

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
                raw.setdefault(key, copy.deepcopy(value))

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
                    f"dropping {len(servers)} platform-bound mcp server(s) (platform MCP disabled): {sorted(servers)}"
                )

            mcp["mcpServers"] = {}

        return raw
