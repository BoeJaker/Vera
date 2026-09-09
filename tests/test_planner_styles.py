"""Planning styles (vera/planning/planner_styles.py) — the pure half.

Guards the defect that motivated the module (board loop-o49, census
exec-family run 1, session 4a524636): the single-pass planner wrote
"returns the integer 207085" for a goal whose answer is 42925, and the loop
threw the correct answer away three times for not matching. A criterion may
describe SHAPE; it may not smuggle in a RESULT the planner worked out itself.

Everything here runs with no model: `plan_detailed` takes its generate
function as an argument.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.planning import planner_styles as ps  # noqa: E402

GOAL = ("Without writing any file, compute the sum of the squares of the "
        "integers 1 to 50 and report the number.")


def run(coro):
    return asyncio.run(coro)


# ── invented values: the loop-o49 defect ─────────────────────────────────────
def test_invented_value_from_the_real_census_failure():
    crit = "The Python process returns the integer 207085 as output"
    assert ps.invented_values(crit, GOAL) == ["207085"]


def test_value_present_in_goal_is_not_invented():
    assert ps.invented_values("prints 12345 on one line", "echo 12345 please") == []


def test_short_literals_are_shape_not_results():
    # exit codes, line counts, HTTP statuses, "1 to 50": below 4 digits is shape.
    crit = "exits 0, prints 5 lines, HTTP 200, covers 1 to 50, has 999 rows"
    assert ps.invented_values(crit, GOAL) == []


def test_thousands_separators_and_underscores_are_canonicalised():
    assert ps.invented_values("returns 207,085", GOAL) == ["207085"]
    assert ps.invented_values("returns 207_085", GOAL) == ["207085"]
    # ...and a goal that spells it with separators grounds the plain form.
    assert ps.invented_values("returns 207085", "expect about 207,085") == []


def test_same_invented_value_reported_once():
    assert ps.invented_values("207085 twice: 207085", GOAL) == ["207085"]


def test_drop_invented_criteria_rejects_rather_than_annotates():
    keep, drop = ps.drop_invented_criteria(
        ["stdout is a single integer", "stdout is 207085", "exit code is 0"], GOAL)
    assert keep == ["stdout is a single integer", "exit code is 0"]
    assert drop == ["stdout is 207085"]


# ── line handling ────────────────────────────────────────────────────────────
def test_clean_lines_strips_bullets_keeps_order_and_dedupes():
    raw = "1. Write the script\n- Run it\n* run it\n\n• Report the output\n"
    assert ps.clean_lines(raw) == ["Write the script", "Run it", "Report the output"]


def test_clean_lines_drops_none_and_prose():
    assert ps.clean_lines("none") == []
    assert ps.clean_lines("N/A") == []
    prose = "x" * (ps.MAX_BULLET_CHARS + 1)
    assert ps.clean_lines(prose + "\nshort") == ["short"]


def test_clean_lines_is_bounded():
    raw = "\n".join("line %d" % i for i in range(100))
    assert len(ps.clean_lines(raw)) == ps.MAX_BULLETS_PER_LENS


# ── caps: never build a step around something that cannot run ───────────────
def test_caps_mentioned_filters_invented_names():
    lines = ["exec.python.run - run it", "file.write - save it", "web.search - no"]
    known = ["exec.python.run", "web.search"]
    assert ps.caps_mentioned(lines, known) == ["exec.python.run", "web.search"]


def test_caps_mentioned_unfiltered_when_no_catalogue():
    assert ps.caps_mentioned(["file.write - x", "exec.bash.run"]) == \
        ["file.write", "exec.bash.run"]


# ── agreement: word overlap, no model ───────────────────────────────────────
def test_agreements_finds_points_two_lenses_reached():
    per = {"decompose": ["write primes.py that prints every prime below 100"],
           "artifacts": ["primes.py - prints every prime below 100"],
           "risks": ["forgets to actually run the script"]}
    agreed = ps.agreements(per)
    assert len(agreed) == 1
    assert "[artifacts, decompose]" in agreed[0]


def test_agreements_ignores_single_lens_points():
    assert ps.agreements({"risks": ["something specific and unique here"]}) == []


# ── the brief and the plan it becomes ────────────────────────────────────────
def _results(criteria="stdout is one integer\nstdout is 207085"):
    return {
        "decompose": "Run a python one-liner\nReport the printed number",
        "artifacts": "none",
        "risks": "writes a file although the goal forbids it",
        "criteria": criteria,
        "caps": "exec.python.run - compute it\nfile.write - no such thing",
    }


def test_merge_brief_names_missing_lenses_and_rejected_criteria():
    b = ps.merge_brief(_results(), goal=GOAL, known_caps=["exec.python.run"])
    assert b["answered"] == ["caps", "criteria", "decompose", "risks"]
    assert b["missing"] == ["artifacts"]
    assert b["caps"] == ["exec.python.run"]
    assert b["rejected_criteria"] == ["stdout is 207085"]
    assert b["lenses"]["criteria"] == ["stdout is one integer"]


def test_merge_brief_drops_criteria_lens_when_every_criterion_is_invented():
    b = ps.merge_brief(_results(criteria="stdout is 207085"), goal=GOAL)
    assert "criteria" not in b["lenses"]
    assert "criteria" in b["missing"]
    assert b["rejected_criteria"] == ["stdout is 207085"]


def test_brief_to_plan_has_the_loops_shape():
    plan = ps.brief_to_plan(ps.merge_brief(_results(), goal=GOAL,
                                           known_caps=["exec.python.run"]))
    assert set(plan) >= {"steps", "reason", "complexity", "recon", "done_when", "brief"}
    steps = plan["steps"]
    assert [s["id"] for s in steps] == [1, 2]
    assert steps[0]["needs"] == [] and steps[1]["needs"] == [1]
    for s in steps:
        assert set(s) >= {"id", "title", "goal", "caps", "skills", "needs",
                          "complex", "phases", "success"}
        assert s["caps"] == ["exec.python.run"]
    # One criterion per step while they last, then nothing — never a recycled one.
    assert steps[0]["success"] == "stdout is one integer"
    assert steps[1]["success"] == ""
    assert plan["done_when"] == "stdout is one integer"
    assert "207085" not in plan["done_when"]
    assert "rejected 1 invented" in plan["reason"]


def test_brief_to_plan_bounds_steps():
    res = _results()
    res["decompose"] = "\n".join("step %d" % i for i in range(12))
    plan = ps.brief_to_plan(ps.merge_brief(res, goal=GOAL), max_steps=3)
    assert len(plan["steps"]) == 3


def test_render_brief_is_empty_for_an_empty_brief_and_bounded_otherwise():
    assert ps.render_brief({}) == ""
    text = ps.render_brief(ps.merge_brief(_results(), goal=GOAL))
    assert text.startswith("PLANNING BRIEF")
    assert "Rejected" in text and "207085" in text
    assert "Not answered: artifacts" in text
    assert len(ps.render_brief(ps.merge_brief(_results(), goal=GOAL), max_chars=80)) <= 110


# ── plan_detailed: concurrency, injection, failure posture ──────────────────
def test_plan_detailed_runs_every_lens_and_only_caps_sees_the_catalogue():
    seen = []

    async def gen(prompt, system=""):
        seen.append((prompt, system))
        if "AVAILABLE CAPABILITIES" in prompt:
            return "exec.python.run - do it"
        if "in order" in system:
            return "compute it\nreport it"
        if "prove the GOAL" in system:
            return "stdout is a single integer"
        return "none"

    plan = run(ps.plan_detailed(GOAL, gen, catalog=["exec.python.run", "web.search"]))
    assert len(seen) == len(ps.LENSES)
    with_cat = [p for p, _ in seen if "AVAILABLE CAPABILITIES" in p]
    assert len(with_cat) == 1
    assert all(p.startswith("GOAL: " + GOAL) for p, _ in seen)
    assert [s["title"] for s in plan["steps"]] == ["compute it", "report it"]
    assert plan["steps"][0]["caps"] == ["exec.python.run"]


def test_a_dead_lens_costs_nothing_but_its_own_answer():
    async def gen(prompt, system=""):
        if "FAILS" in system:
            raise RuntimeError("node down")
        if "in order" in system:
            return "do the thing"
        return "none"

    plan = run(ps.plan_detailed(GOAL, gen))
    assert plan["steps"] and plan["steps"][0]["title"] == "do the thing"
    assert "risks" in plan["brief"]["missing"]
    # ...and WHY it is missing is recorded, not swallowed.
    assert plan["brief"]["errors"]["risks"].startswith("RuntimeError: node down")
    # A lens that answered 'none' is absent by its own account, not an error.
    assert "artifacts" not in plan["brief"]["errors"]


def test_a_slow_lens_times_out_and_is_named_missing():
    async def gen(prompt, system=""):
        if "FAILS" in system:
            await asyncio.sleep(5)
        return "do the thing" if "in order" in system else "none"

    plan = run(ps.plan_detailed(GOAL, gen, timeout_s=0.05))
    assert plan["steps"]
    assert "risks" in plan["brief"]["missing"]
    assert plan["brief"]["errors"]["risks"].startswith("TimeoutError")
    assert "(TimeoutError" in ps.render_brief(plan["brief"])


def test_five_timeouts_read_as_timeouts_not_as_an_empty_goal():
    # The first live run: every lens missing, brief empty, cause invisible.
    async def gen(prompt, system=""):
        await asyncio.sleep(5)

    plan = run(ps.plan_detailed(GOAL, gen, timeout_s=0.05))
    assert plan["steps"] == []
    assert sorted(plan["brief"]["errors"]) == sorted(n for n, _ in ps.LENSES)
    assert all(v.startswith("TimeoutError") for v in plan["brief"]["errors"].values())


def test_an_empty_reply_is_named_as_such():
    b = ps.merge_brief({"decompose": "", "risks": "   ", "criteria": "stdout is 207085"},
                       goal=GOAL)
    assert b["errors"]["decompose"] == "empty reply"
    assert b["errors"]["risks"] == "empty reply"
    assert "never gave" in b["errors"]["criteria"]
    # Lenses that were never asked at all carry no error.
    assert "artifacts" not in b["errors"] and "caps" not in b["errors"]


def test_lenses_run_concurrently_not_serially():
    started = []

    async def gen(prompt, system=""):
        started.append(system[:10])
        await asyncio.sleep(0.05)
        return "x y z"

    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        loop.run_until_complete(ps.plan_detailed(GOAL, gen))
        elapsed = loop.time() - t0
    finally:
        loop.close()
    assert len(started) == len(ps.LENSES)
    # Five 50ms lenses in series would take >= 250ms; concurrent is ~50ms.
    assert elapsed < 0.2


# ── the registry ─────────────────────────────────────────────────────────────
def test_registry_is_honest_about_what_it_does_not_own():
    assert ps.style_ids() == ["detailed", "single"]
    assert ps.get_style("single")["plan"] is None
    assert ps.get_style(" Detailed ")["plan"] is ps.plan_detailed
    assert ps.get_style("nope") is None
