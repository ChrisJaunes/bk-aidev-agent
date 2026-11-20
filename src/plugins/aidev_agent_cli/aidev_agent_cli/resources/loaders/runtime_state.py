"""Highest-priority explicit model override snapshot."""

from typing import Any

from .base import ResourceLoader, ResourceOverrides


class RuntimeInstanceLoader(ResourceLoader):
    name = "runtime"

    @classmethod
    def create(cls, rm):
        return cls(rm.overrides)

    def __init__(self, overrides: ResourceOverrides):
        self.overrides = overrides

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.overrides.agent_code and raw.get("agent_code") != self.overrides.agent_code:
            return raw
        if self.overrides.model:
            section = raw.get("prompt_setting")
            if not isinstance(section, dict):
                section = raw["prompt_setting"] = {}
            section["llm_code"] = self.overrides.model
        return raw
