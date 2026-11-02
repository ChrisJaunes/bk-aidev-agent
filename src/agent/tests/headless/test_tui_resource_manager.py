"""Resource-manager tests: the override surface, the token channel, the overlay.

``LocalResourceManager`` is the promotion of the class that used to be inlined in
``agent_factory.py`` (D-01), so the first thing these tests do is re-assert the two
behaviours it inherited -- approval clearing and the model override -- because a
promotion that quietly dropped one of them would look like a clean refactor.

The rest pin what Phase 3 adds:

* the ``access_token`` constructor channel that existed in the SDK but was never
  passed (D-10), and the setter ``/login`` / ``/logout`` land on (D-11 / D-13);
* the local overlay, including the merge rule this phase settled -- scalars
  replace, collections append and de-duplicate, platform order first;
* that the token never reaches a log (T-03-01), which is a security assertion
  rather than a formatting one.

Nothing here touches the network. The manager is built from a plain creds dict and
``AgentResourceManager.get_agent_config`` is monkeypatched on the MRO, which is the
established technique from ``test_agent_factory.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from aidev_agent.headless import local_resources as resource_manager
from aidev_agent.headless.local_resources import LocalConfig
from aidev_agent.headless.local_resources import McpConfig
from aidev_agent.headless.local_resources import WorkspaceIndex

CREDS = {"BKPAAS_APP_CODE": "app", "BKPAAS_APP_SECRET": "sec"}


def _config(**overrides: Any) -> Any:
    """A real ``AgentConfig`` with the platform fields the overlay touches."""
    from aidev_agent.pydantic_models import AgentConfig

    fields: dict[str, Any] = {
        "agent_code": "test-agent",
        "chat_model": "platform-default",
        "non_thinking_llm": "platform-nonthinking",
        "tool_codes": ["weather_query"],
        "approval_settings": None,
        "mcp_server_config": {},
        "knowledgebase_ids": [],
    }
    fields.update(overrides)

    return AgentConfig.model_construct(**fields)


def _manager(monkeypatch: pytest.MonkeyPatch, config: Any, **kwargs: Any) -> Any:
    """Build a manager whose ``get_agent_config`` returns ``config``, no platform."""
    rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang", **kwargs)

    monkeypatch.setattr(
        type(rm).__mro__[1],  # AgentResourceManager
        "get_agent_config",
        lambda self, agent_code, version=None, **kw: config,
        raising=True,
    )

    return rm


def _payload_from_config(config: Any) -> dict[str, Any]:
    """把 ``_config(...)`` 的 ``AgentConfig`` 反投影成**平台 raw dict**。

    261014-rta 之前，``_manager`` 直接 stub 基类 ``get_agent_config`` 返回 ``_config``；
    链住在那一层，因此链真的被执行。链下移到 ``retrieve_agent_config`` 后，stub 基类不再
    拦截链——链会**完全不执行**（``_raw_manager`` 就是为此而生，见下）。本助手让既有
    用例可以把原先的 ``_config(...)`` 目标转成 raw 层载荷，从而断言不变。

    反投影只覆盖 §6 与 super() 实际读取的字段：``agent_code`` / ``agent_name`` /
    ``prompt_setting``（``llm_code`` / ``temperature`` / ``max_tokens`` /
    ``fallback_model`` / ``non_thinking_llm``）/ ``knowledgebase_settings
    .knowledgebases`` / ``mcp_server_config.mcpServers``（enveloped）/ ``approval_settings``。
    """
    prompt: dict[str, Any] = {}
    model = getattr(config, "chat_model", None)

    if model:
        prompt["llm_code"] = model

    for attr in ("temperature", "max_tokens", "fallback_model", "non_thinking_llm"):
        value = getattr(config, attr, None)

        if value is not None:
            prompt[attr] = value

    payload: dict[str, Any] = {
        "agent_code": getattr(config, "agent_code", "test-agent") or "test-agent",
        "agent_name": getattr(config, "agent_name", "") or "test-agent",
        "prompt_setting": prompt,
        "knowledgebase_settings": {"knowledgebases": list(getattr(config, "knowledgebase_ids", []) or [])},
        "mcp_server_config": {"mcpServers": dict(getattr(config, "mcp_server_config", None) or {})},
        "approval_settings": getattr(config, "approval_settings", None),
        # 平台字段：``super()`` 会原样映射到 ``AgentConfig``，反投影必须带上它们
        # （否则新增的端到端断言会读到缺失的键）。``tool_codes`` 来自
        # ``res["related_tools"]``（SDK base.py:431-439）。
        "related_tools": list(getattr(config, "tool_codes", []) or []),
    }

    return payload


def _apply_legacy_injection(rm: Any, monkeypatch: pytest.MonkeyPatch, *, local: Any = None, mcp: Any = None) -> None:
    """把旧的 ``local=`` / ``mcp=`` 构造注入翻译成 261014-urj 的新注入缝。

    构造参数瘦身后（D1/D7：``local`` / ``mcp`` / ``declarations`` / ``resolver`` /
    ``files`` 全删），测试用这两个替代缝：

    * ``local=LocalConfig(...)`` → patch 每个 loader 的 ``read_config``，使其对任意
      ``agent_code`` 返回同一份声明（``LocalAgentConfigLoader`` +
      ``KnowledgebaseLoader`` 都有该方法）。这是 D1 裁决给出的**正式注入缝**。
    * ``mcp=McpConfig(...)`` → 构造后 ``rm.mcp.seed(mcp)``（``seed`` 是既有公开 API）。

    ``mcp`` 未给出时**默认种子空配置**：链上的 ``McpLoader`` 现为 lazy 读盘，不隔离
    的话测试会读到仓库根的真实 ``mcp.json``。种子优先于读盘（``load`` 的既有语义），
    因此"无 mcp.json 贡献"成为夹具的默认事实。
    """
    if local is not None:
        for loader in rm._loaders:
            if hasattr(loader, "read_config"):
                monkeypatch.setattr(type(loader), "read_config", lambda self, code, _l=local: _l, raising=True)

    rm.mcp.seed(mcp if mcp is not None else None)


def _raw_manager(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], **kwargs: Any) -> Any:
    """一个**真实** manager：平台 payload 由 monkeypatch 注入 ``platform_loader``。

    与 ``_manager`` 的关键区别：它**不** stub 基类 ``get_agent_config``，而是替换
    ``rm.platform_loader.retrieve_agent_config``（链首 ``fetch`` 调用的正是它）。
    因此 ``rm.get_agent_config(...)`` 会真实走完整条链——这正是把链下移到 raw 层后
    唯一能真正见证链的构造方式。

    261014-urj：``local=`` / ``mcp=`` 不再作为构造参数；本助手把它们翻译成新注入缝
    （见 :func:`_apply_legacy_injection`），因此既有调用点的写法不变。
    """
    local = kwargs.pop("local", None)
    mcp = kwargs.pop("mcp", None)
    runtime = kwargs.pop("runtime", None)

    # ``current_model`` 住在**进程级** runtime 上（生产中是 ``SessionState``），不在
    # manager 上。缺省给一个空 runtime，使测试可以 ``rm.runtime.current_model = X``
    # 就地改当前取值——loader 每次都动态读它。
    if runtime is None:
        runtime = SimpleNamespace(current_model=None)

    rm = resource_manager.LocalResourceManager.build(
        CREDS, "judgehuang", runtime=runtime, **kwargs
    )

    _apply_legacy_injection(rm, monkeypatch, local=local, mcp=mcp)

    monkeypatch.setattr(
        rm.platform_loader,
        "retrieve_agent_config",
        lambda agent_code, version=None: payload,
        raising=True,
    )

    return rm


def _raw_manager_for(monkeypatch: pytest.MonkeyPatch, config: Any, **kwargs: Any) -> Any:
    """``_raw_manager`` 的便捷包装：把 ``_config(...)`` 反投影成 raw payload。"""
    return _raw_manager(monkeypatch, _payload_from_config(config), **kwargs)


class TestInheritance:
    def test_is_an_agent_resource_manager(self) -> None:
        from aidev_agent.packages.resource_manager.agent import AgentResourceManager

        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")

        assert issubclass(type(rm), AgentResourceManager)

    def test_get_client_is_inherited_not_overridden(self) -> None:
        """D-02's ``get_client()`` surface is met by inheritance.

        ``AgentResourceManager.get_client`` already re-assembles per call and reads
        ``self.access_token`` first, so an override would only duplicate the parent's
        body and create a second source of truth.
        """
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")
        owner = type(rm).__mro__[1]

        assert type(rm).get_client is owner.get_client
        assert "get_client" not in type(rm).__dict__


class TestAccessTokenChannel:
    def test_constructor_token_reaches_the_instance(self) -> None:
        """D-10: the SDK always accepted this argument; nothing ever passed it."""
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang", access_token="tok")

        assert rm.access_token == "tok"

    def test_absent_token_is_the_empty_string(self) -> None:
        """Unchanged behaviour when nobody has logged in."""
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")

        assert rm.access_token == ""

    def test_the_setter_sets_and_clears(self) -> None:
        """``/login`` sets it, ``/logout`` clears it back to app-state only (D-13)."""
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")

        rm.set_access_token("tok")
        assert rm.access_token == "tok"

        rm.set_access_token("")
        assert rm.access_token == ""

    def test_the_setter_normalises_none_to_empty(self) -> None:
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang", access_token="tok")

        rm.set_access_token(None)  # type: ignore[arg-type]

        assert rm.access_token == ""

    def test_app_credentials_survive_the_token(self) -> None:
        """D-09: the token is ADDED to the app state, not a replacement for it."""
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang", access_token="tok")

        assert rm.app_code == "app"
        assert rm.app_secret == "sec"
        assert rm.username == "judgehuang"


class TestPreservedBehaviours:
    """The two behaviours the promoted class must carry over verbatim.

    Reworked for 261014-rta: the chain now lives in ``retrieve_agent_config``, so
    these use ``_raw_manager_for`` (a real manager) rather than ``_manager`` (which
    stubs the base ``get_agent_config`` and would therefore skip the chain). The
    assertions are unchanged; a raw-layer equivalent was ADDED (approval is already
    ``{}`` on ``retrieve_agent_config``).
    """

    def test_clears_approval_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(
            approval_settings={
                "strategies": [{"strategy_id": "S", "approvers": ["judgehuang"]}],
                "bindings": [{"approval_strategy_id": "S", "tool_id": 10}],
            }
        )
        rm = _raw_manager_for(monkeypatch, cfg)

        result = rm.get_agent_config("test-agent")

        assert result.approval_settings == {}
        # Everything else survives -- this is a narrow override.
        assert result.tool_codes == ["weather_query"]
        assert result.chat_model == "platform-default"

        # 追加（261014-rta）：审批清空现在对**所有**读平台配置的路径生效，因此本方法
        # 的返回值上 ``approval_settings`` 也已是 ``{}``。
        assert rm.retrieve_agent_config("test-agent")["approval_settings"] == {}

    def test_tolerates_absent_approval_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _raw_manager_for(monkeypatch, _config(approval_settings=None))

        assert rm.get_agent_config("a").approval_settings == {}

    def test_overrides_the_main_model_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _raw_manager_for(monkeypatch, _config())
        rm.runtime.current_model = "deepseek-v3"

        result = rm.get_agent_config("test-agent")

        assert result.chat_model == "deepseek-v3"
        # D-07: the secondary models are the platform's, untouched.
        assert result.non_thinking_llm == "platform-nonthinking"
        assert result.tool_codes == ["weather_query"]

    def test_leaves_the_platform_model_when_none_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _raw_manager_for(monkeypatch, _config())
        rm.runtime.current_model = None

        assert rm.get_agent_config("test-agent").chat_model == "platform-default"


class TestLocalOverlay:
    # 261014-urj D8/A-4: ``rm.local`` 与 ``rm.local_agent_code()`` 已删除——
    # per-agent 化后不再有"进程级 single local"，入口 agent_code 的来源迁往
    # ``worker.config.TuiConfig.agent_code``。原 ``test_local_agent_code_is_surfaced`` /
    # ``test_local_agent_code_defaults_to_empty`` 因此**删除**（所测行为已不存在）。
    # 新的入口 agent_code 解析断言见 ``test_agent_runner.py`` 的
    # ``test_agent_code_comes_from_tui_config``。
    def test_a_declared_model_actually_takes_effect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The declaration must move ``chat_model``, not merely be recorded.

        ``models`` now lands inside ``prompt_setting.llm_code``.
        """
        rm = _raw_manager_for(monkeypatch, _config(), local=LocalConfig(prompt_setting={"llm_code": "m"}))

        assert rm.get_agent_config("test-agent").chat_model == "m"

    def test_a_declared_model_matching_the_platform_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = _raw_manager_for(
            monkeypatch, _config(chat_model="m"), local=LocalConfig(prompt_setting={"llm_code": "m"})
        )

        assert rm.get_agent_config("test-agent").chat_model == "m"

    def test_priority_current_model_beats_the_local_declaration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current_model > local.prompt_setting.llm_code > platform value."""
        rm = _raw_manager_for(
            monkeypatch,
            _config(chat_model="platform-default"),
            local=LocalConfig(prompt_setting={"llm_code": "local-model"}),
        )

        assert rm.get_agent_config("test-agent").chat_model == "local-model"

        rm.runtime.current_model = "turn-model"
        assert rm.get_agent_config("test-agent").chat_model == "turn-model"

    def test_mcp_servers_are_appended_after_the_platforms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The merge rule: the platform's bindings are the base, local adds."""
        cfg = _config(mcp_server_config={"bound": {"command": "x"}})
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}),
        )

        result = rm.get_agent_config("test-agent")

        assert list(result.mcp_server_config) == ["bound", "local"]
        # The platform's own entry is not touched.
        assert result.mcp_server_config["bound"] == {"command": "x"}

    def test_mcp_servers_already_bound_are_not_duplicated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(mcp_server_config={"bound": {}})
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            local=LocalConfig(mcp_server_config={"mcpServers": {"bound": {}, "new": {}}}),
        )

        result = rm.get_agent_config("test-agent")

        assert list(result.mcp_server_config) == ["bound", "new"]

    def test_knowledgebases_are_appended_after_the_platforms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(knowledgebase_ids=["bound-kb"])
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            local=LocalConfig(knowledgebase_settings={"knowledgebases": ["local-kb", "bound-kb"]}),
        )

        result = rm.get_agent_config("test-agent")

        # Platform first, then genuinely new local entries; the duplicate is dropped.
        assert result.knowledgebase_ids == ["bound-kb", "local-kb"]

    def test_an_empty_overlay_leaves_the_config_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The normal case: most users never create the file."""
        cfg = _config(mcp_server_config={"bound": {}}, knowledgebase_ids=["kb"])
        rm = _raw_manager_for(monkeypatch, cfg)

        result = rm.get_agent_config("test-agent")

        assert result.mcp_server_config == {"bound": {}}
        assert result.knowledgebase_ids == ["kb"]


class TestLocalMcpDefinitions:
    """MCP is ADD across every source (user ruling) — the third source no longer replaces.

    ``agent.json``'s ``mcpServers`` and ``mcp.json``'s ``mcpServers`` are both
    ADD-only: a same-named entry keeps the FIRST definition seen, and the ORDER is
    first-wins. The ONLY exception is "a definition can complete a placeholder":
    a later source may fill in a definition where an earlier source had only ``{}``.
    """

    def test_definitions_land_in_the_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not just the name — the transport/url the SDK actually needs."""
        rm = _raw_manager_for(
            monkeypatch,
            _config(mcp_server_config={}),
            mcp=McpConfig(servers={"local-url": {"url": "https://x.invalid", "transport": "streamable_http"}}),
        )

        result = rm.get_agent_config("test-agent")

        assert result.mcp_server_config == {
            "local-url": {"url": "https://x.invalid", "transport": "streamable_http"}
        }

    def test_a_definition_does_not_replace_a_same_named_platform_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADD, not REPLACE — the first definition wins, the later one is dropped."""
        platform_entry = {"url": "https://platform.invalid", "mcp_type": "http"}
        cfg = _config(mcp_server_config={"shared": dict(platform_entry)})
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            mcp=McpConfig(servers={"shared": {"url": "https://local.invalid", "transport": "sse"}}),
        )

        result = rm.get_agent_config("test-agent")

        # The platform's definition survives; mcp.json did NOT override it.
        assert result.mcp_server_config == {"shared": platform_entry}
        # One entry, not two — de-duplicated, not shadowed.
        assert list(result.mcp_server_config) == ["shared"]

    def test_a_definition_completes_a_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The nuance: an earlier ``{}`` placeholder CAN be completed by a definition."""
        rm = _raw_manager_for(
            monkeypatch,
            _config(mcp_server_config={"shared": {}}),  # placeholder only
            mcp=McpConfig(servers={"shared": {"url": "https://local.invalid", "transport": "sse"}}),
        )

        result = rm.get_agent_config("test-agent")

        assert result.mcp_server_config == {"shared": {"url": "https://local.invalid", "transport": "sse"}}

    def test_platform_entries_with_no_local_counterpart_survive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding is per-name. Everything else the platform bound is untouched."""
        cfg = _config(mcp_server_config={"platform-only": {"url": "https://p.invalid"}})
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            mcp=McpConfig(servers={"local-only": {"url": "https://l.invalid", "transport": "sse"}}),
        )

        result = rm.get_agent_config("test-agent")

        assert result.mcp_server_config["platform-only"] == {"url": "https://p.invalid"}
        assert set(result.mcp_server_config) == {"platform-only", "local-only"}

    def test_a_name_in_both_local_files_collapses_to_the_definition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``agent.json``'s ``{}`` placeholder is completed by ``mcp.json``, not duplicated."""
        rm = _raw_manager_for(
            monkeypatch,
            _config(mcp_server_config={}),
            local=LocalConfig(mcp_server_config={"mcpServers": {"both": {}, "name-only": {}}}),
            mcp=McpConfig(servers={"both": {"url": "https://b.invalid", "transport": "sse"}}),
        )

        result = rm.get_agent_config("test-agent")

        assert list(result.mcp_server_config) == ["both", "name-only"]
        assert result.mcp_server_config["both"] == {"url": "https://b.invalid", "transport": "sse"}
        # The name with no definition keeps its placeholder — the name is still
        # real (it reaches the tool list) even though it contributes no settings.
        assert result.mcp_server_config["name-only"] == {}

    def test_a_definition_with_no_declared_name_still_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``mcp.json`` is hand-written and never touched by ``/mcps``.

        Requiring an entry in ``agent.json`` first would mean a user who just
        wants to add a server has to declare it twice.
        """
        rm = _raw_manager_for(
            monkeypatch,
            _config(mcp_server_config={}),
            local=LocalConfig(),
            mcp=McpConfig(servers={"hand-written": {"command": "npx", "transport": "stdio"}}),
        )

        assert set(rm.get_agent_config("test-agent").mcp_server_config) == {"hand-written"}

    def test_a_none_config_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pydantic field defaults to ``None`` and the SDK cannot take it.

        ``construct_mcp`` calls ``new_server_config.items()`` (``base.py:576``), so a
        ``None`` that survives this merge raises ``AttributeError`` on the first
        turn — far from the config that caused it.
        """
        rm = _raw_manager_for(monkeypatch, _config(mcp_server_config=None))

        assert rm.get_agent_config("test-agent").mcp_server_config == {}

    def test_a_none_config_with_definitions_becomes_a_real_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = _raw_manager_for(
            monkeypatch,
            _config(mcp_server_config=None),
            mcp=McpConfig(servers={"a": {"url": "https://a.invalid", "transport": "sse"}}),
        )

        assert rm.get_agent_config("test-agent").mcp_server_config == {
            "a": {"url": "https://a.invalid", "transport": "sse"}
        }

    def test_the_platform_dict_is_not_mutated_in_place(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The merge builds a new dict, so a reused config object stays intact.

        ``_config`` hands the SAME ``AgentConfig`` to every ``get_agent_config``
        call, which mirrors the SDK handing back a fresh one — but a merge that
        mutated ``cfg.mcp_server_config`` directly would leak local entries into a
        config that a later call (a sub-agent's) then reads.
        """
        original = {"platform-only": {"url": "https://p.invalid"}}
        cfg = _config(mcp_server_config=original)
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            mcp=McpConfig(servers={"local": {"url": "https://l.invalid", "transport": "sse"}}),
        )

        rm.get_agent_config("test-agent")

        assert original == {"platform-only": {"url": "https://p.invalid"}}


class TestLazyMcpRead:
    """The lazy read path the config chain uses (``McpLoader`` → ``mcp.load()``)."""

    def test_a_seeded_config_takes_effect_on_the_next_pull(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The reason this exists: changed definitions must not need a worker restart."""
        rm = _raw_manager_for(monkeypatch, _config(mcp_server_config={}))

        assert rm.get_agent_config("test-agent").mcp_server_config == {}

        rm.mcp.seed(McpConfig(servers={"added-later": {"url": "https://x.invalid", "transport": "sse"}}))

        assert set(rm.get_agent_config("test-agent").mcp_server_config) == {"added-later"}

    def test_a_none_seed_resets_rather_than_keeping_the_previous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deleted file must be observable, not silently retained."""
        rm = _raw_manager_for(
            monkeypatch,
            _config(mcp_server_config={}),
            mcp=McpConfig(servers={"a": {"url": "https://a.invalid", "transport": "sse"}}),
        )

        rm.mcp.seed(None)

        assert rm.get_agent_config("test-agent").mcp_server_config == {}

    def test_the_initial_state_is_an_empty_config_not_none(self) -> None:
        """Never ``None`` on an instance: the merge reads ``.servers`` unconditionally."""
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")

        assert rm.mcp.config.is_empty()


class TestMcpDefinitionsNeverReachTheLogs:
    """T-03-01 applied to the second credential carrier in this module.

    A real ``mcp.json`` holds ``bk_app_secret`` / ``access_token`` inside
    ``headers``, and the merge logs what it did. Names and counts only.
    """

    def test_no_server_dict_content_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _config(mcp_server_config={"replaced": {"url": "https://platform.invalid"}})
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            local=LocalConfig(
                mcp_server_config={"mcpServers": {"replaced": {}, "added-by-name": {}}}
            ),
            mcp=McpConfig(
                servers={
                    # ``added-by-mcp`` is new to the merge, so the merge log line
                    # has a name to show — that is the assertion target below.
                    "added-by-mcp": {"url": "https://secret-host.invalid/mcp"},
                    "replaced": {
                        "url": "https://secret-host.invalid/mcp",
                        "credential_type": "blueapps",
                        "headers": {"X-Bkapi-Authorization": "SECRET-TOKEN-VALUE"},
                    },
                }
            ),
        )

        rm.get_agent_config("test-agent")
        # Pull again — with the lazy read (``McpLoader`` → ``mcp.load()``) every
        # pull re-runs the merge. The assertion below is unchanged: a re-read must
        # not log server dict contents either.
        rm.get_agent_config("test-agent")
        printed = capsys.readouterr()

        for forbidden in ("SECRET-TOKEN-VALUE", "secret-host.invalid", "X-Bkapi-Authorization"):
            assert forbidden not in printed.err, f"{forbidden!r} reached stderr"
            assert forbidden not in printed.out, f"{forbidden!r} reached stdout"

        # Names and counts ARE logged — that is the diagnostic value.
        assert "added-by-mcp" in printed.err


class TestPlatformMcpKillSwitch:
    """``AGENT_TUI_DISABLE_PLATFORM_MCP`` drops the platform's base, not the overlay.

    The one place local configuration REMOVES rather than adds. Needed because a
    platform-bound MCP server with a dead endpoint is otherwise unavoidable from the
    client side, and one unresponsive server blocks the whole agent build (the SDK's
    MCP fetch has no per-server timeout).
    """

    def test_it_drops_the_platforms_bindings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_TUI_DISABLE_PLATFORM_MCP", "1")
        cfg = _config(mcp_server_config={"space-mcp": {"url": "https://dead.invalid", "transport": "sse"}})
        rm = _raw_manager_for(monkeypatch, cfg)

        assert rm.get_agent_config("test-agent").mcp_server_config == {}

    def test_it_leaves_local_servers_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It drops the platform's BASE — the local overlay is a separate layer.

        Otherwise the switch would be useless for the case it exists for: keeping
        your own working servers while excluding a dead platform one.
        """
        monkeypatch.setenv("AGENT_TUI_DISABLE_PLATFORM_MCP", "1")
        cfg = _config(mcp_server_config={"space-mcp": {"url": "https://dead.invalid"}})
        rm = _raw_manager_for(
            monkeypatch,
            cfg,
            local=LocalConfig(mcp_server_config={"mcpServers": {"declared-locally": {}}}),
            mcp=McpConfig(servers={"from-file": {"url": "https://good.invalid", "transport": "sse"}}),
        )

        result = rm.get_agent_config("test-agent")

        assert "space-mcp" not in result.mcp_server_config
        assert set(result.mcp_server_config) == {"declared-locally", "from-file"}

    def test_it_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_TUI_DISABLE_PLATFORM_MCP", raising=False)
        cfg = _config(mcp_server_config={"kept": {"url": "https://p.invalid"}})
        rm = _raw_manager_for(monkeypatch, cfg)

        assert set(rm.get_agent_config("test-agent").mcp_server_config) == {"kept"}

    def test_it_tolerates_an_empty_platform_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing to drop must not log a drop, and must not raise."""
        monkeypatch.setenv("AGENT_TUI_DISABLE_PLATFORM_MCP", "1")
        rm = _raw_manager_for(monkeypatch, _config(mcp_server_config={}))
        capsys.readouterr()

        assert rm.get_agent_config("test-agent").mcp_server_config == {}
        assert "dropping" not in capsys.readouterr().err


class TestTokenNeverReachesTheLogs:
    """T-03-01 / D-12: the token is a credential and stderr is still a log."""

    def test_token_never_reaches_the_logs(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rm = _manager(monkeypatch, _config(), access_token="SECRET-TOK")

        rm.set_access_token("SECRET-TOK")
        rm.set_access_token("SECRET-TOK-2")
        rm.get_agent_config("test-agent")
        rm.set_access_token("")

        captured = capsys.readouterr()

        assert "SECRET-TOK" not in captured.out
        assert "SECRET-TOK" not in captured.err
        assert "SECRET-TOK-2" not in captured.out
        assert "SECRET-TOK-2" not in captured.err
        # Presence is logged; the value is not.
        assert "access token set" in captured.err
        assert "access token cleared" in captured.err


class TestModuleImportsTheSDK:
    def test_module_import_imports_the_sdk(self) -> None:
        """``LocalResourceManager`` 现在是一个**普通模块级类**。

        这曾是一条“不导入 SDK”的契约，靠 ``exec`` + 字符串类体延迟基类解析来守住。
        该反射机制已移除（用户判定 resource_manager 是对外接口，必须能正常导入）：
        延迟导入只省下 worker 启动首帧约 1.2 s，却让公开名字成为 ``exec`` 生成的
        门面（无 IDE 跳转/类型检查，``is`` 反直觉）。接口正常优先。

        本测试因此断言**相反**的事实：导入本模块会连带导入 ``aidev_agent``。
        若将来有人重新引入延迟解析，这条测试会立刻失败并指向上面的取舍说明。
        """
        code = (
            "import sys;"
            "sys.path.insert(0, '.');"
            "import aidev_agent.headless.local_resources as r;"
            "mods=[m for m in sys.modules if m.startswith('aidev_agent')];"
            "print('SDK_MODULES=' + str(len(mods)))"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            timeout=120,
        )

        assert "SDK_MODULES=0" not in result.stdout, result.stdout + result.stderr
        # 非零即可；不钉死具体数目，否则上游每加一个模块都会误报。
        assert "SDK_MODULES=" in result.stdout, result.stdout + result.stderr

    def test_the_class_is_a_real_module_level_class(self) -> None:
        """公开名字必须就是真类——这正是移除反射的**目的**。

        ``is`` 必须为 ``True``（没有门面），``__module__`` 必须指向本模块，
        且它是 ``AgentResourceManager`` 的真子类。
        """
        import aidev_agent.headless.local_resources as rm
        from aidev_agent.packages.resource_manager.agent import AgentResourceManager

        assert rm.LocalResourceManager.__module__ == "aidev_agent.headless.local_resources"
        assert issubclass(rm.LocalResourceManager, AgentResourceManager)
        assert hasattr(rm.LocalResourceManager, "build")

    def test_the_lazy_exec_machinery_is_gone(self) -> None:
        """反射机制的四个部件必须全部不存在，防止悄悄回退到旧设计。"""
        import aidev_agent.headless.local_resources as rm

        for name in (
            "_LOCAL_RESOURCE_MANAGER_SOURCE",
            "_LocalResourceManagerMeta",
            "_local_resource_manager_class",
            "_agent_resource_manager_base",
            "_LOCAL_RESOURCE_MANAGER",
        ):
            assert not hasattr(rm, name), f"lazy-exec machinery came back: {name}"


# ============================================================================
# Local declaration layer (§1 — <BKAI_HOME>/agent.json, two scopes)
# ============================================================================

from pathlib import Path
from types import SimpleNamespace


#: §1 的配置面句柄。常量与路径辅助已收进 :class:`LocalDeclarations`
#: （类属性/类方法），因此断言统一按 ``local_config.X`` 书写。
#:
#: 用 ``SimpleNamespace`` 而非别名到类，是为了让
#: ``monkeypatch.setattr(local_config, ...)`` 只改这个命名空间、**不会**污染真类
#: 上其他测试共享的状态。与 §2 的 ``mcp_config`` 句柄同一模式。
local_config = SimpleNamespace(
    CONFIG_FILE_OVERRIDE=resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE,
    DECLARATION_FIELDS=resource_manager.LocalDeclarations.DECLARATION_FIELDS,
    LOCAL_CONFIG_KEYS=resource_manager.LocalDeclarations.LOCAL_CONFIG_KEYS,
    REJECTED_SECRET_KEYS=resource_manager.LocalDeclarations.REJECTED_SECRET_KEYS,
    AGENT_CODE_ENV=resource_manager.LocalDeclarations.AGENT_CODE_ENV,
    AGENTS_DIRNAME=resource_manager.LocalDeclarations.AGENTS_DIRNAME,
    user_path=resource_manager.LocalDeclarations.user_path,
    project_path=resource_manager.LocalDeclarations.project_path,
    user_agents_dir=resource_manager.LocalDeclarations.user_agents_dir,
    project_agents_dir=resource_manager.LocalDeclarations.project_agents_dir,
    # LocalConfig 是值对象，刻意留在模块级——句柄里一并带上，使 §1 断言可统一书写。
    LocalConfig=resource_manager.LocalConfig,
)


def _declarations() -> resource_manager.LocalDeclarations:
    """A fresh ``LocalDeclarations`` collaborator (the §1 owner).

    The §1 assertion set is unchanged from when it exercised the module-level
    ``load_local_config`` / ``save_local_declaration``; only the ACCESS PATH moved —
    the operations now live on the collaborator that owns the concern.
    """
    return resource_manager.LocalDeclarations()


def _pin_single_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Pin the per-agent scope to a SINGLE user-space file for shape assertions.

    ``read(path)`` already takes an explicit path, but tests that go through the
    no-argument path (resolution order, writes) need the user scope redirected too.
    ``AGENT_TUI_CONFIG_FILE`` is the single-file override, which short-circuits the
    per-agent two-level scope entirely — the writes in these tests land exactly here.
    """
    monkeypatch.setenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, str(path))
    monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)


