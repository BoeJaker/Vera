import asyncio
import json

import pytest

from vera.execution.durability_fixture import (
    REQUIRED_EVENT_TYPES, REQUIRED_SEMANTICS, DurabilityFixture,
    DurabilityScenario, RuntimeDurabilityProfile, analyze_durability_profile,
    analyze_workflow_adapter_durability, build_durability_fixture,
    durability_profile_from_dict)
from vera.execution.workflow_ir import normalize_workflow


pytestmark = pytest.mark.critical


def conforming_profile(**changes):
    values = dict(
        adapter_id="fixture-runtime/v1", executable=True,
        supported_semantics=REQUIRED_SEMANTICS,
        event_types=REQUIRED_EVENT_TYPES,
        external_effect_delivery="deduplicated_by_key",
        version_change_policy="compatible_only")
    values.update(changes)
    return RuntimeDurabilityProfile(**values)


def test_fixture_is_canonical_vendor_neutral_and_non_executing():
    first = build_durability_fixture()
    second = build_durability_fixture()
    assert first == second
    assert first.fixture_id == second.fixture_id
    assert normalize_workflow(first.workflow) == first.workflow
    payload = json.dumps(first.to_dict(), sort_keys=True).lower()
    assert first.to_dict()["executes"] is False
    for vendor in ("dbos", "temporal", "prefect", "dagster"):
        assert vendor not in payload


def test_fixture_covers_every_step_boundary_and_required_outcome():
    fixture = build_durability_fixture()
    step_ids = [step["id"] for step in fixture.workflow["steps"]]
    assert fixture.crash_boundaries == tuple(
        boundary for step_id in step_ids
        for boundary in (f"before:{step_id}", f"after:{step_id}"))
    crashes = [item for item in fixture.scenarios if item.kind == "crash"]
    assert {item.crash_boundary for item in crashes} == set(fixture.crash_boundaries)
    assert len(crashes) == len(fixture.crash_boundaries)
    assert {item.kind for item in fixture.scenarios} == {
        "clean", "retry", "cancel", "timeout", "version_change", "crash"}
    assert all(item.effect_receipts <= 1 for item in fixture.scenarios)
    assert {item.version_outcome for item in fixture.scenarios
            if item.kind == "version_change"} == {
                "compatible_resume", "reject_mismatch"}


def test_workflow_declares_wait_retry_timeout_and_idempotent_effect_receipt():
    steps = {item["id"]: item for item in build_durability_fixture().workflow["steps"]}
    assert steps["durable_wait"]["extensions"]["vera.semantic"] == "durable_wait"
    assert steps["retryable_step"]["retry"]["owner"] == "runtime"
    assert steps["retryable_step"]["timeout"]["owner"] == "runtime"
    effect = steps["idempotent_effect"]
    assert effect["idempotency"]["owner"] == "runtime"
    assert effect["effects"] == [{
        "kind": "external_service", "target": "fixture://sink",
        "mode": "idempotent-with-receipt"}]
    assert effect["output"] == "receipt"


def test_conforming_profile_passes_without_executing_runtime():
    report = analyze_durability_profile(conforming_profile())
    assert report["ok"]
    assert report["gaps"] == []
    assert report["executes"] is False
    assert report["crash_boundary_count"] == 10
    assert report["scenario_count"] == 16


def test_analysis_fails_closed_for_missing_semantics_events_and_execution():
    report = analyze_durability_profile(RuntimeDurabilityProfile(
        adapter_id="current-native-dag", executable=False,
        supported_semantics=("deterministic_steps",), event_types=("run.started",),
        external_effect_delivery="unsupported", version_change_policy="ignore"))
    assert not report["ok"]
    codes = {gap["code"] for gap in report["gaps"]}
    assert codes == {
        "adapter_not_executable", "missing_semantic", "missing_run_event",
        "effect_delivery_gap", "version_policy_gap"}


def test_exactly_once_external_effect_claim_is_explicitly_rejected():
    report = analyze_durability_profile(conforming_profile(
        external_effect_delivery="exactly_once"))
    assert not report["ok"]
    assert [gap["code"] for gap in report["gaps"]] == ["unsupported_guarantee"]


@pytest.mark.parametrize("adapter_id", ["portable.core", "vera.native_dag", "temporal"])
def test_existing_workflow_adapter_profiles_fail_lib15_without_execution(adapter_id):
    report = analyze_workflow_adapter_durability(adapter_id)
    assert not report["ok"]
    assert report["executes"] is False
    assert "adapter_not_executable" in {gap["code"] for gap in report["gaps"]}


def test_unknown_workflow_adapter_is_an_explicit_nonexecuting_gap():
    report = analyze_workflow_adapter_durability("unregistered")
    assert report == {
        "ok": False, "adapter_id": "unregistered",
        "gaps": [{"code": "unknown_adapter",
                  "detail": "Workflow IR adapter profile is not registered"}],
        "executes": False}


