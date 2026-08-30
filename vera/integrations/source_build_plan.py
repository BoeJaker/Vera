"""Deterministic W3-07 external-source build/activation proposals; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse


SOURCE_BUILD_PLAN_SCHEMA = "vera.external-source-build-plan/v1"
SUPPORTED_SOURCE_KINDS = ("cli", "oci", "python", "repository")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}\Z")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9._+-]{0,127}\Z")
_SECRET_KEY_PARTS = frozenset({
    "access_token", "api_key", "api_token", "apikey", "credential",
    "credentials", "password", "passwd", "private_key", "refresh_token",
    "secret", "token",
})
_EVIDENCE = (
    "approval_receipt", "conformance_report", "license_review",
    "malware_scan", "manifest_review", "policy_decision", "rollback_proof",
    "sbom", "teardown_proof", "vulnerability_scan",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _text(value: Any, name: str, maximum: int = 512) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _identifier(value: Any, name: str) -> str:
    value = _text(value, name, 256)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _looks_secret_key(value: Any) -> bool:
    key = _normal_key(value)
    return any(key == part or key.endswith("_" + part)
               for part in _SECRET_KEY_PARTS)


def _bounded_json(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("source build descriptor nesting exceeds 8 levels")
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise ValueError("source build object exceeds 100 fields")
        for key, item in value.items():
            if len(str(key)) > 128:
                raise ValueError("source build field name exceeds 128 characters")
            if (_looks_secret_key(key) and item is not None
                    and (not isinstance(item, str) or item.strip())):
                raise ValueError("source build descriptors must not contain plaintext credentials")
            _bounded_json(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise ValueError("source build array exceeds 100 items")
        for item in value:
            _bounded_json(item, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError("source build descriptors must contain JSON values")


def _digest(value: Any, name: str = "digest") -> str:
    value = _text(value, name, 72).casefold()
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be an immutable sha256 digest")
    return value


def _https_url(value: Any, name: str) -> str:
    value = _text(value, name, 1024)
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(
            f"{name} must be credential-free HTTPS without a query or fragment")
    return value


def _source_pin(kind: str, provenance: Mapping[str, Any]) -> dict[str, str]:
    if kind == "python":
        package = _text(provenance.get("package"), "Python package", 128)
        version = _text(provenance.get("version"), "Python version", 128)
        if not _PACKAGE.fullmatch(package) or not _VERSION.fullmatch(version):
            raise ValueError("Python sources require an exact package and version")
        return {"package": package, "version": version,
                "artifact_digest": _digest(provenance.get("artifact_digest"),
                                           "Python artifact digest")}
    if kind == "cli":
        artifact = _identifier(provenance.get("artifact"), "CLI artifact")
        version = _text(provenance.get("version"), "CLI version", 128)
        if not _VERSION.fullmatch(version):
            raise ValueError("CLI sources require an exact version")
        return {"artifact": artifact, "version": version,
                "artifact_digest": _digest(provenance.get("artifact_digest"),
                                           "CLI artifact digest")}
    if kind == "oci":
        image = _text(provenance.get("image"), "OCI image", 512)
        match = re.fullmatch(r"([^\s@]+)@(sha256:[0-9a-fA-F]{64})", image)
        if not match:
            raise ValueError("OCI sources must use an image@sha256 digest reference")
        return {"image": match.group(1), "manifest_digest": match.group(2).casefold()}
    url = _https_url(provenance.get("url"), "repository URL")
    revision = _text(provenance.get("revision"), "repository revision", 64).casefold()
    if not _REVISION.fullmatch(revision):
        raise ValueError("repository sources require a full 40- or 64-hex revision")
    return {"url": url, "revision": revision,
            "archive_digest": _digest(provenance.get("archive_digest"),
                                      "repository archive digest")}


def _string_set(value: Any, name: str, *, maximum: int = 30) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty bounded array")
    return tuple(sorted({_identifier(item, name) for item in value}))


def _manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("manifest must be an object")
    entrypoints = _string_set(value.get("entrypoints"), "manifest entrypoint")
    effects = _string_set(value.get("effects"), "manifest effect")
    if "none" in effects and len(effects) != 1:
        raise ValueError("effect 'none' cannot be combined with other effects")
    license_id = _identifier(value.get("license"), "manifest license")
    resources = value.get("resources")
    if not isinstance(resources, Mapping):
        raise ValueError("manifest resources must be an object")
    cpu = _text(resources.get("cpu"), "CPU class", 32)
    accelerator = _text(resources.get("accelerator", "none"), "accelerator", 32)
    memory_mb = resources.get("memory_mb")
    if (not isinstance(memory_mb, int) or isinstance(memory_mb, bool)
            or memory_mb < 16 or memory_mb > 1048576):
        raise ValueError("manifest memory_mb must be an integer from 16 to 1048576")
    secret_refs = value.get("secret_refs") or []
    if not isinstance(secret_refs, (list, tuple)) or len(secret_refs) > 30:
        raise ValueError("manifest secret_refs must be a bounded array")
    refs = []
    for item in secret_refs:
        item = _text(item, "secret reference", 256)
        if not item.startswith("secretref:") or not _IDENTIFIER.fullmatch(item[10:]):
            raise ValueError("secrets must be opaque secretref: references")
        refs.append(item)
    network = value.get("network") or {"mode": "deny", "allowlist": []}
    if not isinstance(network, Mapping):
        raise ValueError("manifest network must be an object")
    mode = str(network.get("mode") or "deny").strip()
    if mode not in {"deny", "allowlist"}:
        raise ValueError("network mode must be deny or allowlist")
    raw_allowlist = network.get("allowlist") or []
    if not isinstance(raw_allowlist, (list, tuple)) or len(raw_allowlist) > 30:
        raise ValueError("network allowlist must be a bounded array")
    allowlist = tuple(sorted({_https_url(item, "network allowlist origin")
                              for item in raw_allowlist}))
    if mode == "deny" and allowlist:
        raise ValueError("deny network mode cannot include an allowlist")
    if mode == "allowlist" and not allowlist:
        raise ValueError("allowlist network mode requires at least one HTTPS origin")
    return {
        "entrypoints": list(entrypoints), "license": license_id,
        "effects": list(effects),
        "resources": {"cpu": cpu, "memory_mb": memory_mb,
                      "accelerator": accelerator},
        "secret_refs": sorted(set(refs)),
        "network": {"mode": mode, "allowlist": list(allowlist)},
    }


@dataclass(frozen=True)
class SourceBuildPlan:
    plan_id: str
    source_id: str
    source_kind: str
    provenance: Mapping[str, str]
    manifest: Mapping[str, Any]
    descriptor_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        stages = (
            ("materialize", ("sbom",)),
            ("scan", ("license_review", "malware_scan", "vulnerability_scan")),
            ("review", ("manifest_review", "policy_decision")),
            ("verify", ("conformance_report", "rollback_proof", "teardown_proof")),
            ("approve", ("approval_receipt",)),
            ("activate", ()), ("export", ()), ("upgrade", ()),
            ("rollback", ()), ("teardown", ()),
        )
        return {
            "schema": SOURCE_BUILD_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "descriptor_fingerprint": self.descriptor_fingerprint,
            "provenance": {**self.provenance, "verified": False},
            "manifest": dict(self.manifest),
            "lifecycle": {"current": "proposed", "next": "built"},
            "stages": [
                {"id": stage, "status": "queued", "required_evidence": list(evidence)}
                for stage, evidence in stages
            ],
            "required_evidence": list(_EVIDENCE),
            "sandbox": {"required": True, "created": False,
                        "least_privilege_applied": False},
            "approval": {"required": True, "granted": False,
                         "scope": "activation"},
            "rollback": {"required_before_activation": True, "verified": False},
            "export": {"planned": True, "performed": False},
            "upgrade": {"requires_new_plan": True, "performed": False},
            "teardown": {"required": True, "verified": False, "performed": False},
            "accepted": True,
            "ready_for_build": False,
            "ready_for_activation": False,
            "provenance_verified": False,
            "evidence_verified": False,
            "approved": False,
            "credentials_resolved": False,
            "network_io": False,
            "fetches": False,
            "installs": False,
            "builds": False,
            "activates": False,
            "registers": False,
            "executes": False,
        }


def plan_source_build(document: Mapping[str, Any]) -> SourceBuildPlan:
    """Validate one inline descriptor and return an inert, reproducible proposal."""
    if not isinstance(document, Mapping):
        raise TypeError("source build descriptor must be an object")
    _bounded_json(document)
    encoded = _canonical(document)
    if len(encoded.encode()) > 65536:
        raise ValueError("source build descriptor exceeds 65536 bytes")
    source_id = _identifier(document.get("source_id"), "source_id")
    kind = str(document.get("kind") or "").strip().casefold()
    if kind not in SUPPORTED_SOURCE_KINDS:
        raise ValueError("kind must be one of: " + ", ".join(SUPPORTED_SOURCE_KINDS))
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("provenance must be an object")
    pin = _source_pin(kind, provenance)
    manifest = _manifest(document.get("manifest"))
    fingerprint = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    identity = {"schema": SOURCE_BUILD_PLAN_SCHEMA, "source_id": source_id,
                "source_kind": kind, "provenance": pin,
                "manifest": manifest, "descriptor_fingerprint": fingerprint}
    plan_id = "sourceplan_" + hashlib.sha256(_canonical(identity).encode()).hexdigest()
    return SourceBuildPlan(plan_id, source_id, kind, pin, manifest, fingerprint)


def build_plan_contract() -> dict[str, Any]:
    return {
        "schema": SOURCE_BUILD_PLAN_SCHEMA,
        "supported_kinds": list(SUPPORTED_SOURCE_KINDS),
        "proposal_contract": "implemented",
        "build_execution": "queued_live",
        "activation_execution": "queued_live",
        "required_evidence": list(_EVIDENCE),
        "network_io": False, "fetches": False, "installs": False,
        "builds": False, "activates": False, "executes": False,
    }
