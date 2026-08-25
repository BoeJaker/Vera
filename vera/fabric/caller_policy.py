"""Small deployment caller policy shared by Fabric provider boundaries."""

from __future__ import annotations

import json
from typing import Any, Iterable


KNOWN_ACTORS = frozenset({"user", "codex", "claude", "claude_code", "autonomous"})


class CallerPolicy:
    def __init__(self, value: dict[str, Any] | None = None, *,
                 write_actions: Iterable[str]):
        value = dict(value or {})
        self.write_actions = frozenset(write_actions)
        if not self.write_actions:
            raise ValueError("write_actions cannot be empty")
        self.readers = self._actors(value.get("readers", ["*"]), "readers")
        self.writers = self._actors(value.get("writers", ["user"]), "writers")
        raw_scopes = value.get("namespaces", {})
        if not isinstance(raw_scopes, dict):
            raise ValueError("namespaces must be an object")
        self.namespaces: dict[str, dict[str, frozenset[str]]] = {}
        for namespace, rules in raw_scopes.items():
            namespace = str(namespace).strip()
            if not namespace or not isinstance(rules, dict):
                raise ValueError("namespace policy entries must be objects")
            self.namespaces[namespace] = {
                "readers": self._actors(rules.get("readers", self.readers), "readers"),
                "writers": self._actors(rules.get("writers", self.writers), "writers"),
            }

    @staticmethod
    def _actors(value: Any, field: str) -> frozenset[str]:
        if isinstance(value, frozenset):
            actors = value
        elif isinstance(value, (list, tuple, set)):
            actors = frozenset(str(item).strip() for item in value)
        else:
            raise ValueError(f"{field} must be an array")
        if not actors or "" in actors or any(
                actor != "*" and actor not in KNOWN_ACTORS for actor in actors):
            raise ValueError(f"{field} contains an invalid actor")
        return actors

    @classmethod
    def parse(cls, raw: str, *, write_actions: Iterable[str]) -> "CallerPolicy":
        if not str(raw or "").strip():
            return cls(write_actions=write_actions)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("caller policy must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("caller policy must be an object")
        return cls(value, write_actions=write_actions)

    def allows(self, action: str, actor: str, resource: dict[str, Any]) -> bool:
        namespace = str(resource.get("namespace") or "").strip()
        rules = self.namespaces.get(namespace, {})
        field = "writers" if action in self.write_actions else "readers"
        allowed = rules.get(field)
        if allowed is None:
            allowed = self.writers if field == "writers" else self.readers
        return "*" in allowed or actor in allowed
