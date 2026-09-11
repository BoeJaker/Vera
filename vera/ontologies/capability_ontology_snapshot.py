"""Portable, content-addressed snapshots of capability-ontology relations."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any


CAPABILITY_ONTOLOGY_SNAPSHOT_SCHEMA = "vera.capability-ontology-snapshot/v1"
AUTO_GENERATION_ENV = "VERA_CAP_ONTOLOGY_AUTO_GENERATION"
MAX_SNAPSHOT_RELATIONS = 10_000
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 16_384
MAX_TAGS = 32


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _text(value: Any, field: str, *, maximum: int = 256,
          allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    result = value.strip()
    if (not result and not allow_empty) or len(result.encode("utf-8")) > maximum:
        raise ValueError(f"{field} must be bounded canonical text")
    return result


def auto_generation_status(environ: Mapping[str, str]) -> dict[str, Any]:
    """Resolve the rollbackable persistent-generation switch fail-closed."""
    raw = str(environ.get(AUTO_GENERATION_ENV, "disabled") or "disabled").strip().lower()
    valid = raw in {"disabled", "enabled"}
    mode = raw if valid else "disabled"
    return {
        "mode": mode,
        "enabled": mode == "enabled" and valid,
        "config_valid": valid,
        "environment": AUTO_GENERATION_ENV,
        "rollback": f"set {AUTO_GENERATION_ENV}=disabled or clear it",
    }


def _normalized_relation(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("snapshot relations must be objects")
    strength = value.get("strength")
    confidence = value.get("confidence")
    for field, number in (("strength", strength), ("confidence", confidence)):
        if isinstance(number, bool) or not isinstance(number, (int, float)) \
                or not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f"relation {field} must be between zero and one")
    tags = value.get("tags", [])
    if not isinstance(tags, (list, tuple)) or len(tags) > MAX_TAGS:
        raise ValueError("relation tags must be a bounded sequence")
    normalized_tags = sorted({_text(tag, "relation tag", maximum=128) for tag in tags})
    direction = _text(value.get("direction"), "relation direction", maximum=32)
    if direction not in {"forward", "backward", "bidirectional"}:
        raise ValueError("relation direction is unsupported")
    auto = value.get("auto")
    if not isinstance(auto, bool):
        raise ValueError("relation auto provenance must be boolean")
    return {
        "from": _text(value.get("from"), "relation from"),
        "to": _text(value.get("to"), "relation to"),
        "relation": _text(value.get("relation"), "relation type", allow_empty=True),
        "description": _text(value.get("description"), "relation description",
                             maximum=MAX_DESCRIPTION_BYTES, allow_empty=True),
        "direction": direction,
        "strength": float(strength),
        "confidence": float(confidence),
        "wire": _text(value.get("wire"), "relation wire", maximum=512,
                      allow_empty=True),
        "auto": auto,
        "tags": normalized_tags,
        "updated_at": _text(value.get("updated_at"), "relation updated_at", maximum=128),
    }


@dataclass(frozen=True, slots=True)
class CapabilityOntologySnapshot:
    snapshot_id: str
    content_sha256: str
    relation_count: int
    manual_count: int
    generated_count: int
    relations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_ONTOLOGY_SNAPSHOT_SCHEMA,
            "snapshot_id": self.snapshot_id,
            "content_sha256": self.content_sha256,
            "relation_count": self.relation_count,
            "manual_count": self.manual_count,
            "generated_count": self.generated_count,
            "relations": [json.loads(_canonical(item)) for item in self.relations],
            "restorable": True,
            "executes": False,
        }


def build_capability_ontology_snapshot(
        relations: Sequence[Mapping[str, Any]]) -> CapabilityOntologySnapshot:
    if isinstance(relations, (str, bytes)):
        raise ValueError("relations must be a sequence")
    try:
        values = tuple(relations)
    except TypeError as exc:
        raise ValueError("relations must be a sequence") from exc
    if len(values) > MAX_SNAPSHOT_RELATIONS:
        raise ValueError("relation snapshot limit exceeded")
    normalized = tuple(sorted(
        (_normalized_relation(value) for value in values),
        key=lambda item: (item["from"], item["to"], item["relation"],
                          item["updated_at"])))
    identities = [(item["from"], item["to"]) for item in normalized]
    if len(identities) != len(set(identities)):
        raise ValueError("relation snapshot contains duplicate capability pairs")
    payload = {"schema": CAPABILITY_ONTOLOGY_SNAPSHOT_SCHEMA,
               "relations": normalized}
    encoded = _canonical(payload).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("encoded relation snapshot limit exceeded")
    digest = hashlib.sha256(encoded).hexdigest()
    generated = sum(item["auto"] for item in normalized)
    return CapabilityOntologySnapshot(
        snapshot_id=f"capsont_{digest}", content_sha256=digest,
        relation_count=len(normalized), manual_count=len(normalized) - generated,
        generated_count=generated, relations=normalized)
