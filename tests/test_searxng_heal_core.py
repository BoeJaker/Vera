"""SearXNG self-healing decisions (vera/web/searxng_heal_core.py; web-ingress §16). Pure: the states search_engines
already reports, turned into probe / restart / escalate with a cooldown, a daily budget and an escalation reminder."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.web import search_engines as SE  # noqa: E402
from vera.web import searxng_heal_core as hc  # noqa: E402

DEAD = {"results": [], "unresponsive_engines": [["bing", "timeout"], ["brave", "timeout"], ["wikipedia", "timeout"]]}
LIMITED = {"results": [{"url": "https://a"}] * 3, "unresponsive_engines": [["brave", "too many requests"], ["duckduckgo", "CAPTCHA"]]}
GOOD = {"results": [{"url": "https://a"}] * 30, "unresponsive_engines": []}


def _status(*bodies, error=""):
    st = {}
    for i, b in enumerate(bodies):
        st = SE.searxng_note(st, b, f"2026-10-18T10:0{i}:00+00:00", error=error)
    return SE.searxng_status(st)


def test_signatures_from_the_states_search_engines_reports():
    assert hc.signature(_status(DEAD, DEAD, DEAD)) == "dead_network"            # 17-18 Oct exactly
    assert hc.signature(_status(None, None, None, error="connect refused")) == "unreachable"
    assert hc.signature(_status(LIMITED)) == "rate_limited"                     # the state after the 18 Oct restart
    assert hc.signature(_status(DEAD)) == "degraded"                             # one empty answer is not down yet
    assert hc.signature(_status(GOOD)) == "ok" and hc.signature({}) == "unknown"


def test_only_the_dead_network_signature_restarts():
    now = 1_000_000.0
    assert hc.decide(now, _status(DEAD, DEAD, DEAD), {})["action"] == "restart"
    assert hc.decide(now, _status(None, None, None, error="x"), {})["action"] == "restart"
    r = hc.decide(now, _status(LIMITED), {})
    assert r["action"] == "none" and "does not help" in r["why"]
    assert hc.decide(now, _status(GOOD), {})["action"] == "none"


def test_cooldown_daily_budget_and_escalation():
    dead = _status(DEAD, DEAD, DEAD)
    now = 2_000_000.0
    h = hc.after_restart({}, now - 600, recovered=False)
    assert hc.decide(now, dead, h)["action"] == "none" and "cooling" in hc.decide(now, dead, h)["why"]
    h = hc.after_restart(h, now - 2 * 3600, recovered=False)
    assert h["failed_streak"] == 2
    e = hc.decide(now, dead, h)
    assert e["action"] == "escalate" and "did not bring it back" in e["why"]
    h2 = hc.after_escalation(h, now, e["why"])
    assert hc.decide(now + 60, dead, h2)["action"] == "none"                    # escalated already
    assert hc.decide(now + hc.ESCALATE_EVERY_S + 1, dead, h2)["action"] == "escalate"   # the reminder
    budget = {"restarts": [now - i * 3600 * 2 for i in range(1, 5)], "failed_streak": 0}
    assert hc.decide(now, dead, budget)["action"] == "escalate"                 # 4 restarts in 24 h
    old = {"restarts": [now - 2 * 86400], "failed_streak": 0}
    assert hc.decide(now, dead, old)["action"] == "restart"                     # yesterday's restarts do not count


def test_recovery_clears_the_streak_and_history_is_bounded():
    h = hc.after_restart({}, 10.0, recovered=False)
    h = hc.after_escalation(h, 20.0, "x")
    h = hc.on_healthy(h)
    assert h["failed_streak"] == 0 and h["last_escalation"] is None and len(h["restarts"]) == 1
    for i in range(40):
        h = hc.after_restart(h, 100.0 + i, recovered=True)
    assert len(h["history"]) <= hc.HISTORY and h["failed_streak"] == 0


def test_probe_only_when_quiet_and_due():
    now = 10_000.0
    assert hc.should_probe(now, None, None)
    assert not hc.should_probe(now, None, now - 60)                              # a search a minute ago is the probe
    assert hc.should_probe(now, now - 400, now - 900)
    assert not hc.should_probe(now, now - 100, now - 900)


def test_the_healer_is_wired_and_switched():
    src = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
    assert 'os.path.join(_here, "web/searxng_heal_capabilities.py")' in src
    from vera.minds import switchboard_core as SB
    assert SB.REGISTRY["web.searxng.self_heal"].default == "off"
    caps = (ROOT / "vera" / "web" / "searxng_heal_capabilities.py").read_text(encoding="utf-8")
    assert '"docker", "restart", CONTAINER' in caps and "note_searxng_answer" in caps and "tg.notify" in caps
