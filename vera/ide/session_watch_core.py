"""Pure detect→classify for stalled Claude Code sessions (swarm §6.6).

§4.2 makes the board the durable state so a killed session loses nothing — but
that only pays off if something notices the session died and acts. This module
is the pure heart of that: given a session's last activity + its board claims +
its pipelines, classify what state it is in and what the RIGHT action is. The
classes matter because the action differs — and the most expensive mistake is
re-running work that already finished (finished-but-unreported), so that is a
distinct state, never a resume.

No I/O — the cap layer assembles the inputs (ide.claude_sessions.list_sessions +
board.items + evolve.pipeline.list) and calls this. Unit-tested standalone.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# Default policy (§4.3 two-tier staleness + §6.6 guards), in seconds.
DEFAULT_POLICY: Dict[str, int] = {
    "stalled_after_s": 45 * 60,     # undeclared silence → 'stalled' (visible, not reclaimed)
    "resumable_after_s": 90 * 60,   # → reclaimable / auto-resume eligible
    "declared_grace_s": 5 * 3600,   # declared block: hold the claim ~5h (usage-window reset)
    "max_resume_attempts": 2,       # a 3rd stall is a human signal, not a 3rd relaunch
}

_TERMINAL_LANES = {"done", "dropped"}
_TERMINAL_PIPE_DECISIONS = {"promoted", "rolled_back"}


# ── pure helpers ─────────────────────────────────────────────────────────────
def parse_ts(ts: Any) -> Optional[float]:
    """ISO-8601 (with or without trailing Z) → epoch seconds, else None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def age_seconds(last_ts: Any, now_ts: float) -> Optional[float]:
    t = parse_ts(last_ts)
    return None if t is None else max(0.0, now_ts - t)


def pipe_is_terminal(p: Dict[str, Any]) -> bool:
    return p.get("decision") in _TERMINAL_PIPE_DECISIONS


def pipe_is_promoted(p: Dict[str, Any]) -> bool:
    return p.get("decision") == "promoted"


def open_claims(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Board items this session holds that are NOT in a terminal lane."""
    return [c for c in (claims or []) if c.get("lane") not in _TERMINAL_LANES]


def is_declared_block(claims: List[Dict[str, Any]]) -> bool:
    """A declared quota block (`blocked:quota`) — §4.3 declared tier, not a stall."""
    return any("blocked:quota" in (c.get("labels") or []) for c in open_claims(claims))


def classify_session(session: Dict[str, Any],
                     claims: List[Dict[str, Any]],
                     pipelines: List[Dict[str, Any]],
                     now_ts: float,
                     policy: Optional[Dict[str, int]] = None,
                     human_took_over: bool = False,
                     resume_attempts: int = 0) -> Dict[str, Any]:
    """Classify one session. Returns
    {state, action, reason, age_s, resume_ok, resume_attempts, claude_session_id}.

    States / actions:
      untracked          none      — no open claim/pipeline; not a candidate
      human              none      — a human took over; never auto-resume
      finished-unreported reconcile — a promoted pipeline but the claim is still
                                    open → reconcile the board, do NOT re-run
      declared-block     wait      — blocked:quota; hold until expected return
      live               none      — active within stalled_after_s
      stalled            watch     — undeclared silence ≥ stalled_after_s (visible)
      resumable          resume    — silence ≥ resumable_after_s (or escalate if
                                    max attempts reached)
    """
    pol = {**DEFAULT_POLICY, **(policy or {})}
    oc = open_claims(claims)
    non_terminal_pipes = [p for p in (pipelines or []) if not pipe_is_terminal(p)]
    tracked = bool(oc or non_terminal_pipes)
    age = age_seconds(session.get("last_ts", ""), now_ts)

    def result(state: str, action: str, reason: str, resume_ok: bool = False) -> Dict[str, Any]:
        return {"state": state, "action": action, "reason": reason,
                "age_s": None if age is None else round(age, 1),
                "resume_ok": resume_ok, "resume_attempts": resume_attempts,
                "claude_session_id": session.get("claude_session_id", "")}

    if not tracked:
        return result("untracked", "none",
                      "no open board claim or non-terminal pipeline — not a stall candidate")
    if human_took_over:
        return result("human", "none", "a human took this over — never auto-resume")
    # The most expensive mistake to avoid: re-running work that already landed.
    if any(pipe_is_promoted(p) for p in (pipelines or [])) and oc:
        return result("finished-unreported", "reconcile",
                      "a pipeline promoted but a board claim is still open — reconcile "
                      "the board from the pipeline/branch, do NOT re-run the work")
    if is_declared_block(claims):
        return result("declared-block", "wait",
                      "declared blocked:quota — hold the claim until its expected return")
    if age is None:
        return result("live", "none", "no activity timestamp available — treat as live")
    if age >= pol["resumable_after_s"]:
        ok = resume_attempts < pol["max_resume_attempts"]
        return result("resumable", "resume" if ok else "escalate",
                      f"undeclared silence {int(age)}s ≥ {pol['resumable_after_s']}s"
                      + ("" if ok else " but max resume attempts reached → hand to a human"),
                      resume_ok=ok)
    if age >= pol["stalled_after_s"]:
        return result("stalled", "watch",
                      f"undeclared silence {int(age)}s ≥ {pol['stalled_after_s']}s — "
                      "visible, not yet reclaimable")
    return result("live", "none", f"active {int(age)}s ago")


def autoresume_candidates(watch_sessions: List[Dict[str, Any]],
                          policy: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The pure observe-vs-act gate for the auto-resume loop (Phase C).

    Given watch()'s already-classified session rows + the effective policy, return
    exactly the sessions to AUTO-resume this tick. Empty unless policy['auto'] is
    truthy — so the loop OBSERVES by default and only acts once auto-resume is
    explicitly turned on. A row qualifies solely on the classifier's own verdict:
    action == 'resume' AND resume_ok. Every 'never auto-resume' case (human takeover,
    declared blocked:quota, finished-unreported → reconcile, and past-max-attempts →
    escalate) already carries a non-'resume' action, so it is excluded here by
    construction rather than re-checked — the classifier stays the single source of
    truth for that safety call.
    """
    if not (policy or {}).get("auto"):
        return []
    return [s for s in (watch_sessions or [])
            if s.get("action") == "resume" and s.get("resume_ok")]
