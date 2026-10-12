import pytest

from vera.fabric.revision_path import RevisionPath
from vera.fabric.revision_store import RevisionStore
from vera.worldview.canonical_corpus_projection import (
    build_candidates,
    materialize,
    plan_summary,
)


pytestmark = pytest.mark.critical


def _source():
    return dict(
        source_dataset_id="legacy.source",
        target_dataset_id="jepa.canonical.source",
        ids=["legacy-b", "legacy-a"],
        documents=["beta", "alpha"],
        metadatas=[
            {"created_at": "2026-10-01T10:00:00Z", "tags": "[\"b\"]"},
            {"created_at": "2026-10-01T09:00:00Z", "tags": ["a"]},
        ],
        embeddings=[[0.1] * 12, [0.2] * 12],
    )


def test_plan_is_deterministic_content_free_and_source_bound():
    values = _source()
    candidates, skipped = build_candidates(**values)
    summary = plan_summary(
        source_dataset_id=values["source_dataset_id"],
        target_dataset_id=values["target_dataset_id"],
        requested=2, candidates=candidates, skipped=skipped)

    assert summary["eligible_records"] == 2
    assert summary["mutates"] is False
    assert summary["retains_source"] is True
    assert summary["plan_digest"].startswith("sha256:")
    assert "alpha" not in repr(summary)
    assert "legacy-a" not in repr(summary)

    changed = _source()
    changed["embeddings"][1][0] = 0.3
    other, other_skipped = build_candidates(**changed)
    other_summary = plan_summary(
        source_dataset_id=changed["source_dataset_id"],
        target_dataset_id=changed["target_dataset_id"],
        requested=2, candidates=other, skipped=other_skipped)
    assert other_summary["plan_digest"] != summary["plan_digest"]


def test_candidates_skip_rows_that_cannot_be_exactly_projected():
    values = _source()
    values["documents"] = ["", "alpha"]
    values["embeddings"] = [[0.1] * 12, []]
    candidates, skipped = build_candidates(**values)

    assert candidates == []
    assert skipped["missing_document"] == 1
    assert skipped["missing_embedding"] == 1


def test_materialize_commits_revisions_and_exact_projection(tmp_path):
    values = _source()
    candidates, skipped = build_candidates(**values)
    summary = plan_summary(
        source_dataset_id=values["source_dataset_id"],
        target_dataset_id=values["target_dataset_id"],
        requested=2, candidates=candidates, skipped=skipped)
    path = RevisionPath(RevisionStore(tmp_path / "revisions.sqlite3"),
                        lambda action, actor, resource: True)
    writes = []

    result = materialize(
        candidates=candidates, expected_digest=summary["plan_digest"],
        summary=summary, revision_path=path, actor="codex",
        chroma_upsert=lambda **kwargs: writes.append(kwargs),
        occurred_at=lambda: "2026-10-01T11:00:00Z")

    assert result == {
        "ok": True, "applied": 2, "existing": 0, "failed": 0,
        "errors": {}, "plan_digest": summary["plan_digest"],
        "target_dataset_id": "jepa.canonical.source", "retains_source": True,
    }
    assert len(writes) == 2
    for candidate, write in zip(candidates, writes):
        metadata = write["metadatas"][0]
        assert write["ids"] == [candidate.revision.record_id]
        assert write["documents"] == [candidate.document]
        assert metadata["revision_id"] == candidate.revision.revision_id
        assert metadata["content_hash"] == candidate.revision.content_hash
        assert metadata["dataset_id"] == "jepa.canonical.source"
        assert path.store.receipt(candidate.revision.revision_id, "chroma")["state"] == "applied"

    replay = materialize(
        candidates=candidates, expected_digest=summary["plan_digest"],
        summary=summary, revision_path=path, actor="codex",
        chroma_upsert=lambda **kwargs: pytest.fail("applied projection rewritten"),
        occurred_at=lambda: "2026-10-01T12:00:00Z")
    assert replay["applied"] == 0
    assert replay["existing"] == 2


def test_materialize_refuses_stale_plan_before_any_write(tmp_path):
    values = _source()
    candidates, skipped = build_candidates(**values)
    summary = plan_summary(
        source_dataset_id=values["source_dataset_id"],
        target_dataset_id=values["target_dataset_id"],
        requested=2, candidates=candidates, skipped=skipped)
    path = RevisionPath(RevisionStore(tmp_path / "revisions.sqlite3"),
                        lambda action, actor, resource: True)

    with pytest.raises(ValueError, match="source plan changed"):
        materialize(
            candidates=candidates, expected_digest="sha256:" + "0" * 64,
            summary=summary, revision_path=path, actor="codex",
            chroma_upsert=lambda **kwargs: pytest.fail("unexpected write"),
            occurred_at=lambda: "2026-10-01T11:00:00Z")

    assert path.store.current(candidates[0].revision.record_id) is None


def test_materialize_retries_a_failed_projection_receipt(tmp_path):
    values = _source()
    values["ids"] = values["ids"][:1]
    values["documents"] = values["documents"][:1]
    values["metadatas"] = values["metadatas"][:1]
    values["embeddings"] = values["embeddings"][:1]
    candidates, skipped = build_candidates(**values)
    summary = plan_summary(
        source_dataset_id=values["source_dataset_id"],
        target_dataset_id=values["target_dataset_id"],
        requested=1, candidates=candidates, skipped=skipped)
    path = RevisionPath(RevisionStore(tmp_path / "revisions.sqlite3"),
                        lambda action, actor, resource: True)

    failed = materialize(
        candidates=candidates, expected_digest=summary["plan_digest"],
        summary=summary, revision_path=path, actor="codex",
        chroma_upsert=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
        occurred_at=lambda: "2026-10-01T11:00:00Z")
    assert failed["failed"] == 1
    revision_id = candidates[0].revision.revision_id
    assert path.store.receipt(revision_id, "chroma")["state"] == "failed"

    writes = []
    retried = materialize(
        candidates=candidates, expected_digest=summary["plan_digest"],
        summary=summary, revision_path=path, actor="codex",
        chroma_upsert=lambda **kwargs: writes.append(kwargs),
        occurred_at=lambda: "2026-10-01T12:00:00Z")
    assert retried["ok"] is True and retried["applied"] == 1
    assert len(writes) == 1
    assert path.store.receipt(revision_id, "chroma")["state"] == "applied"


def test_invalid_or_same_dataset_is_rejected():
    values = _source()
    values["target_dataset_id"] = values["source_dataset_id"]
    with pytest.raises(ValueError, match="must differ"):
        build_candidates(**values)

    values["target_dataset_id"] = "bad dataset"
    with pytest.raises(ValueError, match="invalid target"):
        build_candidates(**values)
