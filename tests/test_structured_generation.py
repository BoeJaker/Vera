import asyncio
import copy
import sys

import pytest

from vera.providers.structured_generation import (
    STRUCTURED_GENERATION_SCHEMA,
    analyze_schema,
    plan_structured_generation,
    plan_structured_retry,
    structured_generation_status,
    validate_structured_value,
)


pytestmark = pytest.mark.critical


def _schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "summary", "tags"],
        "properties": {
            "summary": {"type": "string", "minLength": 3, "maxLength": 80},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "tags": {"type": "array", "minItems": 1, "uniqueItems": True,
                     "items": {"type": "string"}},
        },
    }


def test_schema_normalization_and_plan_identity_are_stable_without_optional_imports():
    before = {name for name in sys.modules
              if name == "instructor" or name.startswith("instructor.")
              or name == "outlines" or name.startswith("outlines.")}
    first_analysis, first_plan = plan_structured_generation(
        _schema(), provider_profile="provider_native", retry_budget=2,
        semantic_validator_ref="validatorref:quality/summary", latency_budget_ms=5000)
    reordered = {key: copy.deepcopy(_schema()[key]) for key in reversed(_schema())}
    second_analysis, second_plan = plan_structured_generation(
        reordered, provider_profile="provider_native", retry_budget=2,
        semantic_validator_ref="validatorref:quality/summary", latency_budget_ms=5000)
    assert first_analysis.to_dict() == second_analysis.to_dict()
    assert first_plan.to_dict() == second_plan.to_dict()
    result = first_plan.to_dict()
    assert result["accepted"] is True
    assert result["retry"] == {"budget": 2, "owner": "vera", "attempts_started": 0}
    assert result["ready_for_generation"] is False
    assert all(result[key] is False for key in (
        "imports_optional_provider", "model_called", "tokens_decoded",
        "stream_started", "executes"))
    after = {name for name in sys.modules
             if name == "instructor" or name.startswith("instructor.")
             or name == "outlines" or name.startswith("outlines.")}
    assert after == before == set()


@pytest.mark.parametrize("profile,owner,mode", [
    ("provider_native", "vera", "provider_schema"),
    ("instructor", "instructor", "typed_validation_and_correction"),
    ("outlines", "vera", "constrained_decoding"),
])
def test_static_provider_profiles_assign_exactly_one_retry_owner(profile, owner, mode):
    _, plan = plan_structured_generation(
        _schema(), provider_profile=profile, retry_budget=1, retry_owner=owner)
    result = plan.to_dict()
    assert result["retry"]["owner"] == owner
    assert result["profile"]["generation_mode"] == mode
    assert result["profile"]["availability"] in {"runtime_dependent", "not_imported"}
    wrong = "instructor" if owner == "vera" else "vera"
    with pytest.raises(ValueError, match="requires retry_owner"):
        plan_structured_generation(
            _schema(), provider_profile=profile, retry_budget=1, retry_owner=wrong)


def test_unsupported_or_malformed_schema_constructs_are_explicit():
    cases = (
        ({"$ref": "https://example/schema"}, "reference_unsupported"),
        ({"type": "object", "patternProperties": {}}, "keyword_unsupported"),
        ({"type": "object", "required": ["missing"], "properties": {}},
         "required_unknown"),
        ({"type": "string", "minLength": 5, "maxLength": 2},
         "string_bounds_invalid"),
        ({"oneOf": []}, "combinator_invalid"),
        ({"type": ["string", "string"]}, "type_invalid"),
    )
    for schema, code in cases:
        analysis = analyze_schema(schema)
        assert not analysis.accepted
        assert code in {issue["code"] for issue in analysis.issues}
        _, plan = plan_structured_generation(schema, provider_profile="outlines")
        assert plan.to_dict()["accepted"] is False


def test_streaming_and_semantic_validator_requirements_fail_closed():
    _, streaming = plan_structured_generation(
        _schema(), provider_profile="provider_native", streaming=True)
    assert streaming.to_dict()["accepted"] is False
    assert streaming.to_dict()["issues"] == ["streaming_unverified"]
    with pytest.raises(ValueError, match="validatorref"):
        plan_structured_generation(
            _schema(), provider_profile="instructor",
            semantic_validator_ref="run arbitrary callback")
    with pytest.raises(ValueError, match="retry_budget"):
        plan_structured_generation(
            _schema(), provider_profile="instructor", retry_budget=6)


