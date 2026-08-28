"""Transactional ModelPackage persistence and read-only artifact verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .model_package import (
    ModelArtifact, ModelPackage, ModelPackageConflict, _identifier,
    model_package_from_dict)
from .legacy_binding import LegacyModelCapabilityBinding
from .admission import (
    ModelAdmissionReceipt, ModelDeploymentTarget, ModelPackageAdmissionRejected,
    ModelTrustPolicy, evaluate_model_admission)


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
                CREATE TABLE IF NOT EXISTS legacy_model_capability_bindings (
                    capability TEXT NOT NULL,
                    selector TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(capability, selector),
                    FOREIGN KEY(package_id) REFERENCES model_packages(package_id));
                CREATE TABLE IF NOT EXISTS model_package_admissions (
                    activation_operation_id TEXT PRIMARY KEY,
                    admission_id TEXT NOT NULL UNIQUE,
                    receipt_json TEXT NOT NULL,
                    FOREIGN KEY(activation_operation_id)
                        REFERENCES model_package_activations(operation_id));
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
            return self._activate_in_connection(
                conn, name, package_id, expected_package_id, operation_id)

    def activate_admitted(
            self, name: str, package_id: str, *, expected_package_id: str = "",
            operation_id: str, policy: ModelTrustPolicy,
            target: ModelDeploymentTarget) -> "AdmittedModelActivation":
        """Re-evaluate admission and atomically persist it with activation."""
        from .model_package import _identifier
        name = _identifier(name, "alias")
        operation_id = _identifier(operation_id, "operation ID")
        expected_package_id = str(expected_package_id or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._activation_by_operation(conn, operation_id)
            if existing is not None:
                row = conn.execute(
                    "SELECT receipt_json FROM model_package_admissions "
                    "WHERE activation_operation_id=?", (operation_id,)).fetchone()
                if not row:
                    raise ModelPackageConflict(
                        "operation ID belongs to an activation without admission evidence")
                receipt = self._admission_from_json(row[0])
                candidate = self._stored_package(conn, package_id)
                expected_receipt = evaluate_model_admission(candidate, policy, target)
                if (existing.kind, existing.alias, existing.previous_package_id,
                        existing.package_id, receipt.admission_id) != (
                            "activate", name, expected_package_id, package_id,
                            expected_receipt.admission_id):
                    raise ModelPackageConflict("operation ID was already used for another request")
                return AdmittedModelActivation(existing, receipt)
            package = self._stored_package(conn, package_id)
            receipt = evaluate_model_admission(package, policy, target)
            if not receipt.accepted:
                raise ModelPackageAdmissionRejected(receipt)
            activation = self._activate_in_connection(
                conn, name, package_id, expected_package_id, operation_id)
            encoded = json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO model_package_admissions VALUES (?,?,?)",
                         (operation_id, receipt.admission_id, encoded))
            return AdmittedModelActivation(activation, receipt)

    def _activate_in_connection(self, conn: sqlite3.Connection, name: str,
                                package_id: str, expected_package_id: str,
                                operation_id: str) -> "ModelActivationReceipt":
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

    def admission_for_activation(self, operation_id: str) -> ModelAdmissionReceipt | None:
        operation_id = _identifier(operation_id, "operation ID")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT receipt_json FROM model_package_admissions "
                "WHERE activation_operation_id=?", (operation_id,)).fetchone()
        return self._admission_from_json(row[0]) if row else None

    def admission_history(self) -> tuple[ModelAdmissionReceipt, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.receipt_json FROM model_package_admissions a "
                "JOIN model_package_activations x "
                "ON x.operation_id=a.activation_operation_id ORDER BY x.sequence").fetchall()
        return tuple(self._admission_from_json(row[0]) for row in rows)

    @staticmethod
    def _stored_package(conn: sqlite3.Connection, package_id: str) -> ModelPackage:
        row = conn.execute("SELECT package_json FROM model_packages WHERE package_id=?",
                           (package_id,)).fetchone()
        if not row:
            raise KeyError("package is not registered")
        try:
            return model_package_from_dict(json.loads(row[0]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ModelPackageStoreCorrupt("stored model package is corrupt") from exc

    @staticmethod
    def _admission_from_json(encoded: str) -> ModelAdmissionReceipt:
        try:
            value = json.loads(encoded)
            policy = ModelTrustPolicy(**value["policy"])
            target = ModelDeploymentTarget(**value["target"])
            receipt = ModelAdmissionReceipt(
                value["package_id"], policy, target, tuple(value["reasons"]))
            if value.get("schema") != receipt.schema or value.get("admission_id") != receipt.admission_id:
                raise ValueError("admission identity mismatch")
            return receipt
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelPackageStoreCorrupt("stored model admission is corrupt") from exc

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

    def bind_legacy_capability(
            self, binding: LegacyModelCapabilityBinding, *,
            expected_package_id: str = "") -> LegacyModelCapabilityBinding:
        """CAS-bind one legacy invocation identity without invoking it."""
        if not isinstance(binding, LegacyModelCapabilityBinding):
            raise TypeError("binding must be LegacyModelCapabilityBinding")
        self.bind_legacy_capabilities(
            (binding,), expected_package_ids={
                binding.legacy_identity: expected_package_id})
        return binding

    def bind_legacy_capabilities(
            self, bindings: Sequence[LegacyModelCapabilityBinding], *,
            expected_package_ids: Mapping[str, str] | None = None,
            ) -> tuple[LegacyModelCapabilityBinding, ...]:
        """CAS-bind a unique group atomically, so migrations cannot be partial."""
        bindings = tuple(bindings)
        if not bindings or not all(isinstance(item, LegacyModelCapabilityBinding)
                                   for item in bindings):
            raise TypeError("bindings must contain LegacyModelCapabilityBinding values")
        keys = tuple((item.capability, item.selector) for item in bindings)
        if len(set(keys)) != len(keys):
            raise ValueError("legacy binding group contains duplicate identities")
        expected = {str(key): str(value or "")
                    for key, value in dict(expected_package_ids or {}).items()}
        identities = {item.legacy_identity for item in bindings}
        if set(expected) - identities:
            raise ValueError("expected package IDs contain an unknown legacy identity")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            decisions = []
            for binding in bindings:
                if not conn.execute("SELECT 1 FROM model_packages WHERE package_id=?",
                                    (binding.package_id,)).fetchone():
                    raise KeyError("package is not registered")
                row = conn.execute(
                    "SELECT package_id,source FROM legacy_model_capability_bindings "
                    "WHERE capability=? AND selector=?",
                    (binding.capability, binding.selector)).fetchone()
                if row == (binding.package_id, binding.source):
                    decisions.append((binding, False))
                    continue
                current = row[0] if row else ""
                if current != expected.get(binding.legacy_identity, ""):
                    raise ModelPackageConflict("legacy binding compare-and-set failed")
                decisions.append((binding, True))
            for binding, should_write in decisions:
                if should_write:
                    conn.execute(
                        "INSERT INTO legacy_model_capability_bindings VALUES (?,?,?,?) "
                        "ON CONFLICT(capability,selector) DO UPDATE SET "
                        "package_id=excluded.package_id,source=excluded.source",
                        (binding.capability, binding.selector,
                         binding.package_id, binding.source))
        return bindings

    def resolve_legacy_capability(
            self, capability: str, selector: str = "") -> LegacyModelCapabilityBinding | None:
        capability = _identifier(capability, "legacy capability")
        if selector:
            selector = _identifier(selector, "legacy selector")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT capability,selector,package_id,source "
                "FROM legacy_model_capability_bindings WHERE capability=? AND selector=?",
                (capability, selector)).fetchone()
        return self._legacy_binding_from_row(row) if row else None

    def legacy_capability_bindings(self) -> tuple[LegacyModelCapabilityBinding, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT capability,selector,package_id,source "
                "FROM legacy_model_capability_bindings ORDER BY capability,selector").fetchall()
        return tuple(self._legacy_binding_from_row(row) for row in rows)

    @staticmethod
    def _legacy_binding_from_row(row) -> LegacyModelCapabilityBinding:
        try:
            return LegacyModelCapabilityBinding(*row)
        except (TypeError, ValueError) as exc:
            raise ModelPackageStoreCorrupt("stored legacy capability binding is corrupt") from exc

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
class AdmittedModelActivation:
    activation: ModelActivationReceipt
    admission: ModelAdmissionReceipt

    def to_dict(self) -> dict:
        return {"activation": self.activation.to_dict(),
                "admission": self.admission.to_dict()}


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
