"""The Loop Lab Tests page (owner, 18 Oct 2026: four-minds' tests "well integrated into the loop lab for monitoring ... a
part of the ui dedicated to monitoring your tests on its own page"), and each long test saying it is running."""
import asyncio
import os
import sys
import types
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import tests_core as TC  # noqa: E402

pytestmark = pytest.mark.critical
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _e(id, cap="decide.shadow.night", **kw):
    d = {"id": id, "kind": "cap", "target": {"name": cap}, "title": id, "enabled": True, "days": list(range(7)),
         "start": "23:40", "end": "23:55", "once_at": ""}
    d.update(kw)
    return d


def test_each_test_finds_its_entries_and_census_arms_by_title():
    t = {x["id"]: x for x in TC.TESTS}
    assert TC.matches(t["matrix"], _e("a")) and not TC.matches(t["matrix"], _e("b", cap="memory.perf.probe"))
    arm = {"id": "c", "kind": "census", "title": "Census · default · arm student-verify-met"}
    assert TC.matches(t["arms"], arm) and not TC.matches(t["arms"], dict(arm, title="Census · default · style broad"))


@pytest.mark.parametrize("entry,runs,health", [
    (_e("x", enabled=False), [], "disabled"),
    (_e("x", recover_due="2026-10-18T10:07:00+00:00"), [], "recovering"),
    (_e("x"), [{"started_at": "2026-10-18T11:50:00+00:00", "finished_at": ""}], "running"),
    (_e("x"), [{"started_at": "2026-10-17T22:40:00+00:00", "finished_at": "x", "result": "interrupted"}], "interrupted"),
    (_e("x"), [{"started_at": "2026-10-17T22:40:00+00:00", "finished_at": "x", "result": "error"}], "failed"),
    (_e("x"), [{"started_at": "2026-10-17T22:40:00+00:00", "finished_at": "x", "result": "done"}], "ok"),
    (_e("x"), [], "never"),
    (_e("x", once_at="2026-10-18T10:05:00+00:00"), [], "missed"),
    (_e("x", once_at="2026-10-18T18:05:00+00:00"), [], "scheduled"),
])
def test_entry_health(entry, runs, health):
    assert TC.entry_health(entry, runs, NOW, "2026-10-18T22:40:00+00:00") == health


def test_a_daily_entry_whose_last_run_is_days_old_missed_a_window():
    runs = [{"started_at": "2026-10-14T22:40:00+00:00", "finished_at": "x", "result": "done"}]
    assert TC.entry_health(_e("x"), runs, NOW, "2026-10-18T22:40:00+00:00") == "missed"


def test_status_joins_entries_runs_and_headlines_worst_first():
    ents = [_e("m1"), _e("p1", cap="memory.perf.probe", once_at="2026-10-18T10:05:00+00:00"),
            _e("p2", cap="memory.perf.probe", once_at="2026-10-18T18:05:00+00:00")]
    runs = [{"schedule_id": "p1", "started_at": "2026-10-18T09:05:00+00:00", "finished_at": "x",
             "result": "interrupted", "detail": "prod restarted while it ran"}]
    out = TC.status(ents, runs, {"m1": {"start": "2026-10-18T22:40:00+00:00"}}, NOW, {"memory": {"seek_p50_ms": 40}})
    by = {t["id"]: t for t in out["tests"]}
    assert by["memory"]["health"] == "interrupted" and by["memory"]["headline"] == {"seek_p50_ms": 40}
    assert by["memory"]["runs"][0]["detail"] == "prod restarted while it ran"
    assert by["matrix"]["health"] == "never" and by["claims"]["health"] == "never"
    assert out["counts"]["interrupted"] == 1


def test_the_page_the_route_and_the_loop_lab_section():
    mon = open(os.path.join(ROOT, "vera", "minds", "tests_monitor.py"), encoding="utf-8").read()
    assert '@APP.get("/decide/tests", include_in_schema=False)' in mon and '"decide.tests.status"' in mon
    page = open(os.path.join(ROOT, "vera", "minds", "tests_panel.html"), encoding="utf-8").read()
    assert "name:'decide.tests.status'" in page and "setInterval(load,60000)" in page
    assert "<button" not in page.split("<main")[1]              # display only: no controls on the page
    ll = open(os.path.join(ROOT, "vera", "evolve", "evolve_panel.html"), encoding="utf-8").read()
    assert 'data-sec="tests" onclick="nav(\'tests\')"' in ll and 'id="sec-tests"' in ll
    assert 'data-src="/decide/tests"' in ll and "tests:()=>" in ll
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "minds/tests_monitor.py")' in orch


def test_long_tests_declare_themselves_and_always_clear(monkeypatch):
    from vera.minds import test_activity as TA
    seen = []

    async def dec(**kw):
        seen.append(("declare", kw["id"], kw["kind"]))

    async def clr(**kw):
        seen.append(("clear", kw["id"]))
    monkeypatch.setattr(TA, "_cap", lambda n: {"sys.activity.declare": dec, "sys.activity.clear": clr}.get(n))

    async def go():
        with pytest.raises(RuntimeError):
            async with TA.declared("t1", "x"):
                raise RuntimeError("the test failed")
    asyncio.run(go())
    assert seen == [("declare", "t1", "test"), ("clear", "t1")]
    for path, fn in (("vera/fabric/memory_perf.py", "cap_memory_perf_probe"), ("vera/minds/site_tap.py", "cap_decide_shadow_sites"),
                     ("vera/minds/claims.py", "cap_output_claims_harvest"),
                     ("vera/minds/tool_shortlist.py", "cap_caps_shortlist_nightly")):
        src = open(os.path.join(ROOT, path), encoding="utf-8").read()
        body = src[src.index(f"async def {fn}("):]
        assert "async with _TA.declared(" in body[:900], fn
