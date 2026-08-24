"""Append-only Run event journal and non-authoritative control ledger contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .run_protocol import (
    ArtifactRef, PROTOCOL_VERSION, Run, RunControl, RunError, RunEvent,
    replay_run, utc_now,
)


class JournalCorruption(ValueError):
    """Raised when sequence, identity, or checksum evidence is inconsistent."""


@dataclass(frozen=True)
class JournalEntry:
    event: RunEvent
    previous_checksum: str
    checksum: str


class RunJournalBackend(Protocol):
    """Minimal portable boundary for Redis, SQL, file, or external backends."""

    def append(self, event: RunEvent) -> JournalEntry: ...
    def register(self, run: Run) -> None: ...
    def checkpoint(self, run: Run) -> None: ...
    def run_ids(self) -> list[str]: ...
    def entries(self, run_id: str) -> list[JournalEntry]: ...
    def export(self, run_id: str) -> dict[str, Any]: ...
    def delete(self, run_id: str, *, expected_checksum: str) -> bool: ...


def _checksum(event: RunEvent, previous: str) -> str:
    body = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(f"{previous}\n{body}".encode("utf-8")).hexdigest()


_IDENTITY_FIELDS = (
    "id", "kind", "parent_run_id", "workflow_id", "task_id", "session_id",
    "trace_id", "created_at",
)


def _identity(run: Run) -> dict[str, Any]:
    return {name: getattr(run, name) for name in _IDENTITY_FIELDS}


def _run_from_dict(value: dict[str, Any]) -> Run:
    raw = dict(value)
    raw.pop("protocol", None)
    raw.pop("events", None)
    error = raw.get("error")
    raw["error"] = RunError(**error) if error else None
    raw["artifacts"] = [ArtifactRef(**item) for item in raw.get("artifacts") or []]
    return Run(**raw)


class MemoryRunJournal:
    """Deterministic reference journal; production persistence remains unselected."""

    def __init__(self) -> None:
        self._entries: dict[str, list[JournalEntry]] = {}
        self._event_ids: dict[str, JournalEntry] = {}
        self._identities: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, tuple[int, dict[str, Any]]] = {}

    def register(self, run: Run) -> None:
        identity = _identity(run)
        existing = self._identities.get(run.id)
        if existing is not None and existing != identity:
            raise JournalCorruption("run id was reused with different identity")
        self._identities[run.id] = identity

    def checkpoint(self, run: Run) -> None:
        sequence = len(run.events)
        if sequence < 1:
            raise ValueError("cannot checkpoint a run without events")
        if sequence != len(self._entries.get(run.id, [])):
            raise JournalCorruption("checkpoint sequence does not match journal tip")
        value = run.to_dict(include_events=False)
        existing = self._snapshots.get(run.id)
        if existing and existing[0] > sequence:
            raise JournalCorruption("checkpoint sequence moved backwards")
        if existing and existing[0] == sequence and existing[1] != value:
            raise JournalCorruption("checkpoint content changed at the same sequence")
        self._snapshots[run.id] = (sequence, value)

    def append(self, event: RunEvent) -> JournalEntry:
        existing = self._event_ids.get(event.id)
        if existing:
            if existing.event != event:
                raise JournalCorruption("event id was reused with different content")
            return existing
        rows = self._entries.setdefault(event.run_id, [])
        expected = len(rows) + 1
        if event.sequence != expected:
            raise JournalCorruption(
                f"event sequence must be gap-free: expected {expected}, got {event.sequence}")
        previous = rows[-1].checksum if rows else ""
        entry = JournalEntry(event=event, previous_checksum=previous,
                             checksum=_checksum(event, previous))
        rows.append(entry)
        self._event_ids[event.id] = entry
        return entry

    def entries(self, run_id: str) -> list[JournalEntry]:
        return list(self._entries.get(run_id, []))

    def run_ids(self) -> list[str]:
        return list(reversed(self._entries))

    def verify(self, run_id: str) -> dict[str, Any]:
        previous = ""
        for expected, entry in enumerate(self._entries.get(run_id, []), start=1):
            if entry.event.run_id != run_id or entry.event.sequence != expected:
                raise JournalCorruption("journal identity or sequence mismatch")
            if entry.previous_checksum != previous:
                raise JournalCorruption("journal checksum chain is broken")
            if entry.checksum != _checksum(entry.event, previous):
                raise JournalCorruption("journal event checksum is invalid")
            previous = entry.checksum
        return {"ok": True, "run_id": run_id,
                "event_count": len(self._entries.get(run_id, [])),
                "last_checksum": previous}

    def rebuild(self, *, run_id: str, kind: str = "", **identity: Any) -> Run:
        self.verify(run_id)
        events = [entry.event for entry in self._entries.get(run_id, [])]
        snapshot = self._snapshots.get(run_id)
        if snapshot:
            sequence, value = snapshot
            if sequence > len(events):
                raise JournalCorruption("checkpoint is ahead of the journal tip")
            run = _run_from_dict(value)
            run.events = list(events[:sequence])
            return replay_run(run, events[sequence:])
        stored = dict(self._identities.get(run_id) or {})
        stored.update(identity)
        stored["id"] = run_id
        stored["kind"] = kind or stored.get("kind") or "unknown"
        return replay_run(Run(**stored), events)

    def export(self, run_id: str) -> dict[str, Any]:
        verified = self.verify(run_id)
        return {
            "protocol": PROTOCOL_VERSION,
            "run_id": run_id,
            "event_count": verified["event_count"],
            "last_checksum": verified["last_checksum"],
            "entries": [{"event": row.event.to_dict(),
                         "previous_checksum": row.previous_checksum,
                         "checksum": row.checksum}
                        for row in self._entries.get(run_id, [])],
        }

    def delete(self, run_id: str, *, expected_checksum: str) -> bool:
        rows = self._entries.get(run_id)
        if not rows:
            return False
        actual = self.verify(run_id)["last_checksum"]
        if not expected_checksum or expected_checksum != actual:
            raise ValueError("delete checksum does not match the verified journal tip")
        removed = self._entries.pop(run_id)
        for entry in removed:
            self._event_ids.pop(entry.event.id, None)
        self._identities.pop(run_id, None)
        self._snapshots.pop(run_id, None)
        return True


class SqliteRunJournal:
    """Durable checksummed journal using only Python's SQLite runtime."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS run_events ("
            "run_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, "
            "event_json TEXT NOT NULL, previous_checksum TEXT NOT NULL, checksum TEXT NOT NULL, "
            "PRIMARY KEY(run_id, sequence))")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS run_identity ("
            "run_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS run_snapshots ("
            "run_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, run_json TEXT NOT NULL)")
        self._db.commit()

    @staticmethod
    def _event(raw: str) -> RunEvent:
        value = json.loads(raw)
        value.pop("protocol", None)
        return RunEvent(**value)

    def append(self, event: RunEvent) -> JournalEntry:
        body = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
        with self._lock:
            existing = self._db.execute(
                "SELECT event_json, previous_checksum, checksum FROM run_events WHERE event_id=?",
                (event.id,)).fetchone()
            if existing:
                stored = self._event(existing[0])
                if stored != event:
                    raise JournalCorruption("event id was reused with different content")
                return JournalEntry(stored, existing[1], existing[2])
            tip = self._db.execute(
                "SELECT sequence, checksum FROM run_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                (event.run_id,)).fetchone()
            expected = (tip[0] if tip else 0) + 1
            if event.sequence != expected:
                raise JournalCorruption(
                    f"event sequence must be gap-free: expected {expected}, got {event.sequence}")
            previous = tip[1] if tip else ""
            checksum = _checksum(event, previous)
            self._db.execute(
                "INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?)",
                (event.run_id, event.sequence, event.id, body, previous, checksum))
            self._db.commit()
            return JournalEntry(event, previous, checksum)

    def register(self, run: Run) -> None:
        identity = json.dumps(_identity(run), sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, default=str)
        with self._lock:
            existing = self._db.execute(
                "SELECT identity_json FROM run_identity WHERE run_id=?", (run.id,)
            ).fetchone()
            if existing and existing[0] != identity:
                raise JournalCorruption("run id was reused with different identity")
            if not existing:
                self._db.execute("INSERT INTO run_identity VALUES (?, ?)",
                                 (run.id, identity))
                self._db.commit()

    def checkpoint(self, run: Run) -> None:
        sequence = len(run.events)
        if sequence < 1:
            raise ValueError("cannot checkpoint a run without events")
        value = json.dumps(run.to_dict(include_events=False), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False, default=str)
        with self._lock:
            tip = self._db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id=?",
                (run.id,)).fetchone()[0]
            if sequence != tip:
                raise JournalCorruption("checkpoint sequence does not match journal tip")
            existing = self._db.execute(
                "SELECT sequence, run_json FROM run_snapshots WHERE run_id=?", (run.id,)
            ).fetchone()
            if existing and existing[0] > sequence:
                raise JournalCorruption("checkpoint sequence moved backwards")
            if existing and existing[0] == sequence and existing[1] != value:
                raise JournalCorruption("checkpoint content changed at the same sequence")
            self._db.execute(
                "INSERT INTO run_snapshots VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET sequence=excluded.sequence, "
                "run_json=excluded.run_json",
                (run.id, sequence, value))
            self._db.commit()

    def entries(self, run_id: str) -> list[JournalEntry]:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_json, previous_checksum, checksum FROM run_events "
                "WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        return [JournalEntry(self._event(row[0]), row[1], row[2]) for row in rows]

    def run_ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id FROM run_events GROUP BY run_id ORDER BY MAX(rowid) DESC"
            ).fetchall()
        return [row[0] for row in rows]

    def verify(self, run_id: str) -> dict[str, Any]:
        rows = self.entries(run_id)
        previous = ""
        for expected, entry in enumerate(rows, start=1):
            if entry.event.run_id != run_id or entry.event.sequence != expected:
                raise JournalCorruption("journal identity or sequence mismatch")
            if entry.previous_checksum != previous or entry.checksum != _checksum(entry.event, previous):
                raise JournalCorruption("journal checksum chain is broken")
            previous = entry.checksum
        return {"ok": True, "run_id": run_id, "event_count": len(rows),
                "last_checksum": previous}

    def rebuild(self, *, run_id: str, kind: str = "", **identity: Any) -> Run:
        self.verify(run_id)
        events = [entry.event for entry in self.entries(run_id)]
        with self._lock:
            snapshot = self._db.execute(
                "SELECT sequence, run_json FROM run_snapshots WHERE run_id=?", (run_id,)
            ).fetchone()
            stored = self._db.execute(
                "SELECT identity_json FROM run_identity WHERE run_id=?", (run_id,)
            ).fetchone()
        if snapshot:
            sequence = int(snapshot[0])
            if sequence > len(events):
                raise JournalCorruption("checkpoint is ahead of the journal tip")
            run = _run_from_dict(json.loads(snapshot[1]))
            run.events = list(events[:sequence])
            return replay_run(run, events[sequence:])
        base = json.loads(stored[0]) if stored else {}
        base.update(identity)
        base["id"] = run_id
        base["kind"] = kind or base.get("kind") or "unknown"
        return replay_run(Run(**base), events)

    def export(self, run_id: str) -> dict[str, Any]:
        verified = self.verify(run_id)
        return {"protocol": PROTOCOL_VERSION, "run_id": run_id,
                "event_count": verified["event_count"],
                "last_checksum": verified["last_checksum"],
                "entries": [{"event": row.event.to_dict(),
                             "previous_checksum": row.previous_checksum,
                             "checksum": row.checksum} for row in self.entries(run_id)]}

    def delete(self, run_id: str, *, expected_checksum: str) -> bool:
        with self._lock:
            rows = self.entries(run_id)
            if not rows:
                return False
            actual = self.verify(run_id)["last_checksum"]
            if not expected_checksum or expected_checksum != actual:
                raise ValueError("delete checksum does not match the verified journal tip")
            self._db.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))
            self._db.execute("DELETE FROM run_identity WHERE run_id=?", (run_id,))
            self._db.execute("DELETE FROM run_snapshots WHERE run_id=?", (run_id,))
            self._db.commit()
            return True

    def close(self) -> None:
        with self._lock:
            self._db.close()


