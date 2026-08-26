import pytest

from vera.fabric.dataset_provider import (
    CancellationSignal, FrozenDatasetProvider, QueryCancelled, QueryRequest,
)


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def provider():
    value = FrozenDatasetProvider()
    snapshot = value.register(
        dataset_id="demo.records", created_at=NOW,
        schema={"type": "object", "required": ["id"]},
        provenance={"source": "fixture", "revision": "abc123"},
        records=[{"id": 1, "kind": "odd", "text": "alpha"},
                 {"id": 2, "kind": "even", "text": "beta"},
                 {"id": 3, "kind": "odd", "text": "alphabet"}],
    )
    return value, snapshot


def test_snapshot_identity_is_stable_and_input_is_copied():
    rows = [{"id": 1}]
    value = FrozenDatasetProvider()
    first = value.register(dataset_id="demo", created_at=NOW, records=rows,
                           schema={"type": "object"}, provenance={"source": "test"})
    replay = value.register(dataset_id="demo", created_at=NOW, records=[{"id": 1}],
                            schema={"type": "object"}, provenance={"source": "test"})
    rows[0]["id"] = 99
    assert first == replay
    assert value.scan(first.snapshot_id).records == ({"id": 1},)
    exposed = first.schema
    exposed["changed"] = True
    assert "changed" not in first.schema


def test_latest_snapshot_uses_created_time_not_registration_order():
    value = FrozenDatasetProvider()
    newer = value.register(dataset_id="demo", created_at="2026-01-02T00:00:00Z",
                           records=[{"id": 2}], schema={}, provenance={})
    value.register(dataset_id="demo", created_at=NOW, records=[{"id": 1}],
                   schema={}, provenance={})
    assert value.stat("demo") == newer


def test_scan_cursor_is_snapshot_bound_and_pages_without_duplicates():
    value, snapshot = provider()
    first = value.scan(snapshot.snapshot_id, limit=2)
    second = value.scan(snapshot.snapshot_id, limit=2, cursor=first.next_cursor)
    assert [row["id"] for row in first.records + second.records] == [1, 2, 3]
    other = value.register(dataset_id="other", created_at=NOW, records=[{"id": 4}],
                           schema={}, provenance={})
    with pytest.raises(ValueError, match="mismatched cursor"):
        value.scan(other.snapshot_id, cursor=first.next_cursor)


def test_query_has_stable_identity_filters_provenance_and_pagination():
    value, snapshot = provider()
    request = QueryRequest(dataset_id="demo.records", snapshot_id=snapshot.snapshot_id,
                           text="alpha", filters={"kind": "odd"}, limit=1,
                           include_data=True)
    first = value.query(request)
    second = value.query(QueryRequest(
        dataset_id=request.dataset_id, snapshot_id=request.snapshot_id,
        text=request.text, filters=request.filters, limit=1,
        include_data=True, cursor=first.next_cursor))
    assert first.query_id == second.query_id
    assert [first.matches[0]["data"]["id"], second.matches[0]["data"]["id"]] == [1, 3]
    assert first.provenance == {"source": "fixture", "revision": "abc123"}


def test_query_identity_cannot_be_changed_through_original_or_exposed_filters():
    value, snapshot = provider()
    filters = {"kind": "odd"}
    request = QueryRequest(dataset_id="demo.records", snapshot_id=snapshot.snapshot_id,
                           filters=filters)
    identity = request.query_id
    filters["kind"] = "even"
    exposed = request.filters
    exposed["kind"] = "even"
    assert request.filters == {"kind": "odd"}
    assert request.query_id == identity


def test_query_cursor_rejects_changed_semantics():
    value, snapshot = provider()
    first = value.query(QueryRequest(
        dataset_id="demo.records", snapshot_id=snapshot.snapshot_id,
        filters={"kind": "odd"}, limit=1))
    with pytest.raises(ValueError, match="mismatched cursor"):
        value.query(QueryRequest(
            dataset_id="demo.records", snapshot_id=snapshot.snapshot_id,
            filters={"kind": "even"}, limit=1, cursor=first.next_cursor))


def test_limits_invalid_json_and_cross_dataset_snapshot_fail_closed():
    value, snapshot = provider()
    with pytest.raises(ValueError, match="limit"):
        value.scan(snapshot.snapshot_id, limit=0)
    with pytest.raises(ValueError, match="canonical JSON"):
        value.register(dataset_id="bad", created_at=NOW, records=[{"x": float("nan")}],
                       schema={}, provenance={})
    with pytest.raises(KeyError, match="not found"):
        value.stat("other", snapshot.snapshot_id)
    with pytest.raises(ValueError, match="filters must"):
        QueryRequest(dataset_id="demo.records", snapshot_id=snapshot.snapshot_id,
                     filters=[])


def test_cancellation_is_observable_before_scan_or_query():
    value, snapshot = provider()
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        value.scan(snapshot.snapshot_id, cancellation=signal)
    with pytest.raises(QueryCancelled):
        value.query(QueryRequest(
            dataset_id="demo.records", snapshot_id=snapshot.snapshot_id),
            cancellation=signal)


def test_query_checks_cancellation_during_iteration():
    value, snapshot = provider()
    class CancelAfterTwo(CancellationSignal):
        def __init__(self):
            super().__init__()
            self.calls = 0
        def checkpoint(self):
            self.calls += 1
            if self.calls == 3:
                self.cancel()
            super().checkpoint()
    with pytest.raises(QueryCancelled):
        value.query(QueryRequest(
            dataset_id="demo.records", snapshot_id=snapshot.snapshot_id),
            cancellation=CancelAfterTwo())
