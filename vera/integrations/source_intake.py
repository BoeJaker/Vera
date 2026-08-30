"""Inspection-only external source intake contracts; this module performs no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse


SOURCE_INTAKE_SCHEMA = "vera.external-source-intake/v1"
LIFECYCLE = (
    "discovered", "inspected", "proposed", "built", "verified",
    "approved", "active", "deprecated", "removed",
)
_OPERATIONS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})
_ID = re.compile(r"[a-z][a-z0-9._-]{1,95}\Z")
_SECRET_KEYS = frozenset({
    "access_token", "api_key", "apikey", "authorization", "credential",
    "credentials", "password", "refresh_token", "secret", "token",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _slug(value: Any, name: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").casefold()).strip("-.")
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} must form a bounded lowercase identifier")
    return value


def _text(value: Any, name: str, maximum: int = 1024) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _remote_slug(value: Any) -> str:
    """Normalize ecosystem operation names without requiring them to start with a letter."""
    original = _text(value, "remote operation ID", 512)
    value = re.sub(r"[^a-z0-9._-]+", "-", original.casefold()).strip("-.")
    if not value:
        raise ValueError("remote operation ID must form an identifier")
    if not value[0].isalpha():
        value = "op-" + value
    if len(value) > 56:
        value = value[:43].rstrip("-.") + "-" + hashlib.sha256(
            original.encode()).hexdigest()[:12]
    if not _ID.fullmatch(value):
        raise ValueError("remote operation ID must form a bounded identifier")
    return value


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _secret_key(value: Any) -> bool:
    key = _key(value)
    return (key in _SECRET_KEYS or key.endswith((
        "_access_token", "_api_key", "_api_token", "_authorization",
        "_credential", "_credentials", "_password", "_refresh_token",
        "_secret", "_token")))


def _bounded_json(value: Any, *, depth: int = 0) -> None:
    if depth > 9:
        raise ValueError("source document nesting exceeds 9 levels")
    if isinstance(value, Mapping):
        if len(value) > 500:
            raise ValueError("source object exceeds 500 fields")
        for key, item in value.items():
            if len(str(key)) > 256:
                raise ValueError("source field name exceeds 256 characters")
            if (_secret_key(key) and isinstance(item, str) and item.strip()
                    and not item.startswith("secretref:")):
                raise ValueError("source documents must not contain plaintext credentials")
            _bounded_json(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise ValueError("source array exceeds 500 items")
        for item in value:
            _bounded_json(item, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError("source documents must contain JSON values")


def _https_origin(value: Any, name: str) -> str:
    parsed = urlparse(_text(value, name, 2048))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{name} must be a credential-free HTTPS URL")
    origin = f"https://{parsed.hostname}"
    if parsed.port:
        origin += f":{parsed.port}"
    return origin


@dataclass(frozen=True)
class SourceCandidate:
    candidate_id: str
    canonical_task: str
    label: str
    input_schema_status: str
    effects_status: str = "unknown"
    authorized: bool = False
    executable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _slug(self.candidate_id, "candidate ID"))
        object.__setattr__(self, "canonical_task", _text(
            self.canonical_task, "canonical task", 256))
        object.__setattr__(self, "label", _text(self.label, "candidate label", 256))
        if self.input_schema_status not in {"declared", "missing", "partial"}:
            raise ValueError("unsupported input schema status")
        if self.effects_status != "unknown" or self.authorized or self.executable:
            raise ValueError("intake candidates cannot infer effects, authorize, or execute")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SourceInspection:
    source_id: str
    kind: str
    document_fingerprint: str
    title: str
    version: str
    endpoint_origins: tuple[str, ...]
    candidates: tuple[SourceCandidate, ...]
    issues: tuple[str, ...]
    metadata: tuple[tuple[str, Any], ...]
    schema: str = SOURCE_INTAKE_SCHEMA
    inspection_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _slug(self.source_id, "source ID"))
        if self.kind not in {"mcp", "openapi"}:
            raise ValueError("source kind must be mcp or openapi")
        object.__setattr__(self, "title", _text(self.title, "source title", 256))
        object.__setattr__(self, "version", _text(self.version, "source version", 128))
        candidates = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ValueError("source candidate IDs must be unique")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "endpoint_origins", tuple(sorted(set(self.endpoint_origins))))
        object.__setattr__(self, "issues", tuple(sorted(set(self.issues))))
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))
        object.__setattr__(self, "inspection_id", "srcinspect_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode()).hexdigest())

    @property
    def accepted(self) -> bool:
        return bool(self.candidates) and not self.issues

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "source_id": self.source_id, "kind": self.kind,
            "document_fingerprint": self.document_fingerprint,
            "title": self.title, "version": self.version,
            "endpoint_origins": list(self.endpoint_origins),
            "candidates": [item.to_dict() for item in self.candidates],
            "issues": list(self.issues), "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspection_id": self.inspection_id, **self.identity_dict(),
            "accepted": self.accepted,
            "lifecycle_state": "proposed" if self.accepted else "inspected",
            "next_state": "built" if self.accepted else "",
            "next_lane": "queued_w3_07" if self.accepted else "remediate",
            "registers": False, "installs": False, "builds": False,
            "activates": False, "uses_secrets": False, "network_io": False,
            "executes": False,
        }


def _candidate(source_id: str, remote_id: Any, label: Any, schema_status: str) -> SourceCandidate:
    remote = _remote_slug(remote_id)
    candidate_id = f"{source_id}.{remote}"
    if len(candidate_id) > 95:
        candidate_id = (source_id[:32].rstrip("-.") + "." + remote[:45].rstrip("-.")
                        + "-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:12])
    return SourceCandidate(
        candidate_id=candidate_id,
        canonical_task=f"external.{source_id}/{remote}",
        label=_text(label or remote, "operation label", 256),
        input_schema_status=schema_status)


def inspect_mcp_source(document: Mapping[str, Any]) -> SourceInspection:
    """Inspect an inline MCP descriptor without spawning or contacting its server."""
    if not isinstance(document, Mapping):
        raise TypeError("MCP descriptor must be an object")
    _bounded_json(document)
    encoded = _canonical(document)
    if len(encoded.encode()) > 131072:
        raise ValueError("MCP descriptor exceeds 131072 bytes")
    source_id = _slug(document.get("id"), "MCP source ID")
    transport = _text(document.get("transport"), "MCP transport", 64).casefold()
    if transport not in {"stdio", "sse", "http", "streamable_http", "vera_proxy"}:
        raise ValueError("unsupported MCP transport")
    origins: tuple[str, ...] = ()
    issues: list[str] = []
    if transport == "stdio":
        _text(document.get("command"), "MCP command", 512)
        if document.get("url"):
            issues.append("stdio_descriptor_must_not_declare_url")
    else:
        origins = (_https_origin(document.get("url"), "MCP URL"),)
        if document.get("command"):
            issues.append("network_descriptor_must_not_declare_command")
    tools = document.get("tools") or []
    if not isinstance(tools, list):
        raise ValueError("MCP tools must be an array")
    if len(tools) > 250:
        raise ValueError("MCP descriptor exceeds 250 tools")
    candidates = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValueError("MCP tool must be an object")
        schema = tool.get("inputSchema")
        status = "declared" if isinstance(schema, Mapping) else "missing"
        candidates.append(_candidate(source_id, tool.get("name"),
                                     tool.get("description") or tool.get("name"), status))
    if not candidates:
        issues.append("no_tools_declared")
    return SourceInspection(
        source_id=source_id, kind="mcp",
        document_fingerprint="sha256:" + hashlib.sha256(encoded.encode()).hexdigest(),
        title=_text(document.get("label") or source_id, "MCP label", 256),
        version=_text(document.get("protocol_version") or "unknown", "MCP version", 128),
        endpoint_origins=origins, candidates=tuple(candidates), issues=tuple(issues),
        metadata=(("candidate_count", len(candidates)), ("transport", transport)))


def inspect_openapi_source(document: Mapping[str, Any], *, source_id: str = "") -> SourceInspection:
    """Inspect one inline OpenAPI document without resolving URLs or external refs."""
    if not isinstance(document, Mapping):
        raise TypeError("OpenAPI document must be an object")
    _bounded_json(document)
    encoded = _canonical(document)
    if len(encoded.encode()) > 1048576:
        raise ValueError("OpenAPI document exceeds 1048576 bytes")
    version = str(document.get("openapi") or document.get("swagger") or "").strip()
    if not (version.startswith("3.0") or version.startswith("3.1") or version == "2.0"):
        raise ValueError("unsupported OpenAPI version")
    info = document.get("info")
    if not isinstance(info, Mapping):
        raise ValueError("OpenAPI info must be an object")
    title = _text(info.get("title"), "OpenAPI title", 256)
    source_id = _slug(source_id or title, "OpenAPI source ID")
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise ValueError("OpenAPI paths must be a non-empty object")
    if len(paths) > 250:
        raise ValueError("OpenAPI document exceeds 250 paths")
    issues: list[str] = []
    if re.search(r'"\$ref":"https?://', encoded):
        issues.append("external_refs_require_separate_intake")
    origins = []
    for server in document.get("servers") or []:
        if not isinstance(server, Mapping):
            raise ValueError("OpenAPI server must be an object")
        url = str(server.get("url") or "")
        if "{" in url:
            issues.append("templated_server_requires_review")
            continue
        origins.append(_https_origin(url, "OpenAPI server URL"))
    if not origins:
        issues.append("no_concrete_https_server")
    candidates = []
    for path, path_item in paths.items():
        _text(path, "OpenAPI path", 512)
        if not isinstance(path_item, Mapping):
            raise ValueError("OpenAPI path item must be an object")
        for method, operation in path_item.items():
            if str(method).casefold() not in _OPERATIONS:
                continue
            if not isinstance(operation, Mapping):
                raise ValueError("OpenAPI operation must be an object")
            fallback = f"{method}-{path}"
            remote_id = operation.get("operationId") or fallback
            request = operation.get("requestBody") or operation.get("parameters")
            status = "declared" if request else "missing"
            candidates.append(_candidate(
                source_id, remote_id,
                operation.get("summary") or operation.get("operationId") or fallback,
                status))
    if len(candidates) > 500:
        raise ValueError("OpenAPI document exceeds 500 operations")
    if not candidates:
        issues.append("no_operations_declared")
    return SourceInspection(
        source_id=source_id, kind="openapi",
        document_fingerprint="sha256:" + hashlib.sha256(encoded.encode()).hexdigest(),
        title=title, version=_text(info.get("version") or version, "API version", 128),
        endpoint_origins=tuple(origins), candidates=tuple(candidates), issues=tuple(issues),
        metadata=(("candidate_count", len(candidates)), ("path_count", len(paths)),
                  ("specification_version", version)))


def inspect_source(kind: str, document: Mapping[str, Any], *, source_id: str = "") -> SourceInspection:
    kind = str(kind or "").strip().casefold()
    if kind == "mcp":
        if source_id and source_id != document.get("id"):
            raise ValueError("MCP source_id must match descriptor id")
        return inspect_mcp_source(document)
    if kind == "openapi":
        return inspect_openapi_source(document, source_id=source_id)
    raise ValueError("source kind must be mcp or openapi")


def plan_transition(source_id: str, current: str, target: str,
                    evidence_refs: tuple[str, ...] = ()) -> dict[str, Any]:
    """Plan, but never apply, one adjacent lifecycle transition."""
    source_id = _slug(source_id, "source ID")
    if current not in LIFECYCLE or target not in LIFECYCLE:
        raise ValueError("unknown source lifecycle state")
    if LIFECYCLE.index(target) != LIFECYCLE.index(current) + 1:
        raise ValueError("source transitions must be adjacent and forward-only")
    refs = tuple(sorted({_text(item, "evidence reference", 256) for item in evidence_refs}))
    deterministic = target in {"inspected", "proposed"}
    if target == "proposed" and not refs:
        raise ValueError("proposal transition requires inspection evidence")
    return {
        "schema": SOURCE_INTAKE_SCHEMA, "source_id": source_id,
        "current": current, "target": target, "evidence_refs": list(refs),
        "allowed": deterministic,
        "lane": "deterministic" if deterministic else "queued_w3_07_or_operator",
        "applied": False, "installs": False, "builds": False,
        "activates": False, "executes": False,
    }


def lifecycle_contract() -> dict[str, Any]:
    return {
        "schema": SOURCE_INTAKE_SCHEMA, "states": list(LIFECYCLE),
        "implemented_states": ["discovered", "inspected", "proposed"],
        "queued_states": list(LIFECYCLE[3:]),
        "supported_kinds": ["mcp", "openapi"],
        "imports_optional_runtimes": False, "network_io": False,
        "installs": False, "builds": False, "activates": False,
        "uses_secrets": False, "executes": False,
    }
