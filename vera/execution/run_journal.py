"""Append-only Run event journal and non-authoritative control ledger contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from .run_protocol import PROTOCOL_VERSION, Run, RunControl, RunEvent, replay_run, utc_now


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
    def entries(self, run_id: str) -> list[JournalEntry]: ...
    def export(self, run_id: str) -> dict[str, Any]: ...
    def delete(self, run_id: str, *, expected_checksum: str) -> bool: ...


def _checksum(event: RunEvent, previous: str) -> str:
    body = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(f"{previous}\n{body}".encode("utf-8")).hexdigest()


class MemoryRunJournal:
    """Deterministic reference journal; production persistence remains unselected."""

    def __init__(self) -> None:
        self._entries: dict[str, list[JournalEntry]] = {}
        self._event_ids: dict[str, JournalEntry] = {}

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

    def rebuild(self, *, run_id: str, kind: str, **identity: Any) -> Run:
        self.verify(run_id)
        events = [entry.event for entry in self._entries.get(run_id, [])]
        return replay_run(Run(id=run_id, kind=kind, **identity), events)

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
        return True


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
