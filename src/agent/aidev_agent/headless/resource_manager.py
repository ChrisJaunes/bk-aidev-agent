# -*- coding: utf-8 -*-
"""headless 资源管理器：不触达平台的最小 ``ResourceManagerProtocol`` 实现（REQ-8）。

设计约束：
- 不导入 Django / blueapps，不发任何平台网络请求；
- agent 配置由调用方注入的 ``agent`` / ``llm`` 段合成，不查平台；
- 会话历史来自内存账本（``HeadlessLedger``），供 ``build_type=SESSION`` 续轮；
- 事件只走 writer，``publish_event`` 为 no-op；
- 任何平台 API 入口统一抛 :class:`HeadlessUnsupportedError`，使既有
  ``try/except`` 兜底把平台写入降级为日志，而不是产生真实调用。
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from aidev_agent.pydantic_models import AgentConfig

from .errors import HeadlessUnsupportedError


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


class HeadlessResourceManager:
    """``ResourceManagerProtocol`` 的无平台实现。

    :param agent_code: 会话使用的 agent 标识
    :param chat_model: ``agent`` 段注入的模型名，同时作为 ``non_thinking_llm``
    :param ledger: 会话内存账本（``get_chat_session_contents`` 的数据源）
    :param username: 调用方身份，仅用于装配期元数据，不参与认证
    """

    def __init__(
        self,
        *,
        agent_code: str,
        chat_model: str,
        ledger: HeadlessLedger | None = None,
        username: str | None = None,
    ) -> None:
        self.agent_code = agent_code
        self.chat_model = chat_model
        self.ledger = ledger if ledger is not None else HeadlessLedger()
        self.username = username
        # 装配期 ``build_executor_info`` 会无条件读取这三个字段；headless 没有平台凭证，
        # 统一为空串（不会触发任何网络请求，也不会写进日志）。
        self.access_token = ""
        self.app_code = ""
        self.app_secret = ""

    # ---------------- 装配与账本（唯一被真实使用的能力） ----------------

    def _agent_config_dict(self) -> dict[str, Any]:
        return {
            "agent_code": self.agent_code,
            "agent_name": self.agent_code,
            "chat_model": self.chat_model,
            "non_thinking_llm": self.chat_model,
            "tool_codes": [],
            "knowledgebase_ids": [],
            "knowledge_ids": [],
            "related_skills": [],
            "related_agents": [],
        }

    def retrieve_agent_config(self, agent_code: str, version: Optional[str] = None, **kwargs) -> dict:
        config = self._agent_config_dict()
        config["agent_code"] = agent_code or self.agent_code
        config["agent_name"] = config["agent_code"]
        return config

    def get_agent_config(self, agent_code: str, version: Optional[str] = None, **kwargs) -> AgentConfig:
        return AgentConfig.model_validate(self.retrieve_agent_config(agent_code, version=version, **kwargs))

    def get_agent_code(self, **kwargs) -> str:
        return self.agent_code

    def get_chat_session_contents(self, session_code: str, **kwargs) -> list[dict]:
        return self.ledger.records()

    def get_chat_session_context(self, session_code: str, **kwargs) -> list[dict]:
        return self.ledger.records()

    # ---------------- 事件发布（headless 只走 writer） ----------------

    def publish_event(self, event: Any) -> None:
        return None

    def event_publishing_enabled(self) -> bool:
        return False

    # ---------------- 平台能力：统一显式失败 ----------------

    def _unsupported(self, method: str):
        raise HeadlessUnsupportedError(method)

    def get_client(self, **kwargs):
        self._unsupported("get_client")

    def retrieve_knowledgebase(self, id: int, **kwargs) -> dict:
        self._unsupported("retrieve_knowledgebase")

    def retrieve_knowledge(self, id: int, **kwargs) -> dict:
        self._unsupported("retrieve_knowledge")

    def is_resume_session(self, session_code: str, **kwargs) -> bool:
        self._unsupported("is_resume_session")

    def create_tool_approval(self, payload: dict, *, username: str | None = None, **kwargs) -> dict:
        self._unsupported("create_tool_approval")

    def retrieve_chat_session(self, session_code: str, **kwargs) -> dict:
        """会话详情在 headless 下为空（事实源归宿主），返回值只用于装配期兜底读取。"""
        return {}

    def update_chat_session_sandbox_pv_id(self, session_code: str, sandbox_pv_id: str, **kwargs) -> dict:
        self._unsupported("update_chat_session_sandbox_pv_id")

    def update_chat_session_caller_context(self, session_code: str, caller_context: dict, **kwargs) -> dict:
        self._unsupported("update_chat_session_caller_context")

    def get_or_create_session(
        self,
        session_code: str,
        session_name: str,
        *,
        protocol_version: str = "",
        is_temporary: bool = False,
        session_type: str = "",
        channel_type: str = "",
        **kwargs,
    ) -> dict:
        self._unsupported("get_or_create_session")

    def retrieve_skill(self, skill_id: str, version: str | None, callee_agent_code: str | None, **kwargs) -> dict:
        self._unsupported("retrieve_skill")

    def check_agent_call_permission(self, caller_app_code: str, username: Optional[str] = None, **kwargs) -> dict:
        self._unsupported("check_agent_call_permission")

    def construct_tool(self, tool_code: str, username: str | None = None, executor_info: dict | None = None, **kwargs):
        self._unsupported("construct_tool")

    def resolve_access_token(self, username: str = None) -> str:
        """返回空凭据。

        ``build_executor_info`` 在装配期无条件调用本方法，但它只读进程内字段、不发请求；
        返回空串让装配继续，同时保证 executor_info 里没有可用凭据（REQ-8 / REQ-11）。
        """
        return ""

    def get_paas_sbx_client(self, executor_info: dict, **kwargs):
        self._unsupported("get_paas_sbx_client")

    def construct_mcp(self, mcp_config: dict, username: str = None, **kwargs):
        """无 MCP 配置时返回空结果（装配期无条件调用，但不触达平台）；有配置则显式失败。"""
        if not mcp_config:
            from aidev_agent.packages.langchain_core.tools.base import McpToolsResult

            return McpToolsResult(tools=[], fetch_failures=[])
        self._unsupported("construct_mcp")

    def knowledge_query(self, data: dict[str, Any]) -> dict:
        self._unsupported("knowledge_query")

    def start_flow_agent(self, data: dict, **kwargs) -> dict:
        self._unsupported("start_flow_agent")

    def get_flow_agent_task_info(self, task_id: str, **kwargs) -> dict:
        self._unsupported("get_flow_agent_task_info")

    def retry_flow_agent_node(self, session_code: str, node_id: str, **kwargs) -> dict:
        self._unsupported("retry_flow_agent_node")

    def skip_flow_agent_node(self, session_code: str, node_id: str, **kwargs) -> dict:
        self._unsupported("skip_flow_agent_node")

    def stop_flow_agent_task(self, session_code: str, **kwargs) -> dict:
        self._unsupported("stop_flow_agent_task")

    def pause_flow_agent_task(self, session_code: str, **kwargs) -> dict:
        self._unsupported("pause_flow_agent_task")

    def resume_flow_agent_task(self, session_code: str, **kwargs) -> dict:
        self._unsupported("resume_flow_agent_task")

    def get_flow_agent_task_node_info(self, task_id: str, node_id: str, **kwargs) -> dict:
        self._unsupported("get_flow_agent_task_node_info")
