"""Agents + Board on the kit (looplab-control §9.4 S8, 19 Oct 2026): "sessions by day, the board as units x lanes with
live links, lifecycle, flow, cycle time" - and the lane moves the flow needs, recorded by the board provider."""
import os
import re

import pytest

from vera.board import board_flow_core as F

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
DAY = 86400.0
NOW = 1760900000.0      # 2025-10-19T18:53:20Z


def _iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


ITEMS = [
    {"id": "a", "lane": "done", "labels": ["unit:looplab-control"], "created_at": _iso(NOW - 3 * DAY), "updated_at": _iso(NOW - 1 * DAY)},
    {"id": "b", "lane": "done", "labels": ["unit:looplab-control"], "created_at": _iso(NOW - 5 * DAY), "updated_at": _iso(NOW - 0.5 * DAY)},
    {"id": "c", "lane": "inbox", "labels": [], "plan": "shared-planning/four-minds/PLAN.md", "created_at": _iso(NOW - 10 * DAY)},
    {"id": "d", "lane": "ready", "labels": [], "plan": "", "created_at": _iso(NOW - 2 * DAY)},
]
MOVES = [{"id": "b", "frm": "", "to": "inbox", "t": NOW - 5 * DAY}, {"id": "b", "frm": "inbox", "to": "in_progress", "t": NOW - 4 * DAY},
         {"id": "b", "frm": "in_progress", "to": "done", "t": NOW - 2 * DAY}]


def test_a_unit_is_the_label_else_the_plan_folder_else_none():
    assert [F.unit_of(i) for i in ITEMS] == ["looplab-control", "looplab-control", "four-minds", F.NO_UNIT]
    assert F.unit_of({"plan": "plan-agentic-loop-improvement"}) == "agentic-loop-improvement"
    assert F.unit_of({"plan": "unit-recon-fixes"}) == F.unit_of({"labels": ["unit:recon-fixes"]})


def test_units_by_lanes():
    cells = {(c["unit"], c["lane"]): c["n"] for c in F.matrix(ITEMS)}
    assert cells == {("looplab-control", "done"): 2, ("four-minds", "inbox"): 1, (F.NO_UNIT, "ready"): 1}


def test_cycle_time_uses_the_recorded_move_and_says_its_basis():
    cy = F.cycle(ITEMS, MOVES, NOW - 30 * DAY)[0]
    # a: updated_at - created_at = 2 d (approx); b: done move - created = 3 d (moves)
    assert cy["unit"] == "looplab-control" and cy["done"] == 2 and cy["basis"] == "mixed"
    assert sorted([cy["p50_h"], cy["p90_h"]]) == [48.0, 72.0]


def test_flow_a_day_open_work_and_time_in_lane():
    days = {d["day"]: d for d in F.per_day(ITEMS, MOVES, NOW - 30 * DAY)}
    assert sum(d["created"] for d in days.values()) == 4 and sum(d["done"] for d in days.values()) == 2
    assert sum(d["moved"] for d in days.values()) == 2, "a new item's first record is not a move"
    w = {r["unit"]: r for r in F.wip(ITEMS, NOW)}
    assert w["four-minds"]["oldest_h"] == 240.0 and "looplab-control" not in w
    lt = {r["lane"]: r for r in F.lane_time(MOVES, NOW)}
    assert lt["inbox"]["p50_h"] == 24.0 and lt["in_progress"]["p50_h"] == 48.0 and "done" not in lt


def test_stream_entries_parse():
    got = F.parse_moves([(b"1-0", {b"id": b"x", b"frm": b"inbox", b"to": b"done", b"at": b"2026-10-19T10:00:00+00:00"}),
                         ("2-0", {"id": "y", "at": "not a time"})])
    assert len(got) == 1 and got[0]["to"] == "done" and got[0]["t"] > 0


def test_the_board_records_every_lane_change_and_board_flow_reads_it():
    src = open(os.path.join(ROOT, "vera", "board", "board_capabilities.py"), encoding="utf-8").read()
    rp = src[src.index("class _RecordingProvider("):src.index("def provider()")]
    assert "async def move(" in rp and "async def upsert(" in rp and rp.count("await _record_move(") == 2
    assert "_PROVIDER = _RecordingProvider()" in src
    rec = src[src.index("async def _record_move("):src.index("class _RecordingProvider(")]
    assert "maxlen=MOVES_MAXLEN, approximate=True" in rec and "except Exception" in rec
    assert '"board.flow", http_method="GET", http_path="/board/flow"' in src and "bfc.flow(items, moves" in src


def test_the_agents_page_draws_s8_on_the_kit():
    p = open(os.path.join(ROOT, "vera", "evolve", "evolve_panel.html"), encoding="utf-8").read()
    sec = p[p.index('<div class="sec" id="sec-agents"'):]
    sec = sec[:sec.index('<div class="card" id="agents-card">')]
    assert 'id="agb-tl-host"' in sec and 'id="agb-mx"' in sec and 'id="agb-cycle"' in sec and 'id="agb-wip"' in sec
    assert re.search(r'<vera-drill[^>]*id="agb-drill"', sec)
    assert "<button" not in sec, "no controls on the display (owner 10 Oct): the window select only"
    js = p[p.index("async function agbKit("):p.index("function agentsRefresh(")]
    assert "VeraDrill.call('board.flow'" in js and "VeraTimeline.registerLane" in js and "el.record={map:{cells:'cells',row:'unit',col:'lane'" in js
    assert "cap:'evolve.agents.rows'" in js and "t:'last_activity'" in js
    for k in ("board-cell", "board-unit", "board-item-moves"):
        assert "registerKind('%s'" % k in js, k
