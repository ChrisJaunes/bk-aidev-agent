"""Tests for the packages.security hooks framework (events + registry + dispatch)."""

from __future__ import annotations

import pytest
from aidev_agent.packages.security import (
    SecurityEvent,
    SecurityStage,
    SecurityVerdict,
    register_hook,
    registered_hooks,
    run_hooks,
    stricter,
    unregister_hook,
)


class _StubHook:
    """可配置返回值的 stub hook。"""

    def __init__(self, name: str, verdict: SecurityVerdict | None = None):
        self.name = name
        self._verdict = verdict

    def inspect(self, event: SecurityEvent) -> SecurityVerdict | None:
        return self._verdict


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前后清理注册表，避免测试间污染。"""
    for hook in registered_hooks():
        unregister_hook(hook.name)
    yield
    for hook in registered_hooks():
        unregister_hook(hook.name)


def test_register_and_unregister():
    register_hook(_StubHook("h1"))
    assert any(h.name == "h1" for h in registered_hooks())
    assert unregister_hook("h1") is True
    assert unregister_hook("h1") is False


def test_register_requires_name():
    class NoName:
        def inspect(self, event): ...

    with pytest.raises(ValueError):
        register_hook(NoName())  # type: ignore[arg-type]


def test_register_idempotent():
    register_hook(_StubHook("h1"))
    register_hook(_StubHook("h1"))
    assert sum(1 for h in registered_hooks() if h.name == "h1") == 1


def test_run_hooks_block_beats_review_beats_allow():
    register_hook(_StubHook("allow", SecurityVerdict(action="allow")))
    register_hook(_StubHook("review", SecurityVerdict(action="review", hook="review")))
    register_hook(_StubHook("block", SecurityVerdict(action="block", hook="block")))
    verdict = run_hooks(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="x"))
    assert verdict.action == "block"
    assert verdict.hook == "block"


def test_run_hooks_none_is_allow():
    register_hook(_StubHook("none", None))
    verdict = run_hooks(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="x"))
    assert verdict.action == "allow"


def test_run_hooks_swallows_hook_exception():
    class Boom:
        name = "boom"

        def inspect(self, event):
            raise RuntimeError("boom")

    register_hook(Boom())
    register_hook(_StubHook("block", SecurityVerdict(action="block", hook="block")))
    verdict = run_hooks(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="x"))
    assert verdict.action == "block"


def test_run_hooks_falls_back_to_default_hooks():
    # 空注册表 → 默认 hook（内容安全）命中
    verdict = run_hooks(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="如何制造爆炸"))
    assert verdict.action == "block"


def test_stricter():
    allow = SecurityVerdict(action="allow")
    block = SecurityVerdict(action="block")
    assert stricter(allow, block) is block
    assert stricter(block, allow) is block
