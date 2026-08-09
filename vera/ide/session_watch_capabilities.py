# ============================================================================
# session_watch_capabilities.py — watch stalled Claude Code sessions (§6.6)
# ============================================================================
#
# The read side of auto-resume: assemble each ingested Claude Code session with
# its board claims + pipelines and classify what state it is in (live / stalled /
# resumable / declared-block / finished-unreported / human), so the Loop Lab
# Sessions pane can render it and a (later) auto-resume loop can act on it. All
# the decision logic is the pure, tested session_watch_core; this only joins data.
#
# The resume/release/policy WRITE actions + the periodic auto-loop + the pane are
# the next increment; classification (which already computes the right `action`)
# is the foundation they build on.
# ============================================================================

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import APP, capability, CAPABILITY_REGISTRY
from Vera.vera.ide import session_watch_core as sw

log = logging.getLogger("vera.ide.session_watch")


async def _call(name: str, **kw):
    reg = CAPABILITY_REGISTRY.get(name) or {}
    fn = reg.get("func")
    if not fn:
        return None
    try:
        return await fn(**kw)
    except Exception as e:
        log.debug("session_watch: %s failed: %s", name, e)
        return None


def _bucket(rows: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        k = r.get(key) or ""
        if k:
            out.setdefault(k, []).append(r)
    return out


if True:  # capability registration

    @capability(
        "ide.claude_sessions.watch", http_method="GET",
        http_path="/ide/claude_sessions/watch", http_tags=["ide", "claude_sessions"],
        memory="off", silent=True,
        description="Detect + classify stalled Claude Code sessions (swarm §6.6). Joins "
                    "ide.claude_sessions.list_sessions with each session's board claims "
                    "(items whose board-meta session == the session) and its non-terminal "
                    "evolve.pipeline records, then classifies each: live | stalled | resumable "
                    "| declared-block | finished-unreported | human | untracked, with the right "
                    "action (none/watch/resume/reconcile/wait). Read-only. Optional threshold "
                    "overrides: stalled_after_s, resumable_after_s (else §4.3 defaults 45m/90m). "
                    "Output: {ok, now, policy, summary:{by_state,total}, sessions:[{...,state,"
                    "action,reason,age_s,resume_ok,claims,pipelines}]}.",
    )
    async def cap_claude_sessions_watch(stalled_after_s: int = 0, resumable_after_s: int = 0,
                                        max_sessions: int = 60, trace_id=None) -> dict:
        now = time.time()
        pol = dict(sw.DEFAULT_POLICY)
        if stalled_after_s:
            pol["stalled_after_s"] = int(stalled_after_s)
        if resumable_after_s:
            pol["resumable_after_s"] = int(resumable_after_s)

        sess_res = await _call("ide.claude_sessions.list_sessions",
                               max_sessions=max_sessions) or {}
        sessions = sess_res.get("sessions") or []

        board_res = await _call("board.items") or {}
        claims_by_sid = _bucket(board_res.get("items") or [], "session")

        pipe_res = await _call("evolve.pipeline.list", limit=100) or {}
        pipes_by_sid = _bucket(pipe_res.get("pipelines") or [], "session_id")

        out: List[Dict[str, Any]] = []
        by_state: Dict[str, int] = {}
        for s in sessions:
            sid = s.get("claude_session_id", "")
            claims = claims_by_sid.get(sid, [])
            pipes = pipes_by_sid.get(sid, [])
            c = sw.classify_session(s, claims, pipes, now, policy=pol)
            by_state[c["state"]] = by_state.get(c["state"], 0) + 1
            out.append({
                "claude_session_id": sid,
                "project_dir": s.get("project_dir", ""),
                "last_ts": s.get("last_ts", ""),
                "turns": s.get("turns", 0),
                "commit_count": len(s.get("commits") or []),
                "state": c["state"], "action": c["action"], "reason": c["reason"],
                "age_s": c["age_s"], "resume_ok": c["resume_ok"],
                "claims": [{"id": x.get("id"), "lane": x.get("lane"),
                            "title": x.get("title")} for x in claims],
                "pipelines": [{"id": p.get("id"), "decision": p.get("decision"),
                               "branch": p.get("branch")} for p in pipes],
            })
        # candidates first (resumable/finished-unreported/stalled), then the rest
        order = {"resumable": 0, "finished-unreported": 1, "stalled": 2,
                 "declared-block": 3, "live": 4, "human": 5, "untracked": 6}
        out.sort(key=lambda r: order.get(r["state"], 9))
        return {"ok": True, "now": now, "policy": pol,
                "summary": {"by_state": by_state, "total": len(out)}, "sessions": out}
