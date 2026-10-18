"""Capability Contract v2 declarations for four-minds' capabilities (backbone review 11 Oct 2026, W1-03 / W1-05: no
four-minds cap declared `contract=`, so the policy shadow reported its writers as indeterminate).

One builder, so every four-minds contract carries every dimension the contract gate requires
(capability_contract_core.GATE_REQUIRED_DIMENSIONS) and the owner is the same.

Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

OWNER = "four-minds"


def contract(canonical_task: str, *, effects: Sequence[str], idempotency: str, resources: Sequence[str] = ("cpu",),
             network: str = "not_required", filesystem: str = "not_required", approval: str = "not_required",
             lifecycle: str = "active", owner: str = OWNER,
             presentation: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A full v2 contract: what the call does (effects), whether repeating it is safe (idempotency), what it uses.

    `presentation` (optional) is how the answer is drawn - the DA-01 {kind, map, form?, draw?, empty?} the desk,
    dashboard and chat read (capability_contract_core). Left out, the contract is exactly as before."""
    out = {
        "canonical_task": canonical_task,
        "lifecycle": lifecycle,
        "effects": list(effects),
        "output_schema": {"type": "object"},
        "approval": {"status": approval},
        "trust": {"status": "untrusted_structured_input"},
        "secrets": {"status": "not_required"},
        "filesystem": {"status": filesystem},
        "network": {"status": network},
        "tenant": {"status": "request_scoped"},
        "idempotency": {"status": idempotency},
        "cancellation": {"status": "not_required"},
        "pagination": {"status": "not_applicable"},
        "resources": {"status": "declared", "classes": list(resources)},
        "owner": owner,
    }
    if presentation is not None:
        out["presentation"] = dict(presentation)
    return out