class TestLocalConfigShape:
    """The new shape carries the platform AgentConfig surface — and no token field."""

    def test_defaults_match_the_platform_conventions(self) -> None:
        cfg = local_config.LocalConfig()

        assert cfg.tenant_id == "system"
        assert cfg.agent_type == "single"
        assert cfg.prompt_setting == {}
        assert cfg.mcp_server_config == {}
        assert cfg.is_empty()

    def test_no_token_class_field_exists(self) -> None:
        """D5: the credential rejection is structural — there is no field to hold it."""
        import dataclasses

        names = {f.name for f in dataclasses.fields(local_config.LocalConfig)}

        assert not (set(local_config.REJECTED_SECRET_KEYS) & names), names

    def test_the_whitelist_keys_come_from_the_field_surface(self) -> None:
        import dataclasses

        assert local_config.LOCAL_CONFIG_KEYS == tuple(
            f.name for f in dataclasses.fields(local_config.LocalConfig)
        )
        assert not (set(local_config.REJECTED_SECRET_KEYS) & set(local_config.LOCAL_CONFIG_KEYS))


class TestTwoLevelScope:
    """D4 (intra-config half): user space, then project overrides user.

    261014-urj: 路径从固定 ``agent.json`` 收窄为 per-agent ``agents/${code}.json``——
    用户空间与项目空间**对称**，都用 ``agents/`` 子目录。
    """

    def test_user_and_project_paths_derive_from_the_constant(self) -> None:
        from aidev_agent.headless.utils import BKAI_HOME_DIRNAME

        assert local_config.user_path("code-a").endswith(f"{BKAI_HOME_DIRNAME}/agents/code-a.json")
        assert BKAI_HOME_DIRNAME in local_config.user_path("code-a")

    def test_the_filename_is_per_agent_not_a_fixed_name(self) -> None:
        """``agent.json`` 已退役：文件名派生自 ``agent_code``。"""
        assert local_config.user_path("alpha").endswith("/alpha.json")
        assert local_config.user_path("beta").endswith("/beta.json")
        assert "agent.json" not in local_config.user_path("gamma")

    def test_project_path_is_omitted_without_a_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        assert local_config.project_path("code-a") is None

    def test_project_path_follows_the_cwd_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(resource_manager.LocalDeclarations.CWD_ENV, str(tmp_path))

        from aidev_agent.headless.utils import BKAI_HOME_DIRNAME

        assert local_config.project_path("code-a") == str(
            tmp_path / BKAI_HOME_DIRNAME / "agents" / "code-a.json"
        )

    def test_agents_dirs_are_symmetric_between_scopes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """用户空间与项目空间对称：都用 ``.bkai/agents``。"""
        from aidev_agent.headless.utils import BKAI_HOME_DIRNAME

        monkeypatch.setenv(resource_manager.LocalDeclarations.CWD_ENV, str(tmp_path))

        assert local_config.user_agents_dir().endswith(f"{BKAI_HOME_DIRNAME}/agents")
        assert local_config.project_agents_dir() == str(tmp_path / BKAI_HOME_DIRNAME / "agents")

    def test_both_scopes_are_read_and_project_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一 ``agent_code`` 的用户/项目两份文件，项目覆盖用户。"""
        project_dir = tmp_path / "proj" / ".bkai" / "agents"
        monkeypatch.setattr(
            resource_manager.LocalDeclarations, "user_agents_dir", classmethod(lambda cls: str(tmp_path / "home"))
        )
        monkeypatch.setattr(
            resource_manager.LocalDeclarations, "project_agents_dir", classmethod(lambda cls: str(project_dir))
        )
        user = tmp_path / "home" / "code-a.json"
        user.parent.mkdir()
        user.write_text(
            json.dumps({"agent_code": "user", "agent_name": "user-name"}),
            encoding="utf-8",
        )
        project_dir.mkdir(parents=True)
        (project_dir / "code-a.json").write_text(
            json.dumps({"agent_name": "project-name"}),
            encoding="utf-8",
        )

        cfg = _declarations().read(code="code-a")

        # project overrides the scalar it declares...
        assert cfg.agent_name == "project-name"
        # ...and the user's other scalar survives.
        assert cfg.agent_code == "user"

    def test_nested_sections_merge_key_by_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nested dict sections merge per key — a project value overrides only its key."""
        project_dir = tmp_path / "proj" / ".bkai" / "agents"
        monkeypatch.setattr(
            resource_manager.LocalDeclarations, "user_agents_dir", classmethod(lambda cls: str(tmp_path / "home"))
        )
        monkeypatch.setattr(
            resource_manager.LocalDeclarations, "project_agents_dir", classmethod(lambda cls: str(project_dir))
        )
        user = tmp_path / "home" / "code-a.json"
        user.parent.mkdir()
        user.write_text(
            json.dumps(
                {
                    "prompt_setting": {"llm_code": "user-model", "temperature": 0.5},
                    "mcp_server_config": {"mcpServers": {"a": {"url": "https://a.invalid"}}},
                }
            ),
            encoding="utf-8",
        )
        project_dir.mkdir(parents=True)
        (project_dir / "code-a.json").write_text(
            json.dumps(
                {
                    "prompt_setting": {"llm_code": "project-model"},
                    "mcp_server_config": {"mcpServers": {"b": {"url": "https://b.invalid"}}},
                }
            ),
            encoding="utf-8",
        )

        cfg = _declarations().read(code="code-a")

        # The overridden key wins; the user's sibling key survives.
        assert cfg.prompt_setting == {"llm_code": "project-model", "temperature": 0.5}
        # mcpServers merges per key too: both names are present.
        assert cfg.mcp_server_config == {
            "mcpServers": {
                "a": {"url": "https://a.invalid"},
                "b": {"url": "https://b.invalid"},
            }
        }

    def test_no_code_and_no_path_yields_an_empty_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """per-agent 模型下没有"默认文件"——无 code 且无 path/override 时读空。"""
        monkeypatch.delenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, raising=False)
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        assert _declarations().read() == local_config.LocalConfig()

    @pytest.mark.parametrize("code", ["../escape", "a/b", "a\\b", ".", "..", ".hidden", ""])
    def test_a_traversing_agent_code_is_rejected_on_read(
        self, code: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """T-261014urj-03: ``agent_code`` 含路径穿越字符时拒绝读取（空配置 + 一行日志）。"""
        monkeypatch.delenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, raising=False)
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        with caplog.at_level("INFO"):
            cfg = _declarations().read(code=code)

        assert cfg == local_config.LocalConfig()

    @pytest.mark.parametrize("code", ["../escape", "a/b", "a\\b", "..", ".hidden"])
    def test_a_traversing_agent_code_is_rejected_on_write(
        self, code: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-261014urj-03: 含路径穿越的 ``agent_code`` 在写入时**响亮失败**。"""
        monkeypatch.delenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, raising=False)
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        with pytest.raises(ValueError):
            _declarations().write("models", "m", code=code)


class TestAgentCodeResolution:
    """D3: environment -> config file -> platform default."""

    def test_environment_beats_the_config_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(local_config.AGENT_CODE_ENV, "from-env")
        cfg = local_config.LocalConfig(agent_code="from-file")

        assert resource_manager.LocalDeclarations.resolve_agent_code(
            cfg, fallback=lambda: "platform"
        ) == "from-env"

    def test_config_file_beats_the_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(local_config.AGENT_CODE_ENV, raising=False)
        cfg = local_config.LocalConfig(agent_code="from-file")

        assert resource_manager.LocalDeclarations.resolve_agent_code(
            cfg, fallback=lambda: "platform"
        ) == "from-file"

    def test_the_platform_fallback_is_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(local_config.AGENT_CODE_ENV, raising=False)

        assert resource_manager.LocalDeclarations.resolve_agent_code(
            local_config.LocalConfig(), fallback=lambda: "platform"
        ) == "platform"

    def test_missing_everywhere_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(local_config.AGENT_CODE_ENV, raising=False)

        assert resource_manager.LocalDeclarations.resolve_agent_code(local_config.LocalConfig()) == ""


class TestLoadLocalConfig:
    def test_reads_every_declared_field(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "agent_id": "id-1",
                    "agent_code": "x",
                    "agent_name": "name",
                    "space_id": "space",
                    "prompt_setting": {"llm_code": "m"},
                    "mcp_server_config": {"mcpServers": {"a": {"url": "https://a.invalid"}}},
                    "knowledgebase_settings": {"knowledgebases": ["k1", "k2"]},
                }
            ),
            encoding="utf-8",
        )

        cfg = _declarations().read(path=str(path))

        assert cfg.agent_id == "id-1"
        assert cfg.agent_code == "x"
        assert cfg.agent_name == "name"
        assert cfg.space_id == "space"
        assert cfg.prompt_setting == {"llm_code": "m"}
        assert cfg.knowledgebase_settings == {"knowledgebases": ["k1", "k2"]}
        assert not cfg.is_empty()

    def test_missing_file_is_not_fatal(self, tmp_path: Path) -> None:
        cfg = _declarations().read(path=str(tmp_path / "nope.json"))

        assert cfg == local_config.LocalConfig()
        assert cfg.is_empty()

    def test_malformed_json_is_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.json"
        path.write_text("{not json", encoding="utf-8")

        assert _declarations().read(path=str(path)) == local_config.LocalConfig()

    def test_a_non_object_toplevel_is_not_fatal(self, tmp_path: Path) -> None:
        # A list and a bare string are both valid JSON and both useless here.
        for body in ('[1, 2]', '"str"'):
            path = tmp_path / "config.json"
            path.write_text(body, encoding="utf-8")

            assert _declarations().read(path=str(path)) == local_config.LocalConfig()

    def test_a_wrongly_typed_field_degrades_alone(self, tmp_path: Path) -> None:
        """One bad field must not discard the others.

        Narrowing is per-field: ``agent_code`` and ``prompt_setting`` are dropped to
        their empty values while the valid fields still load.
        """
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "agent_code": 123,  # not a string
                    "agent_name": "ok-name",
                    "prompt_setting": "x",  # not an object
                    "knowledgebase_settings": {"knowledgebases": ["ok-kb"]},
                }
            ),
            encoding="utf-8",
        )

        cfg = _declarations().read(path=str(path))

        assert cfg.agent_code == ""
        assert cfg.prompt_setting == {}
        assert cfg.agent_name == "ok-name"
        assert cfg.knowledgebase_settings == {"knowledgebases": ["ok-kb"]}

    def test_a_bare_section_is_tolerated_for_mcp(self, tmp_path: Path) -> None:
        """A user may write the ``mcpServers`` map without the ``mcp_server_config``
        envelope — accept it, since the shape is otherwise easy to get wrong."""
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps({"mcpServers": {"a": {"url": "https://a.invalid"}}}),
            encoding="utf-8",
        )

        # Not the canonical key: only the canonical envelope is read. The bare key is
        # simply not recognised, so the field degrades alone to {}.
        assert _declarations().read(path=str(path)).mcp_server_config == {}

    def test_legacy_alias_names_are_still_accepted(self, tmp_path: Path) -> None:
        """The old narrow shape normalises into the new fields — one canonical spelling."""
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "agent_code": "a",
                    "default_model": "m",
                    "mcp_servers": ["x"],
                    "knowledgebases": ["k"],
                }
            ),
            encoding="utf-8",
        )

        cfg = _declarations().read(path=str(path))

        assert cfg.prompt_setting == {"llm_code": "m"}
        assert cfg.mcp_server_config == {"mcpServers": {"x": {}}}
        assert cfg.knowledgebase_settings == {"knowledgebases": ["k"]}

    def test_token_class_keys_are_dropped(self, tmp_path: Path) -> None:
        """D-12: the file is not a carrier for the token, and the reader says so."""
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "agent_code": "a",
                    "access_token": "SECRET-TOK",
                    "bk_token": "SECRET-BK",
                }
            ),
            encoding="utf-8",
        )

        cfg = _declarations().read(path=str(path))

        assert cfg.agent_code == "a"
        # No field of LocalConfig can hold it -- this is structural, not a filter.
        assert "SECRET-TOK" not in repr(cfg)
        assert "SECRET-BK" not in repr(cfg)
        assert not hasattr(cfg, "access_token")


class TestSaveLocalDeclaration:
    def _path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        target = tmp_path / "agent.json"
        _pin_single_file(monkeypatch, target)

        return str(target)

    def test_scalars_replace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._path(tmp_path, monkeypatch)

        _declarations().write("agents", "local-a")
        assert _declarations().read().agent_code == "local-a"

        # ``models`` now lands inside ``prompt_setting.llm_code``.
        _declarations().write("models", "m1")
        assert _declarations().read().prompt_setting == {"llm_code": "m1"}

    def test_collections_append_and_de_duplicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._path(tmp_path, monkeypatch)

        _declarations().write("mcps", "a")
        _declarations().write("mcps", "b")
        # Writing the same value again is a no-op, not a duplicate.
        _declarations().write("mcps", "a")

        assert _declarations().read().mcp_server_config == {"mcpServers": {"a": {}, "b": {}}}

        _declarations().write("knowledge", "k1")
        _declarations().write("knowledge", "k2")
        assert _declarations().read().knowledgebase_settings == {"knowledgebases": ["k1", "k2"]}

    def test_unrelated_keys_survive_a_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A write is a local update, never a whole-file replacement."""
        path = self._path(tmp_path, monkeypatch)

        _declarations().write("agents", "keep-me")
        _declarations().write("mcps", "a")

        cfg = _declarations().read(path=path)

        assert cfg.agent_code == "keep-me"
        assert cfg.mcp_server_config == {"mcpServers": {"a": {}}}

    def test_writes_valid_json_to_the_resolved_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._path(tmp_path, monkeypatch)

        _declarations().write("models", "m1")

        assert Path(path).exists()

        parsed = json.loads(Path(path).read_text(encoding="utf-8"))

        assert isinstance(parsed, dict)
        assert parsed["prompt_setting"] == {"llm_code": "m1"}

    def test_unknown_kind_raises_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._path(tmp_path, monkeypatch)

        with pytest.raises(ValueError):
            _declarations().write("bogus", "x")

        assert not Path(path).exists()

    def test_unknown_kind_leaves_an_existing_file_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._path(tmp_path, monkeypatch)
        _declarations().write("agents", "a")
        before = Path(path).read_text(encoding="utf-8")

        with pytest.raises(ValueError):
            _declarations().write("bogus", "x")

        assert Path(path).read_text(encoding="utf-8") == before

    def test_never_writes_a_token_class_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D5/D-12: the produced file carries the whitelisted keys and nothing else."""
        path = self._path(tmp_path, monkeypatch)

        _declarations().write("agents", "a")
        _declarations().write("models", "m1")
        _declarations().write("mcps", "x")
        _declarations().write("knowledge", "k")

        raw = Path(path).read_text(encoding="utf-8")

        for key in local_config.REJECTED_SECRET_KEYS:
            assert key not in raw, f"{key} must be structurally unable to reach disk"

        assert set(json.loads(raw)) == set(local_config.LOCAL_CONFIG_KEYS)

    def test_starts_from_an_empty_config_when_the_file_is_corrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._path(tmp_path, monkeypatch)
        Path(path).write_text("{not json", encoding="utf-8")

        _declarations().write("mcps", "a")

        assert _declarations().read().mcp_server_config == {"mcpServers": {"a": {}}}


class TestPerAgentWriteCreatesItsDirectory:
    """写 per-agent 配置时必须自建 ``<BKAI_HOME>/agents/``（真实崩溃的回归）。

    **为什么既有测试全都漏掉了**：本文件里所有写入测试都经 ``_pin_single_file``
    （``AGENT_TUI_CONFIG_FILE``）把路径钉到一个**已存在**的显式文件，因此从没走过
    "推导路径 + 父目录不存在" 这条真实路径。新机器上
    ``~/.bkai/agents/`` 必然不存在，于是**首次** ``/models`` 写入必定
    ``FileNotFoundError``——表现为 TUI 里按 Enter 切模型即失败。
    """

    def test_writing_a_per_agent_declaration_creates_the_agents_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """父目录不存在时写入应**成功**并建好目录，而不是抛 FileNotFoundError。"""
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        # 多平台：``Path.home()`` 在 POSIX 上读 HOME。同时清掉单文件 override 与
        # CWD，使解析真正走推导的 per-agent 路径。
        monkeypatch.delenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, raising=False)
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        target = resource_manager.LocalDeclarations.user_path("code-a")
        assert not Path(target).parent.exists(), "the agents dir must NOT pre-exist"

        _declarations().write("models", "m1", code="code-a")

        assert Path(target).exists(), "the write must have created the file (and its dir)"
        assert json.loads(Path(target).read_text(encoding="utf-8"))["prompt_setting"] == {
            "llm_code": "m1"
        }

    def test_an_unwritable_parent_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自建目录**不**意味着吞掉失败：真正的 I/O 错误仍必须响亮抛出。

        用户发起的写入失败必须暴露（与读取路径的合法静默降级相反）。
        """
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, raising=False)
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        # ``agents`` 位置上放一个**文件**（而非目录），于是 makedirs 必然失败。
        agents_dir = Path(resource_manager.LocalDeclarations.user_path("code-a")).parent
        agents_dir.parent.mkdir(parents=True, exist_ok=True)
        agents_dir.write_text("not a directory", encoding="utf-8")

        with pytest.raises(OSError):
            _declarations().write("models", "m1", code="code-a")


