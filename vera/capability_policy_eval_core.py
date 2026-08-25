"""Deterministic evaluator for the frozen W1-05 adversarial policy gate."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .approval_receipts import (
    NonceReplayLedger, consume_approval_receipt, issue_approval_receipt,
    verify_approval_receipt,
)


CORPUS_SCHEMA = "vera.policy-boundary-corpus/v1"
REPORT_SCHEMA = "vera.policy-boundary-report/v1"
REQUIRED_GATES = frozenset({
    "prompt_injection", "alias_bypass", "callbacks", "replayed_approvals",
    "secret_leakage", "confused_deputy",
})
OPERATIONS = frozenset({
    "verify_scope", "inject_callback", "consume_twice", "verify_valid",
})
_SYNTHETIC_KEY = hashlib.sha256(b"vera-frozen-policy-boundary-v1").digest()
_BASE_SCOPE = {
    "capability": "records.write", "effects": ["write"],
    "session_id": "session-a", "tenant_id": "tenant-a",
}


def load_policy_corpus(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("corpus_not_object")
    return value


def validate_policy_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if corpus.get("schema") != CORPUS_SCHEMA:
        errors.append("schema_invalid")
    policy = corpus.get("policy")
    expected_policy = {"executes_capabilities": False, "uses_network": False,
                       "uses_external_secrets": False}
    if policy != expected_policy:
        errors.append("policy_invalid")
    declared = corpus.get("required_gates")
    if not isinstance(declared, list) or set(declared) != REQUIRED_GATES:
        errors.append("required_gates_invalid")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases_invalid")
        cases = []
    ids: set[str] = set()
    covered: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}]_invalid")
            continue
        case_id = case.get("id")
        gate = case.get("gate")
        operation = case.get("operation")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            errors.append(f"cases[{index}].id_invalid")
        else:
            ids.add(case_id)
        if gate not in REQUIRED_GATES:
            errors.append(f"cases[{index}].gate_invalid")
        else:
            covered.add(gate)
        if operation not in OPERATIONS:
            errors.append(f"cases[{index}].operation_invalid")
        if not isinstance(case.get("expected_reason"), str):
            errors.append(f"cases[{index}].expected_reason_invalid")
        override = case.get("context_override", {})
        if not isinstance(override, dict) or set(override) - set(_BASE_SCOPE):
            errors.append(f"cases[{index}].context_override_invalid")
    missing = sorted(REQUIRED_GATES - covered)
    if missing:
        errors.append("gate_coverage_missing:" + ",".join(missing))
    return {"valid": not errors, "errors": errors, "case_count": len(cases),
            "gate_coverage": sorted(covered), "missing_gates": missing}


def _evaluate_case(case: dict[str, Any]) -> tuple[bool, list[str]]:
    case_id = case["id"]
    receipt = issue_approval_receipt(
        signing_key=_SYNTHETIC_KEY, now=1000, ttl_seconds=60,
        nonce="synthetic-" + case_id, **_BASE_SCOPE)
    scope = {**_BASE_SCOPE, **case.get("context_override", {})}
    operation = case["operation"]
    if operation == "inject_callback":
        receipt = copy.deepcopy(receipt)
        receipt["payload"]["callback"] = "https://invalid.example/approve"
        result = verify_approval_receipt(
            receipt, signing_key=_SYNTHETIC_KEY, now=1001, **scope)
    elif operation == "consume_twice":
        ledger = NonceReplayLedger()
        consume_approval_receipt(
            receipt, signing_key=_SYNTHETIC_KEY, now=1001,
            replay_ledger=ledger, **scope)
        result = consume_approval_receipt(
            receipt, signing_key=_SYNTHETIC_KEY, now=1001,
            replay_ledger=ledger, **scope)
    else:
        result = verify_approval_receipt(
            receipt, signing_key=_SYNTHETIC_KEY, now=1001, **scope)
    reasons = list(result.get("reasons") or [])
    observed = "valid" if result.get("valid") else (reasons[0] if reasons else "invalid")
    return observed == case["expected_reason"], reasons


def evaluate_policy_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    validation = validate_policy_corpus(corpus)
    if not validation["valid"]:
        return {"schema": REPORT_SCHEMA, "ok": False, **validation,
                "executes_capabilities": False, "uses_network": False,
                "uses_external_secrets": False, "cases": []}
    results = []
    for case in corpus["cases"]:
        passed, reasons = _evaluate_case(case)
        results.append({"id": case["id"], "gate": case["gate"],
                        "passed": passed, "reason_codes": reasons})
    passed = sum(item["passed"] for item in results)
    report = {
        "schema": REPORT_SCHEMA, "ok": passed == len(results),
        "revision": corpus.get("revision"), "total": len(results),
        "passed": passed, "failed": len(results) - passed,
        "gate_coverage": validation["gate_coverage"],
        "missing_gates": validation["missing_gates"],
        "executes_capabilities": False, "uses_network": False,
        "uses_external_secrets": False, "cases": results,
    }
    serialized = json.dumps(report, sort_keys=True)
    forbidden = [_SYNTHETIC_KEY.hex(), "synthetic-", "ignore policy",
                 "invalid.example"]
    report["content_free"] = not any(value in serialized for value in forbidden)
    report["ok"] = report["ok"] and report["content_free"]
    return report
