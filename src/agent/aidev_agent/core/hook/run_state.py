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

不写图状态、不落库、不进 AG-UI 快照；未注入 hooks 时不写入任何键，运行收尾也不读取
任何图状态（零回归）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig

from .contexts import HookIdentity, ModelPostPayload
from .decisions import PASS, HookDecision
from .points import HookPoint
from .registry import HookSet
from .runner import HookRunner

__all__ = [
    "HOOK_RUN_STATE_KEY",
    "HookRunState",
    "bind_run_state",
    "get_run_state",
    "finalize_run",
    "record_model_response",
]

logger = logging.getLogger(__name__)

HOOK_RUN_STATE_KEY = "aidev_hook_run_state"


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
        model_post_done: 本轮 ``model_post`` 是否已触发（每轮一次）。
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
    model_request_count: int = 0
    last_response: Any = None
    tool_pre_decisions: dict[str, HookDecision] = field(default_factory=dict)

    def identity(self) -> HookIdentity:
        """构造处理器可见的只读标识。"""
        return HookIdentity(
            agent_code=self.agent_code,
            session_code=self.session_code,
            run_id=self.run_id,
            turn=self.turn,
        )


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


def finalize_run(state_values: Any = None, config: RunnableConfig | None = None) -> None:
    """运行收尾：在无最终答复的终止路径（中断 / 续跑上限 / 取消）补触发一次 ``model_post``。

    此时运行已终止，只执行观察语义；``replace`` / ``deny`` 记 warning 后忽略。
    ``hooks`` 未注入或本轮已触发过 ``model_post`` 时直接跳过，不读取任何图状态。
    """
    state = get_run_state(config)
    if state is None or state.runner is None or state.model_post_done or state.last_response is None:
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
        state.model_post_done = True
