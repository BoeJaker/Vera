"""Checksum-addressed artifact provider contract and local implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Protocol


_ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{64}$")
_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True)
class ArtifactStat:
    artifact_id: str
    checksum: str
    size: int
    media_type: str
    created_at: str
    retain_until: str


class ArtifactProvider(Protocol):
    def put(self, data: bytes, *, media_type: str, created_at: str,
            retain_until: str = "") -> ArtifactStat: ...
    def stat(self, artifact_id: str) -> ArtifactStat: ...
    def get(self, artifact_id: str, *, max_bytes: int | None = None) -> bytes: ...
    def verify(self, artifact_id: str) -> bool: ...
    def reference(self, artifact_id: str, reference_id: str, *,
                  created_at: str) -> dict: ...


class ArtifactReplicaBackend(Protocol):
    name: str
    def put(self, key: str, data: bytes, media_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...


def _time(value: str, field: str, optional: bool = False) -> str:
    value = str(value or "").strip()
    if optional and not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LocalArtifactProvider:
    def __init__(self, root: str | Path, *, max_put_bytes: int = 64 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.objects = self.root / "objects"
        self.max_put_bytes = max(1, int(max_put_bytes))
        self.objects.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "artifacts.db"
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                    size INTEGER NOT NULL, media_type TEXT NOT NULL,
                    created_at TEXT NOT NULL, retain_until TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_references (
                    reference_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL
                        REFERENCES artifacts(artifact_id), created_at TEXT NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _path(self, artifact_id: str) -> Path:
        if not _ARTIFACT_ID.fullmatch(str(artifact_id or "")):
            raise ValueError("invalid artifact_id")
        digest = artifact_id[4:]
        return self.objects / digest[:2] / digest[2:4] / digest

    @staticmethod
    def _stat(row: sqlite3.Row) -> ArtifactStat:
        return ArtifactStat(**dict(row))

    def put(self, data: bytes, *, media_type: str, created_at: str,
            retain_until: str = "") -> ArtifactStat:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > self.max_put_bytes:
            raise ValueError("artifact exceeds configured size limit")
        media_type = str(media_type or "").strip().lower()
        if not media_type or len(media_type) > 255:
            raise ValueError("invalid media_type")
        created_at = _time(created_at, "created_at")
        retain_until = _time(retain_until, "retain_until", optional=True)
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = "art_" + digest
        target = self._path(artifact_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.objects.resolve()):
            raise OSError("artifact object path escapes provider root")
        if target.exists():
            if target.stat().st_size != len(data) or hashlib.sha256(
                    target.read_bytes()).hexdigest() != digest:
                raise OSError("artifact object is corrupt")
        else:
            fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if existing:
                stat = self._stat(existing)
                if stat.media_type != media_type:
                    raise ValueError("artifact replay media_type differs")
                if retain_until:
                    conn.execute(
                        "UPDATE artifacts SET retain_until=CASE "
                        "WHEN retain_until='' OR retain_until<? THEN ? "
                        "ELSE retain_until END WHERE artifact_id=?",
                        (retain_until, retain_until, artifact_id))
                    existing = conn.execute(
                        "SELECT * FROM artifacts WHERE artifact_id=?",
                        (artifact_id,)).fetchone()
                return self._stat(existing)
            conn.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                (artifact_id, "sha256:" + digest, len(data), media_type,
                 created_at, retain_until))
        return self.stat(artifact_id)

    def stat(self, artifact_id: str) -> ArtifactStat:
        self._path(artifact_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            raise KeyError("artifact not found")
        return self._stat(row)

    def get(self, artifact_id: str, *, max_bytes: int | None = None) -> bytes:
        stat = self.stat(artifact_id)
        if max_bytes is not None and stat.size > max(0, int(max_bytes)):
            raise ValueError("artifact exceeds read limit")
        try:
            return self._path(artifact_id).read_bytes()
        except FileNotFoundError as exc:
            raise OSError("artifact object is missing") from exc

    def verify(self, artifact_id: str) -> bool:
        stat = self.stat(artifact_id)
        try:
            data = self.get(artifact_id, max_bytes=stat.size)
        except OSError:
            return False
        return len(data) == stat.size and (
            "sha256:" + hashlib.sha256(data).hexdigest()) == stat.checksum

    def reference(self, artifact_id: str, reference_id: str, *,
                  created_at: str) -> dict:
        self.stat(artifact_id)
        reference_id = str(reference_id or "").strip()
        if not _REFERENCE_ID.fullmatch(reference_id):
            raise ValueError("invalid reference_id")
        created_at = _time(created_at, "created_at")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM artifact_references WHERE reference_id=?",
                (reference_id,)).fetchone()
            if existing and existing["artifact_id"] != artifact_id:
                raise ValueError("reference replay targets another artifact")
            conn.execute(
                "INSERT OR IGNORE INTO artifact_references VALUES (?,?,?)",
                (reference_id, artifact_id, created_at))
            row = conn.execute(
                "SELECT * FROM artifact_references WHERE reference_id=?",
                (reference_id,)).fetchone()
            if row["artifact_id"] != artifact_id:
                raise ValueError("reference replay targets another artifact")
        return dict(row)


class ObjectStoreArtifactBackend:
    """Strict adapter over Vera's existing ObjectStore duck type."""

    name = "object_store"

    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def key(artifact_id: str) -> str:
        if not _ARTIFACT_ID.fullmatch(str(artifact_id or "")):
            raise ValueError("invalid artifact_id")
        digest = artifact_id[4:]
        return f"artifacts/sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def put(self, key: str, data: bytes, media_type: str) -> None:
        if not self.store.put(key, data, content_type=media_type):
            raise OSError(str(getattr(self.store, "last_error", "") or
                              "object store put failed"))

    def get(self, key: str) -> bytes:
        value = self.store.get(key)
        if not isinstance(value, bytes):
            raise OSError(str(getattr(self.store, "last_error", "") or
                              "object store get failed"))
        return value


