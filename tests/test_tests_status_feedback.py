"""vera-d2's feedback on decide.tests.status (19 Oct 2026)."""
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import tests_core as TC  # noqa: E402

pytestmark = pytest.mark.critical
NOW = datetime(2026, 10, 19, 7, 30, tzinfo=timezone.utc)


def _e(id, cap, **kw):
    d = {"id": id, "kind": "cap", "target": {"name": cap}, "title": id, "enabled": True, "days": list(range(7)),
         "start": "03:05", "end": "03:20", "once_at": ""}
    d.update(kw)
    return d


def _by(out):
    return {t["id"]: t for t in out["tests"]}


def test_health_is_the_latest_outcome_not_an_old_entrys():
    # memory census: an old one-off interrupted at 10:05, later runs fine
    ents = [_e("old", "memory.perf.probe", once_at="2026-10-18T09:05:00+00:00"), _e("night", "memory.perf.probe")]
    runs = [{"schedule_id": "night", "started_at": "2026-10-19T02:06:00+00:00", "finished_at": "x", "result": "done"},
            {"schedule_id": "old", "started_at": "2026-10-18T09:05:00+00:00", "finished_at": "x", "result": "interrupted"}]
    t = _by(TC.status(ents, runs, {}, NOW))["memory"]
    assert t["health"] == "ok"
    old = next(r for r in t["entries"] if r["id"] == "old")
    assert old["interrupted"] is True and old["ref"] == "old" and old["status"] == old["health"]


def test_a_still_going_refusal_is_not_an_outcome_and_a_later_completion_counts():
    ents = [_e("cc", "chat.corpus.run")]
    runs = [{"schedule_id": "cc", "started_at": "2026-10-18T13:13:47+00:00", "finished_at": "x", "result": "error",
             "detail": "a run is going (pid 2724933, run 20261018T130002)"},
            {"schedule_id": "cc", "started_at": "2026-10-18T13:00:00+00:00", "finished_at": "x", "result": "interrupted"}]
    plain = _by(TC.status(ents, runs, {}, NOW))["chat_corpus"]["health"]
    assert plain == "interrupted"                         # the refusal is skipped; the interrupted run is the outcome
    done = _by(TC.status(ents, runs, {}, NOW,
                         {"chat_corpus": {"completed_at": "2026-10-18T13:30:18+00:00"}}))["chat_corpus"]["health"]
    assert done == "ok"                                   # the runner finished out of band after that run started


def test_entries_carry_the_link_fields_and_the_background_block():
    out = TC.status([_e("m", "decide.shadow.night")], [], {"m": {"start": "2026-10-19T22:40:00+00:00"}}, NOW,
                    background={"depth": 151, "oldest_wait_s": 31000})
    e = _by(out)["matrix"]["entries"][0]
    assert {"ref", "status", "next_fire", "overdue", "interrupted"} <= set(e)
    assert e["next_fire"] == "2026-10-19T22:40:00+00:00" and e["overdue"] is False
    assert out["background"]["depth"] == 151


def test_the_history_cap_and_the_queue_reader_exist():
    src = open(os.path.join(ROOT, "vera", "minds", "tests_monitor.py"), encoding="utf-8").read()
    assert '"decide.tests.history"' in src and '("series.tests",' in src
    assert "background=await _background()" in src and '"four_minds_oldest_wait_s"' in src
    assert '"completed_at": (d.get("run") or {}).get("finished") or None' in src
