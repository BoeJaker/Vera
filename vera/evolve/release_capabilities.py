"""The prod release, gated on the census.

  evolve.release.prod     fast-forward main to the edge and restart prod -
                          but not while a census is in flight: wait for it
                          to finish (`census=finish`, default), or yield it
                          and go once the goal in flight has parked
                          (`census=goal`), or `force=true` to go now.
  evolve.release.status   the pending release, the census, what is next
  evolve.release.cancel   drop the pending release (resumes a census it yielded)

The decisions are in `release_core` (pure). This module owns Redis, the
census reads (the same summary /health carries), the merge
(`evolve.bleeding_edge.promote_to_main`), the restart (`sys.dev.restart`)
and a 30 s job that carries a pending release through: waiting -> releasing
-> restarting -> done. The job runs in the NEW process after the restart
too, which is how the census it yielded gets resumed.

Why a wait and not a refusal: a census takes ~3 h and a person asked for a
release once; making them come back and press again is the failure this
replaces. Why the restart's own gate is not enough: it PAUSES the census,
which cancels the goal in flight and re-runs it - a tainted row and half an
hour lost, every release.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, schedule,
)

try:
    from Vera.vera.evolve import release_core as core
except ImportError:                                        # pragma: no cover
    from vera.evolve import release_core as core           # type: ignore

log = logging.getLogger("vera.evolve.release")

KEY_PENDING = "vera:evolve:release:pending"
KEY_HISTORY = "vera:evolve:release:history"
_BY = "evolve.release"
_TICK_S = 30
_PROCESS_STARTED = datetime.now(timezone.utc).replace(microsecond=0)   # this process's boot
_LOCK = asyncio.Lock()


def _redis():
    return getattr(_orch, "REDIS", None)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


async def _call(name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


async def _pending() -> Optional[Dict[str, Any]]:
    r = _redis()
    if r is None:
        return None
    try:
        raw = await r.get(KEY_PENDING)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _save(p: Optional[Dict[str, Any]]) -> None:
    r = _redis()
    if r is None:
        return
    try:
        if p is None:
            await r.delete(KEY_PENDING)
        else:
            p["updated_at"] = _now().isoformat(timespec="seconds")
            await r.set(KEY_PENDING, json.dumps(p))
    except Exception:
        pass


async def _archive(p: Dict[str, Any]) -> None:
    r = _redis()
    if r is None:
        return
    try:
        await r.lpush(KEY_HISTORY, json.dumps(p))
        await r.ltrim(KEY_HISTORY, 0, 99)
    except Exception:
        pass


async def census_summary() -> Dict[str, Any]:
    """The census as /health reports it (busy, state, goal, control, by)."""
    try:
        import sys as _sys
        cc = _sys.modules.get("census_capabilities")
        if cc is not None and hasattr(cc, "census_health"):
            return await cc.census_health()
    except Exception as e:
        log.debug("census summary unavailable: %s", e)
    return {"busy": False, "state": "none", "control": "run"}


async def _write_yield(reason: str) -> bool:
    res = await _call("census.control.set", action="yield", reason=reason, by=_BY)
    return bool((res or {}).get("ok"))


async def _resume_if_ours() -> bool:
    """Lift a yield THIS module wrote; never someone else's control."""
    c = await census_summary()
    if c.get("control") in ("yield", "pause") and c.get("by") == _BY:
        res = await _call("census.control.set", action="resume",
                          reason="release: prod is back up", by=_BY)
        return bool((res or {}).get("ok"))
    return False


