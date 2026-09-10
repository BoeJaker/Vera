from datetime import datetime, timezone

import pytest

from Vera.vera.integrations.effect_receipts import (
    EffectReceiptConflict, ExternalEffectReceiptLedger,
)
from Vera.vera.integrations.external_effects import plan_external_effect


pytestmark = pytest.mark.critical


def _plan(**overrides):
    values = dict(connection_id="integration:mail", operation="message:42.send",
                  method="POST", idempotency_key="message:42",
                  approval_receipt_ref="approval:1")
    values.update(overrides)
    return plan_external_effect(**values)


def test_effect_identity_survives_retry_and_approval_renewal():
    first = _plan(retry=False, approval_receipt_ref="approval:1")
    retry = _plan(retry=True, approval_receipt_ref="approval:2")
    assert first["plan_id"] == retry["plan_id"]


def test_success_receipt_prevents_repeat_without_retaining_references(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    plan = _plan()
    receipt = ledger.record(
        plan, attempt_id="attempt:1", outcome="succeeded",
        observed_at="2026-09-10T11:00:00+00:00",
        response_sha256="a" * 64, provider_receipt_ref="provider:secret-ref")
    assert receipt["classification"] == "first_seen"
    assert receipt["provider_receipt_ref_sha256"]
    assert "provider:secret-ref" not in str(receipt)
    status = ledger.replay_status(_plan(retry=True, approval_receipt_ref="approval:2"))
    assert status["already_succeeded"] is True
    assert status["decision"] == "do_not_repeat"
    assert status["successful_receipt_id"] == receipt["receipt_id"]


def test_failed_attempt_allows_a_new_attempt_but_duplicate_is_idempotent(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    plan = _plan()
    failed = ledger.record(plan, attempt_id="attempt:1", outcome="failed",
                           observed_at="2026-09-10T11:00:00Z")
    duplicate = ledger.record(plan, attempt_id="attempt:1", outcome="failed",
                              observed_at="2026-09-10T11:01:00Z")
    assert failed["observation_count"] == 1
    assert duplicate["observation_count"] == 2
    assert ledger.replay_status(plan)["decision"] == "no_success_receipt"
    ledger.record(plan, attempt_id="attempt:2", outcome="succeeded",
                  observed_at="2026-09-10T11:02:00Z")
    assert ledger.replay_status(plan)["attempts"] == 2


def test_same_attempt_with_different_evidence_is_rejected(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    plan = _plan()
    ledger.record(plan, attempt_id="attempt:1", outcome="failed",
                  observed_at="2026-09-10T11:00:00Z")
    with pytest.raises(EffectReceiptConflict):
        ledger.record(plan, attempt_id="attempt:1", outcome="succeeded",
                      observed_at="2026-09-10T11:01:00Z")


def test_denied_or_forged_plans_cannot_anchor_receipts(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    with pytest.raises(ValueError, match="denied"):
        ledger.record(plan_external_effect(connection_id="integration:mail",
                                           operation="message.send", method="POST"),
                      attempt_id="attempt:1", outcome="failed",
                      observed_at=datetime.now(timezone.utc).isoformat())
    forged = _plan()
    forged["plan_id"] = "0" * 64
    with pytest.raises(ValueError, match="plan_id"):
        ledger.replay_status(forged)


def test_policy_decision_cannot_be_forged_open(tmp_path):
    ledger = ExternalEffectReceiptLedger(tmp_path / "receipts.sqlite3")
    denied = plan_external_effect(connection_id="integration:mail",
                                  operation="message.send", method="POST")
    denied["admission"] = {"allowed": True, "reasons": []}
    denied["retry"]["allowed"] = True
    with pytest.raises(ValueError, match="reasons"):
        ledger.replay_status(denied)
