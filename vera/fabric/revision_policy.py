"""Deployment policy for the public canonical Fabric revision path."""

from __future__ import annotations

from typing import Any

from .caller_policy import CallerPolicy


class RevisionPolicy(CallerPolicy):
    def __init__(self, value: dict[str, Any] | None = None):
        super().__init__(value, write_actions={"revision.write"})

    @classmethod
    def from_json(cls, raw: str) -> "RevisionPolicy":
        if not str(raw or "").strip():
            return cls()
        parsed = CallerPolicy.parse(raw, write_actions={"revision.write"})
        return cls({"readers": list(parsed.readers), "writers": list(parsed.writers),
                    "namespaces": {name: {key: list(value) for key, value in rules.items()}
                                   for name, rules in parsed.namespaces.items()}})
