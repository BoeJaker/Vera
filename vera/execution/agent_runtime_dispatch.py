"""Content-free dispatch projection for Vera-native agent-loop runtimes.

Native loop capabilities remain the execution authority.  This module exposes
the selection as a portable, deterministic record so execution can be observed
and migrated behind a runtime adapter without changing behaviour first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_PORTABLE_SEMANTICS = (
    "artifact_refs", "capability_intent", "run_events", "workflow_identity",
)
_DEFERRED_SEMANTICS = (
    "runtime_adapter_cancel", "runtime_adapter_launch", "runtime_adapter_stream",
)


def _identifier(value: Any, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"invalid {field_name}")
    return text


def _bounded(value: Any, field_name: str, maximum: int = 240) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"invalid {field_name}")
    return text


@dataclass(frozen=True)
class AgentRuntimeDispatch:
    """An inert description of one already-selected native execution path."""

    session_id: str
    engine: str
    capability: str
    profile: str = ""
    schema: str = "vera.agent-runtime-dispatch/v1"
    dispatch_id: str = field(init=False)

    def __post_init__(self) -> None:
        session_id = _bounded(self.session_id, "session_id")
        engine = _identifier(self.engine, "engine")
        capability = _identifier(self.capability, "capability")
        profile = ""
        if self.profile:
            profile = _identifier(self.profile, "profile")
        canonical = json.dumps(
            {"capability": capability, "engine": engine, "profile": profile,
             "session_id": session_id},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(
            self, "dispatch_id",
            "dispatch_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "dispatch_id": self.dispatch_id,
            "run_id": self.session_id,
            "workflow_id": f"agent-loop:{self.engine}",
            "runtime_family": "vera-native",
            "runtime_id": f"vera.agent-loop.{self.engine}",
            "selected_capability": self.capability,
            "profile": self.profile,
            "adapter_mode": "shadow",
            "execution_authority": "native_capability",
            "portable_semantics": list(_PORTABLE_SEMANTICS),
            "deferred_semantics": list(_DEFERRED_SEMANTICS),
            "executes": False,
            "changes_dispatch": False,
        }


def project_agent_runtime_dispatch(*, session_id: str, engine: str,
                                   capability: str,
                                   profile: str = "") -> dict[str, Any]:
    """Return a bounded projection; never imports or invokes the capability."""
    return AgentRuntimeDispatch(
        session_id=session_id, engine=engine, capability=capability,
        profile=profile,
    ).to_dict()


def select_agent_runtime_dispatch(
        *, session_id: str, profile: str = "", configured_engine: str = "",
        loop_engine: str = "", engine_capabilities: Mapping[str, str],
        default_engine: str = "v6") -> dict[str, Any]:
    """Mirror native selection once, returning only its inert projection."""
    if profile:
        return project_agent_runtime_dispatch(
            session_id=session_id, engine="profile", capability="loops.run",
            profile=profile,
        )
    fallback = loop_engine if loop_engine in engine_capabilities else default_engine
    engine = (configured_engine if configured_engine in engine_capabilities
              else fallback)
    capability = engine_capabilities.get(engine)
    if not capability:
        raise ValueError("no capability for selected engine")
    return project_agent_runtime_dispatch(
        session_id=session_id, engine=engine, capability=capability,
    )


def safely_select_agent_runtime_dispatch(**values: Any) -> dict[str, Any]:
    """Failure-isolate the shadow plane from authoritative native dispatch."""
    try:
        value = select_agent_runtime_dispatch(**values)
        value["projection_status"] = "available"
        return value
    except (TypeError, ValueError):
        return {
            "schema": "vera.agent-runtime-dispatch/v1",
            "projection_status": "unavailable",
            "adapter_mode": "shadow",
            "execution_authority": "native_capability",
            "portable_semantics": [],
            "deferred_semantics": list(_DEFERRED_SEMANTICS),
            "executes": False,
            "changes_dispatch": False,
        }
