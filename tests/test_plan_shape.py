"""A goal classified as complex must not be planned as one step.

O11 was written up as a TIER problem - "a short sentence describing a big build
gets classified simple". True of the pokedex chat goal, but not what has been
failing in the census, and the recorded data says so plainly. Every run of
build-multifile:

    run1            complex  planned=4  ->  done
    run10-prefixes  complex  planned=4  ->  done
    run13-partial   complex  planned=4  ->  done
    run6-stalled    complex  planned=3  ->  done
    run9-partial    complex  planned=3  ->  done
    run11           complex  planned=1  ->  executed 7, wall-cap
    run12-partial   complex  planned=1  ->  executed 8, gate added 3, wall-cap
    run14b-partial  complex  planned=1  ->  executed 5, gate added 2

The tier is `complex` every single time. The planner intermittently returns ONE
step for the same goal at the same tier, and when it does the completion gate
rebuilds the plan a step at a time - 1500s against 420s for the same goal planned
properly.

Same prompt, different answer. So the check has to be on the RESULT's shape, not
on more prompt wording.

Pure: no Redis, no app import - so it runs anywhere, including the host venv.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag.plan_shape_core import better_plan, is_underdecomposed  # noqa: E402

# The runner's ordering: single=0, simple=1, complex=2, strategic=3.
SINGLE, SIMPLE, COMPLEX, STRATEGIC = 0, 1, 2, 3


def _steps(n):
    return [{"id": i, "title": "step %d" % i} for i in range(1, n + 1)]


def _check(tier_rank, n):
    return is_underdecomposed(tier_rank=tier_rank, complex_rank=COMPLEX,
                              steps=_steps(n))


# ── the case this exists for ────────────────────────────────────────────────
def test_a_complex_goal_planned_as_one_step_is_flagged():
    flagged, why = _check(COMPLEX, 1)
    assert flagged is True
    assert "ONE step" in why and "wall cap" in why


def test_strategic_is_flagged_too():
    assert _check(STRATEGIC, 1)[0] is True


def test_a_reason_is_always_given_when_flagged():
    """A silent re-plan would be one more thing that happens to a run without
    explaining itself."""
    flagged, why = _check(COMPLEX, 1)
    assert flagged and why.strip()


# ── what must NOT be touched ────────────────────────────────────────────────
def test_a_simple_goal_planned_as_one_step_is_left_alone():
    """build-simple-code is planned=1 and finishes in ~200s, run after run. A
    one-step plan is usually RIGHT at this tier."""
    assert _check(SIMPLE, 1)[0] is False
    assert _check(SINGLE, 1)[0] is False


def test_a_decomposed_complex_plan_is_left_alone():
    for n in (2, 3, 4, 8):
        assert _check(COMPLEX, n)[0] is False, "%d steps is a decomposition" % n


def test_an_empty_plan_is_left_alone():
    """Zero steps already has its own path - the runner falls through to STEPWISE
    with a bootstrap step. Re-planning here would fight that."""
    assert _check(COMPLEX, 0)[0] is False


def test_an_unknown_tier_is_left_alone():
    """Fail toward doing nothing: an unrecognised tier must not trigger an extra
    planning call on every run."""
    assert is_underdecomposed(tier_rank=None, complex_rank=COMPLEX,
                              steps=_steps(1))[0] is False


# ── the retry is only worth taking if it decomposed ────────────────────────
def test_a_retry_is_accepted_only_when_it_decomposed_further():
    assert better_plan(_steps(1), _steps(4)) is True
    assert better_plan(_steps(1), _steps(2)) is True


def test_an_equally_flat_retry_is_rejected():
    """Taking it would burn a planning call for nothing AND make the run look
    like it recovered when it did not."""
    assert better_plan(_steps(1), _steps(1)) is False


def test_a_worse_or_empty_retry_is_rejected():
    assert better_plan(_steps(3), _steps(1)) is False
    assert better_plan(_steps(1), []) is False
    assert better_plan(_steps(1), None) is False


# ── the wiring itself ──────────────────────────────────────────────────────
# A guard that is correct but never called is inert, and inertness is silent -
# the _V7_CRITERIA_RULE failure mode. Source-level so it needs no app import.
_MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "vera", "dag", "dag_workshop_capabilities.py")


def _src():
    with open(_MODULE, encoding="utf-8") as fh:
        return fh.read()


def test_the_guard_is_wired_before_the_plan_is_emitted():
    """The plan the user sees must be the plan that runs, so the re-plan has to
    happen before the plan event - same rule as the cap re-routing."""
    src = _src()
    assert "is_underdecomposed(" in src, "guard is not called"
    at = src.find('"type": "agent_loop_v6.plan"')
    assert at > 0
    assert "is_underdecomposed(" in src[:at], (
        "the guard must run BEFORE the plan is emitted")


def test_the_guard_uses_the_runners_own_tier_ordering():
    """Passing ranks in (rather than importing a tier list into the pure module)
    is what stops this drifting from _V7_TIERS."""
    src = _src()
    assert "_v7_tier_rank(tier)" in src and '_v7_tier_rank("complex")' in src


def test_the_replan_and_its_outcome_are_both_recorded():
    src = _src()
    assert '"agent_loop_v6.plan_underdecomposed"' in src
    assert '"agent_loop_v6.plan_underdecomposed_replan"' in src
