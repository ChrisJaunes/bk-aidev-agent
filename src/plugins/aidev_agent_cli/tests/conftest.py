# -*- coding: utf-8 -*-
"""插件侧测试配置。

插件不能复用 SDK 的 ``tests/conftest.py``，因此这里自持三项职责：
- 收集前把消息处理器切到 InMemory，避免连接 RabbitMQ/Redis；
- 校验测试前置依赖已安装；
- 把 ``~/.bkai`` 下的真实配置隔离到会话临时目录，任何用例都不得读取开发者
  机器上的 ``bkai_tui.json`` / ``mcp.json`` / ``agents/*.json``。

**不设静默跳过门禁**：checkpoint 相关用例（``tests/runtime``）
需要 sqlite saver，缺了就直接报错。
``make init`` 装的 dev 组已通过 ``aidev-agent-cli[checkpoint]`` 带上它；
若仍缺失，说明环境不是用本仓 Makefile 建的——此时「少收集却显示全绿」
比直接失败更危险。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

#: 测试依赖的 SQLite checkpoint saver（checkpoint 相关用例与 ``install_otel`` 探针都要）
REQUIRED_MODULES = ("langgraph.checkpoint.sqlite",)


#: 会话临时 home：``pytest_configure`` 里创建，供下面的会话级 fixture 复用。
_SESSION_HOME: Path | None = None


def pytest_configure(config):
    """收集前把消息处理器切到 InMemory，并把 ``HOME`` 指向会话临时目录。

    ``HOME`` 必须在**收集之前**就改掉：``utils.logging`` 在模块导入期
    用 ``Path.home()`` 算出 ``~/.bkai/logs/worker.log`` 并立刻打开它。若等到首个
    fixture 再改，那条日志文件已经写进开发者自己的 home 了。
    """
    global _SESSION_HOME

    os.environ["MESSAGE_HANDLER_TYPE"] = "inmemory"
    _SESSION_HOME = Path(tempfile.mkdtemp(prefix="aidev-cli-test-home-"))
    os.environ["HOME"] = str(_SESSION_HOME)
    # 真实 bkai_tui.json 的 env 会打开 OTel 埋点并真的向外部端点导出；测试不需要
    # 埋点，也不该产生这条外部副作用，关掉它（速度收益很小：SDK 链路反正会被
    # workspace_agent 导入，见 test 报告里的慢测试分析）。
    os.environ["AGENT_TUI_OTEL"] = "0"

    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            "缺失测试依赖: " + ", ".join(missing) + "。请在插件目录先执行 make init"
            "（dev 组含 aidev-agent-cli[checkpoint,opentelemetry]），"
            "不要在缺少依赖的环境里跑测试——那会静默少收集 checkpoint 相关用例。"
        )


@pytest.fixture(scope="session", autouse=True)
def _isolated_config_home() -> None:
    """把 ``~/.bkai`` 下所有**导入期固化**的默认路径再显式改指临时 home。

    ``BkAiConfig.USER_CONFIG_FILE`` / ``McpResolver.DEFAULT_MCP_FILE`` 在模块导入期
    用 ``Path.home()`` 固化。``pytest_configure`` 已经改了 ``HOME``，正常情况下它们
    自然落在临时 home；这里再按名字替换一次，保证即使这些模块在 ``pytest_configure``
    之前被某个插件提前导入，也不会残留指向开发者 home 的路径。运行时才解析的
    ``BkAiAgentInfo.user_path`` / ``BkAiConfig.project_path`` 只认 ``HOME``，已被
    ``pytest_configure`` 覆盖；``subprocess`` 拉起的子进程继承 ``os.environ``，同样
    落在这个临时 home。想测 ``install_otel`` 本身的行为，由
    ``tests/bootstrap/test_tui_main.py`` 的 autouse fixture 局部删掉
    ``AGENT_TUI_OTEL`` 开关。
    """
    from aidev_agent_cli.utils.logging import BKAI_HOME_DIRNAME
    from aidev_agent_cli.runtime.settings import BkAiConfig
    from aidev_agent_cli.resources.local_config import McpResolver

    home = _SESSION_HOME or Path(tempfile.mkdtemp(prefix="aidev-cli-test-home-"))
    mp = pytest.MonkeyPatch()
    mp.setattr(BkAiConfig, "USER_CONFIG_FILE", str(home / BKAI_HOME_DIRNAME / "bkai_tui.json"))
    mp.setattr(McpResolver, "DEFAULT_MCP_FILE", str(home / BKAI_HOME_DIRNAME / "mcp.json"))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolated_checkpoint_store(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程级 checkpoint 存储默认写 ~/.bkai：每个测试改指临时文件，绝不碰开发者自己的库。"""
    import os

    if not os.environ.get("BKAI_TUI_CHECKPOINT_FILE"):
        monkeypatch.setenv("BKAI_TUI_CHECKPOINT_FILE", str(tmp_path_factory.mktemp("ckpt") / "checkpoints.sqlite"))

@pytest.fixture(autouse=True)
def _isolated_runtime_observations(tmp_path_factory, monkeypatch):
    runtime_files = tmp_path_factory.mktemp("runtime-files")
    monkeypatch.setenv("BKAI_TUI_HISTORY_FILE", str(runtime_files / "history.sqlite"))
    monkeypatch.setenv("RUNTIME_TEST_CALLS", str(runtime_files / "tool-calls.ndjson"))
    monkeypatch.setenv("RUNTIME_TEST_BUILDS", str(runtime_files / "builds.ndjson"))