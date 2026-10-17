"""Bounded inventory of Intelligence effect boundaries; never executes an effect."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA = "vera.intel-effect-inventory/v1"

CHANNEL_POLICY = {
    "html": ("local_read", "not_external", ""),
    "telegram": ("non_idempotent_write", "downstream_shadow", "telegram"),
    "chat": ("local_write", "native_log_only", ""),
    "email": ("non_idempotent_write", "downstream_shadow", "email"),
    "canvas": ("local_write", "native_log_only", ""),
    "notebook": ("local_write", "native_log_only", ""),
    "memory": ("local_write", "native_receipt", ""),
    "fabric": ("local_distributed_write", "native_log_only", ""),
    "phone": ("non_idempotent_write", "downstream_shadow", "homeassistant"),
    "podcast": ("deferred_local_write", "native_log_only", ""),
    "print": ("non_idempotent_write", "downstream_shadow", "printing"),
}

ACTION_POLICY = {
    "research": ("deferred_local_write", "native_log_only"),
    "discover": ("local_distributed_write", "native_log_only"),
    "repo": ("external_read_and_local_write", "downstream_shadow"),
    "read": ("external_read", "native_log_only"),
    "track": ("local_write", "native_log_only"),
    "sweep": ("external_read_and_local_write", "native_log_only"),
    "watch": ("local_write", "native_log_only"),
}

ACTION_EVIDENCE_FAMILY = {
    "repo": "repository",
}

EXTRA_BOUNDARIES = (
    {"id": "alert.telegram", "kind": "alert", "capability": "tg.notify",
     "classification": "non_idempotent_write", "observation": "downstream_shadow",
     "evidence_family": "telegram"},
    {"id": "alert.phone", "kind": "alert", "capability": "ha.notify",
     "classification": "non_idempotent_write", "observation": "downstream_shadow",
     "evidence_family": "homeassistant"},
    {"id": "narration.telegram", "kind": "delivery", "capability": "tg.send_audio",
     "classification": "non_idempotent_write", "observation": "downstream_shadow",
     "evidence_family": "telegram"},
    {"id": "repo.acquire", "kind": "action", "capability": "intel.repo.get",
     "classification": "external_read_and_local_write", "observation": "downstream_shadow",
     "evidence_family": "repository"},
    {"id": "n8n.proposal.create", "kind": "automation", "capability": "n8n.workflow.create",
     "classification": "non_idempotent_write", "observation": "downstream_shadow",
     "evidence_family": "n8n"},
    {"id": "n8n.retirement.activate", "kind": "automation", "capability": "n8n.workflow.activate",
     "classification": "idempotent_write", "observation": "downstream_shadow",
     "evidence_family": "n8n"},
    {"id": "fabric.source.mutate", "kind": "source", "capability": "fabric.sources.add|delete|pull",
     "classification": "external_read_and_local_write", "observation": "native_log_only"},
)


def _row(*, boundary_id: str, kind: str, capability: str, classification: str,
         observation: str, evidence_family: str = "", available: bool = False) -> dict[str, Any]:
    external = classification in {"external_read", "external_read_and_local_write",
                                  "idempotent_write", "non_idempotent_write"}
    gaps = []
    if external and observation == "unobserved":
        gaps.append("portable_effect_observation_missing")
    if observation == "downstream_shadow":
        gaps.append("intel_attribution_missing")
    return {"id": boundary_id, "kind": kind, "capability": capability,
            "classification": classification, "external": external,
            "observation": observation, "evidence_family": evidence_family,
            "available": bool(available), "enforcement": "native_unchanged", "gaps": gaps}


def project(channels: Mapping[str, Mapping[str, Any]], actions: Mapping[str, Mapping[str, Any]],
            *, available_caps: Sequence[str] = (),
            attributed_evidence: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Describe current Intel boundaries and coverage without probing providers."""
    available = set(available_caps)
    rows = []
    for name, spec in sorted(channels.items()):
        classification, observation, family = CHANNEL_POLICY.get(
            name, ("unknown", "unclassified", ""))
        cap = str(spec.get("cap") or "")
        rows.append(_row(boundary_id=f"channel.{name}", kind="channel", capability=cap,
                         classification=classification, observation=observation,
                         evidence_family=family, available=not cap or cap in available))
        if name not in CHANNEL_POLICY:
            rows[-1]["gaps"].append("classification_missing")
    for name, spec in sorted(actions.items()):
        classification, observation = ACTION_POLICY.get(name, ("unknown", "unclassified"))
        cap = str(spec.get("cap") or "")
        rows.append(_row(boundary_id=f"action.{name}", kind="action", capability=cap,
                         classification=classification, observation=observation,
                         evidence_family=ACTION_EVIDENCE_FAMILY.get(name, ""),
                         available=cap in available))
        if name not in ACTION_POLICY:
            rows[-1]["gaps"].append("classification_missing")
    for spec in EXTRA_BOUNDARIES:
        caps = str(spec["capability"]).split("|")
        rows.append(_row(boundary_id=spec["id"], kind=spec["kind"],
                         capability=spec["capability"], classification=spec["classification"],
                         observation=spec["observation"],
                         evidence_family=spec.get("evidence_family", ""),
                         available=any(cap in available for cap in caps)))
    evidence = {str(family): dict(summary) for family, summary in
                (attributed_evidence or {}).items()}
    available_evidence = {family: summary for family, summary in evidence.items()
                          if not summary.get("error")}
    for row in rows:
        family = row.get("evidence_family") or ""
        if row.get("observation") == "downstream_shadow" and family in available_evidence:
            row["gaps"] = [gap for gap in row["gaps"] if gap != "intel_attribution_missing"]
            if not int((available_evidence[family].get("totals") or {}).get("observations") or 0):
                row["gaps"].append("provider_family_has_no_intel_observations")
    gaps = sorted({gap for row in rows for gap in row["gaps"]})
    attributed = sum(int((summary.get("totals") or {}).get("observations") or 0)
                     for summary in available_evidence.values())
    return {"schema": SCHEMA, "boundaries": rows,
            "counts": {"total": len(rows), "external": sum(row["external"] for row in rows),
                       "downstream_shadow": sum(row["observation"] == "downstream_shadow" for row in rows),
                       "unobserved": sum(row["observation"] == "unobserved" for row in rows)},
            "gaps": gaps,
            "evidence": evidence,
            "readiness": {"intel_attributable": bool(available_evidence),
                          "attributed_observations": attributed,
                          "reason": ("source_filtered_provider_evidence_available" if available_evidence else
                                     "provider_family_evidence_has_no_intel_source_dimension"),
                          "enforcement_enabled": False},
            "claims": {"executes": False, "probes": False, "records_evidence": False,
                       "changes_enforcement": False, "retains_payload": False}}
