"""Checksum-addressed artifact provider contract and local implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Protocol


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
    return value


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
                if retain_until and (not stat.retain_until or
                        _instant(retain_until) > _instant(stat.retain_until)):
                    conn.execute(
                        "UPDATE artifacts SET retain_until=? WHERE artifact_id=?",
                        (retain_until, artifact_id))
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
        return dict(row)