def test_deterministic_validation_covers_shape_bounds_uniqueness_and_semantics():
    valid = validate_structured_value(
        _schema(), {"summary": "useful", "score": 0.8, "tags": ["a", "b"]},
        semantic_status="passed")
    assert valid["valid"] is True
    assert valid["violations"] == []
    assert valid["value_returned"] is False
    assert valid["validator_executed"] is False
    invalid = validate_structured_value(
        _schema(), {"summary": "x", "score": 2, "tags": ["a", "a"],
                    "extra": "not returned"}, semantic_status="failed")
    assert invalid["valid"] is False
    assert invalid["semantic_valid"] is False
    assert {(row["path"], row["code"]) for row in invalid["violations"]} == {
        ("$.summary", "min_length"), ("$.score", "maximum"),
        ("$.tags", "unique_items"), ("$.extra", "additional_property")}
    assert "not returned" not in str(invalid)


def test_combinators_and_json_identity_do_not_confuse_booleans_with_integers():
    one = {"oneOf": [{"const": True}, {"const": 1}]}
    assert validate_structured_value(one, True)["valid"] is True
    assert validate_structured_value(one, 1)["valid"] is True
    assert validate_structured_value(one, "1")["valid"] is False
    schema = {"allOf": [{"type": "number", "minimum": 0},
                         {"type": "number", "maximum": 5}],
              "not": {"const": 3}}
    assert validate_structured_value(schema, 2)["valid"] is True
    assert validate_structured_value(schema, 3)["valid"] is False
    assert validate_structured_value(schema, 8)["valid"] is False


def test_validation_values_are_bounded_and_rejected_schema_plans_do_not_collide():
    with pytest.raises(ValueError, match="exceeds 100 items"):
        validate_structured_value({"type": "array"}, list(range(101)))
    _, ref_plan = plan_structured_generation(
        {"$ref": "https://example/a"}, provider_profile="provider_native")
    _, keyword_plan = plan_structured_generation(
        {"pattern": ".*"}, provider_profile="provider_native")
    assert ref_plan.plan_id != keyword_plan.plan_id


def test_retry_plan_only_allows_schema_or_semantic_failures_inside_budget():
    _, planned = plan_structured_generation(
        _schema(), provider_profile="instructor", retry_budget=2)
    plan = planned.to_dict()
    first = plan_structured_retry(plan, failure_kind="semantic", completed_attempts=0)
    last = plan_structured_retry(plan, failure_kind="schema", completed_attempts=2)
    timeout = plan_structured_retry(plan, failure_kind="timeout", completed_attempts=0)
    cancelled = plan_structured_retry(plan, failure_kind="cancelled", completed_attempts=0)
    assert first["allowed"] is True and first["retry_owner"] == "instructor"
    assert first["attempt_started"] is False
    assert last["allowed"] is False and last["reason"] == "budget_exhausted"
    assert timeout["allowed"] is False and timeout["reason"] == "cancel_or_timeout_terminal"
    assert cancelled["allowed"] is False
    tampered = copy.deepcopy(plan)
    tampered["retry"]["budget"] = 5
    with pytest.raises(ValueError, match="identity does not verify"):
        plan_structured_retry(tampered, failure_kind="schema", completed_attempts=0)


def test_status_capabilities_and_provider_ui_are_inspection_only():
    status = structured_generation_status()
    assert status["contract"] == "implemented"
    assert status["deterministic_validation"] == "implemented"
    assert status["provider_execution"] == "queued_live"
    assert status["model_execution"] == "queued_live"
    assert [row["id"] for row in status["provider_profiles"]] == [
        "instructor", "outlines", "provider_native"]
    from vera.providers import providers_capabilities as caps
    wrapped = asyncio.run(caps.cap_structured_status.__wrapped__())
    planned = asyncio.run(caps.cap_structured_plan.__wrapped__(
        schema=_schema(), provider_profile="outlines", retry_budget=1))
    checked = asyncio.run(caps.cap_structured_validate.__wrapped__(
        schema=_schema(), value={"summary": "good", "score": 1,
                                 "tags": ["one"]}))
    assert wrapped == status
    assert planned["plan"]["schema"] == STRUCTURED_GENERATION_SCHEMA
    assert planned["model_called"] is False
    assert checked["valid"] is True and checked["value_returned"] is False
    panel = caps._PANEL_PATH.read_text(encoding="utf-8")
    assert "/providers/structured/status" in panel
    assert "Structured generation · LIB04" in panel
    assert "inspection only" in panel