class TestModuleConstants:
    def test_field_names_align_with_the_platform(self) -> None:
        """The whitelist is the platform AgentConfig surface — snake_case, no token."""
        assert set(local_config.LOCAL_CONFIG_KEYS) >= {
            "agent_id",
            "tenant_id",
            "agent_code",
            "agent_name",
            "space_id",
            "agent_type",
            "prompt_setting",
            "knowledgebase_settings",
            "mcp_server_config",
        }

        for name in local_config.LOCAL_CONFIG_KEYS:
            assert not name.startswith("_")

    def test_declaration_fields_map_every_family(self) -> None:
        """The four keys are the contract 03-02's ``config.set`` and 03-03's
        ``switch*`` callbacks are named after -- spelling matters."""
        assert set(local_config.DECLARATION_FIELDS) == {"models", "agents", "mcps", "knowledge"}
        assert local_config.DECLARATION_FIELDS["models"] == ("prompt_setting", False)
        assert local_config.DECLARATION_FIELDS["agents"] == ("agent_code", False)
        assert local_config.DECLARATION_FIELDS["mcps"] == ("mcp_server_config", True)
        assert local_config.DECLARATION_FIELDS["knowledge"] == ("knowledgebase_settings", True)

    def test_rejected_secret_keys_cover_the_token_family(self) -> None:
        assert local_config.REJECTED_SECRET_KEYS == ("access_token", "bk_token", "bk_ticket")


