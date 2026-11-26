"""Explicit, server-owned settings for the first serial PTC implementation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PtcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    snapshot: str = ""
    # Host opt-in: only data tools without interrupts/graph mutations may be listed.
    tool_codecs: dict[str, Literal["json", "text", "content"]] = Field(default_factory=dict)
    execution_seconds: int = Field(default=300, ge=1, le=3600)
    tool_seconds: int = Field(default=60, ge=1, le=300)
    wait_seconds: float = Field(default=20, gt=0, le=20)
    http_timeout: int = Field(default=30, ge=10, le=120)
    sandbox_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    max_tool_calls: int = Field(default=100, ge=1, le=1000)
    max_executions: int = Field(default=16, ge=1, le=100)
    max_cached_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    max_code_bytes: int = Field(default=128 * 1024, ge=1, le=128 * 1024)
    max_result_bytes: int = Field(default=512 * 1024, ge=1, le=512 * 1024)
    max_log_bytes: int = Field(default=1024 * 1024, ge=0, le=1024 * 1024)
    reconnect_attempts: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def validate_deadlines(self):
        if self.enabled and not self.snapshot.strip():
            raise ValueError("PTC requires a dedicated sandbox snapshot")
        if self.http_timeout <= self.wait_seconds + 5:
            raise ValueError("PTC HTTP timeout must exceed the bridge wait plus five seconds")
        if self.sandbox_ttl_seconds < self.execution_seconds + 30:
            raise ValueError("PTC sandbox TTL must cover execution and cleanup")
        return self