async def _do_release(p: Dict[str, Any]) -> Dict[str, Any]:
    """Merge, then restart. Updates and saves `p`."""
    p["status"] = "releasing"
    await _save(p)
    merged = await _call("evolve.bleeding_edge.promote_to_main", edge=p.get("edge") or "", confirm=True)
    p["merge"] = {k: merged.get(k) for k in ("ok", "refused", "error", "commit", "restart_required",
                                              "already_up_to_date", "into", "branch")} \
        if isinstance(merged, dict) else {"error": str(merged)}
    if not isinstance(merged, dict) or merged.get("error") or merged.get("refused"):
        p["status"] = "failed"
        p["last_why"] = str((merged or {}).get("error") or (merged or {}).get("refused") or "merge failed")
        await _save(None)
        await _archive(p)
        if p.get("yield_written"):
            await _resume_if_ours()
        await emit_event({"type": "evolve.release.failed", "release_id": p["id"], "why": p["last_why"]})
        return p
    if not p.get("restart", True):
        p["status"] = "done"
        p["finished_at"] = _now().isoformat(timespec="seconds")
        await _save(None)
        await _archive(p)
        if p.get("yield_written"):
            await _resume_if_ours()
        await emit_event({"type": "evolve.release.done", "release_id": p["id"], "commit": p["merge"].get("commit"),
                          "restarted": False})
        return p
    p["status"] = "restarting"
    p["restart_requested_at"] = _now().isoformat(timespec="seconds")
    await _save(p)
    await emit_event({"type": "evolve.release.restarting", "release_id": p["id"], "commit": p["merge"].get("commit")})
    res = await _call("sys.dev.restart", confirm=True, resume_census=True,
                      reason=f"release {p['id']}: main @ {str(p['merge'].get('commit') or '')[:10]} ({p.get('reason') or p.get('by')})")
    if not isinstance(res, dict) or not res.get("ok"):
        p["status"] = "failed"
        p["last_why"] = f"restart refused: {(res or {}).get('error') or (res or {}).get('note') or res}"
        await _save(None)
        await _archive(p)
        if p.get("yield_written"):
            await _resume_if_ours()
        await emit_event({"type": "evolve.release.failed", "release_id": p["id"], "why": p["last_why"]})
    return p


async def run_tick() -> Dict[str, Any]:
    p = await _pending()
    if not p:
        return {"action": "none"}
    census = await census_summary()
    act = core.decide(p, census, now=_now(), process_started_at=_PROCESS_STARTED)
    a = act.get("action")
    if a == "yield":
        if await _write_yield(f"release {p['id']}: let the goal in flight finish, then park"):
            p["yield_written"] = True
            p["last_why"] = "yield written; waiting for the harness to park"
            await _save(p)
            await emit_event({"type": "evolve.release.yielded", "release_id": p["id"]})
        return {"action": "yield", "pending": p}
    if a == "wait":
        p["waits"] = int(p.get("waits") or 0) + 1
        p["last_why"] = act.get("why", "")
        await _save(p)
        return {"action": "wait", "why": act.get("why"), "pending": p}
    if a == "release":
        p["last_why"] = act.get("why", "")
        await emit_event({"type": "evolve.release.go", "release_id": p["id"], "why": act.get("why")})
        return {"action": "release", "pending": await _do_release(p)}
    if a == "finish":
        resumed = await _resume_if_ours() if act.get("resume_census") else False
        p["status"] = "done"
        p["finished_at"] = _now().isoformat(timespec="seconds")
        p["census_resumed"] = resumed
        await _save(None)
        await _archive(p)
        await emit_event({"type": "evolve.release.done", "release_id": p["id"],
                          "commit": (p.get("merge") or {}).get("commit"), "restarted": True,
                          "census_resumed": resumed})
        return {"action": "finish", "pending": p}
    if a == "fail":
        p["status"] = "failed"
        p["last_why"] = act.get("why", "")
        await _save(None)
        await _archive(p)
        if p.get("yield_written"):
            await _resume_if_ours()
        await emit_event({"type": "evolve.release.failed", "release_id": p["id"], "why": p["last_why"]})
        return {"action": "fail", "pending": p}
    return {"action": "none"}


async def _tick() -> None:
    if _LOCK.locked():
        return
    async with _LOCK:
        try:
            await run_tick()
        except Exception as e:
            log.warning("evolve.release tick failed: %s", e)


schedule(_tick, _TICK_S, name="evolve.release.tick", skip_in_sandbox=True, singleton=True)


@capability("evolve.release.prod", memory="off",
            http_method="POST", http_path="/evolve/release/prod", http_tags=["evolve", "release"],
            description="Release bleeding-edge to prod: fast-forward main to the edge, then restart. "
                        "Checks for a census first: census=finish (default) waits until no census is in "
                        "flight; census=goal yields it (the goal in flight finishes, the harness parks, "
                        "the release goes, the census resumes after the restart); force=true releases "
                        "now (the restart's own gate pauses and re-runs the goal). A wait is carried by "
                        "a 30 s job - this call returns at once with held=true. Input: confirm (bool!), "
                        "edge, census, force, restart (bool, default true), reason. Output: {ok|held, "
                        "release_id, status, census, why}.")
