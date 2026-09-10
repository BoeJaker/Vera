"""evolve.agents.rows - the Agents page's one table, one call.

Sessions, Board, Notes, Capacity and Swarm were five Loop Lab pages that
each showed the agents' work from a different store: the ingested Claude
Code / Codex sessions (classified by the session watch), the board's items,
the capture-plane notes, the subscription seats and the Ollama gate, and a
live cross-section of all of it. This capability reads those stores in one
go and hands back a row per agent session (agents_core) - or, in the other
mode, a row per board item - plus what the pages' headers carried: the
swarm's live counts, the seats and the gate. Loaded after evolve/, board/,
capacity and ide/ so their capabilities are registered when it is called
(it reaches them through the registry).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.evolve import agents_core as ac
except ImportError:                                   # pragma: no cover
    from vera.evolve import agents_core as ac

log = logging.getLogger("vera.evolve.agents")

WATCH_N = 500        # every ingested session (the old 60 ceiling silently cut the list)
PIPELINES_N = 200
LOOPS_N = 20
EDITORS_N = 40


def _ev():
    return sys.modules.get("evolve_capabilities")


async def _safe(coro, default):
    try:
        out = await coro
    except Exception as e:
        log.info("agents: a reader failed: %s", e)
        return default
    if isinstance(out, dict) and out.get("error") and not out.get("ok"):
        log.info("agents: a reader answered an error: %s", out.get("error"))
        return default
    return out


def _truthy(v: Any, default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@capability(
    "evolve.agents.rows", memory="off", silent=True,
    http_method="GET", http_path="/evolve/agents/rows", http_tags=["evolve", "board"],
    description=(
        "Every AGENT SESSION Loop Lab knows, in one shape: an ingested Claude Code / Codex "
        "session (the watch's classification: live, resumable, stalled, declared-block, "
        "finished-unreported, human), a session the board names (a codex run known only "
        "from its items), a live Vera improvement loop, an edit-queue agent - each with its "
        "board items (by lane), its pipelines, its sandboxes, its branches, one STATE word "
        "(live, working, queued, resumable, stalled, declared-block, finished-unreported, "
        "human, done, idle) and its last activity; live and working first. mode=items gives "
        "the other table: every board item, live lanes first, naming its session; mode=both "
        "carries both (the page's one poll). Also the "
        "swarm's live counts, the seats and the Ollama gate. Filters (sessions): agent, "
        "state, text, branch, hide_idle; (items): lane, agent, text, repo, project, branch, "
        "plan, hide_done; stalled_after_s (the watch's window); limit (int=400). Output: {mode, "
        "sessions[] | items[], count, total, "
        "summary, swarm, capacity{seats,ollama,summary}, watch{policy,summary}, any_live}."),
)
async def cap_evolve_agents_rows(mode: str = "sessions", agent: str = "", state: str = "", lane: str = "",
                                 text: str = "", branch: str = "", repo: str = "", project: str = "", plan: str = "",
                                 hide_idle: Any = False, hide_done: Any = False, limit: int = 400,
                                 stalled_after_s: Any = "", trace_id=None) -> Dict[str, Any]:
    ev = _ev()
    if ev is None:
        return {"error": "evolve module unavailable", "sessions": [], "items": [], "count": 0, "total": 0}
    wkw: Dict[str, Any] = {"max_sessions": WATCH_N}
    try:
        if stalled_after_s not in (None, "", 0, "0"):
            wkw["stalled_after_s"] = int(float(stalled_after_s))   # the Sessions page's "stalled after" pick
    except Exception:
        pass
    watch, bd, cap, pl, sb, im, eq = await asyncio.gather(
        _safe(ev._call("ide.claude_sessions.watch", **wkw), {}),
        _safe(ev._call("board.items"), {}),
        _safe(ev._call("capacity.status"), {}),
        _safe(ev.evolve_pipeline_list(limit=PIPELINES_N), {}),
        _safe(ev.evolve_sandbox_list(detail=False), {}),
        _safe(ev.evolve_improve_list(limit=LOOPS_N), {}),
        _safe(ev.evolve_editq_list(limit=EDITORS_N), {}),
    )
    items = (bd or {}).get("items") or []
    pipelines = (pl or {}).get("pipelines") or []
    sandboxes = (sb or {}).get("sandboxes") or []
    policy = (watch or {}).get("policy") if isinstance((watch or {}).get("policy"), dict) else {}
    try:
        recent_s = float(policy.get("stalled_after_s") or ac.RECENT_S)
    except Exception:
        recent_s = ac.RECENT_S
    rows = ac.session_rows((watch or {}).get("sessions") or [], items, pipelines, sandboxes,
                           (im or {}).get("sessions") or [], (eq or {}).get("queue") or [], recent_s=recent_s)
    by_id = {r["id"]: r for r in rows}
    item_rows = ac.item_rows(items, by_id)
    try:
        lim = max(1, int(limit))
    except Exception:
        lim = 400
    mode = str(mode or "").strip().lower()
    out: Dict[str, Any] = {
        "mode": mode if mode in ("items", "both") else "sessions",
        "summary": ac.summary(rows),
        "swarm": ac.swarm_counts(rows, items, sandboxes, pipelines),
        "capacity": {"seats": (cap or {}).get("seats") or [], "ollama": (cap or {}).get("ollama") or {},
                     "summary": (cap or {}).get("summary") or {}},
        "watch": {"policy": (watch or {}).get("policy") or {}, "summary": (watch or {}).get("summary") or {}},
        "any_live": any(r.get("live") for r in rows) or any(p.get("live") for p in pipelines if isinstance(p, dict)),
        "lanes": sorted({r["lane"] for r in item_rows if r.get("lane")}),
        "agents": sorted({r["agent"] for r in rows if r.get("agent")}),
        "plans": sorted({r["plan"] for r in item_rows if r.get("plan")}),
        "repos": sorted({r["repo"] for r in item_rows if r.get("repo")}),
        "projects": sorted({r["project"] for r in item_rows if r.get("project")}),
        "board_provider": (bd or {}).get("provider") or "",
    }
    if out["mode"] in ("items", "both"):
        shown = ac.filter_items(item_rows, lane=lane or "", agent=agent or "", text=text or "", repo=repo or "",
                                project=project or "", branch=branch or "", plan=plan or "",
                                hide_done=_truthy(hide_done, False))
        out.update({"items": shown[:lim], "items_count": len(shown), "items_total": len(item_rows)})
        if out["mode"] == "items":
            out.update({"count": len(shown), "total": len(item_rows)})
    if out["mode"] in ("sessions", "both"):
        shown = ac.filter_sessions(rows, agent=agent or "", state=state or "", text=text or "", branch=branch or "",
                                   hide_idle=_truthy(hide_idle, False))
        if out["mode"] == "both":
            # The items ride once, in `items`; a session's own are the ones whose
            # `session` names it (the page joins them) - half the payload of a poll.
            shown = [dict(r, items=[]) for r in shown]
        out.update({"sessions": shown[:lim], "count": len(shown), "total": len(rows)})
    return out