# ============================================================================
# MCP definition layer (§2 — mcp.json)
# ============================================================================

from pathlib import Path
from types import SimpleNamespace


#: §2 的配置面句柄。名字现已收进 :class:`McpResolver`（类属性/方法/staticmethod），
#: 因此断言统一按 ``mcp_config.X`` 书写。两个**刻意留在模块级**的名字
#: （``platform_mcp_disabled`` 与 ``DISABLE_PLATFORM_MCP_ENV``——它们读环境变量，
#: 不属于 ``mcp.json`` 解析）由 ``SimpleNamespace`` 合并进来。
#:
#: ``DEFAULT_MCP_FILE`` **不**在此合集中：它需要被 monkeypatch，而 ``search_paths``
#: 读的是类属性，所以相关测试直接 patch ``resource_manager.McpResolver``。把它复制
#: 到这里只会造出一个「看起来被 patch 了、实际没生效」的假象。
mcp_config = SimpleNamespace(
    CWD_ENV=resource_manager.McpResolver.CWD_ENV,
    DISABLE_PLATFORM_MCP_ENV=resource_manager.DISABLE_PLATFORM_MCP_ENV,
    describe_servers=resource_manager.McpResolver.describe_servers,
    platform_mcp_disabled=resource_manager.platform_mcp_disabled,
)
from aidev_agent.headless.local_resources import McpConfig

#: 收拢后 ``normalise_server`` 是类方法；保留模块级别的本地别名，使断言逐字不变。
normalise_server = resource_manager.McpResolver.normalise_server


def _resolver() -> resource_manager.McpResolver:
    """A fresh :class:`McpResolver` (the §2 owner). See ``_declarations`` above."""
    return resource_manager.McpResolver()


def _load_mcp(path: str | None = None) -> McpConfig:
    """Read + merge ``mcp.json`` through the collaborator that owns it.

    The §2 assertion set is unchanged from when it called the module-level
    ``load_mcp_config``; only the ACCESS PATH moved to ``McpResolver.load``.
    """
    return _resolver().load(path)


def _write(tmp_path: Path, payload: object) -> str:
    """Write ``payload`` as JSON to a temp file and return its path."""
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    return str(path)


class TestTolerance:
    """A file that cannot be read is not a reason to fail a turn."""

    def test_a_missing_file_is_an_empty_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _load_mcp(str(tmp_path / "nope.json"))

        assert config.is_empty()
        assert config.names() == []
        # Exactly one line, naming the failure CLASS.
        assert capsys.readouterr().err.count("not readable") == 1

    def test_invalid_json_is_an_empty_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The realistic typo, verbatim: a trailing comma (this repo had two)."""
        path = tmp_path / "mcp.json"
        path.write_text('{"mcpServers": {"a": {},},}', encoding="utf-8")

        assert _load_mcp(str(path)).is_empty()
        assert "not valid JSON" in capsys.readouterr().err

    def test_a_non_object_document_is_an_empty_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _load_mcp(_write(tmp_path, ["a", "b"])).is_empty()
        assert "must hold a JSON object" in capsys.readouterr().err

    def test_a_broken_file_does_not_echo_its_content(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A JSONDecodeError's message quotes the file — that must not reach a log.

        The file is corrupt, so we do not know what the quoted fragment is. On a
        credential-bearing file the answer is likely a secret.
        """
        path = tmp_path / "mcp.json"
        path.write_text('{"mcpServers": {"a": {"headers": {"X": "SUPER-SECRET"}},}}', encoding="utf-8")

        _load_mcp(str(path))

        captured = capsys.readouterr()
        assert "SUPER-SECRET" not in captured.err
        assert "SUPER-SECRET" not in captured.out

    def test_the_project_env_var_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``AGENT_TUI_CWD`` is how the user's workspace reaches the worker.

        The worker's own cwd is the ``agent_tui/`` module root, so the launch
        directory has to arrive as an environment variable. It is appended with
        ``.agents/mcp.json`` to locate the project-level file.
        """
        project_dir = tmp_path / ".bkai"
        project_dir.mkdir()
        _write(project_dir, {"mcpServers": {"from-env": {"url": "https://x.invalid"}}})
        monkeypatch.setenv(mcp_config.CWD_ENV, str(tmp_path))
        # 用户候选（DEFAULT_MCP_FILE）指到不存在的路径，断言只关于项目这一层。
        # DEFAULT_MCP_FILE 现在是 McpResolver 的类属性（``search_paths`` 读它），
        # 因此 patch 目标随之改为类本身。
        monkeypatch.setattr(
            resource_manager.McpResolver, "DEFAULT_MCP_FILE", str(tmp_path / "absent.json")
        )

        assert _load_mcp().names() == ["from-env"]

    def test_an_explicit_path_reads_only_that_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(mcp_config.CWD_ENV, str(tmp_path / "ignored"))
        explicit = _write(tmp_path, {"mcpServers": {"explicit": {"url": "https://x.invalid"}}})

        assert _load_mcp(explicit).names() == ["explicit"]


class TestMultiLocationMerge:
    """``mcp.json`` is discovered from several places and merged by priority.

    ``input ``project > user > repo default``, so a project can override a user's
    global default for a same-named server.
    """

    def test_all_locations_are_merged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct names from every candidate all survive the merge."""
        # 用户候选就是 DEFAULT_MCP_FILE（~/.bkai/mcp.json）；项目候选在 .bkai/ 下。
        user_dir = tmp_path / "home" / ".bkai"
        user_dir.mkdir(parents=True)
        _write(user_dir, {"mcpServers": {"user-server": {"url": "https://user.invalid"}}})

        project_root = tmp_path / "project"
        (project_root / ".bkai").mkdir(parents=True)
        _write(project_root / ".bkai", {"mcpServers": {"project-server": {"url": "https://p.invalid"}}})

        monkeypatch.setenv(mcp_config.CWD_ENV, str(project_root))
        monkeypatch.setattr(
            resource_manager.McpResolver, "DEFAULT_MCP_FILE", str(user_dir / "mcp.json")
        )

        names = set(_load_mcp().names())
        assert names == {"user-server", "project-server"}

    def test_the_project_file_wins_over_the_user_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A same-named server resolves to the HIGHEST-priority definition."""
        user_dir = tmp_path / "home" / ".bkai"
        user_dir.mkdir(parents=True)
        _write(user_dir, {"mcpServers": {"shared": {"url": "https://user.invalid"}}})

        project_root = tmp_path / "project"
        (project_root / ".bkai").mkdir(parents=True)
        _write(project_root / ".bkai", {"mcpServers": {"shared": {"url": "https://project.invalid"}}})

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv(mcp_config.CWD_ENV, str(project_root))
        # DEFAULT_MCP_FILE 现在是 McpResolver 的类属性（``search_paths`` 读它），
        # 因此 patch 目标随之改为类本身。
        monkeypatch.setattr(
            resource_manager.McpResolver, "DEFAULT_MCP_FILE", str(tmp_path / "absent.json")
        )

        servers = _load_mcp().server_definitions()
        assert servers["shared"]["url"] == "https://project.invalid"

    def test_a_broken_file_only_removes_its_own_contribution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One unparseable candidate does not suppress the others."""
        user_dir = tmp_path / "home" / ".bkai"
        user_dir.mkdir(parents=True)
        (user_dir / "mcp.json").write_text("{ not json", encoding="utf-8")

        project_root = tmp_path / "project"
        (project_root / ".bkai").mkdir(parents=True)
        _write(project_root / ".bkai", {"mcpServers": {"project-server": {"url": "https://p.invalid"}}})

        monkeypatch.setenv(mcp_config.CWD_ENV, str(project_root))
        monkeypatch.setattr(
            resource_manager.McpResolver, "DEFAULT_MCP_FILE", str(user_dir / "mcp.json")
        )

        assert _load_mcp().names() == ["project-server"]


class TestEnvelope:
    """The standard ``mcpServers`` envelope, and the bare-map fallback."""

    def test_the_standard_envelope_is_unwrapped(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"a": {"url": "https://a.invalid", "transport": "sse"}}},
        )

        config = _load_mcp(path)

        # FLAT, and this shape is load-bearing: `chat.py:1822` resolves a specific
        # MCP resource with `config.mcp_server_config.get(each)`, so a nested
        # payload would filter every specific MCP to None.
        assert list(config.server_definitions()) == ["a"]
        assert config.server_definitions()["a"]["transport"] == "sse"

    def test_a_bare_map_is_tolerated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-standard, but accepted — and SAID OUT LOUD rather than silently.

        A file copied from a client that does not wrap its servers is the single
        most likely cause of "my servers all vanished", and silence here is what
        would make it undiagnosable.
        """
        path = _write(tmp_path, {"a": {"url": "https://a.invalid"}})

        config = _load_mcp(path)

        assert config.names() == ["a"]
        assert "no 'mcpServers' key" in capsys.readouterr().err

    def test_an_empty_envelope_is_an_empty_config(self, tmp_path: Path) -> None:
        assert _load_mcp(_write(tmp_path, {"mcpServers": {}})).is_empty()

    def test_a_malformed_envelope_falls_back_to_the_bare_map(self, tmp_path: Path) -> None:
        """``mcpServers`` present but not an object: treat the doc as the map."""
        path = _write(tmp_path, {"mcpServers": "not-an-object", "a": {"url": "https://a.invalid"}})

        assert _load_mcp(path).names() == ["a"]


class TestTransportDerivation:
    """``langchain_mcp_adapters`` requires ``transport``; ``mcp.json`` has none."""

    def test_command_implies_stdio(self) -> None:
        assert normalise_server("n", {"command": "node", "args": ["s.js"]}) == {
            "command": "node",
            "args": ["s.js"],
            "transport": "stdio",
        }

    def test_url_implies_streamable_http(self) -> None:
        assert normalise_server("n", {"url": "https://x.invalid/mcp"}) == {
            "url": "https://x.invalid/mcp",
            "transport": "streamable_http",
        }

    def test_command_wins_when_both_are_present(self) -> None:
        """Malformed input, but the precedence must be deterministic and stdio-first."""
        assert normalise_server("n", {"command": "npx", "url": "https://x.invalid"})["transport"] == "stdio"

    def test_an_explicit_transport_is_preserved(self) -> None:
        assert normalise_server("n", {"url": "https://x.invalid", "transport": "sse"})["transport"] == "sse"

    def test_transport_type_is_dropped_not_translated(self) -> None:
        """This adapter version has no alias for it, so keeping it would be a lie.

        ``transportType`` appears in the wild (and in this repo's own
        ``tests/mcp.json``); the client ignores it, so leaving it in the data would
        suggest it does something.
        """
        result = normalise_server("n", {"url": "https://x.invalid", "transportType": "streamable-http"})

        assert result is not None
        assert "transportType" not in result
        # Derived from the discriminator, NOT from the alias's value.
        assert result["transport"] == "streamable_http"

    def test_an_empty_transport_is_treated_as_absent(self) -> None:
        """``""`` is not a usable transport — derive rather than pass it through."""
        assert normalise_server("n", {"url": "https://x.invalid", "transport": ""})["transport"] == "streamable_http"

    def test_the_result_is_a_copy(self) -> None:
        """Mutating the returned dict must not reach the caller's source."""
        source = {"url": "https://x.invalid"}
        result = normalise_server("n", source)

        assert result is not None
        result["url"] = "tampered"

        assert source == {"url": "https://x.invalid"}


class TestDroppedEntries:
    """One bad entry degrades alone — it never discards the user's other servers."""

    def test_a_server_with_neither_command_nor_url_is_dropped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"broken": {"description": "nothing usable"}, "good": {"url": "https://g.invalid"}}},
        )

        config = _load_mcp(path)

        assert config.names() == ["good"]
        err = capsys.readouterr().err
        assert "'broken'" in err
        # The NAME only — the entry's body is never echoed.
        assert "nothing usable" not in err

    def test_a_non_object_entry_is_dropped(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"bad": "a string", "good": {"url": "https://g.invalid"}}})

        assert _load_mcp(path).names() == ["good"]

    def test_a_blank_name_is_dropped(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"": {"url": "https://x.invalid"}, "good": {"url": "https://g.invalid"}}})

        assert _load_mcp(path).names() == ["good"]

    def test_an_entirely_broken_file_still_loads_the_rest(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"a": {}, "b": {"command": "x"}, "c": {"url": "https://c.invalid"}}})

        assert _load_mcp(path).names() == ["b", "c"]


class TestCredentialsNeverReachTheLogs:
    """T-03-01: a real ``mcp.json`` holds ``bk_app_secret`` inside ``headers``."""

    def test_no_server_dict_content_is_logged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(
            tmp_path,
            {
                "mcpServers": {
                    "bk-data": {
                        "url": "https://secret-host.invalid/mcp",
                        "credential_type": "blueapps",
                        "headers": {"X-Bkapi-Authorization": "SECRET-TOKEN-VALUE"},
                    }
                }
            },
        )

        config = _load_mcp(path)
        printed = capsys.readouterr()

        for forbidden in (
            "SECRET-TOKEN-VALUE",
            "secret-host.invalid",
            "X-Bkapi-Authorization",
            "blueapps",
        ):
            assert forbidden not in printed.err, f"{forbidden!r} reached stderr"
            assert forbidden not in printed.out, f"{forbidden!r} reached stdout"

        # The NAME and the COUNT are logged — that is the diagnostic value.
        assert "bk-data" in printed.err
        assert "1 mcp server definition(s)" in printed.err

        # And the value is still correctly loaded — the log discipline did not
        # strip it from the data.
        assert config.server_definitions()["bk-data"]["headers"]["X-Bkapi-Authorization"] == "SECRET-TOKEN-VALUE"


class TestDescribeServers:
    """The ``config.info`` projection: complete, and never ``None``."""

    def test_none_yields_an_empty_object(self) -> None:
        """The wire field is always a JSON object, so the Node narrowing never
        has to distinguish ``undefined`` from "no mcp.json"."""
        assert mcp_config.describe_servers(None) == {}

    def test_an_empty_config_yields_an_empty_object(self) -> None:
        assert mcp_config.describe_servers(McpConfig()) == {}

    def test_the_definitions_travel_whole(self) -> None:
        """Not a projection, and not a redaction boundary — a reviewed decision.

        The overlay exists to show the user what they configured, so ``headers``
        and ``selected_tools`` come along. Asserted explicitly so a later
        "let's strip this" edit has to change a test that says why.
        """
        config = McpConfig(
            servers={"a": {"url": "https://a.invalid", "headers": {"X": "T"}, "selected_tools": ["t1"]}}
        )

        assert mcp_config.describe_servers(config) == {
            "a": {"url": "https://a.invalid", "headers": {"X": "T"}, "selected_tools": ["t1"]}
        }

    def test_the_result_is_a_copy_of_the_mapping(self) -> None:
        config = McpConfig(servers={"a": {"url": "https://a.invalid"}})
        described = mcp_config.describe_servers(config)
        described["b"] = {}

        assert config.names() == ["a"]


class TestMcpConfigBasics:
    def test_is_empty_distinguishes_no_servers_from_servers(self) -> None:
        assert McpConfig().is_empty()
        assert not McpConfig(servers={"a": {"url": "https://a.invalid"}}).is_empty()

    def test_names_preserve_file_order(self, tmp_path: Path) -> None:
        """File order is the user's order — no sorting, no re-arranging."""
        path = _write(
            tmp_path,
            {"mcpServers": {"zebra": {"url": "https://z.invalid"}, "alpha": {"url": "https://a.invalid"}}},
        )

        assert _load_mcp(path).names() == ["zebra", "alpha"]


