"""Validation and deterministic scoring for Vera's frozen evaluation corpus."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


CORPUS_SCHEMA = "vera.evaluation-corpus/v1"
REPORT_SCHEMA = "vera.evaluation-corpus-report/v1"
RESOLVER_CORPUS_SCHEMA = "vera.resolver-shadow-corpus/v1"
RESOLVER_REPORT_SCHEMA = "vera.resolver-shadow-report/v1"
LANES = {"deterministic", "queued_live"}


def canonical_fingerprint(corpus: Mapping[str, Any]) -> str:
    payload = json.loads(json.dumps(corpus))
    payload.pop("fingerprint", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_corpus(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("corpus_root_must_be_object")
    return value


def validate_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    issues = []
    if corpus.get("schema") != CORPUS_SCHEMA:
        issues.append({"code": "schema.invalid", "path": "schema"})
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        cases = []
        issues.append({"code": "cases.invalid", "path": "cases"})
    seen = set()
    lanes = {lane: 0 for lane in sorted(LANES)}
    domains: dict[str, int] = {}
    for index, case in enumerate(cases):
        path = f"cases.{index}"
        if not isinstance(case, Mapping):
            issues.append({"code": "case.invalid", "path": path})
            continue
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            issues.append({"code": "case.id_required", "path": f"{path}.id"})
        elif case_id in seen:
            issues.append({"code": "case.id_duplicate", "path": f"{path}.id"})
        seen.add(case_id)
        lane = case.get("lane")
        if lane not in LANES:
            issues.append({"code": "case.lane_invalid", "path": f"{path}.lane"})
        else:
            lanes[lane] += 1
        domain = str(case.get("domain") or "").strip()
        if not domain:
            issues.append({"code": "case.domain_required", "path": f"{path}.domain"})
        else:
            domains[domain] = domains.get(domain, 0) + 1
        expected = case.get("expected")
        if not isinstance(expected, Mapping) or not expected:
            issues.append({"code": "case.expected_required", "path": f"{path}.expected"})
        budget = case.get("budget")
        if lane == "queued_live" and not isinstance(budget, Mapping):
            issues.append({"code": "case.live_budget_required", "path": f"{path}.budget"})
    return {"schema": REPORT_SCHEMA, "ok": not issues, "issues": issues,
            "case_count": len(cases), "lanes": lanes,
            "domains": dict(sorted(domains.items())),
            "fingerprint": canonical_fingerprint(corpus)}


def _lookup(value: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def score_case(case: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    """Score declared dotted-path expectations without model or network calls."""
    expected = case.get("expected") if isinstance(case.get("expected"), Mapping) else {}
    checks = []
    for path, wanted in sorted(expected.items()):
        present, actual = _lookup(observation, str(path))
        checks.append({"path": str(path), "passed": present and actual == wanted,
                       "expected": wanted, "actual": actual if present else None,
                       "present": present})
    passed = sum(check["passed"] for check in checks)
    return {"case_id": str(case.get("id") or ""), "passed": passed,
            "total": len(checks), "score": round(passed / len(checks), 4) if checks else 0.0,
            "ok": bool(checks) and passed == len(checks), "checks": checks}


def validate_resolver_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    """Validate frozen synthetic resolver fixtures without loading capabilities."""
    issues = []
    if corpus.get("schema") != RESOLVER_CORPUS_SCHEMA:
        issues.append({"code": "schema.invalid", "path": "schema"})
    policy = corpus.get("policy")
    required_policy = {"synthetic_only": True, "model_calls": False,
                       "network_calls": False, "capability_execution": False}
    if not isinstance(policy, Mapping) or any(
            policy.get(key) is not value for key, value in required_policy.items()):
        issues.append({"code": "policy.nonexecuting_required", "path": "policy"})
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        cases = []
        issues.append({"code": "cases.invalid", "path": "cases"})
    seen = set()
    for index, case in enumerate(cases):
        path = f"cases.{index}"
        if not isinstance(case, Mapping):
            issues.append({"code": "case.invalid", "path": path})
            continue
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            issues.append({"code": "case.id_required", "path": f"{path}.id"})
        elif case_id in seen:
            issues.append({"code": "case.id_duplicate", "path": f"{path}.id"})
        seen.add(case_id)
        registry = case.get("registry")
        if not isinstance(registry, Mapping) or not registry:
            issues.append({"code": "case.registry_required", "path": f"{path}.registry"})
            registry = {}
        if not isinstance(case.get("request"), Mapping):
            issues.append({"code": "case.request_required", "path": f"{path}.request"})
        observations = case.get("observations", [])
        if not isinstance(observations, list):
            issues.append({"code": "case.observations_invalid",
                           "path": f"{path}.observations"})
            observations = []
        observation_names = []
        for observation in observations:
            name = str(observation.get("name") or "").strip() if isinstance(
                observation, Mapping) else ""
            observation_names.append(name)
        if any(not name or name not in registry for name in observation_names):
            issues.append({"code": "case.observation_name_unknown",
                           "path": f"{path}.observations"})
        if len(observation_names) != len(set(observation_names)):
            issues.append({"code": "case.observation_name_duplicate",
                           "path": f"{path}.observations"})
        unsafe_names = case.get("unsafe_names", [])
        if not isinstance(unsafe_names, list) or any(
                not isinstance(name, str) or not name.strip() for name in unsafe_names):
            issues.append({"code": "case.unsafe_names_invalid",
                           "path": f"{path}.unsafe_names"})
        elif any(name not in registry for name in unsafe_names):
            issues.append({"code": "case.unsafe_name_unknown",
                           "path": f"{path}.unsafe_names"})
        expected = case.get("expected")
        if not isinstance(expected, Mapping) or "selected" not in expected:
            issues.append({"code": "case.expected_selected_required",
                           "path": f"{path}.expected.selected"})
        elif (expected.get("selected") is not None and
              (not isinstance(expected.get("selected"), str)
               or expected.get("selected") not in registry)):
            issues.append({"code": "case.expected_selected_unknown",
                           "path": f"{path}.expected.selected"})
    return {"schema": RESOLVER_REPORT_SCHEMA, "ok": not issues, "issues": issues,
            "case_count": len(cases), "fingerprint": canonical_fingerprint(corpus)}


def evaluate_resolver_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    """Run frozen resolver previews and report exact selection and safety rates."""
    validation = validate_resolver_corpus(corpus)
    if not validation["ok"]:
        return {**validation, "selection_accuracy": 0.0,
                "unsafe_choice_rate": 0.0, "cases": []}

    from .capability_contract_core import project_registry
    from .capability_resolver_core import resolve_shadow

    results = []
    unsafe_count = 0
    exact_count = 0
    for case in corpus["cases"]:
        manifests = project_registry(case["registry"])
        resolution = resolve_shadow(
            manifests, case["request"], observations=case.get("observations") or [])
        selected = resolution.get("selected")
        unsafe = selected in set(case.get("unsafe_names") or [])
        expected_selected = case["expected"].get("selected")
        exact = selected == expected_selected
        invariant = resolution.get("authorized") is False and resolution.get("executed") is False
        unsafe_count += int(unsafe)
        exact_count += int(exact)
        results.append({"id": case["id"], "ok": exact and not unsafe and invariant,
                        "selected": selected, "expected_selected": expected_selected,
                        "unsafe_selected": unsafe, "authorized": resolution.get("authorized"),
                        "executed": resolution.get("executed"),
                        "excluded": [row.get("name") for row in resolution.get("excluded", [])]})
    total = len(results)
    return {**validation, "ok": all(case["ok"] for case in results),
            "selection_accuracy": round(exact_count / total, 4) if total else 0.0,
            "unsafe_choice_rate": round(unsafe_count / total, 4) if total else 0.0,
            "cases": results}
