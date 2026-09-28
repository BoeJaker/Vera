from pathlib import Path

import pytest

from vera.fabric.artifact_provider import LocalArtifactProvider
from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled, QueryRequest
from vera.fabric.duckdb_artifact_query import (
    DUCKDB_VERSION,
    DuckDBArtifactQueryProvider,
)


pytestmark = pytest.mark.critical
NOW = "2026-08-27T00:00:00Z"
PARQUET = "application/vnd.apache.parquet"


class FakeResult:
    def __init__(self, rows, columns=("id", "kind")):
        self._rows = list(rows)
        self.description = tuple((column,) for column in columns)

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, rows, record, columns=("id", "kind")):
        self.rows = rows
        self.record = record
        self.columns = columns
        self.closed = False

    def execute(self, statement, parameters):
        self.record["statement"] = statement
        self.record["parameters"] = parameters
        return FakeResult(self.rows, self.columns)

    def close(self):
        self.closed = True
        self.record["closed"] = True


def fixture(tmp_path: Path, rows=((1, "odd"), (2, "even"))):
    artifacts = LocalArtifactProvider(tmp_path / "artifacts")
    stat = artifacts.put(b"PAR1-not-a-live-test", media_type=PARQUET, created_at=NOW)
    calls = []
    record = {}

    def connect(**kwargs):
        calls.append(kwargs)
        return FakeConnection(rows, record)

    provider = DuckDBArtifactQueryProvider(
        artifacts, artifact_id=stat.artifact_id, dataset_id="demo.records",
        connect=connect)
    return artifacts, stat, provider, calls, record


def request(provider, **overrides):
    values = dict(dataset_id="demo.records",
                  snapshot_id=provider.binding.snapshot_id,
                  include_data=True, limit=1)
    values.update(overrides)
    return QueryRequest(**values)


def test_query_is_structured_parameterized_bounded_and_closes(tmp_path):
    _, stat, provider, calls, record = fixture(tmp_path)
    page = provider.query(request(provider, filters={"kind": "odd"}))
    assert page.matches == ({"record_index": 0, "score": 0.0,
                             "data": {"id": 1, "kind": "odd"}},)
    assert page.next_cursor
    assert page.provenance == {
        "artifact_id": stat.artifact_id, "checksum": stat.checksum,
        "media_type": PARQUET, "engine": "duckdb",
        "engine_version": DUCKDB_VERSION, "mode": "structured-read-only"}
    assert record["statement"] == (
        'SELECT * FROM read_parquet(?) WHERE "kind" IS NOT DISTINCT FROM ? '
        'LIMIT ? OFFSET ?')
    assert record["parameters"][1:] == ["odd", 2, 0]
    assert Path(record["parameters"][0]).is_relative_to(
        (tmp_path / "artifacts" / "objects").resolve())
    assert record["closed"] is True
    assert calls[0]["database"] == ":memory:"
    assert calls[0]["config"] == {
        "enable_external_access": False,
        "allowed_paths": [record["parameters"][0]],
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "allow_unsigned_extensions": False,
        "enable_global_s3_configuration": False,
        "lock_configuration": True,
    }


def test_explicit_snapshot_and_stable_record_index_column(tmp_path):
    artifacts = LocalArtifactProvider(tmp_path / "artifacts")
    stat = artifacts.put(b"PAR1-stable-index", media_type=PARQUET, created_at=NOW)
    record = {}

    def connect(**kwargs):
        return FakeConnection(
            ((41, "odd", 7),), record, ("id", "kind", "snapshot_index"))

    exact_snapshot = "snap_" + "1" * 64
    provider = DuckDBArtifactQueryProvider(
        artifacts, artifact_id=stat.artifact_id, dataset_id="demo.records",
        snapshot_id=exact_snapshot, record_index_column="snapshot_index",
        connect=connect)
    page = provider.query(QueryRequest(
        dataset_id="demo.records", snapshot_id=exact_snapshot,
        filters={"kind": "odd"}, limit=1))
    assert page.snapshot_id == exact_snapshot
    assert page.matches == ({"record_index": 7, "score": 0.0},)


