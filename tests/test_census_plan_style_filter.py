"""Census runs are compared per planning style, not only per template (user,
2026-09-27: "filter censuses by planning type so we are comparing apples to
apples"), and a census can be started with a style from Loop Lab's schedule.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.census import census_core as C  # noqa: E402
from vera.evolve import schedule_core as S  # noqa: E402


def _row(gid, style=None, status="done", **kw):
    r = {"id": gid, "status": status, "template": "default", "wall_s": 100.0}
    if style is not None:
        r["plan_style_requested"] = style
    r.update(kw)
    return r


def test_a_run_reports_the_style_it_measured():
    assert C.run_plan_style([_row("a", "broad"), _row("b", "broad")]) == "broad"
    assert C.summarise_run("x", [_row("a", "stepwise")])["plan_style"] == "stepwise"


def test_runs_from_before_styles_existed_are_auto():
    # The loop had one planning path before plan_style; auto still is it.
    assert C.run_plan_style([_row("a"), _row("b")]) == "auto"
    assert C.run_plan_style([_row("a"), _row("b", "auto")]) == "auto"


def test_a_file_mixing_styles_is_not_a_run_of_either():
    assert C.run_plan_style([_row("a", "broad"), _row("b")]) == "mixed:auto+broad"
    assert C.run_plan_style([]) == ""


def test_compare_names_a_style_difference_per_goal():
    out = C.compare_runs([_row("a")], [_row("a", "broad")])
    (rec,) = out
    assert (rec["base_plan_style"], rec["head_plan_style"]) == ("auto", "broad")
    assert "different styles" in rec["note"]
    same = C.compare_runs([_row("a", "broad")], [_row("a", "broad")])[0]
    assert "note" not in same


def _census(target):
    return S.normalize({"kind": "census", "target": target, "days": [0], "start": "05:00",
                        "end": "17:00", "timezone": "UTC"})


def test_a_census_schedule_carries_a_known_style():
    rec = _census({"template": "default", "plan_style": "Broad"})
    assert rec["target"]["plan_style"] == "broad"
    assert "broad planning" in rec["title"]
    base = _census({"template": "default"})
    assert base["target"]["plan_style"] == "" and "planning" not in base["title"]


def test_an_unknown_style_is_refused():
    with pytest.raises(ValueError):
        _census({"template": "default", "plan_style": "sideways"})


def test_every_loop_style_is_schedulable():
    from vera.planning import planner_styles as PS
    assert S.census_plan_styles() == list(PS.LOOP_STYLES.keys())
    for s in PS.LOOP_STYLES:
        assert _census({"plan_style": s})["target"]["plan_style"] == s
