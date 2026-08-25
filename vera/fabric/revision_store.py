"""Transactional SQLite authority and projection receipts for Fabric revisions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from .record_revision import RecordRevision


RECEIPT_STATES = frozenset({
    "pending", "applied", "failed", "stale", "rebuilding", "removed",
})
_TRANSITIONS = {
    "pending": {"applied", "failed", "removed"},
    "applied": {"stale", "removed"},
    "failed": {"pending", "rebuilding", "removed"},
    "stale": {"rebuilding", "removed"},
    "rebuilding": {"applied", "failed", "removed"},
    "removed": {"rebuilding"},
}
_PROJECTION = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


class RevisionConflict(ValueError):
    pass


class RevisionStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS fabric_record_revisions (
                    revision_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    projection_set_json TEXT NOT NULL,
                    tombstone INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_frr_record
                    ON fabric_record_revisions(record_id, created_at);
                CREATE TABLE IF NOT EXISTS fabric_record_heads (
                    record_id TEXT PRIMARY KEY,
                    revision_id TEXT NOT NULL REFERENCES fabric_record_revisions(revision_id),
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fabric_projection_receipts (
                    revision_id TEXT NOT NULL REFERENCES fabric_record_revisions(revision_id),
                    projection TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('pending','applied','failed','stale','rebuilding','removed')),
                    attempt INTEGER NOT NULL CHECK(attempt >= 0),
                    generation INTEGER NOT NULL CHECK(generation >= 0),
                    projection_revision TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (revision_id, projection)
                );
                CREATE TABLE IF NOT EXISTS fabric_revision_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _projections(values: Iterable[str]) -> list[str]:
        result = sorted({str(item).strip() for item in values})
        if not result or len(result) > 32 or any(not _PROJECTION.fullmatch(x) for x in result):
            raise ValueError("projections must contain 1..32 valid unique names")
        return result

    @staticmethod
    def _event(conn: sqlite3.Connection, record_id: str, revision_id: str,
               event_type: str, detail: dict[str, Any], occurred_at: str) -> None:
        conn.execute(
            "INSERT INTO fabric_revision_events "
            "(record_id,revision_id,event_type,detail_json,occurred_at) VALUES (?,?,?,?,?)",
            (record_id, revision_id, event_type,
             json.dumps(detail, sort_keys=True, separators=(",", ":")), occurred_at))

    def put(self, revision: RecordRevision, *, projections: Iterable[str],
            expected_head: str | None = None) -> dict[str, Any]:
        if not isinstance(revision, RecordRevision):
            raise TypeError("revision must be a RecordRevision")
        names = self._projections(projections)
        envelope_value = revision.to_dict()
        envelope = json.dumps(envelope_value, sort_keys=True, separators=(",", ":"))
        projection_json = json.dumps(names, separators=(",", ":"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT envelope_json,projection_set_json FROM fabric_record_revisions "
                "WHERE revision_id=?", (revision.revision_id,)).fetchone()
            if existing:
                if existing["envelope_json"] != envelope or existing["projection_set_json"] != projection_json:
                    raise RevisionConflict("revision identity replay differs")
                conn.commit()
                return {"created": False, "authority_commit": "committed",
                        "record_id": revision.record_id,
                        "revision_id": revision.revision_id,
                        "head": self.head(revision.record_id),
                        "receipts": self.receipts(revision.revision_id)}
            head = conn.execute("SELECT revision_id,generation FROM fabric_record_heads "
                                "WHERE record_id=?", (revision.record_id,)).fetchone()
            actual_head = head["revision_id"] if head else ""
            if expected_head is not None and expected_head != actual_head:
                raise RevisionConflict("head changed")
            parents = envelope_value["provenance"]["parents"]
            if actual_head and actual_head not in parents:
                raise RevisionConflict("current head missing from lineage")
            if parents:
                marks = ",".join("?" for _ in parents)
                present = {row[0] for row in conn.execute(
                    f"SELECT revision_id FROM fabric_record_revisions "
                    f"WHERE revision_id IN ({marks})", tuple(parents)).fetchall()}
                missing = set(parents) - present
                if missing:
                    raise RevisionConflict("parent revision not found")
            conn.execute(
                "INSERT INTO fabric_record_revisions VALUES (?,?,?,?,?,?)",
                (revision.revision_id, revision.record_id, envelope, projection_json,
                 int(revision.tombstone), revision.created_at))
            generation = (head["generation"] + 1) if head else 1
            conn.execute(
                "INSERT INTO fabric_record_heads VALUES (?,?,?) "
                "ON CONFLICT(record_id) DO UPDATE SET revision_id=excluded.revision_id, "
                "generation=excluded.generation",
                (revision.record_id, revision.revision_id, generation))
            for name in names:
                conn.execute(
                    "INSERT INTO fabric_projection_receipts VALUES (?,?,?,0,0,'','',?)",
                    (revision.revision_id, name, "pending", revision.created_at))
            self._event(conn, revision.record_id, revision.revision_id,
                        "authority.committed", {"generation": generation}, revision.created_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"created": True, "authority_commit": "committed",
                "record_id": revision.record_id, "revision_id": revision.revision_id,
                "head": self.head(revision.record_id),
                "receipts": self.receipts(revision.revision_id)}

    def transition(self, revision_id: str, projection: str, *, from_state: str,
                   to_state: str, occurred_at: str, projection_revision: str = "",
                   error_code: str = "") -> dict[str, Any]:
        if from_state not in RECEIPT_STATES or to_state not in _TRANSITIONS[from_state]:
            raise ValueError("invalid receipt transition")
        if not _PROJECTION.fullmatch(projection):
            raise ValueError("invalid projection")
        self._validate_transition_evidence(to_state, projection_revision, error_code)
        self._validate_time(occurred_at)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT r.*,v.record_id FROM fabric_projection_receipts r "
                "JOIN fabric_record_revisions v USING(revision_id) "
                "WHERE r.revision_id=? AND r.projection=?",
                (revision_id, projection)).fetchone()
            if not row:
                raise KeyError("receipt not found")
            if row["state"] != from_state:
                raise RevisionConflict("receipt state changed")
            attempt = row["attempt"] + (1 if to_state in {"pending", "rebuilding"} else 0)
            generation = row["generation"] + (1 if to_state == "rebuilding" else 0)
            conn.execute(
                "UPDATE fabric_projection_receipts SET state=?,attempt=?,generation=?,"
                "projection_revision=?,error_code=?,updated_at=? "
                "WHERE revision_id=? AND projection=?",
                (to_state, attempt, generation, projection_revision, error_code,
                 occurred_at, revision_id, projection))
            self._event(conn, row["record_id"], revision_id, "projection.transition",
                        {"projection": projection, "from": from_state, "to": to_state,
                         "attempt": attempt, "generation": generation}, occurred_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.receipt(revision_id, projection)

    @staticmethod
    def _validate_time(value: str) -> None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be an RFC3339 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")

    @staticmethod
    def _validate_transition_evidence(state: str, projection_revision: str,
                                      error_code: str) -> None:
        if len(projection_revision) > 256 or len(error_code) > 128:
            raise ValueError("transition evidence is too long")
        if state == "applied" and not projection_revision.strip():
            raise ValueError("applied requires projection_revision")
        if state == "failed" and (not error_code.strip() or not _PROJECTION.fullmatch(error_code)):
            raise ValueError("failed requires a valid error_code")
        if state != "failed" and error_code:
            raise ValueError("error_code is only valid for failed receipts")

    def rollback(self, record_id: str, *, expected_head: str, target_revision: str,
                 occurred_at: str) -> dict[str, Any]:
        self._validate_time(occurred_at)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            head = conn.execute("SELECT * FROM fabric_record_heads WHERE record_id=?",
                                (record_id,)).fetchone()
            if not head or head["revision_id"] != expected_head:
                raise RevisionConflict("head changed")
            if target_revision == expected_head:
                raise RevisionConflict("rollback target is already head")
            target = conn.execute(
                "SELECT 1 FROM fabric_record_revisions WHERE record_id=? AND revision_id=?",
                (record_id, target_revision)).fetchone()
            if not target:
                raise KeyError("target revision not found")
            generation = head["generation"] + 1
            conn.execute("UPDATE fabric_record_heads SET revision_id=?,generation=? "
                         "WHERE record_id=?", (target_revision, generation, record_id))
            # Restoring authority also schedules every target projection for a
            # fresh generation. A head-only rollback would leave derived stores
            # reflecting the revision that was just rolled back.
            conn.execute(
                "UPDATE fabric_projection_receipts SET state='rebuilding',"
                "attempt=attempt+1,generation=generation+1,projection_revision='',"
                "error_code='',updated_at=? WHERE revision_id=?",
                (occurred_at, target_revision))
            self._event(conn, record_id, target_revision, "authority.rolled_back",
                        {"from_revision": expected_head, "generation": generation}, occurred_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.head(record_id)

    def head(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM fabric_record_heads WHERE record_id=?",
                               (record_id,)).fetchone()
            return dict(row) if row else None

    def revision(self, revision_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT envelope_json FROM fabric_record_revisions "
                "WHERE revision_id=?", (revision_id,)).fetchone()
            if not row:
                raise KeyError("revision not found")
            return json.loads(row["envelope_json"])

    def current(self, record_id: str) -> dict[str, Any] | None:
        head = self.head(record_id)
        return self.revision(head["revision_id"]) if head else None

    def receipt(self, revision_id: str, projection: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM fabric_projection_receipts "
                               "WHERE revision_id=? AND projection=?",
                               (revision_id, projection)).fetchone()
            if not row:
                raise KeyError("receipt not found")
            return dict(row)

    def receipts(self, revision_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM fabric_projection_receipts WHERE revision_id=? "
                "ORDER BY projection", (revision_id,)).fetchall()]

    def reconcile(self, *, states: Iterable[str] = ("failed", "stale"),
                  limit: int = 100) -> list[dict[str, Any]]:
        wanted = sorted(set(states))
        if not wanted or any(state not in RECEIPT_STATES for state in wanted):
            raise ValueError("invalid reconciliation states")
        limit = max(1, min(int(limit), 500))
        marks = ",".join("?" for _ in wanted)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM fabric_projection_receipts WHERE state IN ({marks}) "
                "ORDER BY updated_at,revision_id,projection LIMIT ?", (*wanted, limit)).fetchall()
            return [dict(row) for row in rows]
