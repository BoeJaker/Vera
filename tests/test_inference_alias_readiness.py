import json
import sqlite3

import pytest

from vera.models import (
    InferenceAliasParityEvidence, REQUIRED_ALIAS_PARITY_SCENARIOS,
    SQLiteInferenceAliasParityStore, evaluate_inference_alias_readiness,
    inference_alias_parity_evidence_from_dict)
from tests.test_inference_alias_route import evidence

pytestmark = pytest.mark.critical

COMMIT = "a" * 40


def observation(route, scenario, *, observed=100, passed=True, commit=COMMIT):
    return InferenceAliasParityEvidence(
        route.route_id, scenario, commit, observed, passed,
        (f"proof_{scenario}",), () if passed else ("parity_mismatch",))


def populated(tmp_path, *, observed=100):
    route, *_ = evidence(tmp_path)
    store = SQLiteInferenceAliasParityStore(tmp_path / "parity.sqlite")
    for scenario in REQUIRED_ALIAS_PARITY_SCENARIOS:
        store.record(observation(route, scenario, observed=observed))
    return route, store


def test_evidence_is_content_addressed_payload_free_and_round_trips(tmp_path):
    route, *_ = evidence(tmp_path)
    item = observation(route, "stream")
    encoded = item.to_dict()
    assert inference_alias_parity_evidence_from_dict(encoded) == item
    assert set(encoded) == {
        "schema", "evidence_id", "route_id", "scenario", "source_commit",
        "observed_at_ms", "passed", "evidence_refs", "failure_codes"}
    assert "output" not in json.dumps(encoded).lower()


def test_store_is_durable_and_exact_replay_is_idempotent(tmp_path):
    route, *_ = evidence(tmp_path)
    path = tmp_path / "parity.sqlite"
    item = observation(route, "nonstream")
    store = SQLiteInferenceAliasParityStore(path)
    assert store.record(item) == item
    assert SQLiteInferenceAliasParityStore(path).record(item) == item
    assert SQLiteInferenceAliasParityStore(path).latest(
        route.route_id, COMMIT) == (item,)


def test_readiness_requires_all_current_passing_scenarios(tmp_path):
    route, store = populated(tmp_path)
    result = evaluate_inference_alias_readiness(
        route, store, source_commit=COMMIT, as_of_ms=150, max_age_ms=100)
    assert result.ready_for_activation_review is True
    assert result.reasons == ()
    assert len(result.evidence_ids) == len(REQUIRED_ALIAS_PARITY_SCENARIOS)
    assert result.traffic_activation_allowed is False


def test_missing_or_foreign_commit_evidence_fails_closed(tmp_path):
    route, store = populated(tmp_path)
    result = evaluate_inference_alias_readiness(
        route, store, source_commit="b" * 40, as_of_ms=150, max_age_ms=100)
    assert result.ready_for_activation_review is False
    assert result.reasons == tuple(
        f"missing_{scenario}" for scenario in REQUIRED_ALIAS_PARITY_SCENARIOS)
    assert result.evidence_ids == ()


@pytest.mark.parametrize(
    "observed,passed,expected",
    ((1, True, "stale_stream"), (200, True, "future_stream"),
     (100, False, "failed_stream")))
def test_latest_stale_future_or_failed_scenario_blocks_readiness(
        tmp_path, observed, passed, expected):
    route, *_ = evidence(tmp_path)
    store = SQLiteInferenceAliasParityStore(tmp_path / "parity.sqlite")
    for scenario in REQUIRED_ALIAS_PARITY_SCENARIOS:
        if scenario != "stream":
            store.record(observation(route, scenario))
    store.record(observation(
        route, "stream", observed=observed, passed=passed))
    result = evaluate_inference_alias_readiness(
        route, store, source_commit=COMMIT, as_of_ms=150, max_age_ms=100)
    assert result.ready_for_activation_review is False
    assert expected in result.reasons
    assert result.traffic_activation_allowed is False


def test_corrupt_persisted_evidence_fails_visibly(tmp_path):
    route, store = populated(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE inference_alias_parity_evidence SET evidence_json='{}' "
            "WHERE scenario='stream'")
    with pytest.raises(ValueError, match="parity schema"):
        store.latest(route.route_id, COMMIT)


def test_passing_and_failure_shape_are_mutually_exclusive(tmp_path):
    route, *_ = evidence(tmp_path)
    with pytest.raises(ValueError, match="failure codes"):
        InferenceAliasParityEvidence(
            route.route_id, "stream", COMMIT, 100, True,
            ("proof",), ("failure",))
    with pytest.raises(ValueError, match="failure codes"):
        InferenceAliasParityEvidence(
            route.route_id, "stream", COMMIT, 100, False,
            ("proof",), ())