class TestPlatformMcpKillSwitch:
    """The all-or-nothing escape hatch for a dead platform-bound MCP server.

    No local file can remove a platform binding: ``.agent_tui.json`` is a list of
    names to ADD, and ``chat.py:1820-1824`` loads ALL of ``config.mcp_server_config``
    whenever a turn carries no ``specific_resources`` (this worker's case). One
    unresponsive server then blocks the entire agent build, because the SDK fetches
    MCP tool lists with ``asyncio.gather`` and no per-server timeout. This switch is
    the only client-side remedy.
    """

    def test_it_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset must leave the platform's bindings alone — this is opt-in."""
        monkeypatch.delenv(mcp_config.DISABLE_PLATFORM_MCP_ENV, raising=False)

        assert mcp_config.platform_mcp_disabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
    def test_truthy_spellings_turn_it_on(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(mcp_config.DISABLE_PLATFORM_MCP_ENV, value)

        assert mcp_config.platform_mcp_disabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "2"])
    def test_everything_else_leaves_it_off(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """A near-miss spelling must NOT silently disable the platform's resources."""
        monkeypatch.setenv(mcp_config.DISABLE_PLATFORM_MCP_ENV, value)

        assert mcp_config.platform_mcp_disabled() is False

    def test_it_is_read_at_call_time_not_import_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So a running process can be reconfigured without a restart."""
        monkeypatch.delenv(mcp_config.DISABLE_PLATFORM_MCP_ENV, raising=False)
        assert mcp_config.platform_mcp_disabled() is False

        monkeypatch.setenv(mcp_config.DISABLE_PLATFORM_MCP_ENV, "1")
        assert mcp_config.platform_mcp_disabled() is True


class TestMcpModuleReachesTheSdk:
    def test_importing_the_mcp_section_imports_the_sdk(self) -> None:
        """本模块（含 §2 MCP 部分）现在会导入 ``aidev_agent``。

        见 ``TestModuleImportsTheSDK`` 的说明：延迟基类解析已移除，因为
        resource_manager 是对外接口，必须能被正常导入。此处保留一条针对
        §2 所在模块的独立断言，使该取舍的两个断点都可被单独定位。
        """
        import os
        import subprocess
        import sys

        code = (
            "import sys;"
            "sys.path.insert(0, '.');"
            "import aidev_agent.headless.local_resources as m;"
            "mods=[x for x in sys.modules if x.startswith('aidev_agent')];"
            "print('SDK_MODULES=' + str(len(mods)))"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            timeout=120,
        )

        assert "SDK_MODULES=0" not in result.stdout, result.stdout + result.stderr
        assert "SDK_MODULES=" in result.stdout, result.stdout + result.stderr


# ============================================================================
# Task 1 (quick-261012-voy) — the local file index, and where ``agent.info``
#                          gets its two halves
# ============================================================================
#
# The user's constraint is one sentence with two clauses: the platform HTTP request
# fires ONCE, and local resources are reloaded EVERY time. Both clauses are asserted
# here, because a test for one alone would pass against an implementation that
# cached everything (the first clause) or one that re-fetched the platform on every
# projection (the second).
#
# ``describe_agent_config`` lives on ``SessionState`` in ``agent_runner.py``, but it
# is the consumer of §4 of the module under test — so its merge contract is pinned
# here, next to the index that feeds it. The ``_manager`` monkeypatch technique the
# rest of this file uses is reused below so nothing here touches the network either.


class TestWorkspaceIndexRead:
    """工作区第一层的实时读取（``os.scandir``）——D-28 已推翻，不缓存。

    每个测试用 ``tmp_path`` 造真实目录，直接断言 ``read`` / ``rows_for``。
    没有 ``set()`` / 缓存 / 就绪 / 重试：那些随 D-28 一并删除。
    ``reason`` / ``payload`` 也随 ``local_files`` 通道一并删除（文件行现在只走
    ``resources[]``），因此不可用状态下 ``read`` 返回**空列表**而不是携带原因。
    """

    def test_read_returns_files_and_directories_one_level_deep(self, tmp_path: Path) -> None:
        """第一层：目录条目带结尾 ``/``，文件不带；深层的 ``nested`` 不出现。"""
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "b.py").write_text("x")
        (tmp_path / "src" / "nested").mkdir()
        (tmp_path / "src" / "nested" / "c.py").write_text("x")

        listing = WorkspaceIndex().read(str(tmp_path))

        assert sorted(listing.entries) == ["a.py", "src/"]

    def test_read_orders_directories_first_then_by_name(self, tmp_path: Path) -> None:
        """排序：目录在前，再按名不区分大小写，再按原名。"""
        for name in ("Zeta.py", "alpha.py", "zdir", "Adir"):
            (tmp_path / name).mkdir() if name.endswith("dir") else (tmp_path / name).write_text("x")

        entries = WorkspaceIndex().read(str(tmp_path)).entries

        # 目录在前：Adir/ 与 zdir/（Adir 的大小写不敏感比较落在 zdir 之前），
        # 随后文件 alpha.py、Zeta.py。
        assert entries == ["Adir/", "zdir/", "alpha.py", "Zeta.py"]

    def test_read_yields_an_empty_listing_for_a_missing_root(self, tmp_path: Path) -> None:
        """root 缺失时返回空列表，不抛异常——TUI 必须仍能启动。"""
        listing = WorkspaceIndex().read(str(tmp_path / "nope"))

        assert listing.entries == []

    def test_read_yields_an_empty_listing_for_an_oserror(self, tmp_path: Path, monkeypatch) -> None:
        """权限等 ``OSError`` 降级为空列表，绝不抛（T-t1s-05）。"""
        monkeypatch.setattr(
            resource_manager.os,
            "scandir",
            lambda _path: (_ for _ in ()).throw(PermissionError("denied")),
        )

        listing = WorkspaceIndex().read(str(tmp_path))

        assert listing.entries == []

    def test_an_empty_workspace_yields_an_empty_listing(self, tmp_path: Path) -> None:
        """空工作区无话可说——空列表，不是失败。"""
        listing = WorkspaceIndex().read(str(tmp_path))

        assert listing.entries == []

    def test_rows_for_re_reads_the_workspace_every_call(self, tmp_path: Path, monkeypatch) -> None:
        """**推翻 D-28 的核心断言**：没有缓存，第二次读能看到新文件。

        若读取缓存了第一次的结果，第二次断言会返回旧列表——这正是本测试钉住的 bug。
        """
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
        index = WorkspaceIndex()
        (tmp_path / "first.py").write_text("x")

        assert [row["name"] for row in index.rows_for("")] == ["first.py"]

        (tmp_path / "second.py").write_text("x")

        assert sorted(row["name"] for row in index.rows_for("")) == ["first.py", "second.py"]

    def test_rows_for_collapses_directories_with_trailing_slash(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """形状契约：目录行 ``name`` 结尾 ``/``，``type`` 恒为 ``"file"``。"""
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
        (tmp_path / "README.md").write_text("x")
        (tmp_path / "src").mkdir()

        rows = WorkspaceIndex().rows_for("")

        assert [row["name"] for row in rows] == ["src/", "README.md"]
        assert all(row["type"] == "file" for row in rows)
        assert rows[0] == {"code": "src", "name": "src/", "type": "file"}

    def test_rows_for_carries_the_workspace_relative_name_as_code(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """第一层模式下 ``rows_for("")`` 的 ``code`` 就是名字本身（只有一层）。"""
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
        (tmp_path / "README.md").write_text("x")

        row = WorkspaceIndex().rows_for("")[0]

        assert row == {"code": "README.md", "name": "README.md", "type": "file"}

    def test_rows_for_is_empty_below_the_first_level(self, tmp_path: Path, monkeypatch) -> None:
        """第一层读取下没有深层路径可供折叠——进入 ``src/`` 无内容。

        这是本轮的有意变化：``paths`` 只含第一层，故 ``rows_for`` 无法列出 ``src/``
        内部（旧递归爬取的语义已被 D-28 推翻）。
        """
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("x")

        assert WorkspaceIndex().rows_for("src") == []

    def test_rows_for_re_reads_the_workspace_every_call(self, tmp_path: Path, monkeypatch) -> None:
        """每次 ``rows_for`` 都重新 ``read``——新落地的文件下一帧即出现。"""
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
        index = WorkspaceIndex()
        (tmp_path / "old.ts").write_text("x")

        assert [row["name"] for row in index.rows_for("")] == ["old.ts"]

        (tmp_path / "new.ts").write_text("x")

        assert sorted(row["name"] for row in index.rows_for("")) == ["new.ts", "old.ts"]

    def test_workspace_root_honours_the_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, "/tmp/some-workspace")

        assert resource_manager.WorkspaceIndex.workspace_root() == "/tmp/some-workspace"


def _row(name: str) -> dict[str, Any]:
    """A file resource row in the shape ``WorkspaceIndex.rows_for`` yields."""
    return {"code": name.removesuffix("/"), "name": name, "type": "file"}


def _real_session(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    tmp_path: Path,
    *,
    files: tuple[str, ...] = (),
) -> tuple[Any, Any, dict[str, int]]:
    """A ``SessionState`` on a **REAL** manager; only the platform fetch is stubbed.

    假 manager 已按裁决移除：合并与降级现在住在 ``LocalResourceManager`` 里，用替身
    会让这些断言**静默地**不再覆盖真实实现（替身自己实现一遍等于没测）。
    因此这里构建真 manager，只替换链首的 platform fetch；工作区用
    ``WORKSPACE_ENV_VAR`` 指向 ``tmp_path``，"文件出现"由**真的**创建文件来模拟。

    返回 ``(state, rm, counts)``——``counts["platform"]`` 计平台拉取次数，
    ``counts["workspace"]`` 计工作区扫描次数。
    """
    from aidev_agent.headless.agent_runner import SessionState

    monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
    for name in files:
        (tmp_path / name).write_text("x", encoding="utf-8")

    counts = {"platform": 0, "workspace": 0}

    def _make_manager() -> Any:
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")

        def _fetch(agent_code: str, version: str | None = None) -> dict[str, Any]:
            counts["platform"] += 1

            return payload

        rm.platform_loader.retrieve_agent_config = _fetch  # type: ignore[method-assign]
        rm.get_agent_code = lambda: "fake-agent"  # type: ignore[method-assign]

        return rm

    original_read = resource_manager.WorkspaceIndex.read

    def _counted_read(self: Any, root: str | None = None) -> Any:
        counts["workspace"] += 1

        return original_read(self, root)

    monkeypatch.setattr(resource_manager.WorkspaceIndex, "read", _counted_read)

    state = SessionState()

    # 镜像真实 ``ensure_manager`` 的缓存语义：丢弃后重建。TTL 缓存住在 manager 上，
    # 因此"invalidate 后重新拉取"只有靠**真的换一个 manager** 才成立——把
    # ``ensure_manager`` 钉死在同一个实例上会让这条断言失去意义。
    def _ensure() -> Any:
        if state._resource_manager is None:
            state._resource_manager = _make_manager()

        return state._resource_manager

    monkeypatch.setattr(state, "ensure_manager", _ensure)

    return state, _ensure(), counts


class TestDescribeAgentConfigMerge:
    """``agent.info`` 的两半：平台半经 manager（TTL 缓存决定去重），本地半每次重读。

    两半都由**真 manager** 提供：本地文件行由链中的 :class:`WorkspaceFileLoader` 并入，
    不是由调用方在外面追加——所以这些断言必须走真 manager 才有意义。
    """

    def test_the_platform_half_is_read_through_the_manager(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """平台半**每次**都经 manager 取；"只发一次 HTTP" 由 manager 的 TTL 缓存承担。

        2626 之后：``SessionState`` 只做委派，平台半由 ``rm.describe_agent_config``
        经 ``retrieve_agent_config`` 取。TTL 去重由 ``TestPlatformConfigLoaderCache`` 覆盖。
        """
        state, _rm, counts = _real_session(monkeypatch, {"resources": [{"name": "p", "type": "tool"}]}, tmp_path)

        state.describe_agent_config()
        state.describe_agent_config()

        assert counts["platform"] >= 1, "the platform half must be read through the manager"

    def test_the_local_half_is_re_read_on_every_call(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The SECOND clause. Would fail against a cache of the local half."""
        state, _rm, counts = _real_session(monkeypatch, {"resources": []}, tmp_path)

        first = state.describe_agent_config()
        assert [row["name"] for row in first["resources"]] == []

        # A file appears in the workspace between the two projections.
        (tmp_path / "appeared.ts").write_text("x", encoding="utf-8")

        second = state.describe_agent_config()

        assert [row["name"] for row in second["resources"]] == ["appeared.ts"]
        # 一次投影只扫一遍工作区（此前是三遍：行 / reason / paths 各一次）。
        assert counts["workspace"] == 2, "one scan per projection; the local half is never cached"

    def test_local_rows_and_platform_rows_travel_on_the_same_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One narrowing path on the Node side (T-261012-01) — no local-only bypass key."""
        (tmp_path / "src").mkdir()
        state, _rm, _counts = _real_session(
            monkeypatch,
            {"resources": [{"code": "weather_query", "name": "天气查询", "type": "tool"}]},
            tmp_path,
        )

        config = state.describe_agent_config()
        names = [row["name"] for row in config["resources"]]

        assert names == ["天气查询", "src/"]

    def test_the_cached_platform_dict_is_not_mutated_in_place(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A shallow copy, so a later projection never sees rows it did not ask for."""
        platform = {"resources": [{"name": "p", "type": "tool"}]}
        state, _rm, _counts = _real_session(monkeypatch, platform, tmp_path, files=("local.ts",))

        state.describe_agent_config()

        assert platform == {"resources": [{"name": "p", "type": "tool"}]}

    def test_invalidate_clears_the_platform_half_but_not_the_local_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``/login`` / ``/logout`` / ``config.set`` refresh platform data — not the workspace."""
        state, _rm, counts = _real_session(monkeypatch, {"resources": []}, tmp_path, files=("keep.ts",))

        first = state.describe_agent_config()
        assert counts["platform"] == 1
        assert [row["name"] for row in first["resources"]] == ["keep.ts"]

        state.invalidate()
        assert state._resource_manager is None, "invalidate() must drop the manager"
        second = state.describe_agent_config()

        # Refetched after invalidate: the platform half is platform data.
        assert counts["platform"] == 2
        # ...and the local workspace rows are still served, because they are not platform data.
        assert [row["name"] for row in second["resources"]] == ["keep.ts"]

    def test_the_payload_carries_local_rows_on_the_resources_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """本地文件行只走 ``resources[]``（``type == "file"``）——没有 ``local_files`` 兄弟键。

        此前同一份扫描会同时写两个键，前端因此把同一个文件投影进两个分组。
        """
        state, _rm, _counts = _real_session(monkeypatch, {"resources": []}, tmp_path, files=("a.ts",))

        config = state.describe_agent_config()

        assert [row["name"] for row in config["resources"]] == ["a.ts"]
        assert all(row["type"] == "file" for row in config["resources"])
        assert "local_files" not in config

    def test_a_platform_failure_still_serves_the_local_half(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The degradation edge: a platform hiccup must not also swallow the workspace.

        降级住在 ``rm.describe_agent_config``（不是 SessionState），因此这里用真 manager
        才能覆盖它——替身会绕过这条路径。
        """
        state, rm, _counts = _real_session(monkeypatch, {"resources": []}, tmp_path, files=("local.ts",))

        def _boom(agent_code: str, version: str | None = None) -> dict[str, Any]:
            raise RuntimeError("platform unreachable")

        monkeypatch.setattr(rm.platform_loader, "retrieve_agent_config", _boom)

        config = state.describe_agent_config()

        assert [row["name"] for row in config["resources"]] == ["local.ts"]

    def test_a_platform_failure_with_no_local_files_still_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The error only escapes when there is nothing local to serve — D-08 stays loud."""
        state, rm, _counts = _real_session(monkeypatch, {"resources": []}, tmp_path)

        def _boom(agent_code: str, version: str | None = None) -> dict[str, Any]:
            raise RuntimeError("platform unreachable")

        monkeypatch.setattr(rm.platform_loader, "retrieve_agent_config", _boom)

        with pytest.raises(RuntimeError, match="platform unreachable"):
            state.describe_agent_config()


# ============================================================================
# §6 加载器层 —— ConfigLoader 协议与六个实现
#     链现在住在 ``retrieve_agent_config``（raw dict 层），``super().get_agent_config``
#     在合并结果上做字段映射（Quick 261014-qzp 建链；261014-rta 把链下移到 raw 层）。
# ============================================================================


def _loader_manager(monkeypatch: pytest.MonkeyPatch, config: Any, **kwargs: Any) -> Any:
    """§6 测试用的**真实** manager：链真实执行，平台由 ``config`` 反投影注入。

    261014-rta 之前它委托 ``_manager``（stub 基类 ``get_agent_config``）——链住在那一层
    所以链真被执行。链下移后该 stub 不再拦截 ``retrieve_agent_config``，会**静默地**
    让链不执行（plan ruling (h) 的陷阱）。因此改为委托 ``_raw_manager_for``：平台 payload
    由 ``config`` 反投影成 raw dict，链真实跑完整条，``super().get_agent_config`` 在
    合并结果上做字段映射——既有断言因此**语义不变**。
    """
    return _raw_manager_for(monkeypatch, config, **kwargs)


class TestPlatformConfigLoaderCache:
    """平台加载器：TTL 实例缓存，只缓存成功。"""

    def _loader(self) -> tuple[Any, list[str]]:
        calls: list[str] = []

        class _Fake:
            def retrieve_agent_config(self, agent_code: str, version: str | None = None) -> dict[str, Any]:
                calls.append(agent_code)

                return {"agent_code": agent_code, "resources": []}

        return resource_manager.PlatformConfigLoader(_Fake()), calls

    def test_first_call_fetches_and_caches(self) -> None:
        loader, calls = self._loader()

        first = loader.fetch("a")

        assert first == {"agent_code": "a", "resources": []}
        assert calls == ["a"]

    def test_a_second_call_within_the_ttl_reuses_the_same_object(self) -> None:
        loader, calls = self._loader()

        first = loader.fetch("a")
        second = loader.fetch("a")

        assert second is first
        assert calls == ["a"]

    def test_the_cache_expires_after_the_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader, calls = self._loader()
        clock = [100.0]
        loader._clock = lambda: clock[0]
        monkeypatch.setattr(type(loader), "DEFAULT_TTL_SECONDS", 10.0)

        loader.fetch("a")
        clock[0] = 111.0  # beyond the 10 s TTL

        loader.fetch("a")

        assert calls == ["a", "a"]

    def test_distinct_agent_codes_cache_separately(self) -> None:
        loader, calls = self._loader()

        loader.fetch("a")
        loader.fetch("b")
        loader.fetch("a")

        assert calls == ["a", "b"]

    def test_a_failed_fetch_is_not_cached(self) -> None:
        """失败不写缓存：抛异常后再调用会重试。"""
        calls: list[str] = []

        class _Flaky:
            def retrieve_agent_config(self, agent_code: str, version: str | None = None) -> dict[str, Any]:
                calls.append(agent_code)

                if len(calls) == 1:
                    raise RuntimeError("platform unreachable")

                return {"ok": True}

        loader = resource_manager.PlatformConfigLoader(_Flaky())

        with pytest.raises(RuntimeError, match="platform unreachable"):
            loader.fetch("a")

        assert loader.fetch("a") == {"ok": True}
        assert calls == ["a", "a"]

    def test_apply_normalises_the_envelope_and_returns_the_same_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """平台加载器的 apply：兜底归一信封形状 + 逃生清空，就地返回同一 dict。

        不再是恒等变换（261014-rta）：作用点是 ``raw["mcp_server_config"]["mcpServers"]``
        （enveloped），而不是对象层的 ``cfg.mcp_server_config``。这里不设逃生开关，因此
        仅验证「返回同一对象 + 非 dict 的信封被归一」。
        """
        monkeypatch.delenv(resource_manager.DISABLE_PLATFORM_MCP_ENV, raising=False)
        rm = _raw_manager(monkeypatch, {})

        raw: dict[str, Any] = {"mcp_server_config": None}
        result = rm.platform_loader.apply(raw)

        assert result is raw
        assert raw["mcp_server_config"] == {"mcpServers": {}}


class TestPlatformTtlResolution:
    """TTL 解析：未设用默认，非法值回退默认且不抛。"""

    def _loader(self) -> Any:
        class _Noop:
            def retrieve_agent_config(self, agent_code: str, version: str | None = None) -> dict[str, Any]:
                return {}

        return resource_manager.PlatformConfigLoader(_Noop())

    def test_constants_are_pinned(self) -> None:
        assert resource_manager.PlatformConfigLoader.TTL_ENV == "BKAI_AGENT_CONFIG_TTL"
        assert resource_manager.PlatformConfigLoader.DEFAULT_TTL_SECONDS == 300.0

    def test_unset_uses_the_default_not_no_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(resource_manager.PlatformConfigLoader.TTL_ENV, raising=False)

        assert self._loader().ttl_seconds() == 300.0

    def test_blank_uses_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(resource_manager.PlatformConfigLoader.TTL_ENV, "   ")

        assert self._loader().ttl_seconds() == 300.0

    def test_zero_is_honoured_as_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(resource_manager.PlatformConfigLoader.TTL_ENV, "0")

        assert self._loader().ttl_seconds() == 0.0

    @pytest.mark.parametrize("value", ["abc", "-1", "nan-ish", "--5"])
    def test_invalid_values_fall_back_without_raising(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(resource_manager.PlatformConfigLoader.TTL_ENV, value)

        assert self._loader().ttl_seconds() == 300.0

    def test_it_is_read_at_call_time_not_import_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader()

        monkeypatch.setenv(resource_manager.PlatformConfigLoader.TTL_ENV, "12")
        assert loader.ttl_seconds() == 12.0

        monkeypatch.setenv(resource_manager.PlatformConfigLoader.TTL_ENV, "34")
        assert loader.ttl_seconds() == 34.0


class TestLocalAgentConfigLoaderBehaviour:
    """261014-urj：原名 ``TestLocalConfigLoaderBehaviour``，加载器改名为
    ``LocalAgentConfigLoader``（per-agent ``agents/${code}.json``）。断言值不变。"""

    def test_a_declared_model_overrides_the_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(
            monkeypatch, _config(chat_model="platform"), local=LocalConfig(prompt_setting={"llm_code": "declared"})
        )

        cfg = rm.get_agent_config("a")

        assert cfg.chat_model == "declared"

    def test_an_empty_declaration_leaves_the_platform_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(monkeypatch, _config(chat_model="platform"))

        assert rm.get_agent_config("a").chat_model == "platform"

    def test_merge_to_raw_returns_the_same_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """契约：``apply``/``merge_to_raw`` 就地修改并返回**同一个** raw dict。"""
        rm = _loader_manager(monkeypatch, _config(), local=LocalConfig(prompt_setting={"llm_code": "m"}))
        loader = next(
            loader for loader in rm._loaders if isinstance(loader, resource_manager.LocalAgentConfigLoader)
        )

        raw: dict[str, Any] = {
            "agent_code": "a",
            "prompt_setting": {"llm_code": "platform"},
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
        }
        result = loader.merge_to_raw(raw)

        assert result is raw
        assert raw["prompt_setting"]["llm_code"] == "m"

    def test_it_reads_per_agent_config_via_read_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """261014-urj 替代 ``test_it_reads_the_managers_local_attribute``（``rm.local`` 已删除）。

        新契约：加载器经 :meth:`LocalAgentConfigLoader.read_config` 现场读 per-agent 配置
        （**不是** ``rm.local``）。用 ``read_config`` 覆写注入不同声明，断言生效。
        """
        rm = _loader_manager(monkeypatch, _config(chat_model="platform"))
        loader = next(
            x for x in rm._loaders if isinstance(x, resource_manager.LocalAgentConfigLoader)
        )
        monkeypatch.setattr(
            type(loader), "read_config", lambda self, code: LocalConfig(prompt_setting={"llm_code": "mutated"})
        )

        raw: dict[str, Any] = {
            "agent_code": "a",
            "prompt_setting": {"llm_code": "platform"},
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
        }

        assert loader.merge_to_raw(raw)["prompt_setting"]["llm_code"] == "mutated"

    # --- 本任务的核心价值见证：raw 层 12 字段端到端覆盖（改前为红） --------------

    @pytest.mark.parametrize(
        ("key", "platform_value", "local_value"),
        [
            ("temperature", 0.9, 0.11),
            ("max_tokens", 999, 42),
            ("fallback_model", "platform-fallback", "local-fallback"),
            ("non_thinking_llm", "platform-ntl", "local-ntl"),
        ],
    )
    def test_declared_prompt_setting_fields_reach_the_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        platform_value: Any,
        local_value: Any,
    ) -> None:
        """``prompt_setting`` 的四个非模型字段端到端覆盖（**改前不生效**）。

        261014-rta 之前链住在对象层，``super()`` 已从平台 dict 映射了这些字段，本地写入
        被丢弃——只有 ``llm_code`` 碰巧生效。下移到 raw 层后本地值进入合并 dict，
        ``super()`` 的字段映射读到的是本地那套。
        """
        platform_prompt = {"llm_code": "platform", key: platform_value}
        payload = {
            "agent_code": "a",
            "agent_name": "a",
            "prompt_setting": platform_prompt,
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": None,
        }
        rm = _raw_manager(
            monkeypatch, payload, local=LocalConfig(prompt_setting={"llm_code": "local", key: local_value})
        )

        cfg = rm.get_agent_config("a")

        assert getattr(cfg, key) == local_value

    def test_declared_agent_name_and_space_id_override_the_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "agent_code": "a",
            "agent_name": "platform-name",
            "prompt_setting": {"llm_code": "platform"},
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": None,
        }
        rm = _raw_manager(
            monkeypatch, payload, local=LocalConfig(agent_name="local-name", space_id="sp1")
        )

        cfg = rm.get_agent_config("a")

        assert cfg.agent_name == "local-name"
        # ``agent_info`` 是合并 dict 的浅拷贝，因此 ``space_id`` 也在其上可见。
        assert cfg.agent_info["space_id"] == "sp1"

    def test_an_empty_declaration_does_not_blank_the_platform_agent_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反向断言：本地未声明 ``agent_name`` 时保持平台值（防"空值覆盖平台"）。"""
        payload = {
            "agent_code": "a",
            "agent_name": "platform-name",
            "prompt_setting": {"llm_code": "platform"},
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": None,
        }
        rm = _raw_manager(monkeypatch, payload)

        assert rm.get_agent_config("a").agent_name == "platform-name"

    def test_conversation_settings_merge_key_by_key_but_replace_commands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``conversation_settings`` 逐键并集；嵌套的 ``commands`` 列表整体替换。"""
        payload = {
            "agent_code": "a",
            "agent_name": "a",
            "prompt_setting": {"llm_code": "platform"},
            "conversation_settings": {
                "opening_remark": "platform-hello",
                "commands": [{"id": "p", "agent_code": "p"}],
            },
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": None,
        }
        rm = _raw_manager(
            monkeypatch,
            payload,
            local=LocalConfig(conversation_settings={"commands": [{"id": "l", "agent_code": "l"}]}),
        )

        raw = rm.retrieve_agent_config("a")

        # 未冲突的键保留平台值……
        assert raw["conversation_settings"]["opening_remark"] == "platform-hello"
        # ……而本地声明的 ``commands`` 整体替换平台那份。
        assert raw["conversation_settings"]["commands"] == [{"id": "l", "agent_code": "l"}]

    def test_declared_knowledgebases_are_appended_in_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本地 ``knowledgebases`` 现在是**追加**（平台在前、去重），不再是整体替换。"""
        payload = {
            "agent_code": "a",
            "agent_name": "a",
            "prompt_setting": {"llm_code": "platform"},
            "knowledgebase_settings": {"knowledgebases": ["bound"]},
            "mcp_server_config": {"mcpServers": {}},
            "approval_settings": None,
        }
        rm = _raw_manager(
            monkeypatch,
            payload,
            local=LocalConfig(knowledgebase_settings={"knowledgebases": ["local", "bound"]}),
        )

        raw = rm.retrieve_agent_config("a")

        assert raw["knowledgebase_settings"]["knowledgebases"] == ["bound", "local"]


class TestMcpLoaderBehaviour:
    """加载器层面的 ADD-only 守护（等价于 ``TestLocalMcpDefinitions`` 的语义）。

    断言层级改为 raw 层：``raw["mcp_server_config"]["mcpServers"]``（**ENVELOPED**）。
    """

    def test_declared_and_defined_servers_are_union(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(
            monkeypatch,
            _config(mcp_server_config={"bound": {"command": "x"}}),
            local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}),
            mcp=McpConfig(servers={"defined": {"url": "https://x", "transport": "streamable_http"}}),
        )

        assert list(rm.get_agent_config("a").mcp_server_config) == ["bound", "local", "defined"]

    def test_a_later_definition_never_replaces_a_first_definition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform_entry = {"url": "https://platform", "mcp_type": "http"}
        rm = _loader_manager(
            monkeypatch,
            _config(mcp_server_config={"shared": dict(platform_entry)}),
            mcp=McpConfig(servers={"shared": {"url": "https://local", "transport": "sse"}}),
        )

        result = rm.get_agent_config("a").mcp_server_config

        assert result == {"shared": platform_entry}
        assert list(result) == ["shared"]

    def test_a_definition_completes_a_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(
            monkeypatch,
            _config(mcp_server_config={"shared": {}}),
            mcp=McpConfig(servers={"shared": {"url": "https://local", "transport": "sse"}}),
        )

        assert rm.get_agent_config("a").mcp_server_config == {"shared": {"url": "https://local", "transport": "sse"}}

    def test_raw_layer_holds_an_enveloped_mcpservers_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """raw 层形状是 ENVELOPED（``{"mcpServers": {...}}``），不是 FLAT。"""
        rm = _loader_manager(
            monkeypatch,
            _config(mcp_server_config={}),
            mcp=McpConfig(servers={"a": {"url": "https://a", "transport": "sse"}}),
        )

        raw = rm.retrieve_agent_config("a")

        assert set(raw["mcp_server_config"]) == {"mcpServers"}
        assert set(raw["mcp_server_config"]["mcpServers"]) == {"a"}

    def test_the_platform_payload_is_not_mutated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """语义反转（261014-rta）：链**就地**改 ``fetch`` 返回的那个 dict。

        因此必须证明**调用方传入的 payload 未被就地改写**——``retrieve_agent_config``
        对 ``fetch`` 结果做了浅拷贝（T-261014rta-05），链改的是拷贝。这不仅保护缓存对象，
        也保护调用方（与 ``describe_agent_config`` 对平台 dict 做浅拷贝的既有纪律一致）。
        """
        payload = {
            "agent_code": "a",
            "agent_name": "a",
            "prompt_setting": {"llm_code": "platform"},
            "knowledgebase_settings": {"knowledgebases": []},
            "mcp_server_config": {"mcpServers": {"bound": {}}},
            "approval_settings": None,
        }
        rm = _raw_manager(
            monkeypatch,
            payload,
            local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}),
        )

        rm.get_agent_config("a")

        # payload 原样——本地 'local' 没有漏进调用方持有的那份 dict。
        assert payload["mcp_server_config"] == {"mcpServers": {"bound": {}}}
        assert payload["prompt_setting"] == {"llm_code": "platform"}
        assert payload["approval_settings"] is None

    def test_two_calls_do_not_accumulate_local_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同 ``agent_code`` 连续两次调用，第二次的 ``mcpServers`` 不出现重复累积。

        守卫 T-261014rta-05：若链就地改写缓存对象，本地条目会跨调用累积。
        """
        rm = _raw_manager(
            monkeypatch,
            {
                "agent_code": "a",
                "agent_name": "a",
                "prompt_setting": {"llm_code": "platform"},
                "knowledgebase_settings": {"knowledgebases": []},
                "mcp_server_config": {"mcpServers": {"bound": {}}},
                "approval_settings": None,
            },
            local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}),
        )

        first = rm.retrieve_agent_config("a")["mcp_server_config"]["mcpServers"]
        second = rm.retrieve_agent_config("a")["mcp_server_config"]["mcpServers"]

        assert sorted(first) == ["bound", "local"]
        assert sorted(second) == ["bound", "local"]

    def test_the_kill_switch_drops_platform_bindings_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(resource_manager.DISABLE_PLATFORM_MCP_ENV, "1")
        rm = _loader_manager(
            monkeypatch,
            _config(mcp_server_config={"bound": {"command": "x"}}),
            local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}),
        )

        assert list(rm.get_agent_config("a").mcp_server_config) == ["local"]


class TestKnowledgebaseLoaderBehaviour:
    def test_local_knowledgebases_are_appended_after_the_platforms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(
            monkeypatch,
            _config(knowledgebase_ids=["bound"]),
            local=LocalConfig(knowledgebase_settings={"knowledgebases": ["local", "bound"]}),
        )

        # raw 层形状（平台在前）+ 端到端 ``AgentConfig.knowledgebase_ids`` 断言。
        raw = rm.retrieve_agent_config("a")
        assert raw["knowledgebase_settings"]["knowledgebases"] == ["bound", "local"]
        assert rm.get_agent_config("a").knowledgebase_ids == ["bound", "local"]

    def test_no_declaration_leaves_the_platform_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(monkeypatch, _config(knowledgebase_ids=["bound"]))

        assert rm.get_agent_config("a").knowledgebase_ids == ["bound"]


class TestRuntimeInstanceLoaderBehaviour:
    def test_the_current_model_beats_the_local_declaration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(
            monkeypatch,
            _config(chat_model="platform"),
            local=LocalConfig(prompt_setting={"llm_code": "declared"}),
        )

        assert rm.get_agent_config("a").chat_model == "declared"

        rm.runtime.current_model = "turn"
        assert rm.get_agent_config("a").chat_model == "turn"

    def test_an_absent_current_model_leaves_the_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = _loader_manager(monkeypatch, _config(chat_model="platform"))
        rm.runtime.current_model = None

        assert rm.get_agent_config("a").chat_model == "platform"

    def test_it_is_the_highest_priority_in_the_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``current_model`` 必须是链中最后一个（最高）来源。"""
        rm = _loader_manager(monkeypatch, _config())
        kinds = [type(x).__name__ for x in rm._loaders]

        assert kinds[-1] == "RuntimeInstanceLoader"

    def test_current_model_takes_effect_on_retrieve_agent_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``current_model`` 在 ``retrieve_agent_config`` 上就生效（不经 ``get_agent_config``）。

        作用域扩大（261014-rta）：链下移后它对**每一次**读平台配置的调用都生效——包括
        ``agent_runner`` 直接调 ``retrieve_agent_config`` 的那一处与 SDK
        ``factory.get_agent_config`` 内部的那一处。
        """
        rm = _raw_manager(
            monkeypatch,
            {
                "agent_code": "a",
                "agent_name": "a",
                "prompt_setting": {"llm_code": "platform"},
                "knowledgebase_settings": {"knowledgebases": []},
                "mcp_server_config": {"mcpServers": {}},
                "approval_settings": None,
            },
        )
        rm.runtime.current_model = "turn-model"

        assert rm.retrieve_agent_config("a")["prompt_setting"]["llm_code"] == "turn-model"

    def test_the_manager_has_no_model_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``current_model`` 不住在 manager 上——它属于进程级状态。

        反回归：manager 每次构建都会新建，挂在实例上的槽位会**随 manager 分裂**，于是
        「哪个是真源」变成未决问题，而经另一份走的路径（``agent.info`` 快照等）永远
        看到 ``None``。当前取值必须只有一个权威：注入的 ``runtime``。
        """
        rm = _raw_manager(monkeypatch, {})

        assert not hasattr(rm, "current_model"), (
            "the model selection came back on the manager; it belongs to the "
            "process-level runtime state (SessionState) and must be read through rm.runtime"
        )
        # The loader holds the injected runtime — not the manager.
        loader = rm._loaders[-1]

        assert isinstance(loader, resource_manager.RuntimeInstanceLoader)
        assert loader.runtime is rm.runtime
        assert loader.runtime is not rm


