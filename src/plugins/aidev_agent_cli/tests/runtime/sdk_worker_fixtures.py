"""Child-local SDK fault injection; RuntimeService and ProcessWorker stay real."""

import os
import signal
import time


def install_sdk_faults():
    from aidev_agent_cli.runtime.sdk.session import SDKSession

    def build(self, **kwargs):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.writer({"type": "CUSTOM", "name": "test_worker", "value": {"pid": os.getpid()}})
        if self.agent_code == "hang-build":
            while True:
                time.sleep(0.01)
        return Agent(self, kwargs.get("hook_handlers"))

    original_close = SDKSession.close

    def close(self):
        if self.agent_code == "crash-close":
            os._exit(19)
        return original_close(self)

    SDKSession.close = close
    SDKSession.ensure_platform_session = lambda self: None
    SDKSession._build_agent = build
    SDKSession._build_agent_via_resume = lambda self, payload, **kwargs: build(self, **kwargs)
    return None


class Agent:
    def __init__(self, session, hook_handlers=None):
        self.session = session
        self.event_handler = session.writer
        self.hook_handlers = hook_handlers

    def execute(self, kwargs):
        if self.hook_handlers is not None:
            # 与 SDK 图的 turn_start 节点一致：本轮开始时触发一次
            from aidev_agent.core.hook import HookRunner

            HookRunner(self.hook_handlers).turn_start({}, [])
        if self.session.agent_code == "hang-execute":
            while True:
                time.sleep(0.01)
        if self.session.agent_code == "interrupt":
            from aidev_agent_cli.runtime.hook_contract import HOOK_ASK_REASON

            if self.session.hook_bus is not None:
                self.session.hook_bus._ask_memo = {"memo": "saved"}
            self.session.writer(
                {
                    "type": "RUN_FINISHED",
                    "runId": "interrupt-run",
                    "threadId": self.session.session_code,
                    "outcome": {"type": "interrupt", "interrupts": [{"id": "pending-1", "reason": HOOK_ASK_REASON}]},
                }
            )
        if self.session.agent_code == "error":
            self.session.writer({"type": "RUN_ERROR", "message": "injected SDK error"})
        if self.session.agent_code == "ok":
            self.session.writer({"type": "TEXT_MESSAGE_START", "messageId": "assistant", "role": "assistant"})
            self.session.writer({"type": "TEXT_MESSAGE_CONTENT", "messageId": "assistant", "delta": "sdk worker"})
            self.session.writer({"type": "TEXT_MESSAGE_END", "messageId": "assistant"})
        yield "done"

    def stop(self):
        pass


def hanging_build_factory():
    from pathlib import Path

    if Path(os.environ["RUNTIME_HANG_MARKER"]).exists():
        return real_tool_factory()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.environ["RUNTIME_HANG_MARKER"], "w", encoding="utf-8") as marker:
        marker.write(str(os.getpid()))
    while True:
        time.sleep(0.01)


def real_tool_factory():
    from langchain_core.tools import tool

    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    @tool
    def hanging_tool(q: str) -> str:
        """Run a deterministic tool for the process isolation acceptance test."""
        if q == "hang":
            with open(os.environ["RUNTIME_HANG_MARKER"], "w", encoding="utf-8") as marker:
                marker.write(str(os.getpid()))
            while True:
                time.sleep(0.01)
        return "done"

    return hanging_tool
