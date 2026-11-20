# -*- coding: utf-8 -*-
"""T-9：错误处理与进程可用性（REQ-5 / REQ-10 / ACC-13）。

六类坏请求 —— 非法 JSON、未知方法、未知会话、``close`` 后请求、装配异常、
执行期未归类异常 —— 都必须返回结构化错误、写出 ``RUN_ERROR``，且进程/连接继续可用。
错误响应不得包含完整请求体与凭据。
"""


import pytest
from ag_ui.core import EventType

from tests.integration._fake_llm import FakeLLMServer
from tests.integration._harness import Harness
from tests.integration._paths import AGENT_ROOT

PROJECT_ROOT = AGENT_ROOT
SECRET = "SECRET-TOKEN-DO-NOT-LEAK"


@pytest.fixture
def llm():
    with FakeLLMServer(["好的"]) as server:
        yield server


def test_runtime_exception_maps_to_internal_error_and_process_stays_usable(llm):
    harness = Harness()
    harness.send_obj("c1", "create", {
        "session_code": "s1", "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
        "tool_factories": ["tests.integration._worker_observations:execute_error_factory"],
    })
    harness.result("c1")
    harness.send("t1", "turn", session_code="s1", input="hi")
    harness.wait()
    assert harness.error("t1")["code"] == "agent.internal_error"
    assert harness.event_types("t1")[-1] == EventType.RUN_ERROR.value
    harness.send_obj("c2", "create", {
        "session_code": "s2", "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
    })
    harness.result("c2")
    harness.send("t2", "turn", session_code="s2", input="after failure")
    harness.wait()
    assert harness.result("t2")["status"] == "finished"


@pytest.mark.slow
def test_model_5xx_returns_structured_failure_without_duplicate_run_error():
    """执行链只产出 RUN_ERROR 事件（不抛异常）时，响应必须 ok=false 且带稳定错误码。

    F-1 回归：模型端点 5xx / 不可达时，平台侧只发 RUN_ERROR + 补发 RUN_FINISHED(success)，
    不抛出到 headless。修复前该轮以 ``ok=true`` + ``status=error`` + 空回复结束，
    违反 REQ-5（不得返回空回复）与 REQ-7（三态可区分）。
    """
    with FakeLLMServer(["不会用到"], error_status=500, error_message="upstream model is unavailable") as broken:
        harness = Harness()
        harness.send_obj(
            "c1",
            "create",
            {
                "session_code": "s1",
                "agent": {"agent_code": "agent-hook"},
                "llm": {"base_url": broken.base_url, "model": "fake-model"},
            },
        )
        harness.result("c1")

        harness.send("t1", "turn", session_code="s1", input="hi")
        harness.wait()

        error = harness.error("t1")
        assert error["code"] == "agent.internal_error"
        assert "upstream model is unavailable" in error["message"]

        # 基线事件序列不变：RUN_ERROR 后由平台补发 RUN_FINISHED(success)，且只写一条 RUN_ERROR
        types = harness.event_types("t1")
        assert types[0] == EventType.RUN_STARTED.value
        assert types[-1] == EventType.RUN_ERROR.value
        assert types.count(EventType.RUN_FINISHED.value) == 0, "失败轮不得发布成功终态"
        assert types.count(EventType.RUN_ERROR.value) == 1, types
        terminal = harness.events("t1")[-1]
        assert terminal["type"] == EventType.RUN_ERROR.value

        # resume 同形：同一会话续轮遇到同样的执行期错误，同样是 ok=false + 稳定错误码
        harness.send("r1", "resume", session_code="s1", input="again")
        harness.wait()
        resume_error = harness.error("r1")
        assert resume_error["code"] == "agent.internal_error"
        assert "upstream model is unavailable" in resume_error["message"]
        resume_types = harness.event_types("r1")
        assert resume_types.count(EventType.RUN_ERROR.value) == 1, resume_types
        assert resume_types[-1] == EventType.RUN_ERROR.value
        assert resume_types.count(EventType.RUN_FINISHED.value) == 0


def test_build_error_at_create_does_not_leave_a_broken_session(llm):
    harness = Harness()
    harness.send_obj(
        "c1",
        "create",
        {"session_code": "s1", "agent": {"agent_code": "agent-hook"}, "llm": {"model": ""}},
    )
    assert harness.error("c1")["code"] == "agent.build_failed"
    # 失败后同名会话可重新创建（不残留半成品）
    harness.send_obj(
        "c2",
        "create",
        {
            "session_code": "s1",
            "agent": {"agent_code": "agent-hook"},
            "llm": {"base_url": llm.base_url, "model": "fake-model"},
        },
    )
    assert harness.result("c2")["created"] is True