@pytest.mark.parametrize("value", [True, -1, "7"])
def test_stable_record_index_must_be_non_negative_integer(tmp_path, value):
    artifacts = LocalArtifactProvider(tmp_path / "artifacts")
    stat = artifacts.put(b"PAR1-bad-index", media_type=PARQUET, created_at=NOW)
    provider = DuckDBArtifactQueryProvider(
        artifacts, artifact_id=stat.artifact_id, dataset_id="demo.records",
        record_index_column="snapshot_index",
        connect=lambda **kwargs: FakeConnection(
            ((1, "odd", value),), {}, ("id", "kind", "snapshot_index")))
    with pytest.raises(RuntimeError, match="stable record index"):
        provider.query(request(provider))


def test_cursor_is_bound_to_query_semantics(tmp_path):
    _, _, provider, _, _ = fixture(tmp_path)
    first = provider.query(request(provider, filters={"kind": "odd"}))
    with pytest.raises(ValueError, match="mismatched cursor"):
        provider.query(request(provider, filters={"kind": "even"},
                               cursor=first.next_cursor))


@pytest.mark.parametrize("column", ["x; DROP TABLE secrets", 'a"b', "", "../x"])
def test_filter_column_injection_is_rejected(tmp_path, column):
    _, _, provider, calls, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="filter column"):
        provider.query(request(provider, filters={column: 1}))
    assert calls == []


@pytest.mark.parametrize("value", [float("inf"), [1], {"nested": True}])
def test_non_scalar_filter_is_rejected(tmp_path, value):
    _, _, provider, calls, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="canonical JSON|finite JSON scalar"):
        provider.query(request(provider, filters={"id": value}))
    assert calls == []


def test_text_cross_binding_and_oversized_cursor_fail_closed(tmp_path):
    _, _, provider, calls, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="text search"):
        provider.query(request(provider, text="needle"))
    with pytest.raises(KeyError, match="dataset binding"):
        provider.query(QueryRequest(dataset_id="other",
                                    snapshot_id=provider.binding.snapshot_id))
    with pytest.raises(KeyError, match="snapshot"):
        provider.query(QueryRequest(dataset_id="demo.records",
                                    snapshot_id="snap_" + "0" * 64))
    with pytest.raises(ValueError, match="cursor exceeds"):
        provider.query(request(provider, cursor="x" * 16_385))
    assert calls == []


def test_artifact_must_be_parquet_and_verified_before_every_query(tmp_path):
    artifacts = LocalArtifactProvider(tmp_path / "artifacts")
    wrong = artifacts.put(b"csv", media_type="text/csv", created_at=NOW)
    with pytest.raises(ValueError, match="Parquet"):
        DuckDBArtifactQueryProvider(
            artifacts, artifact_id=wrong.artifact_id, dataset_id="demo")
    _, stat, provider, calls, _ = fixture(tmp_path / "second")
    provider._path.write_bytes(b"tampered")
    with pytest.raises(OSError, match="checksum"):
        provider.query(request(provider))
    assert calls == []


def test_cancellation_and_backend_errors_are_bounded(tmp_path):
    _, _, provider, _, _ = fixture(tmp_path)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        provider.query(request(provider), cancellation=signal)

    def fail(**kwargs):
        raise RuntimeError("secret path and credentials")

    provider._connect = fail
    with pytest.raises(RuntimeError, match=r"failed \(RuntimeError\)") as error:
        provider.query(request(provider))
    assert "secret" not in str(error.value)


def test_row_shape_and_json_contract_are_enforced(tmp_path):
    _, _, mismatch, _, _ = fixture(tmp_path / "mismatch", rows=((1,),))
    with pytest.raises(RuntimeError, match="schema"):
        mismatch.query(request(mismatch))
    _, _, non_json, _, _ = fixture(tmp_path / "json", rows=((object(), "odd"),))
    with pytest.raises(ValueError, match="canonical JSON"):
        non_json.query(request(non_json))
