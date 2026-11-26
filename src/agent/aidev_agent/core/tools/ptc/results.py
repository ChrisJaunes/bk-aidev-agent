"""Typed, sanitized program results; display limits never corrupt program data."""

import json
from dataclasses import dataclass

from langchain_core.messages import ToolMessage

from aidev_agent.packages.security.redaction import RedactionPurpose, redact_payload
from aidev_agent.packages.security.threat_patterns import sanitize_pollution_keys

from .protocol import PtcError, encode


@dataclass(frozen=True)
class ProgrammaticResult:
    payload: dict


def safe_payload(value, security):
    # PTC always redacts its data boundary, independently of model display toggles.
    return redact_payload(
        sanitize_pollution_keys(value), purpose=RedactionPurpose.MODEL_OUTPUT, settings=security.redaction
    )


def result_adapter(codecs, settings, security):
    def adapt(request, message):
        if not isinstance(message, ToolMessage):
            raise PtcError("UNSUPPORTED_RESULT", "PTC data tools cannot return graph commands")
        try:
            if message.status == "error":
                payload = {"ok": False, "error": {"code": "TOOL_FAILED", "message": str(message.content)}}
            else:
                codec = codecs[request.tool_call["name"]]
                value = message.content
                if codec == "json" and isinstance(value, str):
                    value = json.loads(value)
                elif codec == "text" and not isinstance(value, str):
                    raise ValueError("Expected text result")
                payload = {"ok": True, "value": value}
            payload = safe_payload(payload, security)
            data = encode(payload, settings.max_result_bytes)
        except (PtcError, ValueError, TypeError, KeyError):
            payload = {
                "ok": False,
                "error": {"code": "INVALID_TOOL_RESULT", "message": "Invalid or oversized tool result"},
            }
            data = encode(payload)
        # The artifact contains only the same sanitized version as the display.
        return message.model_copy(
            update={
                "artifact": ProgrammaticResult(payload),
                "content": data[:16384].decode("utf-8", "replace"),
                "status": "success" if payload["ok"] else "error",
            }
        )

    return adapt