class TestWorkspaceFileLoader:
    """§6 的工作区文件加载器——无状态适配，包装传入的那个 ``WorkspaceIndex``。

    索引现在从**真实目录**读第一层（``tmp_path``），不再是 ``set(payload)`` 缓存。
    """

    @staticmethod
    def _index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkspaceIndex:
        monkeypatch.setenv(resource_manager.WorkspaceIndex.WORKSPACE_ENV_VAR, str(tmp_path))
        return WorkspaceIndex()

    def test_file_resources_match_rows_for_the_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "README.md").write_text("x")
        (tmp_path / "src").mkdir()
        index = self._index(monkeypatch, tmp_path)
        loader = resource_manager.WorkspaceFileLoader(index)

        assert loader.file_resources() == index.rows_for("")

    def test_project_matches_rows_for_the_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "a.ts").write_text("x")
        index = self._index(monkeypatch, tmp_path)
        loader = resource_manager.WorkspaceFileLoader(index)

        assert loader.project() == index.rows_for("")

    def test_apply_is_a_noop_on_the_raw_dict(self) -> None:
        """文件资源属于 ``agent.info`` 载荷，不进入配置 dict（裁决 A）。"""
        loader = resource_manager.WorkspaceFileLoader(WorkspaceIndex())
        raw: dict[str, Any] = {"agent_name": "a", "prompt_setting": {"llm_code": "m"}}

        assert loader.apply(raw) is raw
        assert raw["prompt_setting"]["llm_code"] == "m"

    def test_it_wraps_the_index_it_was_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """加载器包装**传入的那个**索引对象，不新建、不共享全局。"""
        one, two = WorkspaceIndex(), WorkspaceIndex()
        monkeypatch.setattr(type(one), "read", lambda self, root=None: resource_manager.DirectoryListing())
        monkeypatch.setattr(type(one), "rows_for", lambda self, directory: [{"code": "x", "name": f"{id(self)}", "type": "file"}])

        assert resource_manager.WorkspaceFileLoader(one)._index is one
        assert resource_manager.WorkspaceFileLoader(two)._index is two
        # Each loader projects through ITS OWN index — the two rows differ.
        assert (
            resource_manager.WorkspaceFileLoader(one).file_resources()[0]["name"]
            != resource_manager.WorkspaceFileLoader(two).file_resources()[0]["name"]
        )

    def test_an_empty_index_yields_no_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert (
            resource_manager.WorkspaceFileLoader(self._index(monkeypatch, tmp_path)).file_resources()
            == []
        )


