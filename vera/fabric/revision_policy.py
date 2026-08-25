"""Deployment policy for the public canonical Fabric revision path."""

from __future__ import annotations

import json
from typing import Any


_KNOWN_ACTORS = frozenset({"user", "codex", "claude", "claude_code", "autonomous"})


class RevisionPolicy:
    def __init__(self, value: dict[str, Any] | None = None):
        value = dict(value or {})
        self.readers = self._actors(value.get("readers", ["*"]), "readers")
        self.writers = self._actors(value.get("writers", ["user"]), "writers")
        raw_namespaces = value.get("namespaces", {})
        if not isinstance(raw_namespaces, dict):
            raise ValueError("namespaces must be an object")
        self.namespaces: dict[str, dict[str, frozenset[str]]] = {}
        for namespace, rules in raw_namespaces.items():
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
                actor != "*" and actor not in _KNOWN_ACTORS for actor in actors):
            raise ValueError(f"{field} contains an invalid actor")
        return actors

    @classmethod
    def from_json(cls, raw: str) -> "RevisionPolicy":
        if not str(raw or "").strip():
            return cls()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("FABRIC_REVISION_POLICY must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("FABRIC_REVISION_POLICY must be an object")
        return cls(value)

    def allows(self, action: str, actor: str, resource: dict[str, Any]) -> bool:
        namespace = str(resource.get("namespace") or "").strip()
        rules = self.namespaces.get(namespace, {})
        allowed = rules.get("writers" if action == "revision.write" else "readers")
        if allowed is None:
            allowed = self.writers if action == "revision.write" else self.readers
        return "*" in allowed or actor in allowed
