"""Reject malformed identities, non-JSON values and invalid server budgets."""

import pytest
from pydantic import ValidationError

from aidev_agent.core.tools.ptc.protocol import PtcError, encode, validate_event
from aidev_agent.core.tools.ptc.settings import PtcSettings


@pytest.mark.parametrize("value", [float("nan"), {"x": object()}, "x" * 50])
def test_invalid_or_oversized_values(value):
    with pytest.raises(PtcError):
        encode(value, limit=10)


@pytest.mark.parametrize("changes", [{"execution_id": "other"}, {"seq": True}, {"runner_instance_id": "other"}])
def test_event_identity_and_sequence(changes):
    event = {
        "protocol_version": 1,
        "execution_id": "exec",
        "runner_instance_id": "runner",
        "type": "finished",
        "seq": 1,
    }
    with pytest.raises(PtcError):
        validate_event({**event, **changes}, "exec", "runner")


@pytest.mark.parametrize("settings", [{"enabled": True}, {"http_timeout": 20}, {"sandbox_ttl_seconds": 100}])
def test_invalid_settings(settings):
    with pytest.raises(ValidationError):
        PtcSettings(**settings)
