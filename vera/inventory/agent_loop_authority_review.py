"""Source-bound review of Vera's generic and domain-specific agent loops."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence


SCHEMA = "vera.agent-loop-authority-review/v1"
ROLES = frozenset({
    "executor", "controller", "adapter", "router", "program_orchestrator",
    "domain_executor",
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")
_TOKEN_CHARS = r"A-Za-z0-9._:/-"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
CONSUMER_SCAN_PATHS = (
    "vera/activity/activity_capabilities.py", "vera/agents/agents.py",
    "vera/calendar/calendar_capabilities.py", "vera/calendar/longterm_scheduler.py",
    "vera/capability_orchestration.py", "vera/chat/chat_panel.html",
    "vera/dag/dag_workshop_capabilities.py", "vera/dag/dag_workshop_panel.html",
    "vera/dag/engine_params.py", "vera/dag/loop_orchestrator.py",
    "vera/dag/loop_profiles.py", "vera/dream/dream_capabilities.py",
    "vera/evolve/census_seed.py", "vera/fabric/context.py",
    "vera/planning/planner_styles.py", "vera/registry/registry_capabilities.py",
    "vera/vera_graph.js", "vera/workers/workers_ollama_panel.html",
    "tests/test_agent_runtime_dispatch.py", "tests/test_census_seed.py",
    "tests/test_engine_params.py", "documentation/03-dag-engine.md",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _text(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class AgentLoopSurface:
    name: str
    role: str
    semantic_scope: str
    source_path: str
    symbol: str
    capability: str = "none"
    delegates_to: str = "none"
    strategy: str = "none"
    portable_run_projection: bool = False
    surface_id: str = field(init=False)

    def __post_init__(self) -> None:
        for attr in ("name", "semantic_scope", "capability", "delegates_to",
                     "strategy"):
            object.__setattr__(self, attr, _text(getattr(self, attr), attr))
        if self.role not in ROLES:
            raise ValueError("unsupported agent-loop role")
        path = str(self.source_path or "").replace("\\", "/").strip("/")
        if not path.startswith("vera/") or ".." in path.split("/"):
            raise ValueError("source path must stay within vera")
        object.__setattr__(self, "source_path", path)
        symbol = str(self.symbol or "").strip()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol must be a Python identifier")
        object.__setattr__(self, "symbol", symbol)
        if self.role in {"adapter", "router", "controller", "program_orchestrator"} \
                and self.delegates_to == "none":
            raise ValueError("delegating loop roles require a target")
        if not isinstance(self.portable_run_projection, bool):
            raise ValueError("portable_run_projection must be boolean")
        object.__setattr__(self, "surface_id", _identity("als_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "role": self.role,
            "semantic_scope": self.semantic_scope,
            "source_path": self.source_path, "symbol": self.symbol,
            "capability": self.capability, "delegates_to": self.delegates_to,
            "strategy": self.strategy,
            "portable_run_projection": self.portable_run_projection,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, **self.identity_dict()}


CURRENT_SURFACES = (
    AgentLoopSurface("agent_loop.v1", "executor", "generic_capability_steps",
                     "vera/fabric/context.py", "cap_dag_agent_loop",
                     "dag.agent_loop", strategy="simple_react"),
    AgentLoopSurface("agent_loop.v2", "executor", "generic_capability_steps",
                     "vera/fabric/context.py", "cap_dag_agent_loop_v2",
                     "dag.agent_loop_v2", strategy="triage_dynamic_toolkit"),
    AgentLoopSurface("agent_loop.v3", "executor", "generic_capability_steps",
                     "vera/dag/dag_workshop_capabilities.py", "cap_dag_agent_loop_v3",
                     "dag.agent_loop_v3", strategy="phased_react"),
    AgentLoopSurface("agent_loop.v4", "executor", "generic_capability_steps",
                     "vera/dag/dag_workshop_capabilities.py", "cap_dag_agent_loop_v4",
                     "dag.agent_loop_v4", strategy="planned_verified_steps"),
    AgentLoopSurface("agent_loop.v5", "executor", "generic_capability_steps",
                     "vera/dag/dag_workshop_capabilities.py", "cap_dag_agent_loop_v5",
                     "dag.agent_loop_v5", strategy="orchestrated_steps",
                     portable_run_projection=True),
    AgentLoopSurface("agent_loop.v6", "controller", "generic_capability_steps",
                     "vera/dag/dag_workshop_capabilities.py", "cap_dag_agent_loop_v6",
                     "dag.agent_loop_v6", "dag.agent_loop_v5.step_machinery",
                     "adaptive_controller", True),
    AgentLoopSurface("agent_loop.v7", "adapter", "generic_capability_steps",
                     "vera/dag/dag_workshop_capabilities.py", "cap_dag_agent_loop_v7",
                     "dag.agent_loop_v7", "dag.agent_loop_v6",
                     "long_horizon_policy", True),
    AgentLoopSurface("agent_loop.v8", "program_orchestrator", "loop_programs",
                     "vera/dag/loop_orchestrator.py", "cap_agent_loop_v8",
                     "dag.agent_loop_v8", "dag.agent_loop_v5_v6_v7",
                     "program_generation", True),
    AgentLoopSurface("loops.profile_runner", "adapter", "generic_capability_steps",
                     "vera/dag/loop_profiles.py", "cap_loops_run", "loops.run",
                     "dag.agent_loop_v5_v6_v7", "profile_adapter", True),
    AgentLoopSurface("workshop.stream_router", "router", "generic_capability_steps",
                     "vera/dag/dag_workshop_capabilities.py",
                     "workshop_agent_loop_stream", delegates_to="dag.agent_loop_v1_v7",
                     strategy="version_router", portable_run_projection=True),
    AgentLoopSurface("dream.agent_loop_adapter", "adapter", "dream_stage_execution",
                     "vera/dream/dream_capabilities.py", "_run_agent_loop",
                     delegates_to="configured_agent_loop_capability",
                     strategy="dream_stage_adapter", portable_run_projection=True),
    AgentLoopSurface("operator.browser_loop", "domain_executor",
                     "browser_observe_think_act", "vera/operator/operator_loop.py",
                     "run_loop", strategy="browser_operator"),
)


def _safe_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"agent-loop source is unavailable: {relative}")
    return path


def _source_evidence(root: Path, surface: AgentLoopSurface) -> dict[str, Any]:
    path = _safe_file(root, surface.source_path)
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"agent-loop source is too large: {surface.source_path}")
    text = raw.decode("utf-8")
    if not re.search(
            rf"\b(?:async\s+def|def)\s+{re.escape(surface.symbol)}\s*\(", text):
        raise ValueError(f"agent-loop source symbol is missing: {surface.name}")
    if surface.capability != "none" and surface.capability not in text:
        raise ValueError(f"agent-loop capability declaration is missing: {surface.name}")
    return {
        "surface_id": surface.surface_id,
        "source_ref_digest": "sha256:" + hashlib.sha256(
            surface.source_path.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _consumer_evidence(
    root: Path, surfaces: Sequence[AgentLoopSurface],
) -> list[dict[str, Any]]:
    patterns = {
        item.surface_id: re.compile(
            rf"(?<![{_TOKEN_CHARS}]){re.escape(item.capability)}(?![{_TOKEN_CHARS}])")
        for item in surfaces if item.capability != "none"
    }
    rows = []
    for relative in CONSUMER_SCAN_PATHS:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"declared consumer evidence path is unavailable: {relative}")
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError(f"consumer evidence source is too large: {relative}")
        text = raw.decode("utf-8")
        top = relative.split("/", 1)[0]
        kind = "code_reference" if top == "vera" else (
            "test_reference" if top == "tests" else "documentation_reference")
        for surface in surfaces:
            pattern = patterns.get(surface.surface_id)
            if pattern is None or relative == surface.source_path:
                continue
            count = len(pattern.findall(text))
            if not count:
                continue
            rows.append({
                "surface_id": surface.surface_id, "kind": kind, "count": count,
                "source_ref_digest": "sha256:" + hashlib.sha256(
                    relative.encode()).hexdigest(),
            })
    return rows


def build_agent_loop_authority_review(
    repo_root: Path, surfaces: Sequence[AgentLoopSurface] = CURRENT_SURFACES,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    surfaces = tuple(sorted(surfaces, key=lambda item: item.surface_id))
    if not surfaces or len({item.surface_id for item in surfaces}) != len(surfaces):
        raise ValueError("agent-loop surfaces must be non-empty and unique")
    sources = [_source_evidence(root, item) for item in surfaces]
    consumers = _consumer_evidence(root, surfaces)
    generic_executors = [item.surface_id for item in surfaces
                         if item.semantic_scope == "generic_capability_steps"
                         and item.role == "executor"]
    active = {}
    for item in surfaces:
        rows = [row for row in consumers if row["surface_id"] == item.surface_id]
        active[item.surface_id] = sum(row["count"] for row in rows)
    payload = {
        "schema": SCHEMA,
        "surfaces": [item.to_dict() for item in surfaces],
        "source_evidence": sorted(sources, key=lambda item: item["surface_id"]),
        "consumer_evidence": sorted(
            consumers, key=lambda item: (item["surface_id"], item["kind"],
                                         item["source_ref_digest"])),
        "consumer_reference_counts": active,
        "consumer_scan": {
            "status": "partial",
            "declared_path_count": len(CONSUMER_SCAN_PATHS),
            "excludes": ["stored_workflows", "external_consumers", "runtime_calls"],
        },
        "parallel_generic_executor_surface_ids": sorted(generic_executors),
        "recommendations": [
            "extract_shared_step_kernel_beneath_strategy_policies",
            "preserve_v1_v4_until_ui_saved_flow_and_caller_migration",
            "retain_v5_v7_as_active_strategies_during_kernel_migration",
            "treat_v8_as_program_orchestrator_not_step_engine_replacement",
            "keep_browser_operator_as_domain_specific_loop",
            "route_all_engines_through_portable_run_events_and_cancellation",
        ],
        "removal_authority": False,
        "executes": False,
        "mutates": False,
    }
    return {"review_id": _identity("alr_", payload), **payload}
