"""A census template is a set of suite tasks — the translation must preserve
what made the census's numbers mean something.

Step 3 of the flattening. The risk is not that the conversion crashes; it is
that it quietly changes the experiment. Every number from runs 1-41 came from
`dag.agent_loop_v7` called with the goal and nothing else, under a 1800s cap.
A seeded task that adds a cap restriction, a different engine or a different
ceiling produces numbers that LOOK like a continuation of the timeline and are
not one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.evolve import census_seed as CS                  # noqa: E402


GOAL = {
    "id": "build-simple-code", "intent": "build", "tier": "simple",
    "output": "code", "shape": "one-file",
    "goal": "Create clock.html - a live digital clock.",
    "checks": [{"file": "clock.html", "exists": True, "why": "it was written"},
               {"file": "clock.html", "min_bytes": 200, "why": "not a stub"}],
}
TEMPLATE = {"name": "default", "description": "the twelve-goal spread",
            "model": "", "goals": [GOAL]}


# ── parity with the harness ─────────────────────────────────────────────────
def test_the_engine_is_the_one_the_census_actually_used():
    """planning is the only profile whose engine is v7. Anything else reaches a
    different engine and the timeline restarts without saying so."""
    assert CS.goal_to_task(GOAL, "default")["profile"] == "planning"
    assert CS.CENSUS_PROFILE == "planning"


def test_nothing_restricts_the_toolkit():
    """The harness restricts nothing. A restriction here is an extra variable
    in a comparison that exists to have none."""
    assert CS.goal_to_task(GOAL, "default")["allowed_caps"] == ""


def test_the_wall_cap_is_the_harness_ceiling():
    assert CS.DEFAULT_WALL_CAP_S == 1800
    assert CS.goal_to_task(GOAL, "default")["timeout_s"] == 1800


def test_a_template_may_set_its_own_ceiling():
    t = dict(TEMPLATE, wall_cap_s=600)
    assert CS.template_to_tasks(t)[0]["timeout_s"] == 600


def test_the_checks_are_carried_through_untouched():
    """The census's file checks are the half of the score the suite could not
    previously produce. Rewriting them here would be rewriting the exam."""
    assert CS.goal_to_task(GOAL, "default")["checks"] == GOAL["checks"]


def test_the_census_axes_survive():
    """intent/tier/output/shape are how the census panel groups its results."""
    c = CS.goal_to_task(GOAL, "default")["census"]
    assert (c["intent"], c["tier"], c["output"], c["shape"]) == \
           ("build", "simple", "code", "one-file")
    assert c["template"] == "default" and c["goal_id"] == "build-simple-code"


# ── the model pin ───────────────────────────────────────────────────────────
def test_a_model_pin_rides_as_an_override():
    """loops.run only passes a v7 engine the CALLER's explicit arguments, and
    `overrides` is how a task supplies one."""
    t = CS.template_to_tasks(dict(TEMPLATE, model="llama3:8b"))[0]
    assert t["overrides"] == {"model": "llama3:8b"}


def test_no_model_means_no_override_at_all():
    """An absent pin must leave the live routing alone, not pin it to ''."""
    assert "overrides" not in CS.goal_to_task(GOAL, "default")


def test_a_goal_may_pin_its_own_model():
    g = dict(GOAL, model="qwen3.5:9b")
    assert CS.goal_to_task(g, "default")["overrides"] == {"model": "qwen3.5:9b"}


# ── identity and selection ──────────────────────────────────────────────────
def test_one_tag_selects_exactly_one_template():
    assert CS.tag_for("default") == "census-default"
    assert CS.tag_for("Model Compare") == "census-model-compare"
    assert CS.tag_for("default") != CS.tag_for("model-compare")


def test_every_task_carries_both_tags():
    """`census` finds the whole census set beside the hand-written Loop Lab
    tasks; `census-<name>` finds one template's timeline."""
    assert CS.goal_to_task(GOAL, "default")["tags"] == ["census", "census-default"]


