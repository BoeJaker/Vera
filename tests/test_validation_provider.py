from __future__ import annotations

import copy

import pytest

from vera.models.evaluation_evidence import EvaluationCaseIdentity
from vera.models.validation_provider import (
    MANIFEST_SCHEMA,
    PROJECTION_SCHEMA,
    import_guardrails_projection,
    import_validator_projection,
)


pytestmark = pytest.mark.critical
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _case(index: int) -> EvaluationCaseIdentity:
    return EvaluationCaseIdentity(
        "dataset-revision-1", f"case-{index}", DIGEST_A,
        "sha256:" + format(index + 1, "064x"))


def _projection(*, complete: bool = True) -> dict:
    cases = [_case(0), _case(1)]
    validators = [{
        "validator_id": "pii", "validator_version": "1",
        "stage": "output", "effects": [],
        "requested_failure_mode": "block", "timeout_ms": 20,
        "model_package_id": "", "egress_scope_digest": "",
        "requires_secret_refs": False,
    }, {
        "validator_id": "grounding", "validator_version": "2",
        "stage": "retrieval", "effects": ["model", "network"],
        "requested_failure_mode": "report", "timeout_ms": 100,
        "model_package_id": "model-grounding-1",
        "egress_scope_digest": DIGEST_B,
        "requires_secret_refs": True,
    }]
    observations = []
    for case in cases:
        observations.extend([{
            "case_id": case.case_id, "validator_id": "pii",
            "status": "passed", "violation_codes": [], "citations": [],
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cost_microunits": 0, "latency_ms": 2},
            "error_code": "",
        }, {
            "case_id": case.case_id, "validator_id": "grounding",
            "status": "violated" if case is cases[1] else "passed",
            "violation_codes": ["unsupported_claim"] if case is cases[1] else [],
            "citations": [{"record_id": "record-1", "revision_id": "revision-1"}],
            "usage": {"input_tokens": 10, "output_tokens": 1,
                      "cost_microunits": 5, "latency_ms": 8},
            "error_code": "",
        }])
    if not complete:
        observations.pop()
    return {
        "schema": PROJECTION_SCHEMA,
        "manifest": {
            "schema": MANIFEST_SCHEMA, "provider_id": "guardrails-frozen",
            "provider_version": "1.0", "source_kind": "guardrails",
            "validators": validators, "policy_authority": False,
        },
        "expected_cases": [case.to_dict() for case in cases],
        "observations": observations,
    }


def test_imports_complete_guardrails_projection_without_effects_or_authority() -> None:
    report = import_guardrails_projection(_projection())
    result = report.to_dict()
    assert report.status == "completed"
    assert report.violated is True
    assert result["policy_authority"] is False
    assert result["effect"] == "none"
    assert result["provider_invoked"] is False
    assert result["payloads_retained"] is False
    assert result["projection_digest"].startswith("sha256:")
    validators = {item["validator_id"]: item for item in result["manifest"]["validators"]}
    assert validators["grounding"]["effects"] == ["model", "network"]


def test_incomplete_cartesian_case_validator_matrix_is_honest_partial() -> None:
    report = import_guardrails_projection(_projection(complete=False))
    assert report.status == "partial"
    assert len(report.observations) == 3


@pytest.mark.parametrize("field", [
    "input", "output", "prompt", "response", "message", "text", "content",
    "reason", "error_detail", "traceback", "secret", "token", "api_key",
])
def test_rejects_payload_bearing_fields_at_any_depth(field: str) -> None:
    projection = _projection()
    projection["observations"][0][field] = "sensitive"
    with pytest.raises(ValueError, match="payload-bearing"):
        import_guardrails_projection(projection)


def test_effect_declarations_fail_closed() -> None:
    missing_model = _projection()
    missing_model["manifest"]["validators"][1]["model_package_id"] = ""
    with pytest.raises(ValueError, match="model effects"):
        import_guardrails_projection(missing_model)

    missing_egress = _projection()
    missing_egress["manifest"]["validators"][1]["egress_scope_digest"] = ""
    with pytest.raises(ValueError, match="network effects"):
        import_guardrails_projection(missing_egress)

    unknown_effect = _projection()
    unknown_effect["manifest"]["validators"][0]["effects"] = ["policy_override"]
    with pytest.raises(ValueError, match="unsupported validator effect"):
        import_guardrails_projection(unknown_effect)

    invalid_secret_flag = _projection()
    invalid_secret_flag["manifest"]["validators"][0]["requires_secret_refs"] = "no"
    with pytest.raises(ValueError, match="must be boolean"):
        import_guardrails_projection(invalid_secret_flag)


def test_external_provider_cannot_claim_policy_authority() -> None:
    projection = _projection()
    projection["manifest"]["policy_authority"] = True
    with pytest.raises(ValueError, match="policy authority"):
        import_guardrails_projection(projection)


def test_rejects_unknown_fields_and_forged_or_duplicate_identity() -> None:
    unknown = _projection()
    unknown["observations"][0]["timestamp"] = "not admitted"
    with pytest.raises(ValueError, match="unsupported fields"):
        import_guardrails_projection(unknown)

    usage_unknown = _projection()
    usage_unknown["observations"][0]["usage"]["provider_cost"] = 1
    with pytest.raises(ValueError, match="observation usage"):
        import_guardrails_projection(usage_unknown)

    forged = _projection()
    forged["expected_cases"][0]["case_key"] = "changed"
    with pytest.raises(ValueError, match="identity"):
        import_guardrails_projection(forged)

    duplicate = _projection()
    duplicate["observations"][1] = copy.deepcopy(duplicate["observations"][0])
    with pytest.raises(ValueError, match="pairs must be unique"):
        import_guardrails_projection(duplicate)


def test_guardrails_wrapper_rejects_other_source_kinds() -> None:
    projection = _projection()
    projection["manifest"]["source_kind"] = "other"
    with pytest.raises(ValueError, match="source_kind guardrails"):
        import_guardrails_projection(projection)
    generic = import_validator_projection(projection)
    assert generic.manifest.source_kind == "other"