class ReplicatedArtifactProvider:
    """Local authority with durable, retryable replication receipts."""

    def __init__(self, local: LocalArtifactProvider, backend: ArtifactReplicaBackend):
        self.local = local
        self.backend = backend
        with self.local._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifact_replica_receipts (
                    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                    backend TEXT NOT NULL, state TEXT NOT NULL,
                    attempt INTEGER NOT NULL, error_code TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, backend))
            """)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        code = re.sub(r"[^a-z0-9._-]", "_", type(exc).__name__.lower())[:128]
        return code or "replica_failed"

    @staticmethod
    def _key(artifact_id: str) -> str:
        return ObjectStoreArtifactBackend.key(artifact_id)

    def _receipt(self, artifact_id: str) -> dict:
        with self.local._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_replica_receipts "
                "WHERE artifact_id=? AND backend=?",
                (artifact_id, self.backend.name)).fetchone()
        if not row:
            raise KeyError("replica receipt not found")
        return dict(row)

    def _record(self, artifact_id: str, state: str, *, updated_at: str,
                error_code: str = "", increment: bool = False) -> dict:
        if state not in {"applied", "failed", "rebuilding"}:
            raise ValueError("invalid replica receipt state")
        with self.local._connect() as conn:
            conn.execute(
                "INSERT INTO artifact_replica_receipts "
                "(artifact_id,backend,state,attempt,error_code,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(artifact_id,backend) DO UPDATE SET "
                "state=excluded.state,attempt=artifact_replica_receipts.attempt+?,"
                "error_code=excluded.error_code,updated_at=excluded.updated_at",
                (artifact_id, self.backend.name, state, int(increment), error_code,
                 updated_at, int(increment)))
        return self._receipt(artifact_id)

    def put(self, data: bytes, *, media_type: str, created_at: str,
            retain_until: str = "") -> dict:
        stat = self.local.put(data, media_type=media_type, created_at=created_at,
                              retain_until=retain_until)
        try:
            self.backend.put(self._key(stat.artifact_id), data, stat.media_type)
            receipt = self._record(
                stat.artifact_id, "applied", updated_at=created_at, increment=True)
        except Exception as exc:
            receipt = self._record(
                stat.artifact_id, "failed", updated_at=created_at,
                error_code=self._error_code(exc), increment=True)
        return {"artifact": stat, "replica_receipt": receipt}

    def reconcile(self, *, updated_at: str, limit: int = 25) -> list[dict]:
        updated_at = _time(updated_at, "updated_at")
        limit = max(1, min(int(limit), 100))
        with self.local._connect() as conn:
            rows = conn.execute(
                "SELECT artifact_id FROM artifact_replica_receipts "
                "WHERE backend=? AND state IN ('failed','rebuilding') "
                "ORDER BY updated_at,artifact_id LIMIT ?",
                (self.backend.name, limit)).fetchall()
        results = []
        for row in rows:
            artifact_id = row["artifact_id"]
            try:
                self._record(artifact_id, "rebuilding", updated_at=updated_at,
                             increment=True)
                stat = self.local.stat(artifact_id)
                if not self.local.verify(artifact_id):
                    raise OSError("local authority object failed verification")
                data = self.local.get(artifact_id, max_bytes=stat.size)
                self.backend.put(self._key(artifact_id), data, stat.media_type)
                receipt = self._record(artifact_id, "applied", updated_at=updated_at)
            except Exception as exc:
                receipt = self._record(
                    artifact_id, "failed", updated_at=updated_at,
                    error_code=self._error_code(exc))
            results.append(receipt)
        return results

    def restore_local(self, artifact_id: str, *, created_at: str) -> ArtifactStat:
        _time(created_at, "created_at")
        stat = self.local.stat(artifact_id)
        if self.local.verify(artifact_id):
            return stat
        data = self.backend.get(self._key(artifact_id))
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest != stat.checksum or len(data) != stat.size:
            raise OSError("replica checksum mismatch")
        target = self.local._path(artifact_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.local.objects.resolve()):
            raise OSError("artifact object path escapes provider root")
        fd, temporary = tempfile.mkstemp(prefix=".partial-restore-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        if not self.local.verify(artifact_id):
            raise OSError("restored artifact verification failed")
        return self.local.stat(artifact_id)
