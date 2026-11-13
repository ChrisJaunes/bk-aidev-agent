# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.

hook 运行态：随 ``config["configurable"]`` 传递的 per-run 内存对象。

中断续流是一次新的运行（新的 config），因此运行态中的「每轮一次」标记与 ``tool_pre`` 裁决必须
按 thread 续接：整轮只读的进度快照落在图状态 ``hook_run_state`` 通道（由模型节点与 pv 节点写、
无 hooks 时不写），新运行在模型节点 / pv 节点 / 工具 wrapper 入口按快照续接，续流后不重复触发、
不丢失裁决。未注入 hooks 时不写入任何键，运行收尾也不读取任何图状态（零回归）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage, messages_from_dict, messages_to_dict
from langchain_core.runnables import RunnableConfig

from .protocol import PASS, HookDecision, HookDecisionKind, HookIdentity, HookPoint, ModelPostPayload
from .registry import HookSet
from .runner import HookRunner

__all__ = [
    "HOOK_RUN_STATE_KEY",
    "HOOK_STATE_CHANNEL",
    "HookRunState",
    "bind_run_state",
    "dump_run_state",
    "get_run_state",
    "finalize_run",
    "record_model_response",
    "restore_run_state",
]

logger = logging.getLogger(__name__)

HOOK_RUN_STATE_KEY = "aidev_hook_run_state"

HOOK_STATE_CHANNEL = "hook_run_state"
"""图状态中的续接快照通道（仅在注入 hooks 时进入 state schema 并被写入）。"""


@dataclass
class HookRunState:
    """一次运行的 hook 运行态。

    Attributes:
        turn: 轮序号；一次运行（一次用户输入到最终答复）为一轮，起始 1。
        session_started: ``session_start`` 是否已触发（一次运行一次）。
        session_append: ``session_start.append`` 的追加片段，按次应用到发送内容。
        model_pre_done: 本轮 ``model_pre`` 是否已触发（每轮一次）。
        model_pre_override: ``model_pre.replace`` 的覆盖消息列表。
        model_pre_anchor_ids: 覆盖发生时已存在的消息 id 集合，用于切出新追加的尾部消息。
        model_post_done: 本轮最终答复已触发 ``model_post``（AC4，每轮一次）。
        terminal_post_done: 本轮的终止补触发（AC6）已消费；与 AC4 独立计数。
        model_request_count: 本轮模型请求次数。
        last_response: 本轮最后一次模型响应，供无最终答复的终止路径补触发。
        tool_pre_decisions: ``tool_call_id`` 到 ``tool_pre`` 裁决的映射。
    """

    agent_code: str = ""
    session_code: str = ""
    run_id: str = ""
    runner: HookRunner | None = None
    turn: int = 1
    session_started: bool = False
    session_append: str | None = None
    model_pre_done: bool = False
    model_pre_override: list[AnyMessage] | None = None
    model_pre_anchor_ids: frozenset[str] = frozenset()
    model_post_done: bool = False
    terminal_post_done: bool = False
    model_request_count: int = 0
    last_response: Any = None
    tool_pre_decisions: dict[str, HookDecision] = field(default_factory=dict)
    persisted_merged: bool = False

    def identity(self) -> HookIdentity:
        """构造处理器可见的只读标识。"""
        return HookIdentity(
            agent_code=self.agent_code,
            session_code=self.session_code,
            run_id=self.run_id,
            turn=self.turn,
        )

    def to_mapping(self) -> dict:
        """导出可 JSON 序列化的续接快照（写进图状态 ``hook_run_state`` 通道）。"""
        override = None
        if self.model_pre_override is not None:
            override = messages_to_dict(self.model_pre_override)
        return {
            "turn": self.turn,
            "model_pre_done": self.model_pre_done,
            "model_pre_override": override,
            "model_pre_anchor_ids": sorted(self.model_pre_anchor_ids),
            "model_post_done": self.model_post_done,
            "terminal_post_done": self.terminal_post_done,
            "model_request_count": self.model_request_count,
            "tool_pre_decisions": {
                tool_call_id: _decision_to_mapping(decision)
                for tool_call_id, decision in self.tool_pre_decisions.items()
            },
        }

    def merge_mapping(self, persisted: dict) -> None:
        """把续接快照并回运行态：只前进不后退，已有裁决不被覆盖。"""
        turn = persisted.get("turn")
        if isinstance(turn, int) and turn > self.turn:
            self.turn = turn
        self.model_pre_done = self.model_pre_done or bool(persisted.get("model_pre_done"))
        self.model_post_done = self.model_post_done or bool(persisted.get("model_post_done"))
        self.terminal_post_done = self.terminal_post_done or bool(persisted.get("terminal_post_done"))
        self.model_request_count = max(self.model_request_count, int(persisted.get("model_request_count") or 0))
        if self.model_pre_override is None:
            override = persisted.get("model_pre_override")
            if isinstance(override, list) and override:
                self.model_pre_override = messages_from_dict(override)
                anchor_ids = persisted.get("model_pre_anchor_ids") or []
                self.model_pre_anchor_ids = frozenset(str(each) for each in anchor_ids)
        for tool_call_id, raw in (persisted.get("tool_pre_decisions") or {}).items():
            if tool_call_id in self.tool_pre_decisions:
                continue
            decision = _decision_from_mapping(raw)
            if decision is not None:
                self.tool_pre_decisions[tool_call_id] = decision


