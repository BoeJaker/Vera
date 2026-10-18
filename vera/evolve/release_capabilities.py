"""The prod release, gated on the census.

  evolve.release.prod     fast-forward main to the edge and restart prod -
                          but not while a census is in flight: wait for it
                          to finish (`census=finish`, default), or yield it
                          and go once the goal in flight has parked
                          (`census=goal`), or `force=true` to go now.
  evolve.release.status   the pending release, the census, what is next
  evolve.release.cancel   drop the pending release (resumes a census it yielded)
  evolve.release.node_sync  the node sync a release queued (status, or run it now)

With `sync_nodes` (default on) a finished release also brings the nodes onto
it: the node-side components (provision.component.sync for every syncable
one), every node worker (nodes.workers.sync) and the warm model slots
(ollama.warm.apply). It is queued when the release is done and carried out by
the same 30 s job - in the NEW process after a restart, never while a census
goal is in flight. A census the release parked stays parked until that sync
has run, then is resumed (release_core.hold_census_for_sync). The decisions
are release_core.node_sync_*.

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
    from Vera.vera.evolve import release_lane_core as lane
except ImportError:                                        # pragma: no cover
    from vera.evolve import release_lane_core as lane      # type: ignore
try:
    from Vera.vera.evolve import release_core as core
except ImportError:                                        # pragma: no cover
    from vera.evolve import release_core as core           # type: ignore

log = logging.getLogger("vera.evolve.release")

KEY_PENDING = "vera:evolve:release:pending"
KEY_HISTORY = "vera:evolve:release:history"
KEY_NODE_SYNC = "vera:evolve:release:node_sync"
KEY_LANE = "vera:evolve:release:lane"          # hash  request id -> json (B1.0 release lane)
_BY = "evolve.release"
_TICK_S = 30
_PROCESS_STARTED = datetime.now(timezone.utc).replace(microsecond=0)   # this process's boot
_LOCK = asyncio.Lock()
_SYNC_TASK: Dict[str, Any] = {"task": None}


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
    """The census as /health reports it (busy, state, goal, control, by), plus `due`:
    census schedules due right now when none is in flight (release_core.may_release
    holds a census=finish release for them, so a pair's arms share one build)."""
    s: Dict[str, Any] = {"busy": False, "state": "none", "control": "run"}
    try:
        import sys as _sys
        cc = _sys.modules.get("census_capabilities")
        if cc is not None and hasattr(cc, "census_health"):
            s = dict(await cc.census_health() or s)
    except Exception as e:
        log.debug("census summary unavailable: %s", e)
    if not s.get("busy"):
        try:
            import sys as _sys
            sc = (_sys.modules.get("schedule_capabilities")
                  or _sys.modules.get("Vera.vera.evolve.schedule_capabilities"))
            if sc is not None and hasattr(sc, "census_due_now"):
                s["due"] = await sc.census_due_now()
        except Exception as e:
            log.debug("census due-now unavailable: %s", e)
    # Agent loops alive in this process: a restart kills them (release_core.may_release).
    try:
        import sys as _sys
        dm = (_sys.modules.get("dag_workshop_capabilities")
              or _sys.modules.get("Vera.vera.dag.dag_workshop_capabilities"))
        ids = list(core.live_loop_ids(getattr(dm, "_AGENT_LOOP_TASKS", None) or {}))
        # ...and runs started through /mcp/call (census goals, probes), which have no task
        # in that registry: their Redis run state (dag._live_loop_session_ids).
        _live = getattr(dm, "_live_loop_session_ids", None)
        if _live is not None:
            for sid in await _live():
                if sid not in ids:
                    ids.append(sid)
        s["loops"] = ids
    except Exception as e:
        log.debug("live loops unavailable: %s", e)
    # Gates RUNNING in this process (evolve._GATES_ACTIVE). Queued ones are left out: they
    # wait for the release (DRAIN_KEY), and counting them would deadlock the two.
    try:
        import sys as _sys
        em = (_sys.modules.get("evolve_capabilities")
              or _sys.modules.get("Vera.vera.evolve.evolve_capabilities"))
        s["gates"] = [dict(g) for g in (getattr(em, "_GATES_ACTIVE", None) or {}).values()
                      if g.get("state") == "running"]
    except Exception as e:
        log.debug("running gates unavailable: %s", e)
    # Protected scheduled test runs (release_core.may_release), from the schedule's Redis run list
    try:
        import sys as _sys
        sc = (_sys.modules.get("schedule_capabilities")
              or _sys.modules.get("Vera.vera.evolve.schedule_capabilities"))
        if sc is not None and hasattr(sc, "protected_running"):
            s["protected"] = await sc.protected_running()
    except Exception as e:
        log.debug("protected runs unavailable: %s", e)
    return s


async def _drain(on: bool) -> None:
    """Hold (or drop) the gate drain key - see release_core.DRAIN_KEY."""
    r = _redis()
    if r is None:
        return
    try:
        if on:
            await r.set(core.DRAIN_KEY, "1", ex=core.DRAIN_TTL_S)
        else:
            await r.delete(core.DRAIN_KEY)
    except Exception as e:
        log.debug("release drain key: %s", e)


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
        await _queue_node_sync(p)
        if p.get("yield_written") and not core.hold_census_for_sync(p):
            await _resume_if_ours()
        await emit_event({"type": "evolve.release.done", "release_id": p["id"], "commit": p["merge"].get("commit"),
                          "restarted": False})
        return p
    p["status"] = "restarting"
    p["restart_requested_at"] = _now().isoformat(timespec="seconds")
    await _save(p)
    await emit_event({"type": "evolve.release.restarting", "release_id": p["id"], "commit": p["merge"].get("commit")})
    # force: may_release already held for protected runs before the merge - a merged release must restart
    res = await _call("sys.dev.restart", confirm=True, resume_census=True, force=True,
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


async def _node_sync_rec() -> Optional[Dict[str, Any]]:
    r = _redis()
    try:
        raw = await r.get(KEY_NODE_SYNC) if r is not None else None
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _node_sync_save(rec: Dict[str, Any]) -> None:
    r = _redis()
    try:
        if r is not None:
            rec["updated_at"] = _now().isoformat(timespec="seconds")
            await r.set(KEY_NODE_SYNC, json.dumps(rec), ex=14 * 86400)
    except Exception:
        pass


async def _queue_node_sync(p: Dict[str, Any]) -> None:
    if not p.get("sync_nodes", True):
        return
    rec = core.new_node_sync(release_id=p["id"], commit=str((p.get("merge") or {}).get("commit") or ""),
                             now=_now(), resume_census=core.hold_census_for_sync(p))
    try:
        rec = core.carry_census_resume(await _node_sync_rec(), rec)
    except Exception as e:
        log.debug("node sync: could not read the previous record: %s", e)
    await _node_sync_save(rec)
    await emit_event({"type": "evolve.release.node_sync", "release_id": p["id"], "status": "pending"})


def _syncable_components() -> list:
    import sys as _sys
    comp = _sys.modules.get("components_capabilities")
    table = getattr(comp, "_COMPONENTS", None) or {}
    return sorted(k for k, c in table.items() if (c or {}).get("sync"))


async def _run_node_sync(rec: Dict[str, Any]) -> Dict[str, Any]:
    """One pass: every syncable component, then the workers, then the warm
    slots. Each step's outcome is kept; one failing does not stop the rest."""
    rec["status"] = "running"
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    await _node_sync_save(rec)
    results: Dict[str, Dict[str, Any]] = {}
    for comp in _syncable_components():
        res = await _call("provision.component.sync", component=comp, dry_run=False)
        deployed = sorted((res or {}).get("results") or {}) if isinstance(res, dict) else []
        results[comp] = {"ok": bool(isinstance(res, dict) and res.get("ok")),
                         "deployed": deployed,
                         "error": str((res or {}).get("error") or "")[:300] if isinstance(res, dict) else str(res)[:300]}
        rec["results"] = results
        await _node_sync_save(rec)
    tres = await _call("nodes.ollama.tap", dry_run=False)
    if isinstance(tres, dict):
        rows = tres.get("nodes") or []
        bad = [x for x in rows if (x.get("result") or {}).get("ok") is False
               and not (x.get("result") or {}).get("skipped")]
        results["tap"] = {"ok": not bad and not tres.get("error"),
                          "refreshed": sorted(x["instance"] for x in rows
                                              if (x.get("result") or {}).get("refreshed")),
                          "skipped": sorted(x["instance"] for x in rows
                                            if (x.get("result") or {}).get("skipped")),
                          "error": str(tres.get("error") or "; ".join(
                              f"{x['instance']}: {(x.get('result') or {}).get('error', '')[:100]}"
                              for x in bad))[:300]}
    wres = await _call("nodes.workers.sync", limit=16)
    results["workers"] = {"ok": bool(isinstance(wres, dict) and wres.get("ok")
                                     and all(v.get("ok") for v in (wres.get("results") or {}).values()
                                             if isinstance(v, dict))),
                          "refreshed": sorted((wres or {}).get("results") or {}) if isinstance(wres, dict) else [],
                          "blocked": ((wres or {}).get("plan") or {}).get("blocked", "") if isinstance(wres, dict) else "",
                          "error": str((wres or {}).get("error") or "")[:300] if isinstance(wres, dict) else str(wres)[:300]}
    if results["workers"]["blocked"] and "census" in results["workers"]["blocked"]:
        results["workers"].update(ok=False, error=results["workers"]["blocked"])
    if "nodes.testenv.sync" in CAPABILITY_REGISTRY:
        # the nodes' pytest venvs follow the gate image (a no-op where current)
        tv = await _call("nodes.testenv.sync", dry_run=False)
        results["testenv"] = {"ok": bool(isinstance(tv, dict) and tv.get("ok")),
                              "error": (str((tv or {}).get("error") or "") or "; ".join(
                                  f"{h}: {v.get('error') or ', '.join(v.get('failed') or [])}"
                                  for h, v in ((tv or {}).get("nodes") or {}).items()
                                  if not v.get("ok") or v.get("failed")))[:300]
                              if isinstance(tv, dict) else str(tv)[:300]}
    if "ollama.warm.apply" in CAPABILITY_REGISTRY:
        ares = await _call("ollama.warm.apply", dry_run=False)
        results["warm"] = {"ok": bool(isinstance(ares, dict) and not ares.get("error")),
                           "error": str((ares or {}).get("error") or "")[:300] if isinstance(ares, dict) else ""}
    status, why = core.node_sync_outcome(results)
    if status == "pending":            # refused for a census, not a failure: not an attempt
        rec["attempts"] = max(0, int(rec.get("attempts") or 1) - 1)
    rec.update(status=status, last_why=why, results=results)
    if status == "done":
        rec["finished_at"] = _now().isoformat(timespec="seconds")
    await _node_sync_save(rec)
    await emit_event({"type": "evolve.release.node_sync", "release_id": rec.get("release_id"),
                      "status": status, "why": why})
    return rec


async def _maybe_node_sync() -> Dict[str, Any]:
    t = _SYNC_TASK.get("task")
    running_here = bool(t is not None and not t.done())
    rec = await _node_sync_rec()
    ok, why = core.node_sync_due(rec, await census_summary(), now=_now(), running_here=running_here)
    if ok and rec is not None:
        _SYNC_TASK["task"] = asyncio.create_task(_run_node_sync(rec))
    elif core.node_sync_releases_census(rec, why):
        await _release_held_census(rec)
    return {"due": ok, "why": why}


async def _release_held_census(rec: Dict[str, Any]) -> None:
    """Resume the census the release held parked for the node sync (once)."""
    rec["resume_census"] = False
    rec["census_resumed"] = await _resume_if_ours()
    await _node_sync_save(rec)
    await emit_event({"type": "evolve.release.node_sync", "release_id": rec.get("release_id"),
                      "status": rec.get("status"), "census_resumed": rec["census_resumed"]})


async def _lane_load() -> list:
    r = _redis()
    if r is None:
        return []
    try:
        raw = await r.hgetall(KEY_LANE) or {}
    except Exception:
        return []
    out = []
    for v in raw.values():
        try:
            out.append(json.loads(v if isinstance(v, str) else v.decode()))
        except Exception:
            continue
    return out


async def _lane_put(reqs) -> None:
    r = _redis()
    if r is None:
        return
    for q in reqs:
        await r.hset(KEY_LANE, q["id"], json.dumps(q))


async def _last_release_done() -> Optional[datetime]:
    r = _redis()
    if r is None:
        return None
    try:
        last = json.loads((await r.lrange(KEY_HISTORY, 0, 0) or [b"{}"])[0])
    except Exception:
        return None
    return core.parse_iso(last.get("finished_at") or last.get("requested_at") or "")


async def _full_tier_last() -> Optional[Dict[str, Any]]:
    """The latest full critical run of bleeding-edge, kept apart from the gate-heavy history (vera-d2's review)."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = await r.get(lane.KEY_FULL_TIER_LAST)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _lane_step(pending: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """B1.0: when the batch is due and nothing is in flight, open ONE release carrying every queued request."""
    reqs = await _lane_load()
    now = _now()
    r = _redis()
    gone = [q["id"] for q in reqs if lane.expired(q, now)]
    if gone and r is not None:
        await r.hdel(KEY_LANE, *gone)
    go, why = lane.should_fire(reqs, now, await _last_release_done(), bool(pending))
    if not go:
        return {"lane": "wait", "why": why}
    held, hwhy = lane.full_tier_hold(await _full_tier_last())
    if held:
        if r is not None and not await r.set("vera:evolve:release:lane:held_note", hwhy[:200], nx=True, ex=3600):
            return {"lane": "held", "why": hwhy}
        await emit_event({"type": "evolve.release.lane.held", "why": hwhy, "requests": [x["id"] for x in lane.queued(reqs)]})
        log.warning("release lane held: %s", hwhy)
        return {"lane": "held", "why": hwhy}
    q = lane.queued(reqs)
    res = await _open_release(mode=lane.batch_mode(q), edge="", by="release-lane", reason=lane.batch_reason(q),
                              restart=True, sync_nodes=True)
    rid = res.get("release_id") or ""
    if rid:
        await _lane_put(lane.mark_carried(q, rid, now))
        await emit_event({"type": "evolve.release.lane.fired", "release_id": rid, "requests": [x["id"] for x in q]})
    return {"lane": "fired", "release_id": rid, "requests": len(q), "result": res}


async def run_tick() -> Dict[str, Any]:
    try:
        await _maybe_node_sync()
    except Exception as e:
        log.warning("release node sync: %s", e)
    p = await _pending()
    if not p:
        try:
            ls = await _lane_step(None)
        except Exception as e:
            log.warning("release lane: %s", e)
            ls = {"lane": "error", "why": str(e)[:200]}
        return {"action": "lane" if ls.get("lane") == "fired" else "none", "lane": ls}
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
        await _drain(core.waiting_on_gates(act.get("why")))
        return {"action": "wait", "why": act.get("why"), "pending": p}
    await _drain(False)
    if a == "release":
        p["last_why"] = act.get("why", "")
        await emit_event({"type": "evolve.release.go", "release_id": p["id"], "why": act.get("why")})
        return {"action": "release", "pending": await _do_release(p)}
    if a == "finish":
        hold = core.hold_census_for_sync(p)
        resumed = await _resume_if_ours() if act.get("resume_census") and not hold else False
        p["status"] = "done"
        p["finished_at"] = _now().isoformat(timespec="seconds")
        p["census_resumed"] = resumed
        await _save(None)
        await _archive(p)
        await _queue_node_sync(p)
        await emit_event({"type": "evolve.release.done", "release_id": p["id"],
                          "commit": (p.get("merge") or {}).get("commit"), "restarted": True,
                          "census_resumed": resumed, "census_held_for_sync": hold})
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


async def _open_release(*, mode: str, edge: str, by: str, reason: str, restart: bool, sync_nodes: bool) -> Dict[str, Any]:
    """Open a release (the caller holds _LOCK and has checked nothing is pending): go now if the census and gates
    allow, else leave it pending for the tick. Shared by evolve.release.prod and the release lane."""
    p = core.new_pending(edge=edge or "", mode=mode, by=str(by), reason=reason, restart=restart,
                         now=_now(), release_id=uuid.uuid4().hex[:8])
    p["sync_nodes"] = bool(sync_nodes)
    summary = await census_summary()
    ok, why = core.may_release(mode, summary, restart=bool(restart))
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
    await _drain(core.waiting_on_gates(why))
    if mode == "goal" and core.goal_yield_needed(summary):
        if await _write_yield(f"release {p['id']}: let the goal in flight finish, then park"):
            p["yield_written"] = True
            await _save(p)
    return {"ok": True, "held": True, "release_id": p["id"], "status": "waiting", "mode": mode,
            "why": why, "census": summary,
            "note": "the release goes out when the census clears; evolve.release.status to watch, "
                    "evolve.release.cancel to drop it, evolve.release.prod force=true to go now"}


@capability("evolve.release.prod", memory="off",
            http_method="POST", http_path="/evolve/release/prod", http_tags=["evolve", "release"],
            description="Release bleeding-edge to prod: fast-forward main to the edge, then restart. "
                        "Checks for a census first: census=finish (default) waits until no census is in "
                        "flight; census=goal yields it (the goal in flight finishes, the harness parks, "
                        "the release goes, the census resumes after the restart); force=true releases "
                        "now (the restart's own gate pauses and re-runs the goal). A wait is carried by "
                        "a 30 s job - this call returns at once with held=true. sync_nodes (default "
                        "true) then brings the nodes onto the release once it is done: every syncable "
                        "node component (provision.component.sync), the node workers and the warm "
                        "model slots - run by the same job in the new process, never while a census "
                        "goal is in flight (evolve.release.node_sync shows it). Input: confirm "
                        "(bool!), edge, census, force, restart (bool, default true), sync_nodes "
                        "(bool, default true), reason. Output: {ok|held, release_id, status, census, why}.")
async def cap_release_prod(confirm: bool = False, edge: str = "", census: str = "finish",
                           force: bool = False, restart: bool = True, reason: str = "",
                           sync_nodes: bool = True, trace_id=None) -> Dict[str, Any]:
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
        return await _open_release(mode=mode, edge=edge, by=str(by), reason=reason, restart=restart,
                                   sync_nodes=sync_nodes)


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
            "node_sync": await _node_sync_rec(),
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
        await _drain(False)                 # gates held for this release may start now
        await _archive(p)
        resumed = await _resume_if_ours() if p.get("yield_written") else False
        await emit_event({"type": "evolve.release.cancelled", "release_id": p["id"], "census_resumed": resumed})
        return {"ok": True, "release_id": p["id"], "census_resumed": resumed}


@capability("evolve.release.node_sync", memory="off",
            http_method="POST", http_path="/evolve/release/node_sync", http_tags=["evolve", "release", "nodes"],
            description="The node sync the last release queued: {release_id, status (pending | "
                        "running | done | failed), results per step (each syncable component, "
                        "workers, warm slots), attempts, last_why}. run=true queues one now for the "
                        "running release (e.g. after a release made with sync_nodes=false, or a "
                        "failed sync) - it still waits for any census goal in flight.")
async def cap_release_node_sync(run: bool = False, trace_id=None) -> Dict[str, Any]:
    if run:
        if _orch.is_dev_sandbox():
            return {"ok": False, "error": "this is a dev sandbox: the nodes are prod's"}
        rec = core.new_node_sync(release_id="manual-" + uuid.uuid4().hex[:6], commit="", now=_now())
        await _node_sync_save(rec)
        due = await _maybe_node_sync()
        return {"ok": True, "queued": rec["release_id"], **due}
    rec = await _node_sync_rec()
    t = _SYNC_TASK.get("task")
    return {"ok": True, "node_sync": rec, "running_here": bool(t is not None and not t.done()),
            "syncable": _syncable_components()}


@capability("evolve.release.tick", memory="off",
            http_method="POST", http_path="/evolve/release/tick", http_tags=["evolve", "release"],
            description="Run the release job's pass by hand.")
async def cap_release_tick(trace_id=None) -> Dict[str, Any]:
    async with _LOCK:
        return await run_tick()



# ── the release lane (backbone B1.0, owner's yes 18 Oct 2026) ────────────────────────────────────────────────────
@capability("evolve.release.submit", memory="off",
            http_method="POST", http_path="/evolve/release/submit", http_tags=["evolve", "release"],
            description="Ask for a prod release through the RELEASE LANE instead of releasing yourself: every release "
                        "restarts prod for every session, so requests are batched into one release of bleeding-edge - "
                        "the lane waits up to 10 min for others to join, keeps 15 min between lane releases unless a "
                        "request is urgent, and the release still waits for census goals, gates and protected runs. "
                        "Input: confirm (bool!), reason (str!), by (str - your session), census ('goal' default | "
                        "'finish'), urgent (bool). Output: {ok, request, fires_at, queue}.")
async def cap_release_submit(confirm: bool = False, reason: str = "", by: str = "", census: str = "goal",
                             urgent: bool = False, trace_id=None) -> Dict[str, Any]:
    if not confirm:
        return {"ok": False, "refused": "confirmation-required",
                "note": "a release moves what prod serves and restarts it; pass confirm=true"}
    if not str(reason or "").strip():
        return {"ok": False, "error": "reason required - say what the release carries"}
    r = _redis()
    if r is None:
        return {"ok": False, "error": "no redis - the lane cannot hold a request"}
    sha = ""
    try:
        import subprocess as _sp
        sha = _sp.run(["git", "rev-parse", "--short=12", "bleeding-edge"], capture_output=True, text=True,
                      cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]), timeout=10).stdout.strip()
    except Exception:
        pass
    req = lane.new_request(rid=uuid.uuid4().hex[:8], by=by or "session", reason=reason, now=_now(), census=census,
                           urgent=bool(urgent), edge_sha=sha)
    await _lane_put([req])
    reqs = await _lane_load()
    t = lane.next_fire_at(reqs, await _last_release_done())
    await emit_event({"type": "evolve.release.lane.submitted", "request": req})
    return {"ok": True, "request": req, "fires_at": t.isoformat(timespec="seconds") if t else "",
            "queue": lane.queued(reqs),
            "note": "the lane releases the batch when due; evolve.release.lane to watch, "
                    "evolve.release.withdraw to take this back"}


@capability("evolve.release.lane", memory="off", silent=True,
            http_method="GET", http_path="/evolve/release/lane", http_tags=["evolve", "release"],
            description="The release lane: queued requests (oldest first), when the batch fires and why it waits, "
                        "and the requests of the last day with the release that carried each. Output: {queued, "
                        "fires_at, why, recent, pending}.")
async def cap_release_lane(trace_id=None) -> Dict[str, Any]:
    reqs = await _lane_load()
    p = await _pending()
    last = await _last_release_done()
    t = lane.next_fire_at(reqs, last)
    go, why = lane.should_fire(reqs, _now(), last, bool(p))
    held, hwhy = lane.full_tier_hold(await _full_tier_last())
    if held and lane.queued(reqs):
        go, why = False, hwhy        # full_tier below shows the hold whether or not anything is queued
    recent = sorted([q for q in reqs if q.get("status") != "queued"], key=lambda q: str(q.get("done_at") or ""), reverse=True)
    return {"queued": lane.queued(reqs), "fires_at": t.isoformat(timespec="seconds") if t else "",
            "due": go, "why": why, "recent": recent[:30], "full_tier": {"held": held, "why": hwhy},
            "pending": {"id": p.get("id"), "status": p.get("status"), "why": p.get("last_why")} if p else None}


@capability("evolve.release.withdraw", memory="off",
            http_method="POST", http_path="/evolve/release/withdraw", http_tags=["evolve", "release"],
            description="Take a queued release request back out of the lane. Input: id (str!). Output: {ok, request}.")
async def cap_release_withdraw(id: str = "", trace_id=None) -> Dict[str, Any]:
    reqs = {q["id"]: q for q in await _lane_load()}
    q = reqs.get(str(id or ""))
    if not q:
        return {"ok": False, "error": "no such request"}
    if q.get("status") != "queued":
        return {"ok": False, "error": f"request already {q.get('status')}", "request": q}
    q = dict(q, status="withdrawn", done_at=_now().isoformat(timespec="seconds"))
    await _lane_put([q])
    return {"ok": True, "request": q}
