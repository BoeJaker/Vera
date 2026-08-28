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
    ModelArtifact, ModelPackage, ModelPackageConflict, _identifier,
    model_package_from_dict)


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
                CREATE TABLE IF NOT EXISTS model_package_activations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ('activate', 'rollback')),
                    alias TEXT NOT NULL,
                    previous_package_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    reverted_operation_id TEXT UNIQUE);
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

    def activate(self, name: str, package_id: str, *, expected_package_id: str = "",
                 operation_id: str) -> "ModelActivationReceipt":
        """Move an alias and append its receipt in one immediate transaction."""
        from .model_package import _identifier
        name = _identifier(name, "alias")
        operation_id = _identifier(operation_id, "operation ID")
        expected_package_id = str(expected_package_id or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._activation_by_operation(conn, operation_id)
            if existing is not None:
                if (existing.kind, existing.alias, existing.previous_package_id,
                        existing.package_id, existing.reverted_operation_id) != (
                            "activate", name, expected_package_id, package_id, ""):
                    raise ModelPackageConflict("operation ID was already used for another request")
                return existing
            if not conn.execute("SELECT 1 FROM model_packages WHERE package_id=?",
                                (package_id,)).fetchone():
                raise KeyError("package is not registered")
            current = self._current_alias(conn, name)
            if current != expected_package_id:
                raise ModelPackageConflict("activation compare-and-set failed")
            if current == package_id:
                raise ModelPackageConflict("package is already active")
            conn.execute("INSERT INTO model_package_aliases VALUES (?,?) "
                         "ON CONFLICT(alias) DO UPDATE SET package_id=excluded.package_id",
                         (name, package_id))
            cursor = conn.execute(
                "INSERT INTO model_package_activations "
                "(operation_id,kind,alias,previous_package_id,package_id,reverted_operation_id) "
                "VALUES (?,?,?,?,?,NULL)",
                (operation_id, "activate", name, current, package_id))
            return ModelActivationReceipt(cursor.lastrowid, operation_id, "activate", name,
                                          current, package_id, "")

    def rollback(self, activation_operation_id: str, *, operation_id: str) -> "ModelActivationReceipt":
        """Reverse one activation iff its alias still points at that activation's target."""
        from .model_package import _identifier
        activation_operation_id = _identifier(activation_operation_id, "activation operation ID")
        operation_id = _identifier(operation_id, "operation ID")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._activation_by_operation(conn, operation_id)
            if existing is not None:
                if (existing.kind != "rollback" or
                        existing.reverted_operation_id != activation_operation_id):
                    raise ModelPackageConflict("operation ID was already used for another request")
                return existing
            activation = self._activation_by_operation(conn, activation_operation_id)
            if activation is None or activation.kind != "activate":
                raise KeyError("activation receipt was not found")
            already = conn.execute(
                "SELECT 1 FROM model_package_activations WHERE reverted_operation_id=?",
                (activation_operation_id,)).fetchone()
            if already:
                raise ModelPackageConflict("activation was already rolled back")
            current = self._current_alias(conn, activation.alias)
            if current != activation.package_id:
                raise ModelPackageConflict("rollback compare-and-set failed")
            if activation.previous_package_id:
                conn.execute("UPDATE model_package_aliases SET package_id=? WHERE alias=?",
                             (activation.previous_package_id, activation.alias))
            else:
                conn.execute("DELETE FROM model_package_aliases WHERE alias=?", (activation.alias,))
            cursor = conn.execute(
                "INSERT INTO model_package_activations "
                "(operation_id,kind,alias,previous_package_id,package_id,reverted_operation_id) "
                "VALUES (?,?,?,?,?,?)",
                (operation_id, "rollback", activation.alias, current,
                 activation.previous_package_id, activation_operation_id))
            return ModelActivationReceipt(
                cursor.lastrowid, operation_id, "rollback", activation.alias,
                current, activation.previous_package_id, activation_operation_id)

    def activation_history(self, name: str = "") -> tuple["ModelActivationReceipt", ...]:
        from .model_package import _identifier
        if name:
            name = _identifier(name, "alias")
        with self._connect() as conn:
            if name:
                rows = conn.execute(
                    "SELECT sequence,operation_id,kind,alias,previous_package_id,package_id,"
                    "COALESCE(reverted_operation_id,'') FROM model_package_activations "
                    "WHERE alias=? ORDER BY sequence", (name,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT sequence,operation_id,kind,alias,previous_package_id,package_id,"
                    "COALESCE(reverted_operation_id,'') FROM model_package_activations "
                    "ORDER BY sequence").fetchall()
        return tuple(self._receipt_from_row(row) for row in rows)

    @staticmethod
    def _current_alias(conn: sqlite3.Connection, name: str) -> str:
        row = conn.execute("SELECT package_id FROM model_package_aliases WHERE alias=?",
                           (name,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def _activation_by_operation(conn: sqlite3.Connection,
                                 operation_id: str) -> "ModelActivationReceipt | None":
        row = conn.execute(
            "SELECT sequence,operation_id,kind,alias,previous_package_id,package_id,"
            "COALESCE(reverted_operation_id,'') FROM model_package_activations "
            "WHERE operation_id=?", (operation_id,)).fetchone()
        return SQLiteModelPackageRegistry._receipt_from_row(row) if row else None

    @staticmethod
    def _receipt_from_row(row) -> "ModelActivationReceipt":
        try:
            return ModelActivationReceipt(*row)
        except (TypeError, ValueError) as exc:
            raise ModelPackageStoreCorrupt("stored activation receipt is corrupt") from exc


@dataclass(frozen=True)
class ModelActivationReceipt:
    sequence: int
    operation_id: str
    kind: str
    alias: str
    previous_package_id: str
    package_id: str
    reverted_operation_id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or int(self.sequence) < 1:
            raise ValueError("activation sequence must be positive")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "operation_id", _identifier(
            self.operation_id, "operation ID"))
        if self.kind not in {"activate", "rollback"}:
            raise ValueError("unsupported activation receipt kind")
        object.__setattr__(self, "alias", _identifier(self.alias, "alias"))
        for field_name in ("previous_package_id", "package_id"):
            value = getattr(self, field_name)
            if value:
                object.__setattr__(self, field_name, _identifier(value, field_name))
        if self.reverted_operation_id:
            object.__setattr__(self, "reverted_operation_id", _identifier(
                self.reverted_operation_id, "reverted operation ID"))
        if self.kind == "activate" and self.reverted_operation_id:
            raise ValueError("activation cannot revert another operation")
        if self.kind == "rollback" and not self.reverted_operation_id:
            raise ValueError("rollback must name the reverted activation")

    def to_dict(self) -> dict:
        return dict(self.__dict__)


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
