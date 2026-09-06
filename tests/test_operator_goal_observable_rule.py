"""A verification goal the operator cannot possibly settle.

Census 37, author-then-edit. Four operator.run calls, 1803s, wall-cap. The
operator's own thoughts show it reading the clock correctly throughout - 01:00,
00:47, 00:31, 00:00 - so perception was never the problem. The GOALS were:

    run 765710bbeb  "verify that initial countdown value displayed is 01:35
                     (90 seconds) instead of 01:00"
    run 49d0c4d184  "observe that countdown starts from 01:30 instead of 01:00"

The first names a target that does not exist: 90 seconds is 01:30, not 01:35.
The second is arithmetically right but still unobservable - startTimer() uses
setInterval(..., 1000), so 01:30 is first painted one second after the click and
is immediately replaced. Each operator observation costs a model call and lands
roughly every 30-60s, so it next saw 00:47.

Both ask for a single transient frame. The rule this file guards tells the
executor to ask for a state or a relation instead, and to have the operator
REPORT what it displays rather than assert a computed value.

Pure: the rule registry has no app imports.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag import loop_prompt_rules as R                # noqa: E402

RULE = R.OPERATOR_GOAL_OBSERVABLE


# ── it reaches the stage that writes the goal ───────────────────────────────
def test_the_rule_is_registered():
    assert R.RULES.get("operator_goal_observable") is RULE


def test_it_reaches_the_executor_stage():
    """The EXECUTOR writes operator.run(goal=...) - the planner does not. A rule
    aimed at the wrong stage is inert, which is the failure this whole registry
    exists to make visible."""
    assert "operator_goal_observable" in R.rule_ids_for("executor:operator")


def test_it_does_not_leak_into_the_planner_stages():
    for stage in ("planner:full", "planner:minimal", "controller", "adjust"):
        assert "operator_goal_observable" not in R.rule_ids_for(stage)


def test_drift_is_detectable():
    """missing_from is the check that catches a rule defined and never wired."""
    assert R.missing_from("executor:operator", "") == ["operator_goal_observable"]
    assert R.missing_from("executor:operator", RULE.text) == []


# ── what it actually tells the executor ─────────────────────────────────────
def test_it_names_the_observation_cadence():
    """Without the number, "be reasonable" is not actionable."""
    assert "30-60" in RULE.text


def test_it_forbids_asking_for_one_transient_frame():
    low = RULE.text.lower()
    assert "transient" in low or "one second" in low


def test_it_gives_the_settleable_alternative():
    """Telling a model what NOT to do without the replacement just produces a
    different unusable goal."""
    assert "counting down" in RULE.text.lower()
    assert "two readings" in RULE.text.lower()


def test_it_carries_the_arithmetic_that_actually_went_wrong():
    assert "90 seconds is 01:30" in RULE.text
    assert "01:35" in RULE.text


def test_it_routes_a_mismatch_back_to_the_file():
    """The other half of the census 37 failure: the loop re-ran the browser
    instead of fixing a genuinely half-edited file."""
    assert "FILE" in RULE.text and "report" in RULE.text.lower()


# ── the record ──────────────────────────────────────────────────────────────
def test_the_evidence_names_the_run_it_came_from():
    ev = RULE.evidence.lower()
    assert "census 37" in ev
    assert "01:35" in ev or "01:30" in ev


def test_the_existing_rules_are_untouched():
    """This adds a rule; it must not disturb the two the golden test pins."""
    assert set(R.RULES) == {"cap_routing", "criteria_settleable",
                            "operator_goal_observable"}
    assert R.rule_ids_for("planner:minimal") == ["cap_routing", "criteria_settleable"]
