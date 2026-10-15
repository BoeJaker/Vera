from dataclasses import FrozenInstanceError

import pytest

from vera.fabric.memory_dual_write_receipt import create_memory_dual_write_receipt
from vera.fabric.record_revision import create_record_revision


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def revision(text="portable memory"):
    return create_record_revision(
        namespace="memory.general", record_type="fact", logical_key="native-1",
        content=text, content_schema={"media_type": "text/plain"},
        source={"kind": "native_memory"},
        policy={"classification": "internal"}, metadata={}, created_at=NOW,
    )


def outcomes(**changes):
    result = {"postgres": "committed", "chroma": "committed", "neo4j": "committed"}
    result.update(changes)
    return result


def make(**changes):
    values = {
        "operation_id": "memory-write-1",
        "native_memory_id": "native-1",
        "native_content_hash": "native-sha16:0123456789abcdef",
        "revision": revision(),
        "observed_at": NOW,
        "revision_outcome": "committed",
        "native_outcomes": outcomes(),
    }
    values.update(changes)
    return create_memory_dual_write_receipt(**values)


def test_receipt_binds_exact_revision_and_is_deterministic_payload_free():
    first = make()
    second = make()
    assert first == second
    data = first.to_dict()
    assert data["receipt_id"].startswith("mdwr_")
    assert data["record_id"] == revision().record_id
    assert data["revision_id"] == revision().revision_id
    assert data["content_hash"] == revision().content_hash
    assert data["authority_state"] == "dual_committed"
    assert data["requires_reconciliation"] is False
    assert data["authority_switch_allowed"] is False
    assert data["producer_kind"] == "internal"
    assert "portable memory" not in repr(data)


@pytest.mark.parametrize(
    "revision_outcome,native_outcomes,state",
    [
        ("committed", outcomes(postgres="failed"), "canonical_only"),
        ("failed", outcomes(), "native_only"),
        ("failed", outcomes(postgres="failed"), "uncommitted"),
    ],
)
def test_receipt_classifies_split_authority_without_granting_cutover(
    revision_outcome, native_outcomes, state,
):
    receipt = make(
        revision_outcome=revision_outcome,
        native_outcomes=native_outcomes,
        error_codes=(
            {"revision": "write_failed", "postgres": "write_failed"}
            if revision_outcome == "failed" and native_outcomes["postgres"] == "failed"
            else {"revision": "write_failed"} if revision_outcome == "failed"
            else {"postgres": "write_failed"}
        ),
    )
    assert receipt.authority_state == state
    assert receipt.requires_reconciliation is True
    assert receipt.authority_switch_allowed is False


def test_projection_failure_requires_reconciliation_but_keeps_dual_commit_visible():
    receipt = make(
        native_outcomes=outcomes(chroma="timed_out"),
        error_codes={"chroma": "backend_timeout"},
    )
    assert receipt.authority_state == "dual_committed"
    assert receipt.requires_reconciliation is True


def test_receipt_identity_changes_with_revision_or_outcome():
    base = make()
    changed_revision = make(revision=revision("changed memory"))
    changed_outcome = make(
        native_outcomes=outcomes(neo4j="failed"),
        error_codes={"neo4j": "backend_failed"},
    )
    assert len({base.receipt_id, changed_revision.receipt_id, changed_outcome.receipt_id}) == 3


def test_receipt_is_immutable_and_copies_mappings():
    supplied = outcomes()
    receipt = make(native_outcomes=supplied)
    supplied["postgres"] = "failed"
    assert receipt.native_outcomes["postgres"] == "committed"
    with pytest.raises(TypeError):
        receipt.native_outcomes["postgres"] = "failed"
    with pytest.raises(FrozenInstanceError):
        receipt.operation_id = "other"


def test_invalid_or_incomplete_evidence_fails_closed():
    with pytest.raises(ValueError, match="cover postgres"):
        make(native_outcomes={"postgres": "committed"})
    with pytest.raises(ValueError, match="unsupported outcome"):
        make(native_outcomes=outcomes(chroma="unknown"))
    with pytest.raises(ValueError, match="error code requires"):
        make(error_codes={"postgres": "not_an_error"})
    with pytest.raises(ValueError, match="every failed"):
        make(native_outcomes=outcomes(chroma="failed"))
    with pytest.raises(ValueError, match="timezone"):
        make(observed_at="2026-01-01T00:00:00")
    with pytest.raises(TypeError, match="RecordRevision"):
        make(revision={})
    with pytest.raises(ValueError, match="producer_kind"):
        make(producer_kind="unknown-path")


def test_additional_bounded_backend_is_accepted_and_affects_reconciliation():
    receipt = make(
        native_outcomes={**outcomes(), "qdrant": "failed"},
        error_codes={"qdrant": "backend_failed"},
    )
    assert receipt.native_outcomes["qdrant"] == "failed"
    assert receipt.requires_reconciliation is True
