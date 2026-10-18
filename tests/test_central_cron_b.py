"""looplab-control Part B (17 Oct 2026): the central cron - one view of everything scheduled, repeating
triggers that roll forward, and work the owner schedules for a Claude session (live sessions only)."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import agent_work_core as aw  # noqa: E402
from vera.evolve import schedule_core as core  # noqa: E402

pytestmark = pytest.mark.critical
UTC = timezone.utc


def _ts(*a):
    return datetime(*a, tzinfo=UTC).timestamp()


# ── B2: repeat rules ─────────────────────────────────────────────────────────
def test_every_and_named_repeats():
    assert aw.repeat_rule("every 30m") == ("every", 1800)
    assert aw.repeat_rule("every 2 hours") == ("every", 7200)
    assert aw.repeat_rule("daily") == ("every", 86400)
    assert aw.repeat_rule("every 10s") is None                # under a minute is refused
    assert aw.repeat_rule("nightly 01:45") is None            # free text stays free text
    assert aw.next_fire("every 30m", _ts(2026, 10, 18, 1, 0)) == _ts(2026, 10, 18, 1, 30)


@pytest.mark.parametrize("expr,after,want", [
    ("*/15 * * * *", (2026, 10, 18, 1, 7), (2026, 10, 18, 1, 15)),
    ("cron: 7 2 * * *", (2026, 10, 18, 3, 0), (2026, 10, 19, 2, 7)),
    ("0 9 * * 1-5", (2026, 10, 17, 10, 0), (2026, 10, 19, 9, 0)),          # Sat -> Mon
    ("30 4 1 * *", (2026, 10, 18, 0, 0), (2026, 11, 1, 4, 30)),
    ("0 0 * * 0", (2026, 10, 18, 0, 0), (2026, 10, 25, 0, 0)),             # Sunday, strictly after
])
def test_cron_next_fire_in_utc(expr, after, want):
    assert aw.next_fire(expr, _ts(*after), "UTC") == _ts(*want)


def test_cron_rejects_bad_fields():
    for bad in ("60 * * * *", "* 24 * * *", "* * * *", "a b c d e", "*/0 * * * *", "5-1 * * * *"):
        assert aw.repeat_rule(bad) is None, bad


def test_repeating_trigger_rolls_forward_when_done():
    e = aw.normalize({"kind": "session_cron", "title": "t", "session": "vera-fb", "when": "2026-10-18T01:00:00Z",
                      "repeat": "every 1h"}, now=_ts(2026, 10, 18, 0, 0))
    assert e["repeat_ok"] is True
    nxt = aw.roll(dict(e, status="done"), now=_ts(2026, 10, 18, 1, 5))
    assert nxt["status"] == "armed" and nxt["when"] == "2026-10-18T02:05:00Z" and nxt["fired"] == 1
    req = aw.normalize({"kind": "request", "title": "r", "session": "any", "repeat": "daily",
                        "when": "2026-10-18T01:00:00Z"}, now=_ts(2026, 10, 18, 0, 0))
    assert aw.roll(dict(req, status="done"), now=_ts(2026, 10, 18, 1, 0))["status"] == "pending"
    once = aw.normalize({"kind": "one_shot", "title": "o", "session": "x"}, now=_ts(2026, 10, 18, 0, 0))
    assert aw.roll(once) is None


# ── B3/B5: the claude_session kind ───────────────────────────────────────────
def test_claude_session_kind():
    r = core.normalize({"kind": "claude_session", "target": {"session": "vera-62", "what": "read the arm"},
                        "timezone": "UTC", "days": [1], "start": "02:00", "end": "03:00"})
    assert r["exclusive"] is False                           # a request does not hold the box
    assert r["title"].startswith("Session · vera-62")
    assert "claude_session" in core.RUN_TRACKED_KINDS       # one request at a time per schedule
    with pytest.raises(ValueError):
        core.normalize({"kind": "claude_session", "target": {"what": "x"}})
    with pytest.raises(ValueError):
        core.normalize({"kind": "claude_session", "target": {"session": "x"}})


def test_a_session_request_is_followed_until_settled():
    assert core.follow_args("agents.work.request", {"ok": True, "id": "w1"}) == ("agents.work.get", {"id": "w1"})
    assert core.follow_state("agents.work.request", {"state": "pending"})[0] is False
    assert core.follow_state("agents.work.request", {"state": "claimed"})[0] is False
    assert core.follow_state("agents.work.request", {"state": "done"})[:2] == (True, "done")
    assert core.follow_state("agents.work.request", {"state": "declined"})[:2] == (True, "declined")
    assert core.follow_state("agents.work.request", {"state": "expired"})[:2] == (True, "expired")


# ── B1: the central view ─────────────────────────────────────────────────────
def test_crontab_and_timers_parse():
    rows = core.parse_crontab("# c\nMAILTO=x\n* * * * * /home/boejaker/loop-census/watch/node_clients.sh\n"
                              "@reboot /usr/bin/true\n")
    assert [(r["repeat"], r["title"]) for r in rows] == [
        ("* * * * *", "/home/boejaker/loop-census/watch/node_clients.sh"), ("@reboot", "/usr/bin/true")]
    t = core.parse_systemd_timers("Sun 2026-10-18 00:00:00 BST 8min left - - logrotate.timer logrotate.service\n")
    assert t[0]["title"] == "logrotate.timer" and t[0]["next_fire"].startswith("Sun 2026-10-18")


def test_new_calendar_layers():
    assert core.layer_of({"source": "agents-work"}) == "agents"
    assert core.layer_of({"source": "host-cron"}) == "host"
    assert "agents" in core.LAYERS and "host" in core.LAYERS



def test_background_window_shows_as_its_own_kind():
    """Owner 18 Oct 2026: it must be clear from the UI what each slot is doing."""
    t = {"name": "evolve.background.window", "arguments": {"minutes": 90}}
    assert core.display_kind("cap", t) == "background" and core.display_kind("cap", {"name": "obs.x"}) == "cap"
    rec = core.normalize({"kind": "cap", "title": "Background work", "target": t, "timezone": "UTC",
                          "days": list(range(7)), "start": "03:30", "end": "05:00"})
    ev = core.project_events([rec], datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC))
    assert ev and ev[0]["kind"] == "background" and ev[0]["color"] == core.COLORS["background"]
    run = core.history_events([{"id": "r1", "kind": "cap", "target": t, "started_at": "2026-10-19T03:30:00+00:00"}],
                              datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC))
    assert run[0]["kind"] == "background" and run[0]["run_id"] == "r1" and run[0]["running"] is True
