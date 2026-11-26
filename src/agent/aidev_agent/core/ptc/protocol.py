"""Bounded JSON envelopes shared by Runtime and its untrusted sandbox."""

from __future__ import annotations

import hashlib
import json

PROTOCOL_VERSION = 1
TERMINAL_EVENTS = frozenset({"finished", "failed", "cancelled"})
RESERVED_ARGUMENTS = frozenset({"config", "state", "runtime", "tool_call_id"})


class PtcError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def encode(value: object, limit: int = 1024 * 1024) -> bytes:
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PtcError("INVALID_JSON", "PTC values must be finite JSON data") from error
    if len(data) > limit:
        raise PtcError("PAYLOAD_TOO_LARGE", "PTC payload exceeds its byte limit")
    return data


def digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate_event(event: object, execution_id: str, instance_id: str | None = None) -> dict:
    if not isinstance(event, dict):
        raise PtcError("PROTOCOL_ERROR", "PTC event must be an object")
    if event.get("execution_id") != execution_id or event.get("protocol_version") != PROTOCOL_VERSION:
        raise PtcError("PROTOCOL_ERROR", "PTC event identity or version does not match")
    if not isinstance(event.get("seq"), int) or isinstance(event["seq"], bool) or event["seq"] < 0:
        raise PtcError("PROTOCOL_ERROR", "Invalid PTC event sequence")
    if not isinstance(event.get("runner_instance_id"), str) or not event["runner_instance_id"]:
        raise PtcError("PROTOCOL_ERROR", "Missing PTC process identity")
    if instance_id is not None and event["runner_instance_id"] != instance_id:
        raise PtcError("EXECUTION_LOST", "PTC process identity changed")
    pid = event.get("worker_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise PtcError("PROTOCOL_ERROR", "Invalid PTC code process identity")
    kind = event.get("type")
    if kind not in TERMINAL_EVENTS | {"tool_call", "running"}:
        raise PtcError("PROTOCOL_ERROR", "Unknown PTC event")
    if kind == "tool_call":
        if event.get("call_id") != f"{execution_id}:{event['seq']}":
            raise PtcError("PROTOCOL_ERROR", "Invalid PTC call identity")
        if not isinstance(event.get("name"), str) or not isinstance(event.get("arguments"), dict):
            raise PtcError("PROTOCOL_ERROR", "Invalid PTC tool request")
    return event
