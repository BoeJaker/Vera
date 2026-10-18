"""Payload-free joined evidence for an already-made Four Minds decision.

The native Decision/Outcome ledger remains authoritative.  This pure adapter
validates those frozen rows, projects route/model/timing/context identities,
and reports absent links without invoking a capability, model, node, store, or
policy.  It is an evidence boundary, not a new decision or activation path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from . import ledger_core as LC


SCHEMA = "vera.four-minds-decision-evidence/v1"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,191}$")
_ROUTE_KINDS = {"native", "nlp_knn", "nlp_model", "onnx", "llm", "human"}
_ROUTE_FIELDS = (
    "node_id", "index_id", "index_version", "provider_id", "deployment_id",
    "model_package_id", "model_revision", "route_revision",
)
_REQUIRED_ROUTE = {
    "native": (),
    "human": (),
    "nlp_knn": ("node_id", "index_id", "index_version"),
    "nlp_model": ("node_id", "provider_id", "deployment_id", "model_package_id"),
    "onnx": ("deployment_id", "model_package_id"),
    "llm": ("provider_id", "deployment_id", "model_package_id"),
}
_TIMING_FIELDS = (
    "sense_ms", "dispatch_ms", "compute_ms", "decision_ms", "total_ms",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _token(value: Any, label: str, *, required: bool = False) -> str:
    value = str(value or "").strip()
    if required and not value:
        raise ValueError(f"{label} is required")
    if value and not _TOKEN.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _milliseconds(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative finite number")
    try:
        number = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative finite number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return round(number, 3)


def _route_projection(route: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    route = route or {}
    if not isinstance(route, Mapping):
        raise ValueError("route evidence must be an object")
    kind = str(route.get("kind") or "native").strip()
    if kind not in _ROUTE_KINDS:
        raise ValueError("unsupported route kind")
    projected: dict[str, Any] = {"kind": kind}
    for field in _ROUTE_FIELDS:
        projected[field] = _token(route.get(field), field)
    missing = [f"route.{field}" for field in _REQUIRED_ROUTE[kind]
               if not projected[field]]
    projected["route_id"] = _identity("fmroute_", projected)
    return projected, missing


def _timing_projection(decision: Mapping[str, Any], route: Mapping[str, Any] | None,
                       timing: Mapping[str, Any] | None) -> dict[str, float]:
    timing = timing or {}
    route = route or {}
    if not isinstance(timing, Mapping):
        raise ValueError("timing evidence must be an object")
    unknown = sorted(set(timing) - set(_TIMING_FIELDS))
    if unknown:
        raise ValueError(f"unsupported timing fields: {unknown}")
    values = {field: _milliseconds(timing.get(field), field)
              for field in _TIMING_FIELDS}
    if not values["decision_ms"]:
        values["decision_ms"] = _milliseconds(decision.get("ms"), "decision.ms")
    if not values["dispatch_ms"]:
        values["dispatch_ms"] = _milliseconds(
            route.get("round_trip_ms"), "route.round_trip_ms")
    if not values["compute_ms"]:
        values["compute_ms"] = _milliseconds(route.get("ms"), "route.ms")
    return values


def _route_matches_decider(decision: Mapping[str, Any], route_kind: str) -> bool:
    decider = str(decision.get("decider") or "")
    implementation = str(decision.get("implementation") or "").lower()
    if decider == "rule":
        return route_kind == "native"
    if decider == "human":
        return route_kind == "human"
    if decider == "llm":
        return route_kind == "llm"
    if decider != "student":
        return False
    if implementation.startswith("knn:"):
        return route_kind == "nlp_knn"
    if implementation.startswith("onnx:"):
        return route_kind == "onnx"
    if implementation.startswith(("card:", "zeroshot:", "nli:", "rerank:")):
        return route_kind == "nlp_model"
    if implementation.startswith("llm:"):
        return route_kind == "llm"
    return route_kind in {"nlp_knn", "nlp_model", "onnx", "llm"}


def _outcome_projection(outcome: Mapping[str, Any]) -> dict[str, Any]:
    problems = LC.check_outcome(dict(outcome))
    if problems:
        raise ValueError("invalid outcome row: " + "; ".join(problems))
    return {
        "decision_id": str(outcome["decision_id"]),
        "timestamp": str(outcome["ts"]),
        "source": _token(outcome["source"], "outcome source", required=True),
        "strength": str(outcome["strength"]),
        "label_digest": _digest(outcome.get("label")),
        "detail_digest": _digest(outcome.get("detail") or {}),
    }


def project_decision_evidence(
        decision: Mapping[str, Any], *,
        outcomes: Sequence[Mapping[str, Any]] = (),
        route: Mapping[str, Any] | None = None,
        timing: Mapping[str, Any] | None = None,
        context_evidence_ids: Sequence[str] = (),
        require_context: bool = False,
        require_model_package: bool = False) -> dict[str, Any]:
    """Join supplied identities without reading or changing their owners.

    ``eligible_for_evaluation`` means the requested evidence links are present;
    it never means that a student/model is safe, enabled, or authoritative.
    """
    if not isinstance(decision, Mapping):
        raise ValueError("decision evidence must be an object")
    if isinstance(outcomes, (str, bytes, Mapping)):
        raise ValueError("outcomes must be a sequence of outcome objects")
    if isinstance(context_evidence_ids, (str, bytes)):
        raise ValueError("context evidence IDs must be a sequence")
    row = dict(decision)
    problems = LC.check_decision(row)
    if problems:
        raise ValueError("invalid decision row: " + "; ".join(problems))
    route_projection, missing = _route_projection(route)
    timing_projection = _timing_projection(row, route, timing)
    if not _route_matches_decider(row, route_projection["kind"]):
        missing.append("route.kind_for_decider")
    decision_node = _token(row.get("node"), "decision node")
    if (decision_node and route_projection["node_id"] and
            decision_node != route_projection["node_id"]):
        raise ValueError("route node does not match decision node")

    context_ids = tuple(sorted(_token(value, "context evidence ID", required=True)
                               for value in context_evidence_ids))
    if len(set(context_ids)) != len(context_ids):
        raise ValueError("context evidence IDs must be unique")
    if require_context and not context_ids:
        missing.append("context_evidence_ids")
    if require_model_package and not route_projection["model_package_id"]:
        missing.append("route.model_package_id")

    projected_outcomes = tuple(sorted(
        (_outcome_projection(item) for item in outcomes),
        key=lambda item: (item["timestamp"], item["source"], item["label_digest"])))
    outcome_identities = tuple(_canonical(item) for item in projected_outcomes)
    if len(set(outcome_identities)) != len(outcome_identities):
        raise ValueError("outcome evidence must be unique")
    if any(item["decision_id"] != row["decision_id"] for item in projected_outcomes):
        raise ValueError("outcome decision ID does not match decision")
    strongest = LC.strongest([dict(item) for item in outcomes]) if outcomes else None
    strongest_projection = (_outcome_projection(strongest) if strongest else None)
    if strongest_projection is None:
        missing.append("outcome")

    decision_projection = {
        "decision_id": str(row["decision_id"]),
        "timestamp": str(row["ts"]),
        "origin": str(row["origin"]),
        "surface": _token(row["surface"], "surface", required=True),
        "question_id": str(row["question_id"]),
        "qtype": str(row["qtype"]),
        "labels_hash": str(row["labels_hash"]),
        "state_hash": str(row["state_hash"]),
        "state_ref_digest": _digest(row.get("state_ref") or {}),
        "answer_digest": _digest(row.get("answer")),
        "decider": str(row["decider"]),
        "implementation": _token(row.get("implementation"), "implementation"),
        "package": _token(row.get("package"), "decision package"),
        "node": decision_node,
        "mode": str(row["mode"]),
        "minds_digest": _token(row.get("minds_digest") or "default", "minds digest",
                               required=True),
        "run_id": _token(row.get("run_id"), "run ID"),
        "session_id": _token(row.get("session_id"), "session ID"),
        "turn_id": _token(row.get("turn_id"), "turn ID"),
        "detail_digest": _digest(row.get("detail") or {}),
    }
    missing = sorted(set(missing))
    identity_payload = {
        "schema": SCHEMA,
        "decision": decision_projection,
        "route": route_projection,
        "timing_ms": timing_projection,
        "context_evidence_ids": list(context_ids),
        "outcomes": list(projected_outcomes),
        "strongest_outcome": strongest_projection,
        "requirements": {
            "context": bool(require_context),
            "model_package": bool(require_model_package),
        },
        "missing": missing,
    }
    return {
        "evidence_id": _identity("fmde_", identity_payload),
        **identity_payload,
        "eligible_for_evaluation": not missing,
        "payload_retained": False,
        "executes": False,
        "writes": False,
        "changes_routing": False,
        "changes_authority": False,
        "activation_authority": False,
    }