def _decision_to_mapping(decision: HookDecision) -> dict:
    return {"kind": decision.kind.value, "reason": decision.reason, "payload": decision.payload}


def _decision_from_mapping(raw: Any) -> HookDecision | None:
    if not isinstance(raw, dict):
        return None
    try:
        kind = HookDecisionKind(raw.get("kind"))
    except ValueError:
        return None
    return HookDecision(kind=kind, reason=raw.get("reason") or "", payload=raw.get("payload"))


def bind_run_state(
    cfg: RunnableConfig,
    hooks: HookSet | None,
    *,
    agent_code: str = "",
    session_code: str = "",
    run_id: str = "",
) -> HookRunState | None:
    """把运行态绑定到 ``cfg["configurable"]``；``hooks`` 为空时不写任何键。"""
    if hooks is None:
        return None
    configurable = cfg.setdefault("configurable", {})
    state = HookRunState(
        agent_code=agent_code,
        session_code=session_code,
        run_id=run_id,
        runner=HookRunner(hooks),
    )
    configurable[HOOK_RUN_STATE_KEY] = state
    return state


def get_run_state(config: RunnableConfig | None) -> HookRunState | None:
    """从运行配置中取运行态；未注入 hooks 时返回 ``None``。"""
    if not config:
        return None
    configurable = config.get("configurable") if isinstance(config, dict) else None
    if not isinstance(configurable, dict):
        return None
    state = configurable.get(HOOK_RUN_STATE_KEY)
    return state if isinstance(state, HookRunState) else None


def record_model_response(config: RunnableConfig | None, response: Any) -> None:
    """记录一次模型请求的响应，供 ``model_post`` 补触发使用。"""
    state = get_run_state(config)
    if state is None:
        return
    state.model_request_count += 1
    state.last_response = response


def dump_run_state(config: RunnableConfig | None) -> dict | None:
    """导出当前运行态的续接快照；未注入 hooks 时返回 ``None``（不写图状态）。"""
    state = get_run_state(config)
    if state is None:
        return None
    return state.to_mapping()


