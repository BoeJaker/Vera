"""The switchboard as capabilities: list, read and set switches; validate a set; show what is on.

switchboard_core owns every decision worth testing. This module owns the global layer
(Redis vera:minds:switchboard) and the HTTP/MCP surface. Nothing here is embedded or
written to memory: switches are configuration, not knowledge.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event, now_iso  # noqa: F401

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import switchboard_core as SB
except ImportError:                      # pragma: no cover
    from vera.minds import switchboard_core as SB   # type: ignore

log = logging.getLogger("vera.minds")

KEY_GLOBAL = "vera:minds:switchboard"


async def read_global() -> Dict[str, Any]:
    """The global layer, or {} when Redis is absent or the value is unreadable."""
    r = _orch.REDIS
    if r is None:
        return {}
    try:
        raw = await r.get(KEY_GLOBAL)
        if not raw:
            return {}
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("minds: global layer unreadable (%s) - using defaults", e)
        return {}


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "minds.switch.list", http_method="GET", http_path="/minds/switches",
    contract=_MC.contract("switchboard.switch.list", effects=["read"], idempotency="idempotent",
                          presentation={"kind": "table", "form": "table", "map": {"rows": "switches"},
                                        "draw": {"columns": ["id", "default", "modes", "surface", "package",
                                                             "owner", "census"]},
                                        "empty": "No switches"}),
    http_tags=["minds"], memory="off", silent=True,
    description="Every registered switch (four-minds switchboard): id, modes (first is the "
                "default), surface, roadmap package, owner, whether a census arm may set it. "
                "Output: {schema, switches[]}.",
)
async def cap_minds_switch_list(trace_id=None) -> Dict[str, Any]:
    return {"schema": SB.SCHEMA, "switches": SB.listing()}


@capability(
    "minds.switch.get", http_method="GET", http_path="/minds/switch",
    contract=_MC.contract("switchboard.switch.get", effects=["read"], idempotency="idempotent"),
    http_tags=["minds"], memory="off", silent=True,
    description="The global switchboard layer (what is switched on for every run unless a "
                "run, census arm or chat session says otherwise). Output: {global, killed}.",
)
async def cap_minds_switch_get(trace_id=None) -> Dict[str, Any]:
    g = await read_global()
    return {"global": g, "killed": SB.resolve(global_=g)["killed"]}


@capability(
    "minds.switch.set", http_method="POST", http_path="/minds/switch",
    contract=_MC.contract("switchboard.switch.set", effects=["write"], idempotency="idempotent"),
    http_tags=["minds"], memory="off",
    description="Set one switch in the global layer. Inputs: id (str! - a registered switch, "
                "or '*' with mode 'default' for the kill switch that puts every behaviour back "
                "to its default; '*' with mode 'clear' lifts it), mode (str! - one of the "
                "switch's modes, or 'unset' to remove the global entry), reason (str), by (str). "
                "Unknown ids or modes are refused. Output: {ok, global}.",
)
async def cap_minds_switch_set(id: str = "", mode: str = "", reason: str = "", by: str = "",
                               trace_id=None) -> Dict[str, Any]:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "redis unavailable"}
    g = await read_global()
    if id == SB.KILL:
        if mode == "default":
            g[SB.KILL] = {"mode": "default", "reason": reason[:300], "by": by[:80], "ts": now_iso()}
        elif mode == "clear":
            g.pop(SB.KILL, None)
        else:
            return {"ok": False, "error": "'*' takes mode 'default' (kill) or 'clear'"}
    else:
        if mode == "unset":
            g.pop(id, None)
        else:
            v = SB.validate({id: mode})
            if not v["ok"]:
                return {"ok": False, "error": SB.explain(v)}
            g[id] = {"mode": mode, "reason": reason[:300], "by": by[:80], "ts": now_iso()}
    await r.set(KEY_GLOBAL, json.dumps(g))
    try:
        await emit_event({"type": "minds.switch.set", "id": id, "mode": mode, "by": by[:80]})
    except Exception:
        pass
    return {"ok": True, "global": g}


@capability(
    "minds.validate", http_method="POST", http_path="/minds/validate",
    contract=_MC.contract("switchboard.validate", effects=["read"], idempotency="idempotent"),
    http_tags=["minds"], memory="off", silent=True,
    description="Strictly validate a requested set {feature_id: mode} - what a run's minds= or a "
                "census arm's target.minds would be. Inputs: minds (object), census (bool - also "
                "refuse switches a census may not set). Output: {ok, unknown_ids[], bad_modes[], "
                "not_census[], digest}. The census harness calls this before goal 1 and refuses "
                "to start on ok=false.",
)
async def cap_minds_validate(minds: Optional[Dict[str, Any]] = None, census: bool = False,
                             trace_id=None) -> Dict[str, Any]:
    return SB.validate(minds or {}, census=bool(census))


@capability(
    "minds.effective", http_method="POST", http_path="/minds/effective",
    contract=_MC.contract("switchboard.effective", effects=["read"], idempotency="idempotent"),
    http_tags=["minds"], memory="off", silent=True,
    description="What would be on for a run requesting `minds` right now: the effective mode of "
                "every switch and which layer set it (call > arm > session > global > default). "
                "Inputs: minds (object, optional), arm (object, optional). Output: {modes, source, "
                "ignored, killed, digest}.",
)
async def cap_minds_effective(minds: Optional[Dict[str, Any]] = None,
                              arm: Optional[Dict[str, Any]] = None, trace_id=None) -> Dict[str, Any]:
    for layer in (minds, arm):
        v = SB.validate(layer or {})
        if not v["ok"]:
            return {"ok": False, "error": SB.explain(v)}
    eff = SB.resolve(call=minds or {}, arm=arm or {}, global_=await read_global())
    eff["digest"] = SB.digest(minds or {})
    return eff
