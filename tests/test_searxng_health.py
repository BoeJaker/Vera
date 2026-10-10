"""A dead SearXNG shows in /health (info-seeking plan SK-0.1). 8-10 Oct 2026 the searxng container lost its network
after a host restart: every engine answered "Suspended: timeout", each search came back empty, web.search fell back
to DDG snippets for days, and nothing said so. Pure: no network."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.web import search_engines as SE                  # noqa: E402

DEAD = {"results": [], "unresponsive_engines": [["brave", "Suspended: timeout"], ["duckduckgo", "Suspended: timeout"],
                                                ["google", "Suspended: timeout"]]}
GOOD = {"results": [{"url": "https://a"}] * 10, "unresponsive_engines": [["startpage", "Suspended: CAPTCHA"]]}


def _fold(*bodies):
    st = {}
    for i, b in enumerate(bodies):
        st = SE.searxng_note(st, b, f"2026-10-10T19:0{i}:00+00:00")
    return st


def test_unknown_before_any_search():
    assert SE.searxng_status({})["state"] == "unknown"


def test_every_engine_timing_out_three_times_is_down():
    assert SE.searxng_status(_fold(DEAD, DEAD))["state"] == "degraded"
    st = SE.searxng_status(_fold(DEAD, DEAD, DEAD))
    assert st["state"] == "down" and "network" in st["message"] and st["empty_streak"] == 3
    assert "google: Suspended: timeout" in st["unresponsive"]


def test_one_good_answer_resets_and_a_captcha_is_only_degraded():
    st = SE.searxng_status(_fold(DEAD, DEAD, DEAD, GOOD))
    assert st["state"] == "degraded" and st["empty_streak"] == 0 and st["ok_at"].startswith("2026-10-10T19:03")
    assert SE.searxng_status(_fold({"results": [{"url": "x"}]}))["state"] == "ok"


def test_unreachable_is_down_after_three():
    st = {}
    for _ in range(3):
        st = SE.searxng_note(st, None, "2026-10-10T19:00:00+00:00", error="ConnectError")
    s = SE.searxng_status(st)
    assert s["state"] == "down" and "cannot be reached" in s["message"] and s["last_error"] == "ConnectError"


def test_the_web_module_notes_page_one_and_health_reports_it():
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    assert "if pageno == 1:\n                    _note_searxng(body)" in src
    assert '"web.engines.health"' in src
    orch = (Path(__file__).resolve().parents[1] / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
    assert '"search":await _health_search(),' in orch


def test_both_searxng_copies_report_to_the_one_record():
    # prod 10 Oct after release: /health said "no search since start" after a search had run - researcher_api's
    # copy (the one that runs when it imports) did not report, only web_capabilities' did
    root = Path(__file__).resolve().parents[1] / "vera"
    research = (root / "research" / "researcher_api.py").read_text(encoding="utf-8")
    assert "_engines.note_searxng_answer(body)" in research and "_engines.note_searxng_answer(None, error=str(e))" in research
    web = (root / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    assert "_engines.note_searxng_answer(body, error=error)" in web and "searxng_status(_engines.SEARXNG_STATE)" in web


def test_note_searxng_answer_updates_the_shared_state_and_never_raises():
    SE.SEARXNG_STATE.clear()
    SE.note_searxng_answer(GOOD)
    assert SE.searxng_status(SE.SEARXNG_STATE)["state"] == "degraded"       # startpage CAPTCHA
    SE.note_searxng_answer({"results": "not-a-list-but-truthy", "unresponsive_engines": None})
    SE.note_searxng_answer(None, error="x" * 500)
    assert len(SE.SEARXNG_STATE["last_error"]) == 160
    SE.SEARXNG_STATE.clear()