def restore_run_state(state_values: Any, config: RunnableConfig | None) -> None:
    """按本轮语义续接运行态（模型节点、pv 节点与工具 wrapper 入口调用）。

    同一运行内只处理一次：本运行后续节点的写入不会再被旧快照覆盖。

    - 非续流（新的用户轮次）：只承接轮序号（上一轮 ``turn`` + 1），本轮级标记（``model_pre_done``
      / ``model_post_done`` / ``model_pre_override`` 等）全部重置；仍处于挂起状态（尚无 ToolMessage）
      的 ``tool_pre`` 裁决按 ``tool_call_id`` 保留，避免丢弃尚未落地的审批裁决。
    - 纯续流（同一条消息的中断续流）：并入快照，轮序号延续；上一段运行没有最终答复即终止，
      终止触发（AC6）已消费过，续流后不再重复补触发。
    - 续流携带新的用户输入（跳过提问）：视为新一轮，轮序号 +1 并重新开放点位；挂起批次的
      ``tool_pre`` 裁决同样保留，未获批准的 ``ask`` 不得因换轮而放行。
    """
    state = get_run_state(config)
    if state is None or not isinstance(state_values, dict) or state.persisted_merged:
        return
    state.persisted_merged = True

    configurable = config.get("configurable") if isinstance(config, dict) else None
    execute_kwargs = (configurable or {}).get("execute_kwargs")
    resuming = bool(getattr(execute_kwargs, "resume", None))
    if resuming and not getattr(execute_kwargs, "input", ""):
        persisted = state_values.get(HOOK_STATE_CHANNEL)
        if isinstance(persisted, dict):
            state.merge_mapping(persisted)
            if not state.model_post_done and state.model_request_count > 0:
                # 上一段运行无最终答复即终止（中断）：AC6 终止触发已消费一次
                state.terminal_post_done = True
        return

    # 新一轮：承接轮序号并重置本轮级标记；挂起批次的 tool_pre 裁决继续保留
    persisted = state_values.get(HOOK_STATE_CHANNEL)
    previous_turn = persisted.get("turn") if isinstance(persisted, dict) else 0
    state.turn = int(previous_turn or 0) + 1
    state.session_started = False
    state.session_append = None
    state.model_pre_done = False
    state.model_pre_override = None
    state.model_pre_anchor_ids = frozenset()
    state.model_post_done = False
    state.model_request_count = 0
    state.last_response = None
    state.terminal_post_done = False
    state.tool_pre_decisions = _pending_decisions(state_values, persisted)


def _pending_decisions(state_values: dict, persisted: Any) -> dict[str, HookDecision]:
    """只保留仍挂起（尚未产出 ToolMessage）的 tool_call 裁决。

    「跳过提问 + 新输入」会在挂起批次尚未落地时进入新一轮；裁决按 ``tool_call_id`` 继续生效，
    未获批准的 ``ask`` 不会因换轮而放行，``deny`` / ``replace`` 也保持原有语义。
    """
    if not isinstance(persisted, dict):
        return {}
    raw_decisions = persisted.get("tool_pre_decisions") or {}
    if not raw_decisions:
        return {}
    messages = state_values.get("messages") or []
    if not messages:
        return {}
    last = messages[-1]
    if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
        return {}
    done_ids = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    pending_ids = {call.get("id") for call in last.tool_calls if call.get("id") and call.get("id") not in done_ids}
    retained: dict[str, HookDecision] = {}
    for tool_call_id in pending_ids:
        decision = _decision_from_mapping(raw_decisions.get(tool_call_id))
        if decision is not None:
            retained[tool_call_id] = decision
    return retained


def finalize_run(state_values: Any = None, config: RunnableConfig | None = None) -> None:
    """运行收尾：在无最终答复的终止路径（中断 / 续跑上限 / 取消）补触发一次 ``model_post``。

    与 AC4 的「最终答复触发」各自独立计数：终止补触发按运行记一次（``terminal_post_done``），
    续流后的最终答复仍会按 AC4 再触发一次并可 ``replace``。此处运行已终止，仅观察语义；
    ``replace`` / ``deny`` 记 warning 后忽略。未注入 hooks 或本轮已有最终答复时直接跳过，
    不读取任何图状态。
    """
    state = get_run_state(config)
    if (
        state is None
        or state.runner is None
        or state.model_post_done
        or state.terminal_post_done
        or state.last_response is None
    ):
        return
    try:
        payload = ModelPostPayload(
            response=state.last_response,
            model_request_count=state.model_request_count,
            turn=state.turn,
        )
        decision = state.runner.run_sync(HookPoint.MODEL_POST, state.identity(), payload)
        if decision.kind is not PASS:
            logger.warning(
                "[Hook] terminal model_post decision ignored point=%s decision=%s run=%s",
                HookPoint.MODEL_POST.value,
                decision.kind.value,
                state.run_id,
            )
    except Exception:
        logger.warning("[Hook] terminal model_post failed run=%s", state.run_id, exc_info=True)
    finally:
        state.terminal_post_done = True
