import pytest

from vera.fabric.record_revision import create_record_revision
from vera.fabric.revision_path import RevisionAccessDenied, RevisionPath
from vera.fabric.revision_store import RevisionStore


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def revision():
    return create_record_revision(
        namespace="test.dataset", record_type="document", logical_key="one",
        content={"value": 1}, created_at=NOW, policy={"classification": "internal"})


def test_denial_happens_before_authority_mutation(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    path = RevisionPath(store, lambda action, actor, resource: False)
    value = revision()
    with pytest.raises(RevisionAccessDenied, match="write denied"):
        path.put(value, actor="agent-a", projections=["vector"], expected_head="")
    assert store.head(value.record_id) is None


def test_authorizer_receives_bounded_identity_and_policy_context(tmp_path):
    seen = []
    store = RevisionStore(tmp_path / "fabric.db")
    path = RevisionPath(store, lambda action, actor, resource:
                        not seen.append((action, actor, resource)))
    value = revision()
    path.put(value, actor="agent-a", projections=["vector"], expected_head="")
    action, actor, resource = seen[0]
    assert (action, actor) == ("revision.write", "agent-a")
    assert resource == {"record_id": value.record_id,
                        "revision_id": value.revision_id,
                        "namespace": "test.dataset", "record_type": "document",
                        "tombstone": False,
                        "policy": {"classification": "internal"}}


def test_read_current_or_exact_revision_and_prevent_record_confusion(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    path = RevisionPath(store, lambda action, actor, resource: actor == "agent-a")
    value = revision()
    path.put(value, actor="agent-a", projections=["vector"], expected_head="")
    assert path.get(value.record_id, actor="agent-a")["revision_id"] == value.revision_id
    assert path.get(value.record_id, actor="agent-a",
                    revision_id=value.revision_id)["content"]["inline"] == {"value": 1}
    with pytest.raises(KeyError, match="does not belong"):
        path.get("rec_" + "f" * 64, actor="agent-a",
                 revision_id=value.revision_id)
    with pytest.raises(RevisionAccessDenied, match="read denied"):
        path.get(value.record_id, actor="agent-b")
