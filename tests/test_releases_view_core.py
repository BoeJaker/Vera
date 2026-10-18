"""Loop Lab Releases section and the calendar's releases & promotions layer (owner, 18 Oct 2026)."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import releases_view_core as R  # noqa: E402

pytestmark = pytest.mark.critical
UTC = timezone.utc

PROMOTED = {"id": "82637ed4", "branch": "feat/bg-window-controls", "status": "adopted", "decision": "promoted",
            "commits": ["a"], "changed_files": ["x", "y"], "steps": [
                {"stage": "adopt", "ok": True, "ts": "2026-10-18T08:30:54Z", "detail": "1 commit(s)"},
                {"stage": "test-pool", "ok": True, "ts": "2026-10-18T08:30:56Z", "detail": "12 chunks"},
                {"stage": "critical-tests", "ok": True, "ts": "2026-10-18T08:38:42Z",
                 "detail": "7718 passed, 0 failed, 0 errors, 37 skipped (rc=0, 12 shards)"},
                {"stage": "merge", "ok": True, "ts": "2026-10-18T08:38:44Z",
                 "detail": "merged feat/bg-window-controls → bleeding-edge @ 2bf343a0b2"}]}
READY = {"id": "r1", "branch": "fix/r", "steps": [{"stage": "adopt", "ok": True, "ts": "2026-10-18T09:00:00Z"},
                                                {"stage": "critical-tests", "ok": True, "ts": "2026-10-18T09:08:00Z",
                                                 "detail": "7700 passed, 0 failed, 0 errors"}]}
GATING = {"id": "98e3388f", "branch": "fix/gate-actions-asked", "status": "adopted",
          "steps": [{"stage": "adopt", "ok": True, "ts": "2026-10-18T10:12:00Z"},
                    {"stage": "test-pool", "ok": True, "ts": "2026-10-18T10:12:05Z"}]}
FAILED = {"id": "f1", "branch": "x", "steps": [{"stage": "adopt", "ok": True, "ts": "2026-10-18T09:00:00Z"},
                                               {"stage": "critical-tests", "ok": False, "ts": "2026-10-18T09:09:00Z",
                                                "detail": "7668 passed, 1 failed, 0 errors"}]}
ADOPTED = {"id": "a1", "branch": "feat/desk-elements", "steps": [{"stage": "adopt", "ok": True, "ts": "2026-10-18T09:46:33Z"}]}


def test_columns():
    cols = R.board([PROMOTED, GATING, FAILED, ADOPTED, READY])
    assert [c["id"] for c in cols["ready"]] == ["r1"]
    assert [c["id"] for c in cols["edge"]] == ["82637ed4"]
    assert [c["id"] for c in cols["gating"]] == ["98e3388f"]
    assert [c["id"] for c in cols["failed"]] == ["f1"] and [c["id"] for c in cols["adopted"]] == ["a1"]
    # its merge commit is on main -> released
    assert [c["id"] for c in R.board([PROMOTED], ["2bf343a0b2"])["released"]] == ["82637ed4"]


def test_card_carries_tests_merge_and_duration():
    c = R.card(PROMOTED)
    assert c["tests"]["passed"] == 7718 and c["tests"]["failed"] == 0
    assert c["merge_sha"] == "2bf343a0b2" and c["promoted_at"].startswith("2026-10-18T08:38:44")
    assert c["commits"] == 1 and c["changed_files"] == 2 and c["minutes"] == 7.8


def test_release_rows_and_carried_merges():
    carried = R.carried_from_log(["c337107b Loop Lab: merge feat/central-cron-b (pipeline 4ffdd5be)",
                                  "abc1234 a plain commit"])
    assert carried[0] == {"sha": "c337107b", "subject": "Loop Lab: merge feat/central-cron-b (pipeline 4ffdd5be)",
                          "branch": "feat/central-cron-b", "pipeline": "4ffdd5be"}
    assert carried[1]["pipeline"] == ""
    r = R.release_row({"id": "d32f7984", "status": "done", "mode": "goal", "requested_at": "2026-10-18T08:40:10+00:00",
                       "finished_at": "2026-10-18T08:54:56+00:00", "last_why": "census yielded", "sync_nodes": True,
                       "merge": {"commit": "e00e1b92c9c213ae"}}, carried)
    assert r["minutes"] == 14.8 and r["commit"] == "e00e1b92c9c2" and r["nodes"] == "synced" and len(r["carried"]) == 2


def test_calendar_layer_events():
    s, e = datetime(2026, 10, 18, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC)
    rel = R.release_row({"id": "d32f7984", "status": "done", "requested_at": "2026-10-18T08:40:10+00:00",
                         "finished_at": "2026-10-18T08:54:56+00:00"})
    ev = R.events([R.card(PROMOTED)], [rel], s, e)
    assert [x["kind"] for x in ev] == ["promotion", "release"]
    assert all(x["source"] == "releases" for x in ev) and ev[0]["pipeline_id"] == "82637ed4"
    assert R.events([R.card(PROMOTED)], [rel], datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)) == []


def test_schedule_core_has_the_layer():
    from vera.evolve import schedule_core as core
    assert "releases" in core.LAYERS and core.layer_of({"source": "releases"}) == "releases"