def test_fixture_rejects_missing_or_duplicate_crash_coverage():
    fixture = build_durability_fixture()
    base = dict(
        definition_revision=fixture.definition_revision,
        implementation_revision=fixture.implementation_revision,
        workflow_json=fixture.workflow_json,
        crash_boundaries=fixture.crash_boundaries,
        requirements=fixture.requirements)
    one_crash = next(item for item in fixture.scenarios if item.kind == "crash")
    without_one_crash = tuple(item for item in fixture.scenarios if item is not one_crash)
    with pytest.raises(ValueError, match="cover every declared boundary"):
        DurabilityFixture(scenarios=without_one_crash, **base)
    with pytest.raises(ValueError, match="scenario IDs must be unique"):
        DurabilityFixture(scenarios=fixture.scenarios + (fixture.scenarios[0],), **base)


def test_scenarios_reject_incoherent_crash_and_terminal_metadata():
    with pytest.raises(ValueError, match="require a boundary"):
        DurabilityScenario("crash", "crash", "completed", ("run.started",))
    with pytest.raises(ValueError, match="only crash"):
        DurabilityScenario(
            "clean", "clean", "completed", ("run.completed",),
            crash_boundary="before:prepare", resume_after="before:prepare")
    with pytest.raises(ValueError, match="terminal status"):
        DurabilityScenario("bad", "clean", "running", ("run.started",))


def test_fixture_rejects_incoherent_events_receipts_and_version_outcomes():
    fixture = build_durability_fixture()
    base = dict(
        definition_revision=fixture.definition_revision,
        implementation_revision=fixture.implementation_revision,
        workflow_json=fixture.workflow_json,
        crash_boundaries=fixture.crash_boundaries,
        requirements=fixture.requirements)

    def replace(scenario_id, replacement):
        return tuple(replacement if item.scenario_id == scenario_id else item
                     for item in fixture.scenarios)

    clean = next(item for item in fixture.scenarios
                 if item.scenario_id == "clean-completion")
    bad_terminal = DurabilityScenario(
        clean.scenario_id, clean.kind, clean.terminal_status,
        clean.expected_events[:-1] + ("run.failed",),
        attempts=clean.attempts, effect_receipts=clean.effect_receipts)
    with pytest.raises(ValueError, match="terminal Run event"):
        DurabilityFixture(scenarios=replace(clean.scenario_id, bad_terminal), **base)

    no_receipt = DurabilityScenario(
        clean.scenario_id, clean.kind, clean.terminal_status, clean.expected_events,
        attempts=clean.attempts, effect_receipts=0)
    with pytest.raises(ValueError, match="exactly one effect receipt"):
        DurabilityFixture(scenarios=replace(clean.scenario_id, no_receipt), **base)


def test_profile_rejects_unknown_semantics_and_invalid_policies():
    with pytest.raises(ValueError, match="unknown durability semantics"):
        conforming_profile(supported_semantics=REQUIRED_SEMANTICS + ("magic",))
    with pytest.raises(ValueError, match="delivery claim"):
        conforming_profile(external_effect_delivery="magical-once")
    with pytest.raises(ValueError, match="version change policy"):
        conforming_profile(version_change_policy="always-resume")


def test_profile_reconstruction_is_strict_and_round_trips():
    profile = conforming_profile()
    assert durability_profile_from_dict(profile.to_dict()) == profile
    unknown = {**profile.to_dict(), "secret": "must-not-be-ignored"}
    with pytest.raises(ValueError, match="unknown profile fields"):
        durability_profile_from_dict(unknown)
    missing = profile.to_dict()
    missing.pop("event_types")
    with pytest.raises(ValueError, match="missing profile field: event_types"):
        durability_profile_from_dict(missing)


def test_inspection_capabilities_expose_fixture_and_gaps_without_execution():
    from vera import capability_orchestration as orchestration

    fixture = asyncio.run(
        orchestration.cap_workflow_durability_fixture.__wrapped__())
    assert fixture["executes"] is False
    assert fixture["fixture_id"].startswith("durability_")

    current = asyncio.run(orchestration.cap_workflow_durability_gaps.__wrapped__(
        adapter="vera.native_dag"))
    assert not current["ok"]
    assert current["executes"] is False

    portable = asyncio.run(orchestration.cap_workflow_durability_gaps.__wrapped__(
        profile=conforming_profile().to_dict()))
    assert portable["ok"]
    assert portable["executes"] is False

    ambiguous = asyncio.run(orchestration.cap_workflow_durability_gaps.__wrapped__(
        adapter="vera.native_dag", profile=conforming_profile().to_dict()))
    assert ambiguous == {
        "ok": False, "error": "exactly_one_profile_source_required",
        "executes": False}
