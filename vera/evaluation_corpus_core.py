"""Validation and deterministic scoring for Vera's frozen evaluation corpus."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


CORPUS_SCHEMA = "vera.evaluation-corpus/v1"
REPORT_SCHEMA = "vera.evaluation-corpus-report/v1"
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
