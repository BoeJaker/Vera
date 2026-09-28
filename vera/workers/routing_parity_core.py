"""A new sandbox starts with prod's model routing, not the code defaults.

Every sandbox gets its own private Redis, so the three USER routing layers prod
keeps there - job-type routing profiles, per-capability rules and role profiles
(the Model Routing page) - never reached one. A sandbox ran the code-declared
defaults instead: measured 2026-09-27, the bleeding-edge mirror's loop routed the
coder to qwen2.5-coder:14b (prod: qwen2.5:7b) and left executor/writer unset
(prod: the 9b pinned to the GPU node). Anything measured there - a census, a loop
test - measured different models from prod.

So a sandbox seeds each layer from prod the first time it boots with that layer
ABSENT from its own Redis. Once seeded the layer is saved, so a later boot never
re-seeds and an edit made in the sandbox is never overwritten. Pure: environment
and prod's GET responses in, what to seed out; the orchestrator does the I/O.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

#: The three user layers, by the name the orchestrator persists each under.
LAYERS = ("routing", "cap_routing", "role_profiles")

#: prod's read-only GET route for each layer.
PATHS = {"routing": "/ollama/routing",
         "cap_routing": "/ollama/cap_routing",
         "role_profiles": "/ollama/role_profiles"}

_OFF = ("0", "false", "no", "off")


def enabled(env: Mapping[str, str]) -> bool:
    """Only a dev sandbox seeds; VERA_ROUTING_PARITY=0 turns it off."""
    return (str(env.get("VERA_IS_DEV_SANDBOX", "")).strip() == "1"
            and str(env.get("VERA_ROUTING_PARITY", "1")).strip().lower() not in _OFF)


def prod_base_url(env: Mapping[str, str]) -> str:
    """Where prod answers: VERA_PROD_URL if set, else the scheme+host of the gate
    broker URL every sandbox is already given (https://host.docker.internal:8999)."""
    explicit = str(env.get("VERA_PROD_URL", "")).strip()
    if explicit:
        return explicit.rstrip("/")
    broker = str(env.get("VERA_GATE_BROKER_URL", "")).strip()
    if broker:
        parts = urlsplit(broker)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def missing_layers(present: Mapping[str, bool]) -> List[str]:
    """The layers this sandbox's Redis has never held - the only ones seeded."""
    return [layer for layer in LAYERS if not present.get(layer)]


def _dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def user_layers(routing_doc: Optional[dict], cap_doc: Optional[dict],
                role_doc: Optional[dict]) -> Dict[str, Any]:
    """prod's USER layers out of its GET responses - never the declared or
    effective views, which the sandbox already derives from its own code."""
    routing_doc, cap_doc, role_doc = _dict(routing_doc), _dict(cap_doc), _dict(role_doc)
    profiles: Dict[str, Any] = {}
    for name, prof in _dict(routing_doc.get("profiles")).items():
        prof = _dict(prof)
        rules = {jt: r for jt, r in _dict(prof.get("rules")).items() if isinstance(r, dict)}
        profiles[str(name)] = {"label": prof.get("label", name), "rules": rules}
    active = str(routing_doc.get("active_profile") or "")
    if active not in profiles:
        active = next(iter(profiles), "default")
    return {
        "routing": {"active_profile": active, "profiles": profiles},
        "cap_routing": {str(p): r for p, r in _dict(cap_doc.get("user")).items()
                        if isinstance(r, dict)},
        "role_profiles": {str(n): p for n, p in _dict(role_doc.get("user")).items()
                          if isinstance(p, dict)},
    }
