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

import json

from Vera.vera.capability_orchestration import APP, capability, CAPABILITY_REGISTRY, emit_event
import Vera.vera.capability_orchestration as _orch
from Vera.vera.ide import session_watch_core as sw

log = logging.getLogger("vera.ide.session_watch")

_POLICY_KEY = "vera:session_watch:policy"      # JSON policy overrides
_ATTEMPTS_KEY = "vera:session_watch:attempts"  # hash: claude_session_id -> resume count


def _redis():
    return getattr(_orch, "REDIS", None)


async def _get_policy() -> dict:
    pol = dict(sw.DEFAULT_POLICY)
    pol["auto"] = False   # auto-resume OFF by default (HITL first, §6.6)
    r = _redis()
    if r:
        try:
            raw = await r.get(_POLICY_KEY)
            if raw:
                pol.update(json.loads(raw if isinstance(raw, str) else raw.decode()))
        except Exception as e:
            log.debug("session_watch policy read: %s", e)
    return pol


async def _attempts(sid: str) -> int:
    r = _redis()
    if not r or not sid:
        return 0
    try:
        v = await r.hget(_ATTEMPTS_KEY, sid)
        return int(v) if v else 0
    except Exception:
        return 0


async def _bump_attempts(sid: str) -> int:
    r = _redis()
    if not r or not sid:
        return 1
    try:
        return int(await r.hincrby(_ATTEMPTS_KEY, sid, 1))
    except Exception:
        return 1


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
                            "title": x.get("title"), "agent": x.get("agent")}
                           for x in claims],
                "pipelines": [{"id": p.get("id"), "decision": p.get("decision"),
                               "branch": p.get("branch")} for p in pipes],
            })
        # candidates first (resumable/finished-unreported/stalled), then the rest
        order = {"resumable": 0, "finished-unreported": 1, "stalled": 2,
                 "declared-block": 3, "live": 4, "human": 5, "untracked": 6}
        out.sort(key=lambda r: order.get(r["state"], 9))
        return {"ok": True, "now": now, "policy": pol,
                "summary": {"by_state": by_state, "total": len(out)}, "sessions": out}

    async def _find_watched(sid: str):
        w = await _call("ide.claude_sessions.watch") or {}
        for s in (w.get("sessions") or []):
            if s.get("claude_session_id") == sid:
                return s
        return None

    @capability(
        "ide.claude_sessions.resume", http_method="POST",
        http_path="/ide/claude_sessions/resume", http_tags=["ide", "claude_sessions"],
        memory="on",
        description="Resume a stalled Claude Code session (§6.6). GUARDED: refuses a human "
                    "takeover, a declared blocked:quota (wait for its return), a "
                    "finished-unreported session (reconcile, never re-run), and a session past "
                    "max_resume_attempts (escalate). Records the attempt, ANNOUNCES it (event + a "
                    "board 'resume' comment on each claimed item), then runs ide.remote.run with "
                    "--resume on the given target (native continue). Auto-picking the target/seat "
                    "is the orchestrator (§0.3) — for now pass instance_id. Inputs: "
                    "claude_session_id (str!), instance_id (str! — target to resume on), task "
                    "(str — override the board-assembled brief). Output: {ok, mode, attempt, run}.",
    )
    async def cap_claude_sessions_resume(claude_session_id: str = "", instance_id: str = "",
                                         task: str = "", trace_id=None) -> dict:
        if not claude_session_id:
            return {"ok": False, "error": "claude_session_id required"}
        s = await _find_watched(claude_session_id)
        if not s:
            return {"ok": False, "error": "session not found in watch"}
        state = s.get("state")
        if state == "human":
            return {"ok": False, "error": "a human took this over — refusing to resume"}
        if state == "declared-block":
            return {"ok": False, "error": "declared blocked:quota — wait for its expected return"}
        if state == "finished-unreported":
            return {"ok": False, "action": "reconcile",
                    "error": "finished-unreported — reconcile the board, do NOT re-run the work"}
        pol = await _get_policy()
        att = await _attempts(claude_session_id)
        if att >= pol["max_resume_attempts"]:
            return {"ok": False, "attempts": att,
                    "error": f"max resume attempts ({pol['max_resume_attempts']}) reached — "
                             "escalate to a human, don't relaunch"}
        if not instance_id:
            return {"ok": False, "error": "instance_id required — pick the target to resume on "
                                          "(auto-dispatch/orchestrator not built yet, §0.3)"}
        claims = s.get("claims") or []
        # Re-brief from the board (durable state), NOT a transcript replay (§6.6).
        brief = task or (
            "Resume the stalled work. Board claim(s): "
            + "; ".join(f"#{c.get('id')} {c.get('title')}" for c in claims)
            + ". Re-read the board and reconcile before continuing." if claims else
            "Resume the stalled session; re-read the board and reconcile before continuing.")
        attempt = await _bump_attempts(claude_session_id)
        await emit_event({"type": "ide.claude_sessions.resume", "claude_session_id": claude_session_id,
                          "instance_id": instance_id, "attempt": attempt})
        for c in claims:
            await _call("board.comment", id=c.get("id"), frm="session-watch", kind="resume",
                        body=f"auto-resume attempt {attempt} on {instance_id} "
                             f"(session {claude_session_id[:12]}).")
        run = await _call("ide.remote.run", instance_id=instance_id, task=brief,
                          engine="claude", resume_session_id=claude_session_id, source="resume")
        return {"ok": bool(run and run.get("ok")), "mode": "native-resume",
                "attempt": attempt, "instance_id": instance_id, "run": run}

    @capability(
        "ide.claude_sessions.release", http_method="POST",
        http_path="/ide/claude_sessions/release", http_tags=["ide", "claude_sessions"],
        memory="on",
        description="Release a dead/stale session's board claims back to the pool (§6.6): for each "
                    "open claim, post a withdraw (cancels the §3.3 lease) + a release note and move "
                    "it back to 'ready' so another agent can pick it up. Announced. Inputs: "
                    "claude_session_id (str!), reason (str). Output: {ok, released, count}.",
    )
    async def cap_claude_sessions_release(claude_session_id: str = "", reason: str = "",
                                          trace_id=None) -> dict:
        if not claude_session_id:
            return {"ok": False, "error": "claude_session_id required"}
        s = await _find_watched(claude_session_id)
        if not s:
            return {"ok": False, "error": "session not found in watch"}
        released = []
        for c in (s.get("claims") or []):
            cid = c.get("id")
            if not cid:
                continue
            agent = c.get("agent")
            if agent:   # cancel the lease so resolve_claim frees it
                await _call("board.comment", id=cid, frm=agent, kind="withdraw",
                            body=f"released from stale session {claude_session_id[:12]}"
                                 + (f": {reason}" if reason else ""))
            await _call("board.item.move", id=cid, lane="ready")
            released.append(cid)
        await emit_event({"type": "ide.claude_sessions.release",
                          "claude_session_id": claude_session_id, "released": released})
        return {"ok": True, "released": released, "count": len(released)}

    @capability(
        "ide.claude_sessions.policy", http_method="POST",
        http_path="/ide/claude_sessions/policy", http_tags=["ide", "claude_sessions"],
        memory="off",
        description="Get or set the auto-resume policy (§4.3/§6.6). Pass no args to GET the "
                    "effective policy; pass any of stalled_after_s, resumable_after_s, "
                    "max_resume_attempts, auto (bool — auto-resume on/off, default OFF) to SET "
                    "(persisted cross-process in Redis). Output: {ok, policy, changed}.",
    )
    async def cap_claude_sessions_policy(stalled_after_s: int = 0, resumable_after_s: int = 0,
                                         max_resume_attempts: int = 0, auto: str = "",
                                         trace_id=None) -> dict:
        pol = await _get_policy()
        changed = {}
        if stalled_after_s:
            changed["stalled_after_s"] = int(stalled_after_s)
        if resumable_after_s:
            changed["resumable_after_s"] = int(resumable_after_s)
        if max_resume_attempts:
            changed["max_resume_attempts"] = int(max_resume_attempts)
        if auto:   # "on"/"off"/"1"/"0"/"true"/"false" — explicit set only
            changed["auto"] = str(auto).strip().lower() in ("1", "true", "yes", "on")
        if changed:
            pol.update(changed)
            r = _redis()
            if r:
                try:
                    await r.set(_POLICY_KEY, json.dumps(pol))
                except Exception as e:
                    return {"ok": False, "error": f"policy save failed: {e}", "policy": pol}
        return {"ok": True, "policy": pol, "changed": changed}