class RunControlLedger:
    """Records control intent and acknowledgement; never executes native control."""

    ACTIONS = {"cancel", "retry", "approve", "reject", "pause", "resume"}

    def __init__(self) -> None:
        self._controls: dict[str, RunControl] = {}

    def request(self, *, run_id: str, action: str, requested_by: str = "",
                reason: str = "", control_id: str = "") -> RunControl:
        action = str(action or "").strip().lower()
        if action not in self.ACTIONS:
            raise ValueError(f"unsupported run control action: {action}")
        control = RunControl(id=control_id or str(uuid4()), run_id=run_id,
                             action=action, requested_at=utc_now(),
                             requested_by=requested_by, reason=reason)
        if control.id in self._controls:
            raise ValueError("control id already exists")
        self._controls[control.id] = control
        return control

    def decide(self, control_id: str, *, accepted: bool, acknowledged_by: str,
               reason: str = "") -> RunControl:
        current = self._controls.get(control_id)
        if not current:
            raise KeyError("run control not found")
        if current.status != "requested":
            raise ValueError("run control already decided")
        decided = replace(current, status="acknowledged" if accepted else "rejected",
                          acknowledged_at=utc_now(), acknowledged_by=acknowledged_by,
                          reason=reason or current.reason)
        self._controls[control_id] = decided
        return decided

    def get(self, control_id: str) -> RunControl | None:
        return self._controls.get(control_id)

    def export(self, run_id: str) -> list[dict[str, Any]]:
        return [asdict(control) for control in self._controls.values()
                if control.run_id == run_id]