async def cap_release_prod(confirm: bool = False, edge: str = "", census: str = "finish",
                           force: bool = False, restart: bool = True, reason: str = "",
                           trace_id=None) -> Dict[str, Any]:
    if not confirm:
        return {"ok": False, "refused": "confirmation-required",
                "note": "a release moves what prod serves and restarts it; pass confirm=true"}
    mode = "force" if force else (census or "finish").strip().lower()
    if mode not in core.MODES:
        return {"ok": False, "error": f"census must be one of {', '.join(core.MODES)}"}
    async with _LOCK:
        cur = await _pending()
        if cur:
            return {"ok": False, "held": True, "error": "a release is already pending",
                    "release_id": cur["id"], "status": cur["status"], "pending": cur,
                    "note": "evolve.release.cancel to drop it, or force=true after cancelling"}
        ck = getattr(_orch, "CALLER_KIND", None)
        try:
            by = (ck.get("") if hasattr(ck, "get") else str(ck or "")) or "release"
        except Exception:
            by = "release"
        p = core.new_pending(edge=edge or "", mode=mode, by=str(by), reason=reason, restart=restart,
                             now=_now(), release_id=uuid.uuid4().hex[:8])
        summary = await census_summary()
        ok, why = core.may_release(mode, summary)
        await emit_event({"type": "evolve.release.requested", "release_id": p["id"], "edge": edge,
                          "mode": mode, "census": summary.get("state"), "go_now": ok})
        if ok:
            await _save(p)
            p = await _do_release(p)
            return {"ok": p.get("status") in ("restarting", "done"), "release_id": p["id"],
                    "status": p.get("status"), "merge": p.get("merge"), "why": why,
                    "census": summary, "error": p.get("last_why") if p.get("status") == "failed" else ""}
        p["last_why"] = why
        await _save(p)
        if mode == "goal":
            if await _write_yield(f"release {p['id']}: let the goal in flight finish, then park"):
                p["yield_written"] = True
                await _save(p)
        return {"ok": True, "held": True, "release_id": p["id"], "status": "waiting", "mode": mode,
                "why": why, "census": summary,
                "note": "the release goes out when the census clears; evolve.release.status to watch, "
                        "evolve.release.cancel to drop it, evolve.release.prod force=true to go now"}


@capability("evolve.release.status", memory="off", silent=True,
            http_method="GET", http_path="/evolve/release/status", http_tags=["evolve", "release"],
            description="The pending prod release (if any), the census summary and what happens next; "
                        "plus the last few releases.")
async def cap_release_status(trace_id=None) -> Dict[str, Any]:
    p = await _pending()
    census = await census_summary()
    r = _redis()
    hist = []
    if r is not None:
        try:
            hist = [json.loads(x) for x in (await r.lrange(KEY_HISTORY, 0, 4) or [])]
        except Exception:
            hist = []
    return {"pending": p, "census": census, "summary": core.describe(p, census),
            "next": core.decide(p, census, now=_now(), process_started_at=_PROCESS_STARTED),
            "process_started_at": _PROCESS_STARTED.isoformat(timespec="seconds"), "history": hist}


@capability("evolve.release.cancel", memory="off",
            http_method="POST", http_path="/evolve/release/cancel", http_tags=["evolve", "release"],
            description="Drop the pending prod release. A census it yielded is resumed. Input: reason.")
async def cap_release_cancel(reason: str = "", trace_id=None) -> Dict[str, Any]:
    async with _LOCK:
        p = await _pending()
        if not p:
            return {"ok": False, "error": "no release pending"}
        if p.get("status") in ("releasing", "restarting"):
            return {"ok": False, "error": f"the release is {p['status']}; it cannot be cancelled now"}
        p["status"] = "cancelled"
        p["last_why"] = reason or "cancelled"
        await _save(None)
        await _archive(p)
        resumed = await _resume_if_ours() if p.get("yield_written") else False
        await emit_event({"type": "evolve.release.cancelled", "release_id": p["id"], "census_resumed": resumed})
        return {"ok": True, "release_id": p["id"], "census_resumed": resumed}


@capability("evolve.release.tick", memory="off",
            http_method="POST", http_path="/evolve/release/tick", http_tags=["evolve", "release"],
            description="Run the release job's pass by hand.")
async def cap_release_tick(trace_id=None) -> Dict[str, Any]:
    async with _LOCK:
        return await run_tick()
