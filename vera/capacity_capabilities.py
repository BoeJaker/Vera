# ============================================================================
# capacity_capabilities.py — capacity pools + seat registry (swarm §6.5.2/6.5.5)
# ============================================================================
#
# The routing substrate: register Claude subscription SEATS as named capacity
# pools (on the generalized capacity_pool primitive) and expose a unified live
# view of seats + Ollama nodes for the capacity UI. Decisions baked in (§10.1):
#   • Seats are SUBSCRIPTION-ONLY — api-key seats are out of scope; the router
#     never provisions/routes to a metered-billing pool.
#   • A seat is PINNED to a target — the record carries a concrete `target`.
#   • Seat spend is a JOIN onto the existing providers.usage subsystem (§6.5.5),
#     not a new meter — surfaced by the UI, not stored here.
#
# Leases themselves live in capacity_pool (crash-safe, fail-open); this is the
# registry + observability on top. Ollama nodes are READ from ollama.gate.status
# (the gate owns those leases — no double-gating).
# ============================================================================

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import APP, capability, CAPABILITY_REGISTRY, emit_event
import Vera.vera.capability_orchestration as _orch
from Vera.vera import capacity_pool as cp

log = logging.getLogger("vera.capacity")

_VALID_AUTH_POOLS = ("oauth-token", "host-login")   # subscription only (§10.1)


def _redis():
    return getattr(_orch, "REDIS", None)


async def _call(name: str, **kw):
    reg = CAPABILITY_REGISTRY.get(name) or {}
    fn = reg.get("func")
    if not fn:
        return None
    try:
        return await fn(**kw)
    except Exception as e:
        log.debug("capacity: %s failed: %s", name, e)
        return None


async def _load_seats() -> Dict[str, dict]:
    r = _redis()
    out: Dict[str, dict] = {}
    if not r:
        return out
    try:
        raw = await r.hgetall(cp.seats_key())
        for k, v in (raw or {}).items():
            fid = k.decode() if isinstance(k, (bytes, bytearray)) else k
            try:
                out[fid] = json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v)
            except Exception:
                continue
    except Exception as e:
        log.debug("capacity seats load: %s", e)
    return out


async def _save_seat(seat: dict) -> None:
    r = _redis()
    if r:
        await r.hset(cp.seats_key(), seat["seat_id"], json.dumps(seat))


