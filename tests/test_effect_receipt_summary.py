from pathlib import Path

import pytest

from vera.integrations.effect_receipts import ExternalEffectReceiptLedger
from vera.integrations.external_effects import plan_external_effect


pytestmark = pytest.mark.critical


def plan(operation="orders.create"):
    return plan_external_effect(
        connection_id="integration:demo", operation=operation, method="POST",
        idempotency_key="order:42", approval_receipt_ref="approval:42",
        retry=True)


def test_summary_is_bounded_payload_free_and_aggregated(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    first = plan()
    ledger.record(first, attempt_id="attempt:1", outcome="failed",
                  observed_at="2026-09-10T10:00:00Z",
                  response_sha256="a" * 64,
                  provider_receipt_ref="provider:secret-ref")
    ledger.record(first, attempt_id="attempt:1", outcome="failed",
                  observed_at="2026-09-10T10:01:00Z",
                  response_sha256="a" * 64,
                  provider_receipt_ref="provider:secret-ref")
    ledger.record(first, attempt_id="attempt:2", outcome="succeeded",
                  observed_at="2026-09-10T10:02:00Z")

    result = ledger.summary(limit=1)
    assert result["totals"] == {"plans": 1, "receipts": 2, "observations": 3}
    assert result["outcomes"]["failed"] == {"receipts": 1, "observations": 2}
    assert result["outcomes"]["succeeded"] == {"receipts": 1, "observations": 1}
    assert result["window"] == {"requested": 1, "returned": 1}
    assert len(result["recent"]) == 1
    encoded = str(result)
    assert "provider:secret-ref" not in encoded
    assert "approval:42" not in encoded
    assert "order:42" not in encoded
    assert all(result[key] is False for key in ("executes", "retries", "retains_payload"))


def test_summary_filter_is_exact_and_does_not_merge_plans(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    first, second = plan(), plan("orders.cancel")
    ledger.record(first, attempt_id="attempt:1", outcome="failed",
                  observed_at="2026-09-10T10:00:00Z")
    ledger.record(second, attempt_id="attempt:2", outcome="succeeded",
                  observed_at="2026-09-10T10:01:00Z")
    result = ledger.summary(plan_id=first["plan_id"])
    assert result["filter"]["plan_id"] == first["plan_id"]
    assert result["totals"] == {"plans": 1, "receipts": 1, "observations": 1}
    assert {item["plan_id"] for item in result["recent"]} == {first["plan_id"]}


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 201}, {"limit": True}, {"plan_id": "not-a-digest"},
])
def test_summary_rejects_unbounded_or_unsafe_filters(tmp_path, kwargs):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    with pytest.raises(ValueError):
        ledger.summary(**kwargs)


def test_integrations_ui_explains_empty_evidence_without_mutation_controls():
    source = (Path(__file__).resolve().parents[1] / "vera" / "integrations" /
              "integrations_panel.html").read_text(encoding="utf-8")
    assert "Effect evidence" in source
    assert "/integrations/effect/receipts?limit=50" in source
    assert "/integrations/effect/retry/policy" in source
    assert "Vera does not automatically retry" in source
    assert "policy.reason_descriptions" in source
    assert "No external-effect receipts have been recorded yet" in source
    assert "no retries or external calls" in source
    assert "record receipt" not in source.lower()