def test_task_ids_are_namespaced_by_template():
    """Two templates sharing a goal id must not overwrite each other's task."""
    a = CS.task_id_for("default", "build-simple-code")
    b = CS.task_id_for("model-compare", "build-simple-code")
    assert a != b and a.startswith("census-default")


def test_an_unnamed_template_still_produces_a_usable_tag():
    assert CS.tag_for("") == "census-unnamed"


# ── validation, before the GPU is spent ─────────────────────────────────────
def test_a_good_template_has_no_problems():
    assert CS.problems(TEMPLATE) == []


def test_a_duplicate_goal_id_is_refused():
    t = {"name": "d", "goals": [GOAL, dict(GOAL)]}
    assert any("duplicate" in p for p in CS.problems(t))


def test_a_goal_with_no_checks_is_flagged():
    """Without checks the task can only report what the run SAID — which is
    exactly the failure mode the census's file checks were added to catch."""
    t = {"name": "d", "goals": [dict(GOAL, checks=[])]}
    assert any("no checks" in p for p in CS.problems(t))


def test_a_check_that_asserts_nothing_is_flagged():
    """It inflates the denominator and reads as coverage."""
    t = {"name": "d", "goals": [dict(GOAL, checks=[{"file": "x", "why": "hmm"}])]}
    assert any("asserts nothing" in p for p in CS.problems(t))


def test_a_suite_style_check_is_assertable_too():
    assert CS.check_is_assertable({"type": "cap_called", "value": "echo"})
    assert CS.check_is_assertable({"file": "a", "exists": True})
    assert not CS.check_is_assertable({"file": "a", "why": "no assertion"})


def test_an_empty_or_broken_template_is_refused():
    assert CS.problems(None)
    assert CS.problems({})
    assert any("no goals" in p for p in CS.problems({"name": "d"}))
    assert any("no name" in p for p in CS.problems({"goals": [GOAL]}))


def test_a_goal_with_no_text_is_flagged():
    t = {"name": "d", "goals": [dict(GOAL, goal="  ")]}
    assert any("no goal text" in p for p in CS.problems(t))


def test_an_absurd_wall_cap_is_refused():
    assert any("wall_cap_s" in p for p in CS.problems(dict(TEMPLATE, wall_cap_s=5)))
    assert any("wall_cap_s" in p for p in CS.problems(dict(TEMPLATE, wall_cap_s="soon")))


# ── comparability ───────────────────────────────────────────────────────────
def test_an_unchanged_template_is_comparable_with_itself():
    assert CS.comparable(TEMPLATE, dict(TEMPLATE)) == []


def test_adding_a_goal_breaks_comparability():
    b = {"name": "default", "goals": [GOAL, {"id": "new", "goal": "g", "checks": []}]}
    assert any("added" in p for p in CS.comparable(TEMPLATE, b))


def test_removing_a_goal_breaks_comparability():
    assert any("removed" in p for p in CS.comparable(TEMPLATE, {"name": "d", "goals": []}))


def test_changing_the_model_breaks_comparability():
    assert any("model" in p for p in
               CS.comparable(TEMPLATE, dict(TEMPLATE, model="llama3:8b")))


def test_changing_the_wall_cap_breaks_comparability():
    """Half the default set finishes within a minute of the cap, so moving it
    changes the result without changing a single goal."""
    assert any("wall cap" in p for p in
               CS.comparable(TEMPLATE, dict(TEMPLATE, wall_cap_s=3600)))


def test_reordering_goals_is_still_comparable():
    """Order is not part of the question set."""
    g2 = {"id": "second", "goal": "g", "checks": [{"type": "no_error"}]}
    a = {"name": "d", "goals": [GOAL, g2]}
    b = {"name": "d", "goals": [g2, GOAL]}
    assert CS.comparable(a, b) == []
