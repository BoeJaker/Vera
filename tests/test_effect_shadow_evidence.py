import pytest
import sqlite3

from vera.integrations.effect_shadow_evidence import (
    ExternalEffectShadowEvidence, evaluate_enforcement_readiness)
from vera.integrations.effect_observation_context import attributed, current_source
from vera.integrations.external_effects import plan_api_effect_shadow


pytestmark = pytest.mark.critical


def shadow(method="POST", approval="approval:1", key="effect:1"):
    return plan_api_effect_shadow(
        integration_id="private-service", method=method, path="/private/order?id=7",
        approval_receipt_ref=approval, idempotency_key=key, retry=True)


def test_shadow_summary_is_bounded_payload_free_and_aggregated(tmp_path):
    ledger = ExternalEffectShadowEvidence(tmp_path / "shadow.sqlite3")
    admitted = shadow()
    denied = shadow(approval="")
    denied["decision"]["would_execute"] = False
    ledger.record(admitted, observed_at="2026-09-10T10:00:00Z")
    ledger.record(denied, observed_at="2026-09-10T10:01:00Z")

    result = ledger.summary(limit=1)
    assert result["totals"] == {"observations": 2, "would_admit": 1,
                                "would_execute": 1, "would_suppress": 0}
    assert result["window"] == {"requested": 1, "returned": 1}
    assert result["reasons_in_window"] == {
        "approval_receipt_required": 1, "retry_approval_receipt_required": 1}
    encoded = str(result)
    assert "private-service" not in encoded
    assert "/private/order" not in encoded
    assert "approval:1" not in encoded
    assert "effect:1" not in encoded
    assert all(result[key] is False for key in ("executes", "retries", "retains_payload"))


def test_async_local_source_is_recorded_and_filterable_without_payload(tmp_path):
    ledger = ExternalEffectShadowEvidence(tmp_path / "shadow.sqlite3")
    with attributed("intel"):
        ledger.record(shadow(), observed_at="2026-09-10T10:00:00Z")
    ledger.record(shadow(key="effect:2"), observed_at="2026-09-10T10:01:00Z")

    result = ledger.summary(source="intel")
    assert result["schema"] == "vera.external-effect-shadow-evidence/v2"
    assert result["totals"]["observations"] == 1
    assert result["source_filter"] == "intel"
    assert result["sources"] == {"intel": 1, "unattributed": 1}
    assert result["recent"][0]["source"] == "intel"
    assert current_source() == ""


def test_existing_evidence_database_is_additively_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE external_effect_shadow_evidence ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, "
            "plan_id TEXT NOT NULL, connection_sha256 TEXT NOT NULL, "
            "classification TEXT NOT NULL, method TEXT NOT NULL, "
            "would_admit INTEGER NOT NULL, would_execute INTEGER NOT NULL, "
            "would_suppress INTEGER NOT NULL, reasons_json TEXT NOT NULL)")
    ledger = ExternalEffectShadowEvidence(path)
    ledger.record(shadow())
    assert ledger.summary()["sources"] == {"unattributed": 1}


def test_source_rejects_content_not_opaque_identity(tmp_path):
    ledger = ExternalEffectShadowEvidence(tmp_path / "shadow.sqlite3")
    with pytest.raises(ValueError, match="opaque identifier"):
        ledger.record(shadow(), source="Intel report for private@example.test")


@pytest.mark.parametrize("limit", [0, 201, True])
def test_shadow_summary_rejects_unbounded_windows(tmp_path, limit):
    ledger = ExternalEffectShadowEvidence(tmp_path / "shadow.sqlite3")
    with pytest.raises(ValueError):
        ledger.summary(limit=limit)


def test_readiness_fails_closed_and_names_missing_evidence():
    result = evaluate_enforcement_readiness({
        "totals": {"observations": 99, "would_admit": 50, "would_suppress": 0},
        "classifications": {"read": 25, "non_idempotent_write": 25}})
    assert result["eligible_for_operator_review"] is False
    assert result["enforcement_enabled"] is False
    assert result["unmet_checks"] == ["minimum_observations", "minimum_replay_suppressions"]


def test_readiness_only_qualifies_operator_review_not_enforcement():
    result = evaluate_enforcement_readiness({
        "totals": {"observations": 100, "would_admit": 80, "would_suppress": 1},
        "classifications": {"read": 50, "idempotent_write": 25,
                            "non_idempotent_write": 25}})
    assert result["eligible_for_operator_review"] is True
    assert result["decision"] == "ready_for_operator_review"
    assert result["enforcement_enabled"] is False
    assert result["changes_policy"] is False
