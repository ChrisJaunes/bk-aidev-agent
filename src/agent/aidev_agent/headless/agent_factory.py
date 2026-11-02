"""通过 SDK 自带的工厂构建 worker 的 agent。

为什么不自己拼装执行图
----------------------
最直观的捷径是 ``ReActAgentBuilder().set_llm(...).set_tools(...)``。
但在这个应用里它是错的，因为这个 builder 只认一个 LLM 和一个工具列表。
而平台配置的其他一切——
agent 的角色提示词、知识库、技能、MCP server、模型上下文选项、审批策略，以及会话/事件的回写接线
都由 ``AgentInstanceFactory.build_agent`` 从一个经 ``ResourceManager`` 取回的 ``AgentConfig`` 拼装而成。

因此手工构建不仅会漏掉工具：它会静默地造出一个并非平台所描述的 agent。
工具列表只是可见的症状；其余部分在依赖它的功能出问题时才会暴露。

这里与 ``aidev_bkplugin`` 的 ``AgentBuilder``（生产环境的拼装路径）保持一致， 但只依赖 ``aidev_agent``，不引入 Django 应用。

注意：工作区的 ``mcp.json`` 由配置链上的 ``McpLoader`` 经 ``rm.mcp.load()``
lazy 读取（本模块已不再构造 manager）——任何拿到 ``rm`` 的消费方都会读到文件，
无需在别处加载或推送。
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Any

from aidev_agent.config import settings
from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.enums import AgentBuildType, AgentType
from aidev_agent.pydantic_models import AgentExecutorKwargs
from aidev_agent.services import agent as sdk_agent
from aidev_agent.services.common_agent import CommonQAAgent
from aidev_agent.services.event_handlers import agui_writer

logger = getLogger(__name__)

#: 本地 runtime 的路由名。工具的 ``target_runtime`` 参数按它解析 backend
#: （``RuntimeBackendResolver.resolve_backend``）。
LOCAL_RUNTIME_NAME = "local"

# 导入策略
# --------
# 本模块**全部依赖都在顶层导入**，没有函数内导入。
#
# 关于 stdout 守卫的顺序约束（改动前请先读完）
# ------------------------------------------
# `worker/__main__.py` 的顶层导入发生在 `main()`、也就是 `install_stdout_guard()`
# 之前（见 __main__.py:40）。SDK 一旦在导入期 print，杂散的一行就会污染 fd 1 上的
# NDJSON 协议流，表现为 TUI 卡住而不是报错。
#
# 这条约束**只约束 `__main__.py` 自身的顶层导入**。但导入链会传导：`__main__` 顶层
# 导入 `.dispatch`，`dispatch` 顶层导入 `.agent_runner`，所以 `agent_runner` 也在那条
# 路径上——**它曾经**因此用调用点导入来引用本模块。
#
# 261014-w6d 后续后不再是问题：`agent_runner` 顶层**早已**导入 `.resource_manager`，
# 而后者顶层导入 SDK（`aidev_agent...AgentResourceManager`），即 SDK 本就在那条链上。
# 因此"调用点导入以避免在 guard 前加载 SDK"这条理由早已失效，`agent_runner` 的函内
# import 已全部上移（那时也没有环——本模块从不导入 `.agent_runner`）。
#
# 本模块自己可以放心顶层导入，因为：
#   1. `__main__` 需要本模块时已在 `main()` 内按需导入（__main__.py:831）；
#   2. 实测导入整个 aidev_agent SDK 与 `aidev_agent.headless.checkpointer`，捕获的 stdout 与 fd 1
#      输出均为 0 字节——当前没有任何依赖会在导入期写 stdout。
#
# 若将来某个 SDK 版本开始在导入期 print，受影响的是 `__main__` 这条启动路径的导入
# 时机，与上面的分工一致，不是本模块的问题。
#
# 注 1：`.resource_manager` 自身把 LocalResourceManager 的类体放在函数里，是为它自己的
# _LOCAL_RESOURCE_MANAGER 缓存服务的，与本说明无关。
# 注 2：SDK 的 `AgentInstanceFactory` / `AGUISessionWriter` 刻意按**模块**导入
# （`sdk_agent.X` / `agui_writer.X`）而非 `from ... import X`，这样测试仍可
# `monkeypatch.setattr(sdk_agent, "AgentInstanceFactory", ...)` 施加影响。


def build_agent(
    rm: Any,
    session_code: str,
    agent_code: str,
    username: str | None = None,
    *,
    checkpointer: Any | None = None,
) -> Any:
    """为单个会话组装由平台描述的 agent。返回 ``agent``。

    **``rm`` 由调用方提供，本函数不再构造它。** 以前本函数会
    ``LocalResourceManager.build(...)`` 再建一个全新的 manager 并交给
    ``AgentInstanceFactory``；于是进程里同时存在两个 manager，各自携带不同的凭据状态：
    会话 token（``/login``）只被推到 ``SessionState.ensure_manager`` 那一个上，
    而 agent 用的是另一个——这正是沙箱创建报
    “user authentication failed, please provide a valid user identity” 的原因。
    由 ``AgentRunner`` 传入 ``state.ensure_manager()`` 的那个 manager，token 与运行时
    取值才同源。

    ``username`` 缺省时取 ``rm.username``：manager 已携带构建它的身份，无需再从 env 解析。

    ``agent_code`` 是**必填**的**传入参数**（261014-trk），本函数**不再**自行解析它。
    解析职责已上移到调用方（:meth:`aidev_agent.headless.agent_runner.SessionState._resolve_agent_code`，
    语义为环境变量 → 配置文件 → 平台默认）。这是有意的设计选择，不是遗漏：

    以前本函数从进程级状态（本地声明 + 环境变量）推导 ``agent_code``，于是
    “一个进程 = 一个 agent_code”成为隐含假设，与 SDK「按 ``agent_code`` 逐个拉配置」
    的子 agent 模型（``chat.py:2188`` 的
    ``get_agent_config(agent_code=child_agent_code, ...)``）冲突。入参化之后，
    **每个调用点都能为自己的 agent 指定 code**——子 agent 由此成为可能，而环境变量
    ``AGENT_TUI_AGENT_CODE`` 是进程级单值、无法表达这件事。

    因此参数**没有默认值**，也**没有**“缺省时回退到 env/配置/平台”的兼容分支：
    调用方必须显式给出。这是用户的明确裁决（261014-trk 的 D1），刻意不保留可选回退。

    ``session_code`` 必须已存在于平台上
    在 SESSION 模式下 SDK 会自行通过 ``resource_manager.get_chat_session_contents(session_code)``  拉取会话已存的历史，未知的 code 会返回 404。
    请先调用 :func:`ensure_session`。

    本轮的 ``/model`` 选择**不**经本函数：它由
    ``SessionState.current_model``（``/models`` 的持久选择），经注入的 ``runtime``
    被 loader 链读到。

    ``checkpointer`` 是 worker 的进程级 :class:`aidev_agent.headless.checkpointer.TuiSqliteSaver`
    （:meth:`aidev_agent.headless.agent_runner.SessionState.ensure_checkpointer`），由调用方解析后传入。
    ``None`` 是一个哨兵，表示“调用方未解析该单例”；SDK 工厂随后会抛出``ValueError: Checkpointer is required but not provided``（``chat.py:2206``）。
    这是**正确**且响亮的失败——**不要**用内存版 saver 去“修复”它：
    每次构建都易失的     saver 会让被中断那一轮的状态在下一轮不可读，而这正是这里要修的 bug。
    """
    user = username if username is not None else getattr(rm, "username", "")

    # 会话回写处理器不是可选项。`ChatCompletionAgent`
    # `_prepare_pre_run_history` 只有在 `event_handler` 是 `BaseSessionWriter`
    # 时才会继续，否则提前返回，而这个提前返回恰好决定了本轮的用户输入能否进入
    # 消息列表——没有它，这一轮会以
    # `ValueError: The messages list cannot be empty.` 失败。
    #
    # 在 stdio 下 writer 仍然访问平台 API，这是正确的：会话持久化本就只走远端，
    # 而 worker 自己并不持有会话存储。
    writer = agui_writer.AGUISessionWriter(
        session_code=session_code,
        client=rm.get_client(),
        username=user,
    )

    # 模型不经本函数：``SessionState.current_model`` 经注入的 ``runtime`` 被 loader
    # 链读到。
    agent = sdk_agent.AgentInstanceFactory.build_agent(
        agent_code=agent_code,
        agent_type=AgentType.CHAT,
        # 用 SESSION 而非 DIRECT。DIRECT 需要一个显式的 `session_context_data`
        # 参数，而 worker 从没有这个参数，于是 `chat_history` 回来是**空的**；
        # `InterruptProcessor.resolve_resumes` 随后没有 `role=INTERRUPT` 记录
        # 来路由待处理的 interruptId，于是静默跳过
        # （"resume item 未命中 chat_history interrupt 记录，跳过"），
        # 图重跑最终以 `ValueError: The messages list cannot be empty.` 失败。
        #
        # SESSION 会自行通过
        # `resource_manager.get_chat_session_contents(session_code)`
        # 加载历史（factory.py:268）——所以下面传了 `session_code=`。
        build_type=AgentBuildType.SESSION,
        session_code=session_code,
        # 不能传 `session_context_data`：它**仅**被工厂的 `_build_direct`
        # 路径消费（factory.py:193）。在 SESSION 下加上它是多余的；用它**代替**
        # SESSION 则会复活恢复相关的 bug。
        #
        # 注意：`LocalResourceManager` 未覆写
        # `get_chat_session_contents`，因此会落到
        # `BaseResourceManager.get_chat_session_contents`（base.py:195），
        # 那是一次由 manager 的 `access_token` 鉴权的平台 HTTP 拉取——与
        # `ensure_session` 已使用的 `/login` 通道相同。
        resource_manager=rm,
        username=user,
        checkpointer=checkpointer,
        event_handler=writer,
    )

    return agent


class TuiFileAgent(CommonQAAgent):
    """TUI worker 的通用 agent：在 executor 构建期启用本地文件 runtime。

    SDK 的官方扩展点（``common_agent/agent.py`` docstring）：继承
    :class:`CommonQAAgent` 覆写 :meth:`get_agent_executor`，由 ``__main__`` 在
    ``bootstrap_environment`` 之后经 ``common_agent_factory.replace_defaults``
    注入。除本地 runtime 外，构建行为与父类逐字一致（同一 ``ReActAgentBuilder``
    链，含父类的 ``enable_security_runtime(False)``）。

    **启用条件**（三者齐备，否则跳过、保持父类原样）：

    * ``AGENT_TUI_CWD`` 指向一个存在的目录——它是文件操作的根。绝不回退
      ``os.getcwd()``：worker 的 cwd 是 ``agent_tui/`` 模块根，回退会把模块目录
      当作用户项目（与 §1/§2 记载的同一个坑）。
    * ``options.runtime_backend_resolver`` 存在——会话自带的 resolver
      （chat.py 每会话构造、已带 scoping/defer 语义）。DIRECT 构建路径没有它，
      那不是错误。
    * ``virtual_mode=True`` 是硬编码的安全边界——默认 ``False`` 时绝对路径与
      ``..`` 可逃逸 root_dir，agent 将获得全盘读写（SDK docstring 明确警告）。
    """

    @classmethod
    def get_agent_executor(cls, **kwargs: Any) -> tuple[Any, Any]:
        options = AgentExecutorKwargs.model_validate(kwargs)
        builder = ReActAgentBuilder().set_bkai_options(options).enable_security_runtime(False)

        # 项目根 = ``AGENT_TUI_CWD``：entry.tsx 发布的**用户启动 TUI 时所在目录**
        # （Node 侧的 ``process.cwd()``）。**不能**用 ``os.getcwd()``——worker 子进程
        # 的 cwd 是 ``agent_tui/`` 模块根（stdio-transport 的 DEFAULT_WORKER_CWD），
        # 那会把文件工具锁到 TUI 自己的源码目录上。
        project_root = os.environ.get("AGENT_TUI_CWD", "").strip()
        resolver = getattr(options, "runtime_backend_resolver", None)

        if project_root and os.path.isdir(project_root) and resolver is not None:
            resolver.register_runtime(
                LOCAL_RUNTIME_NAME,
                FilesystemBackend(root_dir=project_root, virtual_mode=True),
            )
            builder.enable_runtime_local(True).set_enable_runtime_tool(True)
            logger.info(
                f"local file runtime enabled: runtime '{LOCAL_RUNTIME_NAME}' "
                f"rooted at {project_root}"
            )
        else:
            logger.info(
                f"local file runtime not enabled: project_root={bool(project_root)}, "
                f"resolver={resolver is not None}"
            )

        agent, cfg = builder.build()
        return agent, cfg


def ensure_session(
    rm: Any,
    session_code: str,
    *,
    session_name: str = "agent-tui",
) -> None:
    """幂等地让平台知晓 ``session_code``。

    worker 以前只在本地生成 ``thread-<uuid>`` 且不告知任何人。平台随后就没有该会话
    的任何记录——这就是列出会话总是返回空、以及针对一个全新 code 的首轮会以
    ``.../chat/session/<code>/`` 返回 404 失败的原因。
    """
    rm.get_or_create_session(
        session_code=session_code,
        session_name=session_name,
        protocol_version="",
    )


def describe_agent(agent: Any) -> str:
    """供日志面板使用的单行、不含密钥的摘要。"""
    tools = getattr(agent, "tools", None) or []
    names = [getattr(t, "name", str(t)) for t in tools]

    return (
        f"agent={type(agent).__name__} "
        f"agent_code={getattr(settings, 'APP_CODE', '?')} "
        f"model={getattr(agent, 'model_name', '?')} "
        f"tools={names}"
    )