if True:  # capability registration

    @capability(
        "capacity.seat.register", http_method="POST", http_path="/capacity/seat/register",
        http_tags=["capacity"], memory="on",
        description="Register (or update) a Claude SUBSCRIPTION seat as a capacity pool, PINNED "
                    "to a target (§10.1). Inputs: seat_id (str! — e.g. 'acct-a'), target (str! — "
                    "the instance_id this seat runs on), label (str), slots (int=1), auth_pool "
                    "(oauth-token|host-login — subscription only; api-key is OUT OF SCOPE and "
                    "rejected). Output: {ok, seat}. NOTE: this records the seat; the one-time "
                    "credential ENROLMENT (browser OAuth / setup-token) is human-only and "
                    "out-of-band (§6.5.4).",
    )
    async def cap_capacity_seat_register(seat_id: str = "", target: str = "", label: str = "",
                                         slots: int = 1, auth_pool: str = "oauth-token",
                                         trace_id=None) -> dict:
        if not seat_id:
            return {"ok": False, "error": "seat_id required"}
        if not target:
            return {"ok": False, "error": "target required — a seat is pinned to a target (§10.1)"}
        if auth_pool not in _VALID_AUTH_POOLS:
            return {"ok": False, "error": f"auth_pool must be one of {_VALID_AUTH_POOLS} — "
                                          "api-key seats are out of scope (§10.1)"}
        seats = await _load_seats()
        seat = seats.get(seat_id, {})
        # A host-login seat is enrolled by definition (the host is already signed
        # into `claude login`); an oauth-token seat needs its sealed setup-token
        # provided in a human enrolment step (§6.5.4) before it is usable.
        enrolled = seat.get("enrolled") or (auth_pool == "host-login")
        seat.update({
            "seat_id": seat_id, "pool": f"claude:{seat_id}", "target": target,
            "kind": "subscription", "label": label or seat.get("label") or seat_id,
            "slots": max(1, int(slots or 1)), "auth_pool": auth_pool,
            "enrolled": enrolled,
            "cooling_until": seat.get("cooling_until"),
            "expires_at": seat.get("expires_at"),
            "registered_at": seat.get("registered_at") or time.time(),
            "updated_at": time.time(),
        })
        await _save_seat(seat)
        await emit_event({"type": "capacity.seat.registered", "seat_id": seat_id, "target": target})
        return {"ok": True, "seat": seat}

    @capability(
        "capacity.seat.cool", http_method="POST", http_path="/capacity/seat/cool",
        http_tags=["capacity"], memory="on",
        description="Mark a seat COOLING until an expected return (§6.5.3 — a reported usage "
                    "limit). Routing skips a cooling seat. Inputs: seat_id (str!), cooling_s "
                    "(int — seconds from now; 0 clears cooling). Output: {ok, seat}.",
    )
    async def cap_capacity_seat_cool(seat_id: str = "", cooling_s: int = 0, trace_id=None) -> dict:
        seats = await _load_seats()
        seat = seats.get(seat_id)
        if not seat:
            return {"ok": False, "error": f"no such seat: {seat_id}"}
        seat["cooling_until"] = (time.time() + int(cooling_s)) if cooling_s and int(cooling_s) > 0 else None
        seat["updated_at"] = time.time()
        await _save_seat(seat)
        await emit_event({"type": "capacity.seat.cooling", "seat_id": seat_id,
                          "cooling_until": seat["cooling_until"]})
        return {"ok": True, "seat": seat}

    @capability(
        "capacity.seat.remove", http_method="POST", http_path="/capacity/seat/remove",
        http_tags=["capacity"], memory="on",
        description="Remove a registered seat. Input: seat_id (str!). Output: {ok, removed}.",
    )
    async def cap_capacity_seat_remove(seat_id: str = "", trace_id=None) -> dict:
        r = _redis()
        removed = 0
        if r:
            try:
                removed = int(await r.hdel(cp.seats_key(), seat_id) or 0)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True, "removed": removed}

    @capability(
        "capacity.status", http_method="GET", http_path="/capacity/status",
        http_tags=["capacity"], memory="off", silent=True,
        description="Unified capacity view for the routing/Sessions UI: every registered Claude "
                    "SEAT with its derived state (available|cooling|expired|unauthed), pinned "
                    "target, live pool occupancy (held/free), plus the Ollama nodes read from "
                    "ollama.gate.status (the gate owns those). Seat spend is a JOIN onto "
                    "providers.usage (§6.5.5), surfaced by the UI. Output: {ok, seats:[...], "
                    "ollama:{...}, summary:{seats,available,cooling}}.",
    )
    async def cap_capacity_status(trace_id=None) -> dict:
        r = _redis()
        seats = await _load_seats()
        now = time.time()
        out_seats: List[dict] = []
        avail = cool = 0
        for sid, seat in sorted(seats.items()):
            state = cp.seat_state(seat, now)
            occ = await cp.occupancy(r, seat.get("pool", f"claude:{sid}"),
                                     int(seat.get("slots", 1)))
            if state == "available":
                avail += 1
            elif state == "cooling":
                cool += 1
            out_seats.append({
                "seat_id": sid, "label": seat.get("label", sid),
                "target": seat.get("target", ""), "auth_pool": seat.get("auth_pool", ""),
                "state": state, "slots": seat.get("slots", 1),
                "held": occ.get("held", 0), "free": occ.get("free", 0),
                "cooling_until": seat.get("cooling_until"),
            })
        ollama = await _call("ollama.gate.status") or {}
        return {"ok": True, "seats": out_seats, "ollama": ollama,
                "summary": {"seats": len(out_seats), "available": avail, "cooling": cool}}