class TestGetAgentConfigPipeline:
    """六源合并流水线的落点现在是 ``retrieve_agent_config``（raw dict 层）。

    ``get_agent_config`` 覆写已删除（ruling (a)）：链住在 ``retrieve_agent_config``，
    ``super().get_agent_config`` 经虚分派调用它，因此在**合并结果**上做字段映射。
    这里用 ``_raw_manager`` 构造真实 manager，并用 ``_Spy`` **证明链真的被执行**
    （plan ruling (h)：旧助手 stub 基类方法会让链完全不跑却仍可能全绿）。
    """

    def test_loaders_are_applied_in_order_on_retrieve_agent_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_Spy`` 记录 ``merge_to_raw`` 调用顺序，断言链按序被执行。"""
        rm = _raw_manager(monkeypatch, {})
        seen: list[str] = []

        class _Spy(resource_manager.RawContributor):
            def __init__(self, name: str) -> None:
                self.name = name

            def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
                seen.append(self.name)

                return raw

        rm._loaders = [_Spy("a"), _Spy("b"), _Spy("c")]

        rm.retrieve_agent_config("x")

        assert seen == ["a", "b", "c"], "链没有被执行——spy 未记录到调用（桩吃掉链？）"

    def test_each_loader_receives_the_same_raw_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """链式应用：加载器就地修改并返回**同一个** raw dict。"""
        rm = _raw_manager(monkeypatch, {})
        ids: list[int] = []
        seen: list[str] = []

        class _Spy(resource_manager.RawContributor):
            def __init__(self, name: str) -> None:
                self.name = name

            def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
                ids.append(id(raw))
                seen.append(self.name)

                return raw

        rm._loaders = [_Spy("a"), _Spy("b"), _Spy("c")]

        rm.retrieve_agent_config("x")

        assert len(set(ids)) == 1
        assert seen == ["a", "b", "c"]

    def test_the_default_chain_is_the_expected_loaders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """链序即优先级（低 → 高）。

        261014-urj 裁决 (a)：链首现在是 :class:`PlatformConfigLoader`（真进链，既 fetch
        又 apply）；随后是审批裁剪 → per-agent 本地声明 → MCP 定义 → 知识库 →
        **工作区文件行** → 运行内存。
        **反转**了旧断言（旧：平台是输入、不在链里；工作区 loader 排除在链外）。
        """
        rm = _raw_manager(monkeypatch, {})

        assert [type(x).__name__ for x in rm._loaders] == [
            "PlatformConfigLoader",
            "ApprovalLoader",
            "LocalAgentConfigLoader",
            "McpLoader",
            "KnowledgebaseLoader",
            "WorkspaceFileLoader",
            "RuntimeInstanceLoader",
        ]
        # 运行时那一源仍是末位（最高优先级）。
        assert type(rm._loaders[-1]).__name__ == "RuntimeInstanceLoader"
        # 链里的文件 loader 与 rm.workspace_files 是**同一实例**：替换 files 后重建时
        # 必须同步换掉链中那一项，否则链会一直读旧索引。
        assert rm._loaders[-2] is rm.workspace_files
        # 平台加载器是链首成员，且与 self.platform_loader 是**同一实例**（TTL 缓存不分裂）。
        assert rm._loaders[0] is rm.platform_loader

    def test_a_loader_that_raises_passes_through_but_get_agent_config_wraps_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """加载器契约要求永不抛异常；链不额外吞异常。

        ``retrieve_agent_config`` **原样透出**异常（不包装）；``get_agent_config``
        （基类）把它包成 ``ValueError("Failed to retrieve agent config: ...")``。
        """
        rm = _raw_manager(monkeypatch, {})

        class _Boom(resource_manager.RawContributor):
            def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("loader exploded")

        rm._loaders = [_Boom()]

        # 原样透出（不包装）。
        with pytest.raises(RuntimeError, match="loader exploded"):
            rm.retrieve_agent_config("x")

        # 基类在本方法之上包装成 ValueError。
        with pytest.raises(ValueError, match="Failed to retrieve agent config"):
            rm.get_agent_config("x")

    def test_approval_clearing_is_a_chain_member_and_a_unit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """审批清空现在是链中一员（ruling (c)）——单元 + 集成两层断言。

        旧测试「空链 + 桩 get_agent_config」无法再表达该行为（链下移后插桩不再拦截），
        因此改为：

        (i) 单元：``ApprovalLoader().merge_to_raw({"approval_settings": {...}})`` 就地置空
            为 ``{}``，返回**同一**对象；
        (ii) 集成：``_raw_manager`` 的 payload 带 ``approval_settings``，
            ``retrieve_agent_config``（链对所有读平台路径生效）与 ``get_agent_config``
            （对象层）都读到 ``{}``。
        """
        # (i) 单元
        raw: dict[str, Any] = {"approval_settings": {"strategies": [{"strategy_id": "S"}]}}
        result = resource_manager.ApprovalLoader().merge_to_raw(raw)

        assert result is raw
        assert raw["approval_settings"] == {}

        # (ii) 集成
        rm = _raw_manager(
            monkeypatch,
            {
                "agent_code": "test-agent",
                "agent_name": "test-agent",
                "prompt_setting": {"llm_code": "platform"},
                "knowledgebase_settings": {"knowledgebases": []},
                "mcp_server_config": {"mcpServers": {}},
                "approval_settings": {"strategies": [{"strategy_id": "S"}]},
            },
        )

        assert rm.retrieve_agent_config("test-agent")["approval_settings"] == {}
        assert rm.get_agent_config("test-agent").approval_settings == {}

    def test_the_chain_actually_runs_a_scoped_loader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """守卫（plan ruling (h)）：把某 loader 的 ``merge_to_raw`` 换成 ``raise`` 必须变红。

        这条是「桩吃掉链」的排除器：若链没被执行，本测试会通过（异常不触发）——因此它
        一旦通过即证明链真的跑了。两次构造：默认链会抛（因为 approval loader 不是
        raise 的那个），这里直接把链换成一个必炸的 spy 并断言抛。
        """
        rm = _raw_manager(monkeypatch, {})

        class _Exploding(resource_manager.RawContributor):
            def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("chain reached")

        rm._loaders = [_Exploding()]

        with pytest.raises(RuntimeError, match="chain reached"):
            rm.retrieve_agent_config("x")

    def test_platform_loader_is_the_first_chain_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """261014-urj 裁决 (a)：平台加载器是链首成员，且与 self.platform_loader 同实例。"""
        rm = _raw_manager(monkeypatch, {})

        assert rm._loaders[0] is rm.platform_loader
        assert type(rm._loaders[0]).__name__ == "PlatformConfigLoader"

    def test_the_platform_dict_is_merged_into_the_chain_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """新增：链首 ``merge_to_raw`` 真的把 ``fetch`` 的键并入 raw（不再预取数）。

        spy 证明平台 dict 经**链首**（而非 ``retrieve_agent_config`` 预取）进入合并结果。
        """
        rm = _raw_manager(
            monkeypatch,
            {"agent_code": "a", "agent_name": "from-platform", "prompt_setting": {"llm_code": "p"}},
        )

        raw = rm.retrieve_agent_config("a")

        assert raw["agent_name"] == "from-platform"
        assert raw["prompt_setting"]["llm_code"] == "p"

    def test_kill_switch_still_clears_before_local_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """性质 1 的回归守卫（裁决 (a)）：逃生清空仍在本地来源叠加**之前**。

        ``platform_mcp_disabled()`` 置位时：平台绑定的 server 被清空，但本地声明的
        server **留存**——证明链首 ``apply``（清空）先于 ``LocalAgentConfigLoader``（叠加）。
        """
        monkeypatch.setenv(resource_manager.DISABLE_PLATFORM_MCP_ENV, "1")
        rm = _raw_manager(
            monkeypatch,
            {"agent_code": "a", "agent_name": "a", "mcp_server_config": {"mcpServers": {"platform-bound": {}}}},
            local=LocalConfig(mcp_server_config={"mcpServers": {"local-kept": {}}}),
        )

        raw = rm.retrieve_agent_config("a")
        servers = raw["mcp_server_config"]["mcpServers"]

        assert "local-kept" in servers, "本地声明必须在逃生清空之后叠加"
        assert "platform-bound" not in servers, "平台绑定必须被逃生清空丢弃"

    def test_the_platform_dict_is_not_mutated_by_the_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """T-261014rta-05 的守卫（仍成立）：链首对 fetch 结果做浅拷贝 + 信封拷贝。

        ``merge_to_raw`` 从 ``_fetch_into`` 起就地修改的是**传给它的 raw**，而 ``fetch``
        返回的对象（此处即 payload）不得被就地污染。
        """
        payload = {
            "agent_code": "a",
            "agent_name": "a",
            "mcp_server_config": {"mcpServers": {"bound": {}}},
        }
        rm = _raw_manager(monkeypatch, payload, local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}))

        rm.retrieve_agent_config("a")

        assert payload["mcp_server_config"] == {"mcpServers": {"bound": {}}}


class TestGetAgentConfigPlatformCache:
    """平台配置经 TTL 缓存拉取（D1/D2），且快照路径与构建路径共用之（裁决 B）。"""

    def _counted_manager(self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None) -> Any:
        """一个真实 manager，平台读取被计数。

        平台读取直接替换 ``rm.platform_loader.retrieve_agent_config``——加载器在
        ``__init__`` 时捕获的是 ``rm.retrieve_agent_config`` 的绑定方法，因此替换
        实例上的那一份才能让计数真正生效（且不触网）。

        261014-rta：不再 stub 基类 ``get_agent_config``——链现在住在
        ``retrieve_agent_config``，取数经 ``platform_loader.fetch``，这两条路径都直接
        被测，无需桩掉基类。
        """
        calls: list[tuple[str, str | None]] = []

        def _fake_retrieve(agent_code: str, version: str | None = None) -> dict[str, Any]:
            calls.append((agent_code, version))

            return payload if payload is not None else {"agent_code": agent_code, "resources": []}

        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")
        monkeypatch.setattr(rm.platform_loader, "retrieve_agent_config", _fake_retrieve)
        # ``rm.get_agent_code`` 是真实实现；固定它，让快照路径的 agent_code 可预期。
        monkeypatch.setattr(rm, "get_agent_code", lambda: "fake-agent", raising=False)
        rm._platform_calls = calls  # type: ignore[attr-defined]

        return rm

    def test_the_loader_does_not_bind_the_retrieve_agent_config_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归守卫（261014-rta 的真实崩溃）：``fetch`` 不得递归进覆写。

        ``PlatformConfigLoader`` 在 ``__init__`` 捕获取数方法。若它绑定
        ``rm.retrieve_agent_config``，而现在那个名字是 ``LocalResourceManager`` 的
        **覆写**（链的家），就会 ``fetch → 覆写 → fetch`` **无限递归**。单测因到处
        stub ``platform_loader.retrieve_agent_config`` 而漏掉它；真实 worker 在启动一轮
        run 时才炸（表现为 SIGTERM 后不退出）。这里断言绑定的是**基类**实现。
        """
        from aidev_agent.packages.resource_manager.base import BaseResourceManager

        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")

        assert rm.platform_loader.retrieve_agent_config.__func__ is BaseResourceManager.retrieve_agent_config
        assert rm.platform_loader.retrieve_agent_config.__func__ is not resource_manager.LocalResourceManager.retrieve_agent_config
        # 取数仍委托给同一个 manager 实例（base 方法需要它的 ``get_client``）。
        assert rm.platform_loader.retrieve_agent_config.__self__ is rm

    def test_the_platform_config_is_cached_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = self._counted_manager(monkeypatch)

        rm.platform_loader.fetch("a")
        rm.platform_loader.fetch("a")

        assert [code for code, _ in rm._platform_calls] == ["a"]

    def test_the_cache_expires_after_the_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rm = self._counted_manager(monkeypatch)
        clock = [0.0]
        rm.platform_loader._clock = lambda: clock[0]
        monkeypatch.setattr(resource_manager.PlatformConfigLoader, "DEFAULT_TTL_SECONDS", 5.0)

        rm.platform_loader.fetch("a")
        clock[0] = 6.0
        rm.platform_loader.fetch("a")

        assert [code for code, _ in rm._platform_calls] == ["a", "a"]

    def test_the_snapshot_and_the_build_share_one_platform_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """裁决 B 的守护：先快照、再取平台，底层 fetch 只发生一次。"""
        rm = self._counted_manager(monkeypatch)

        rm.get_config_snapshot()  # 经 _raw_platform_config → retrieve_agent_config → fetch
        rm._raw_platform_config()

        # ``app_code`` 是 "app"，两条路径都按同一 agent_code 取数 → 只拉取一次。
        assert [code for code, _ in rm._platform_calls] == ["app"]

    def test_retrieve_agent_config_and_the_snapshot_share_one_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """新增（裁决 b 的守护）：``retrieve_agent_config`` 与 ``_raw_platform_config``
        共用底层 fetch。"""
        rm = self._counted_manager(monkeypatch)

        rm.retrieve_agent_config("app")
        rm._raw_platform_config()

        assert [code for code, _ in rm._platform_calls] == ["app"]

    def test_version_is_passed_through_to_the_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """新增：``retrieve_agent_config`` 把 ``version`` 透传到平台的 ``fetch``。

        否则子 agent 的 ``version=None`` 语义与工厂的 ``version`` 路由会被静默丢弃。
        """
        rm = self._counted_manager(monkeypatch)

        rm.retrieve_agent_config("a", version="v2")
        rm.platform_loader.fetch("b", version="v3")

        assert rm._platform_calls == [("a", "v2"), ("b", "v3")]

    def test_a_failed_platform_fetch_is_not_cached_in_the_snapshot_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一次抛异常（快照降级为 {}），第二次成功 → 确实重试。

        261014-rta：成功那次现经 loader 链，因此返回的 dict 还带合并层/守卫键
        （``agent_name`` / ``approval_settings`` / ...）。本测试的**语义**是「失败不写
        缓存、第二次重新拉取」，因此断言 retry 次数与 ``resources`` 值，而非整 dict 相等。
        """
        calls: list[str] = []

        def _flaky(agent_code: str, version: str | None = None) -> dict[str, Any]:
            calls.append(agent_code)

            if len(calls) == 1:
                raise RuntimeError("platform unreachable")

            return {"resources": []}

        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")
        monkeypatch.setattr(rm.platform_loader, "retrieve_agent_config", _flaky)
        monkeypatch.setattr(rm, "get_agent_code", lambda: "fake-agent", raising=False)

        assert rm._raw_platform_config() == {}  # 降级
        second = rm._raw_platform_config()

        # 重试成功。注意不能再用 ``second["resources"] == []`` 表达这一点：本地文件行
        # 现在由链中的 :class:`WorkspaceFileLoader` 并入 ``resources``，因此该键必然还带
        # 工作区行。本用例的语义是「失败不写缓存、第二次重新拉取」，故断言取到了东西
        # 且确实又拉取了一次。
        assert second != {}
        assert calls == ["app", "app"]

    def test_the_snapshot_goes_through_the_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``_raw_platform_config`` 现经 loader 链（裁决 b）：它与构建路径同源。

        用 ``platform_loader.fetch`` 计数证明取数发生，并断言快照的 ``platform`` 半带上
        本地覆盖（此处用 approval 清空作可观察标记——它在所有读平台路径上都生效）。
        """
        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")
        fetched: list[str] = []

        monkeypatch.setattr(
            rm.platform_loader,
            "fetch",
            lambda code, version=None: fetched.append(code) or {
                "agent_name": "a",
                "knowledgebase_settings": {"knowledgebases": []},
                "mcp_server_config": {"mcpServers": {}},
                "approval_settings": {"strategies": [{"strategy_id": "S"}]},
            },
        )

        raw = rm._raw_platform_config()

        assert fetched == ["app"]
        # 经链：审批已被 ApprovalLoader 清空。
        assert raw["approval_settings"] == {}


