"""Tests for the stalled-session classifier (vera/ide/session_watch_core.py).

Pins each §6.6 state to a concrete input, especially the two that are dangerous
to get wrong: finished-but-unreported (must NOT resume) and declared-block (must
wait, not reclaim).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.ide import session_watch_core as sw  # noqa: E402

NOW = 1_000_000.0


def _sess(age_s):
    # a session whose last activity was age_s seconds before NOW
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - age_s, tz=timezone.utc).isoformat()
    return {"claude_session_id": "sess-1", "last_ts": ts}


def _claim(lane="in_progress", labels=None):
    return {"id": "i1", "lane": lane, "labels": labels or []}


def test_untracked_when_no_open_work():
    r = sw.classify_session(_sess(99999), claims=[], pipelines=[], now_ts=NOW)
    assert r["state"] == "untracked" and r["action"] == "none"


def test_untracked_when_only_terminal_claims():
    r = sw.classify_session(_sess(99999), claims=[_claim(lane="done")],
                            pipelines=[], now_ts=NOW)
    assert r["state"] == "untracked"


def test_live_recent_activity():
    r = sw.classify_session(_sess(60), claims=[_claim()], pipelines=[], now_ts=NOW)
    assert r["state"] == "live" and r["action"] == "none"


def test_stalled_between_thresholds():
    r = sw.classify_session(_sess(60 * 60), claims=[_claim()], pipelines=[], now_ts=NOW)
    assert r["state"] == "stalled" and r["action"] == "watch" and r["resume_ok"] is False


def test_resumable_past_threshold():
    r = sw.classify_session(_sess(120 * 60), claims=[_claim()], pipelines=[], now_ts=NOW)
    assert r["state"] == "resumable" and r["action"] == "resume" and r["resume_ok"] is True


def test_resumable_but_max_attempts_escalates():
    r = sw.classify_session(_sess(120 * 60), claims=[_claim()], pipelines=[], now_ts=NOW,
                            resume_attempts=2)
    assert r["state"] == "resumable" and r["action"] == "escalate" and r["resume_ok"] is False


def test_declared_block_waits_even_when_old():
    r = sw.classify_session(_sess(200 * 60),
                            claims=[_claim(labels=["blocked:quota"])],
                            pipelines=[], now_ts=NOW)
    assert r["state"] == "declared-block" and r["action"] == "wait"


def test_finished_unreported_never_resumes():
    # a promoted pipeline but the claim is still open → reconcile, not re-run
    r = sw.classify_session(_sess(200 * 60), claims=[_claim(lane="in_progress")],
                            pipelines=[{"decision": "promoted"}], now_ts=NOW)
    assert r["state"] == "finished-unreported" and r["action"] == "reconcile"
    assert r["resume_ok"] is False


def test_human_takeover_never_resumes():
    r = sw.classify_session(_sess(200 * 60), claims=[_claim()], pipelines=[],
                            now_ts=NOW, human_took_over=True)
    assert r["state"] == "human" and r["action"] == "none"


def test_non_terminal_pipeline_alone_makes_it_tracked():
    r = sw.classify_session(_sess(120 * 60), claims=[],
                            pipelines=[{"decision": "pending"}], now_ts=NOW)
    assert r["state"] == "resumable"


def test_pipe_terminal_helpers():
    assert sw.pipe_is_terminal({"decision": "promoted"})
    assert sw.pipe_is_terminal({"decision": "rolled_back"})
    assert not sw.pipe_is_terminal({"decision": "pending"})
    assert sw.pipe_is_promoted({"decision": "promoted"})


def test_parse_ts_tolerant():
    assert sw.parse_ts("") is None
    assert sw.parse_ts("not a date") is None
    assert sw.parse_ts("2026-08-09T00:00:00+00:00") is not None
    assert sw.parse_ts("2026-08-09T00:00:00Z") is not None


# ── autoresume_candidates — the Phase C observe-vs-act gate ───────────────────
def _row(action, resume_ok, sid="s1"):
    return {"claude_session_id": sid, "action": action, "resume_ok": resume_ok}


def test_autoresume_empty_when_auto_off():
    rows = [_row("resume", True)]
    assert sw.autoresume_candidates(rows, {"auto": False}) == []
    assert sw.autoresume_candidates(rows, {}) == []          # default is observe-only
    assert sw.autoresume_candidates(rows, None) == []


def test_autoresume_picks_only_resume_ok_when_auto_on():
    rows = [
        _row("resume", True, "ok"),          # the only one that should fire
        _row("resume", False, "escalated"),  # max attempts -> not resume_ok
        _row("watch", False, "stalled"),
        _row("reconcile", False, "finished"),
        _row("wait", False, "declared"),
        _row("none", False, "live"),
    ]
    picked = sw.autoresume_candidates(rows, {"auto": True})
    assert [s["claude_session_id"] for s in picked] == ["ok"]


def test_autoresume_never_touches_guarded_states_even_on():
    # human / declared-block / finished-unreported / escalate all carry a
    # non-'resume' action, so the gate excludes them by construction.
    for action in ("none", "watch", "wait", "reconcile", "escalate"):
        assert sw.autoresume_candidates([_row(action, True)], {"auto": True}) == []
