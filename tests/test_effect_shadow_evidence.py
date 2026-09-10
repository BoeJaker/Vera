import pytest

from vera.integrations.effect_shadow_evidence import ExternalEffectShadowEvidence
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


@pytest.mark.parametrize("limit", [0, 201, True])
def test_shadow_summary_rejects_unbounded_windows(tmp_path, limit):
    ledger = ExternalEffectShadowEvidence(tmp_path / "shadow.sqlite3")
    with pytest.raises(ValueError):
        ledger.summary(limit=limit)