# 261014-urj 裁决 (b)：TestDeclarationScope（子 agent 运行时可门控）**整类删除**。
# 子 agent 串味防护已由 per-agent **路径隔离**取代——LocalAgentConfigLoader /
# KnowledgebaseLoader 按 agent_code 逐文件读 agents/${code}.json，父子读的是
# **不同的文件**，物理上不可能互相污染。这是结构性保证，强于原运行时门控。
# 取代它的回归守卫：TestPerAgentIsolation（见下）。


class TestPerAgentIsolation:
    """取代 ``TestDeclarationScope`` 的回归守卫（裁决 (b)）：路径隔离。

    两个 ``agent_code`` 各有自己的 ``${code}.json`` 文件，断言各自的声明**互不串味**——
    这正是原先门控要防的串味，现在由**结构**（不同的文件）保证。
    """

    def _pin_agents_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """把 per-agent 用户目录指向 tmp_path，返回该目录。"""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr(
            resource_manager.LocalDeclarations, "user_agents_dir", classmethod(lambda cls: str(agents_dir))
        )
        monkeypatch.setattr(
            resource_manager.LocalDeclarations, "project_agents_dir", classmethod(lambda cls: None)
        )
        monkeypatch.delenv(resource_manager.LocalDeclarations.CONFIG_FILE_OVERRIDE, raising=False)
        monkeypatch.delenv(resource_manager.LocalDeclarations.CWD_ENV, raising=False)

        return agents_dir

    def test_two_agents_read_their_own_files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        agents_dir = self._pin_agents_dir(monkeypatch, tmp_path)
        (agents_dir / "parent.json").write_text(
            json.dumps(
                {
                    "prompt_setting": {"llm_code": "parent-model"},
                    "mcp_server_config": {"mcpServers": {"parent-mcp": {}}},
                    "knowledgebase_settings": {"knowledgebases": ["parent-kb"]},
                }
            ),
            encoding="utf-8",
        )
        (agents_dir / "child.json").write_text(
            json.dumps(
                {
                    "prompt_setting": {"llm_code": "child-model"},
                    "mcp_server_config": {"mcpServers": {"child-mcp": {}}},
                }
            ),
            encoding="utf-8",
        )

        def _platform(agent_code: str, version: str | None = None) -> dict[str, Any]:
            return {
                "agent_code": agent_code,
                "agent_name": agent_code,
                "prompt_setting": {"llm_code": f"{agent_code}-platform-model"},
                "knowledgebase_settings": {"knowledgebases": [f"{agent_code}-platform-kb"]},
                "mcp_server_config": {"mcpServers": {f"{agent_code}-platform-mcp": {}}},
                "approval_settings": None,
            }

        rm = resource_manager.LocalResourceManager.build(CREDS, "judgehuang")
        monkeypatch.setattr(rm.platform_loader, "retrieve_agent_config", _platform, raising=True)

        parent_raw = rm.retrieve_agent_config("parent")
        child_raw = rm.retrieve_agent_config("child")

        # 各自文件生效，互不串味。
        assert parent_raw["prompt_setting"]["llm_code"] == "parent-model"
        assert "parent-mcp" in parent_raw["mcp_server_config"]["mcpServers"]
        assert "parent-kb" in parent_raw["knowledgebase_settings"]["knowledgebases"]

        assert child_raw["prompt_setting"]["llm_code"] == "child-model"
        assert "child-mcp" in child_raw["mcp_server_config"]["mcpServers"]
        # child 的文件没声明 knowledgebases，故只有平台那条；没有父的 'parent-kb'。
        assert "parent-kb" not in child_raw["knowledgebase_settings"]["knowledgebases"]
        assert "parent-mcp" not in child_raw["mcp_server_config"]["mcpServers"]

    def test_an_agent_without_a_file_gets_only_the_platform(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        agents_dir = self._pin_agents_dir(monkeypatch, tmp_path)
        (agents_dir / "only.json").write_text(
            json.dumps({"prompt_setting": {"llm_code": "only-model"}}), encoding="utf-8"
        )

        rm = _raw_manager(
            monkeypatch,
            {"agent_code": "missing", "agent_name": "missing", "prompt_setting": {"llm_code": "platform-model"}},
        )

        raw = rm.retrieve_agent_config("missing")

        # 没有 files 的 agent 得到纯平台值（无 per-agent 文件 → 贡献空）。
        assert raw["prompt_setting"]["llm_code"] == "platform-model"


class TestCredentialsAreStillStructurallyRejected:
    """D5 端到端守护：凭据键在结构上无法进入 ``LocalConfig`` / raw 输出。

    261014-rta 不得回退：``LocalConfig`` 无 token 字段（字段面由 ``FIELD_ALIASES``
    推导），且链在 raw 层合并时也**不**引入任何凭据键——``retrieve_agent_config`` 的
    输出里不出现 token 类键。
    """

    TOKEN_KEYS = ("access_token", "bk_token", "bk_ticket", "bk_app_secret", "app_secret")

    def test_local_config_has_no_token_field(self) -> None:
        import dataclasses

        names = {field.name for field in dataclasses.fields(LocalConfig)}

        for token in self.TOKEN_KEYS:
            assert token not in names, f"{token} must not be a LocalConfig field"

    def test_retrieve_agent_config_output_carries_no_token_class_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm = _raw_manager(
            monkeypatch,
            {
                "agent_code": "a",
                "agent_name": "a",
                "prompt_setting": {"llm_code": "platform"},
                "knowledgebase_settings": {"knowledgebases": []},
                "mcp_server_config": {"mcpServers": {}},
                "approval_settings": None,
            },
            local=LocalConfig(mcp_server_config={"mcpServers": {"local": {}}}),
        )

        raw = rm.retrieve_agent_config("a")

        for key in self.TOKEN_KEYS:
            assert key not in raw, f"{key} leaked into the merged raw dict"


class TestConfigSnapshotPayload:
    """快照载荷的形状契约——对着真实 manager 钉住（261015）。

    这些断言此前以纯函数 ``project_config_snapshot`` 的形式住在
    ``test_agent_runner.py``；装配合并进
    :meth:`LocalResourceManager.get_config_snapshot` 后，唯一入口是真实
    manager，因此经 ``_raw_manager``（真实 loader 链 + 注入平台 payload）钉住。
    "载荷形状"与"载荷由 manager 单一定义"是同一条契约，此处一并守护。

    ``mcpServers`` 断言的是**完整**定义（含带凭据 ``headers``，经审阅的决定）；
    因此本类任何测试都不得 ``print``/记录快照内容（§2 决定 4 的纪律在 §3 同样
    生效）。
    """

    @staticmethod
    def _payload(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_code": "test-agent",
            "agent_name": "Weather Bot",
            "prompt_setting": {"llm_code": "platform-model"},
            "knowledgebase_settings": {"knowledgebases": ["kb-1", 42, "kb-2"]},
            "mcp_server_config": {"mcpServers": {"z-mcp": {}, "a-mcp": {}}},
            "approval_settings": None,
        }
        payload.update(overrides)

        return payload

    def test_the_wire_payload_key_set_is_fixed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Node consumer narrows exactly these keys — additions are wire changes."""
        rm = _raw_manager(monkeypatch, self._payload(), local=LocalConfig())

        assert set(rm.get_config_snapshot()) == {
            "currentModel",
            "currentModelSource",
            "currentAgent",
            "currentAgentSource",
            "agentName",
            "localAgents",
            "localMcps",
            "localKbs",
            "boundMcps",
            "boundKbs",
            "mcpServers",
        }

    def test_mcp_servers_is_always_an_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``{}`` when no workspace definitions exist — never absent, never ``None``.

        The Node narrowing treats a non-object as "no data"; the explicit empty
        object keeps "no mcp.json" distinguishable from "malformed payload".
        """
        rm = _raw_manager(monkeypatch, self._payload(), local=LocalConfig())

        assert rm.get_config_snapshot()["mcpServers"] == {}

    def test_mcp_servers_travel_whole_headers_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Definitions travel WHOLE — a reviewed decision, not an oversight.

        The overlay's job is to show the user what they configured; a projection
        stripped to ``{transport, url}`` cannot answer "did my header land".
        Asserting the header value (not just the keys) is what pins that intent.
        """
        rm = _raw_manager(
            monkeypatch,
            self._payload(),
            local=LocalConfig(),
            mcp=McpConfig(
                servers={
                    "bk-data": {
                        "transport": "streamable_http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"X-Bkapi-Authorization": "TOKEN-VALUE"},
                        "selected_tools": ["v3_query"],
                    }
                }
            ),
        )

        assert rm.get_config_snapshot()["mcpServers"] == {
            "bk-data": {
                "transport": "streamable_http",
                "url": "https://example.invalid/mcp",
                "headers": {"X-Bkapi-Authorization": "TOKEN-VALUE"},
                "selected_tools": ["v3_query"],
            }
        }

    def test_the_platform_base_is_projected_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bound entries are the base layer (D-03), surfaced as facts.

        ``boundMcps`` sorted for a stable overlay order; non-string knowledgebase
        rows are dropped, not crashed on. ``currentAgent`` is the manager's own
        ``get_agent_code()`` (``"app"`` from CREDS) — the payload's ``agent_code``
        is only a fallback for when the manager has none.
        """
        rm = _raw_manager(monkeypatch, self._payload(), local=LocalConfig())
        snapshot = rm.get_config_snapshot()

        assert snapshot["currentModel"] == "platform-model"
        assert snapshot["currentModelSource"] == "platform"
        assert snapshot["currentAgent"] == "app"
        assert snapshot["currentAgentSource"] == "platform"
        assert snapshot["agentName"] == "Weather Bot"
        assert snapshot["boundMcps"] == ["a-mcp", "z-mcp"]
        assert snapshot["boundKbs"] == ["kb-1", "kb-2"]
        # 无本地声明（``local=LocalConfig()``）→ 本地声明项全部为空。
        assert snapshot["localAgents"] == []
        assert snapshot["localMcps"] == []
        assert snapshot["localKbs"] == []

    def test_local_declarations_arrive_through_the_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本地声明经链合并进 ``raw``（261014-urj），快照从 ``raw`` 读到它们。

        注意 ``*Source`` 徽标仍读**固定空的** local 槽——生效值落 ``raw``、来源
        徽标按"平台"报。这是 261014-urj 以来的既有生产语义（合并未改变它），
        徽标规则若要读链产物属于后续裁决，不在本次收拢里夹带。
        """
        rm = _raw_manager(
            monkeypatch,
            self._payload(),
            local=LocalConfig(prompt_setting={"llm_code": "local-model"}),
        )

        snapshot = rm.get_config_snapshot()

        assert snapshot["currentModel"] == "local-model"
        assert snapshot["currentModelSource"] == "platform"
