"""Transactional ModelPackage persistence and read-only artifact verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable
from urllib.parse import unquote, urlparse

from .model_package import (
    ModelArtifact, ModelPackage, ModelPackageConflict, model_package_from_dict)


MAX_VERIFY_BYTES = 100 * 1024 * 1024 * 1024


class ModelPackageStoreCorrupt(RuntimeError):
    pass


class SQLiteModelPackageRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS model_packages (
                    package_id TEXT PRIMARY KEY, package_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_package_aliases (
                    alias TEXT PRIMARY KEY, package_id TEXT NOT NULL,
                    FOREIGN KEY(package_id) REFERENCES model_packages(package_id));
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def register(self, package: ModelPackage) -> ModelPackage:
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        encoded = json.dumps(package.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            current = conn.execute(
                "SELECT package_json FROM model_packages WHERE package_id=?",
                (package.package_id,)).fetchone()
            if current and current[0] != encoded:
                raise ModelPackageConflict("package identity collision")
            conn.execute("INSERT OR IGNORE INTO model_packages VALUES (?,?)",
                         (package.package_id, encoded))
        return package

    def get(self, identity: str) -> ModelPackage | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.package_json FROM model_packages p WHERE p.package_id=? "
                "UNION ALL SELECT p.package_json FROM model_package_aliases a "
                "JOIN model_packages p USING(package_id) WHERE a.alias=? LIMIT 1",
                (identity, identity)).fetchone()
        if not row:
            return None
        try:
            return model_package_from_dict(json.loads(row[0]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ModelPackageStoreCorrupt("stored model package is corrupt") from exc

    def list(self) -> tuple[ModelPackage, ...]:
        with self._connect() as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT package_id FROM model_packages ORDER BY package_id")]
        return tuple(self.get(package_id) for package_id in ids)  # type: ignore[arg-type]

    def alias(self, name: str, package_id: str, *, expected_package_id: str = "") -> None:
        from .model_package import _identifier
        name = _identifier(name, "alias")
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM model_packages WHERE package_id=?",
                                (package_id,)).fetchone():
                raise KeyError("package is not registered")
            row = conn.execute("SELECT package_id FROM model_package_aliases WHERE alias=?",
                               (name,)).fetchone()
            current = row[0] if row else ""
            if current != expected_package_id:
                raise ModelPackageConflict("alias compare-and-set failed")
            conn.execute("INSERT INTO model_package_aliases VALUES (?,?) "
                         "ON CONFLICT(alias) DO UPDATE SET package_id=excluded.package_id",
                         (name, package_id))


@dataclass(frozen=True)
class ArtifactVerificationReceipt:
    package_id: str
    role: str
    uri: str
    status: str
    expected_sha256: str
    observed_sha256: str
    expected_size_bytes: int
    observed_size_bytes: int

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict:
        return {**self.__dict__, "verified": self.verified}


def _local_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            return None
        return Path(unquote(parsed.path))
    if parsed.scheme:
        return None
    return Path(uri)


def verify_local_artifact(package_id: str, artifact: ModelArtifact, *,
                          max_bytes: int = MAX_VERIFY_BYTES,
                          cancelled: Callable[[], bool] | None = None) -> ArtifactVerificationReceipt:
    if not isinstance(artifact, ModelArtifact):
        raise TypeError("artifact must be ModelArtifact")
    max_bytes = int(max_bytes)
    if max_bytes < 1 or max_bytes > MAX_VERIFY_BYTES:
        raise ValueError("max_bytes is outside the supported verification limit")
    path = _local_path(artifact.uri)
    status, observed_size, observed_hash = "unsupported_uri", -1, ""
    if path is not None:
        try:
            if cancelled and cancelled():
                return ArtifactVerificationReceipt(
                    package_id=package_id, role=artifact.role, uri=artifact.uri,
                    status="cancelled", expected_sha256=artifact.sha256,
                    observed_sha256="", expected_size_bytes=artifact.size_bytes,
                    observed_size_bytes=-1)
            stat = path.stat()
            observed_size = stat.st_size
            if not path.is_file():
                status = "missing"
            elif observed_size > max_bytes:
                status = "limit_exceeded"
            elif observed_size != artifact.size_bytes:
                status = "size_mismatch"
            else:
                digest = hashlib.sha256()
                status = "verified"
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        if cancelled and cancelled():
                            status = "cancelled"
                            break
                        digest.update(chunk)
                if status != "cancelled":
                    observed_hash = digest.hexdigest()
                    if observed_hash != artifact.sha256:
                        status = "hash_mismatch"
        except (FileNotFoundError, OSError):
            status = "missing"
    return ArtifactVerificationReceipt(
        package_id=package_id, role=artifact.role, uri=artifact.uri, status=status,
        expected_sha256=artifact.sha256, observed_sha256=observed_hash,
        expected_size_bytes=artifact.size_bytes, observed_size_bytes=observed_size)
