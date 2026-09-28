"""Bounded, read-only DuckDB queries over verified local Parquet artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from importlib import metadata
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from vera.fabric.artifact_provider import LocalArtifactProvider
from vera.fabric.dataset_provider import (
    CancellationSignal,
    QueryCancelled,
    QueryPage,
    QueryRequest,
    _cursor,
    _identifier,
    _json_copy,
    _read_cursor,
)


DUCKDB_VERSION = "1.5.5"
_PARQUET_TYPES = frozenset({"application/vnd.apache.parquet", "application/x-parquet"})
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_CURSOR_CHARS = 16_384


@dataclass(frozen=True)
class DuckDBArtifactBinding:
    """Immutable identity binding between a Vera dataset and an artifact."""

    dataset_id: str
    artifact_id: str
    snapshot_id: str
    checksum: str
    media_type: str


def _default_connect(*, database: str, config: Mapping[str, Any]):
    try:
        installed = metadata.version("duckdb")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"DuckDB adapter requires optional dependency duckdb=={DUCKDB_VERSION}"
        ) from exc
    if installed != DUCKDB_VERSION:
        raise RuntimeError(
            f"DuckDB adapter requires duckdb=={DUCKDB_VERSION}; found {installed}"
        )
    module = importlib.import_module("duckdb")
    return module.connect(database=database, config=dict(config))


def _column(value: str) -> str:
    value = str(value or "")
    if not _COLUMN.fullmatch(value):
        raise ValueError("invalid DuckDB filter column")
    return value


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("DuckDB filters support only finite JSON scalar values")


class DuckDBArtifactQueryProvider:
    """QueryProvider for one checksum-verified, immutable Parquet artifact.

    The caller supplies :class:`QueryRequest`, never SQL. Only equality filters
    with conservative column names are compiled, and all values and paths remain
    bound parameters. A fresh locked-down in-memory connection is used per page.
    """

    name = "duckdb-artifact"

    def __init__(
        self,
        artifacts: LocalArtifactProvider,
        *,
        artifact_id: str,
        dataset_id: str,
        snapshot_id: str = "",
        record_index_column: str = "",
        connect: Callable[..., Any] | None = None,
    ) -> None:
        stat = artifacts.stat(artifact_id)
        if stat.media_type not in _PARQUET_TYPES:
            raise ValueError("DuckDB artifact queries require a Parquet artifact")
        if not artifacts.verify(artifact_id):
            raise OSError("artifact checksum verification failed")
        # Dataset identifiers are validated by QueryRequest. Constructing one is
        # intentionally avoided here because callers may not query immediately.
        dataset_id = _identifier(dataset_id, "dataset_id")
        self._artifacts = artifacts
        self._path = artifacts._path(artifact_id).resolve()
        if not self._path.is_relative_to(artifacts.objects.resolve()):
            raise OSError("artifact path escapes provider root")
        self._connect = connect or _default_connect
        self._record_index_column = (
            _column(record_index_column) if record_index_column else "")
        if snapshot_id:
            snapshot_id = _identifier(snapshot_id, "snapshot_id")
        self.binding = DuckDBArtifactBinding(
            dataset_id=dataset_id,
            artifact_id=artifact_id,
            snapshot_id=snapshot_id or "snap_" + artifact_id[4:],
            checksum=stat.checksum,
            media_type=stat.media_type,
        )

    @staticmethod
    def _configuration(path: Path) -> dict[str, Any]:
        return {
            "enable_external_access": False,
            "allowed_paths": [str(path)],
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_unsigned_extensions": False,
            "enable_global_s3_configuration": False,
            "lock_configuration": True,
        }

    def _validate_request(self, request: QueryRequest) -> tuple[tuple[str, Any], ...]:
        if request.dataset_id != self.binding.dataset_id:
            raise KeyError("dataset binding not found")
        if request.snapshot_id != self.binding.snapshot_id:
            raise KeyError("artifact snapshot not found")
        if request.text:
            raise ValueError("DuckDB artifact queries do not support text search")
        if len(request.cursor) > _MAX_CURSOR_CHARS:
            raise ValueError("cursor exceeds size limit")
        return tuple(sorted((_column(key), _scalar(value))
                            for key, value in request.filters.items()))

    @staticmethod
    def _statement(filters: Sequence[tuple[str, Any]]) -> str:
        where = ""
        if filters:
            clauses = [f'"{name}" IS NOT DISTINCT FROM ?' for name, _ in filters]
            where = " WHERE " + " AND ".join(clauses)
        return "SELECT * FROM read_parquet(?)" + where + " LIMIT ? OFFSET ?"

    def query(self, request: QueryRequest, *,
              cancellation: CancellationSignal | None = None) -> QueryPage:
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        filters = self._validate_request(request)
        if not self._artifacts.verify(self.binding.artifact_id):
            raise OSError("artifact checksum verification failed")
        offset = _read_cursor(request.cursor, kind="duckdb-query",
                              identity=request.query_id)
        connection = None
        try:
            connection = self._connect(
                database=":memory:", config=self._configuration(self._path))
            signal.checkpoint()
            parameters = [str(self._path), *(value for _, value in filters),
                          request.limit + 1, offset]
            result = connection.execute(self._statement(filters), parameters)
            rows = result.fetchall()
            description = tuple(item[0] for item in (result.description or ()))
        except QueryCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"DuckDB artifact query failed ({type(exc).__name__})"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        signal.checkpoint()
        if len(rows) > request.limit + 1:
            raise RuntimeError("DuckDB adapter returned more rows than requested")
        page_rows = rows[:request.limit]
        matches = []
        for index, row in enumerate(page_rows):
            signal.checkpoint()
            if len(row) != len(description):
                raise RuntimeError("DuckDB result schema does not match its rows")
            record_index = offset + index
            if self._record_index_column:
                try:
                    value = row[description.index(self._record_index_column)]
                except ValueError as exc:
                    raise RuntimeError(
                        "DuckDB result lacks its stable record index column") from exc
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RuntimeError("DuckDB stable record index is invalid")
                record_index = value
            item = {"record_index": record_index, "score": 0.0}
            if request.include_data:
                item["data"] = _json_copy(dict(zip(description, row)), "DuckDB row")
            matches.append(item)
        has_more = len(rows) > request.limit
        next_cursor = (_cursor({"kind": "duckdb-query",
                                "identity": request.query_id,
                                "offset": offset + len(page_rows)})
                       if has_more else "")
        return QueryPage(
            query_id=request.query_id,
            snapshot_id=self.binding.snapshot_id,
            matches=tuple(matches),
            next_cursor=next_cursor,
            provider=self.name,
            provenance={
                "artifact_id": self.binding.artifact_id,
                "checksum": self.binding.checksum,
                "media_type": self.binding.media_type,
                "engine": "duckdb",
                "engine_version": DUCKDB_VERSION,
                "mode": "structured-read-only",
            },
        )
