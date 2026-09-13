"""Independent source-bound reviews of explicit compatibility paths.

The result is advisory.  It neither imports the reviewed modules nor grants
bulk or per-path removal authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence

SCHEMA = "vera.compatibility-path-review/v1"
MAX_SOURCE_BYTES = 4_000_000
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_KINDS = {"deprecated_noop", "capability_alias"}
_DECISIONS = {"retain", "migrate", "insufficient_evidence"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CompatibilityPath:
    name: str
    replacement: str
    kind: str
    source: str
    source_assertions: tuple[str, ...]
    in_repo_runtime_consumer: bool
    decision: str
    rationale: tuple[str, ...]
    required_actions: tuple[str, ...]
    candidate_id: str = field(init=False)

    def __post_init__(self) -> None:
        for attr in ("name", "replacement"):
            if not _IDENTIFIER.fullmatch(str(getattr(self, attr) or "")):
                raise ValueError(f"{attr} must be a bounded dotted identifier")
        if self.name == self.replacement:
            raise ValueError("replacement must differ from compatibility path")
        if self.kind not in _KINDS:
            raise ValueError("unsupported compatibility path kind")
        if self.decision not in _DECISIONS:
            raise ValueError("unsupported compatibility review decision")
        if not self.source.startswith("vera/") or not self.source.endswith(".py"):
            raise ValueError("source must name a Vera Python module")
        assertions = tuple(sorted(set(self.source_assertions)))
        rationale = tuple(sorted(set(self.rationale)))
        actions = tuple(sorted(set(self.required_actions)))
        if not assertions or not rationale or not actions:
            raise ValueError("assertions, rationale, and required actions are required")
        object.__setattr__(self, "source_assertions", assertions)
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "required_actions", actions)
        object.__setattr__(self, "candidate_id", _identity("cpath_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "replacement": self.replacement, "kind": self.kind,
            "source": self.source, "source_assertions": list(self.source_assertions),
            "in_repo_runtime_consumer": self.in_repo_runtime_consumer,
            "decision": self.decision, "rationale": list(self.rationale),
            "required_actions": list(self.required_actions),
        }


_MEMORY_SOURCE = "vera/fabric/memory_hooks.py"
_RESEARCH_DECLARATIONS = "vera/research/alias_compatibility.py"
_RESEARCH_RUNTIME = "vera/research/researcher_api.py"

CURRENT_PATHS = (
    CompatibilityPath(
        "memory.record_cap_interaction", "capability_orchestration.act_enqueue",
        "deprecated_noop", _MEMORY_SOURCE,
        ("async def record_cap_interaction(*args, **kwargs):",
         "DEPRECATED — kept as a no-op for backwards compatibility."),
        False, "insufficient_evidence",
        ("no_in_repo_runtime_consumer", "external_import_contract_explicit"),
        ("complete_external_consumer_inventory", "retain_import_shim")),
    CompatibilityPath(
        "memory.patch_capability_for_memory", "capability_orchestration.act_enqueue",
        "deprecated_noop", _MEMORY_SOURCE,
        ("def patch_capability_for_memory():", "patch_capability_for_memory()"),
        True, "migrate",
        ("startup_still_calls_noop", "second_wrapper_would_duplicate_activity"),
        ("remove_internal_startup_call_first", "complete_external_consumer_inventory",
         "retain_import_shim")),
    CompatibilityPath(
        "memory.patch_new_cap", "capability_orchestration.capability_decorator",
        "deprecated_noop", _MEMORY_SOURCE,
        ("def patch_new_cap(cap_name: str):",
         "DEPRECATED — kept as a no-op (see patch_capability_for_memory)."),
        False, "insufficient_evidence",
        ("no_in_repo_runtime_consumer", "external_import_coverage_missing"),
        ("complete_external_consumer_inventory", "retain_import_shim")),
)

_RESEARCH = (
    ("research.report", "research.run"),
    ("research.parallel", "research.run"),
    ("research.deep", "research.run"),
    ("research.code", "research.run"),
    ("research.guide", "research.run"),
    ("research.filestore", "research.run"),
    ("research.quick_search", "research.report"),
)

CURRENT_PATHS += tuple(
    CompatibilityPath(
        name, replacement, "capability_alias", _RESEARCH_DECLARATIONS,
        (f'ResearchAlias("{name}"',), True, "retain",
        ("registered_callable_capability", "alias_preserves_request_projection"),
        ("retain_callable_identity", "measure_stored_and_external_consumers"),
    ) for name, replacement in _RESEARCH
)


def _source_evidence(repo_root: Path, path: CompatibilityPath) -> dict[str, Any]:
    root = repo_root.resolve()
    source = (root / path.source).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError(f"source is unavailable: {path.source}")
    raw = source.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"source is too large: {path.source}")
    text = raw.decode("utf-8")
    missing = [item for item in path.source_assertions if item not in text]
    if missing:
        raise ValueError(f"source assertions are missing from {path.source}: {missing}")
    evidence = {
        "source": path.source,
        "content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "assertion_digests": [
            "sha256:" + hashlib.sha256(item.encode()).hexdigest()
            for item in path.source_assertions],
    }
    if path.kind == "capability_alias":
        runtime = (root / _RESEARCH_RUNTIME).resolve()
        runtime_raw = runtime.read_bytes()
        marker = f'@capability("{path.name}"'
        if marker not in runtime_raw.decode("utf-8"):
            raise ValueError(f"runtime registration is missing for {path.name}")
        evidence["runtime_source"] = _RESEARCH_RUNTIME
        evidence["runtime_content_digest"] = "sha256:" + hashlib.sha256(runtime_raw).hexdigest()
        evidence["runtime_assertion_digest"] = "sha256:" + hashlib.sha256(marker.encode()).hexdigest()
    return evidence


def review_compatibility_path(repo_root: Path, path: CompatibilityPath) -> dict[str, Any]:
    """Review one path without allowing evidence to bleed between candidates."""
    evidence = _source_evidence(Path(repo_root), path)
    payload = {
        "schema": SCHEMA, "candidate_id": path.candidate_id,
        **path.identity_dict(), "source_evidence": evidence,
        "coverage": {
            "code_declaration": "complete", "in_repo_runtime": "complete",
            "stored_definitions": "not_examined", "schedules": "not_examined",
            "configuration": "not_examined", "runtime_calls": "not_examined",
            "external_consumers": "not_examined",
        },
        "independent": True, "removal_authority": False,
        "executes": False, "imports_reviewed_module": False, "mutates": False,
    }
    return {"review_id": _identity("cprev_", payload), **payload}


def build_compatibility_path_review_set(
    repo_root: Path,
    paths: Sequence[CompatibilityPath] = CURRENT_PATHS,
) -> dict[str, Any]:
    """Collect independent reports; never convert the set into bulk authority."""
    paths = tuple(sorted(paths, key=lambda item: item.candidate_id))
    if not paths or len({item.name for item in paths}) != len(paths):
        raise ValueError("compatibility paths must be non-empty and unique")
    reviews = [review_compatibility_path(repo_root, item) for item in paths]
    payload = {
        "schema": "vera.compatibility-path-review-set/v1",
        "reviews": reviews,
        "decisions": {
            decision: sorted(item["name"] for item in reviews if item["decision"] == decision)
            for decision in sorted(_DECISIONS)
        },
        "removal_candidates": [],
        "conclusion": "no_path_has_complete_removal_evidence",
        "bulk_removal_authority": False, "executes": False, "mutates": False,
    }
    return {"review_set_id": _identity("cprset_", payload), **payload}
