"""The loop must never be offered evolve.*, and MUST be offered operator.run.

Two findings from the 2026-08-27 census, in the same place - what reaches the
loop's catalog.

evolve.* : a step trying to run the tests it had just written reached for
  evolve.unittest.run and got "unknown repo '/workspace/statkit' - register it
  via evolve.repo.add", twice, then flailed through context.search_caps and
  memory.seek. That cap runs pytest for a REGISTERED REPO BRANCH; it cannot see
  a session workspace. Its NAME is the trap. The family is also Vera's own
  CI/CD - promote, sandbox teardown - which a goal-driven loop has no business
  wandering into.

operator.run : P6 was recorded as "the controller will not insert operator.run".
  The controller was never the problem. operator.run was seeded ONLY when the
  GOAL TEXT matched a verify-intent regex needing a verb AND a UI noun, so
  "Create clock.html - a page showing a live digital clock" never matched, the
  cap was absent from the catalog, and the controller then correctly obeyed its
  own instruction: "If NO cap in the catalog can settle the check, do not insert
  it." Matching the ARTIFACT closes the gap at the source.

Imports the app module, so it runs in-container.
"""
import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")


def test_this_module_actually_imported_the_app():
    assert M is not None and hasattr(M, "_v5_cap_denied")


# â”€â”€ evolve.* is not offered to the loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@pytest.mark.parametrize("cap", [
    "evolve.unittest.run",        # the one the census caught
    "evolve.pipeline.promote",    # lands code
    "evolve.pipeline.adopt",
    "evolve.sandbox.down",        # tears down a container
    "evolve.repo.add",
    "evolve.task.run",
])
def test_the_evolve_family_is_denied(cap):
    assert M._v5_cap_denied(cap) is True, "%s must not be offered to the loop" % cap


def test_the_review_request_is_the_one_deliberate_exception():
    """Two shipped coding profiles are built around it, and it mutates nothing."""
    assert M._v5_cap_denied("evolve.pipeline.review.request") is False


def test_the_deny_is_by_prefix_not_a_hand_listed_set():
    """A new evolve.* cap must be denied the day it is added, not when noticed."""
    assert M._v5_cap_denied("evolve.some.future.cap.nobody.has.written.yet") is True


def test_unrelated_caps_are_untouched():
    for cap in ("code.author", "operator.run", "web.fetch", "exec.bash.run",
                "evolveXnotaprefix.thing"):
        assert M._v5_cap_denied(cap) is False, cap


def test_the_existing_denylist_still_applies():
    assert M._v5_cap_denied("browser.navigate") is True


def test_deflood_actually_drops_denied_caps():
    """The single choke point every candidate cap funnels through."""
    out = M._v5_deflood_catalog(
        ["code.author", "evolve.unittest.run", "operator.run",
         "evolve.pipeline.promote", "browser.navigate"], "build a page")
    assert "evolve.unittest.run" not in out
    assert "evolve.pipeline.promote" not in out
    assert "browser.navigate" not in out
    assert "code.author" in out and "operator.run" in out


# â”€â”€ operator.run reaches goals that BUILD something viewable â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@pytest.mark.parametrize("goal", [
    "Create clock.html - a self-contained HTML page showing a live digital clock",
    "Build a habit tracker WEB APP as a single self-contained index.html",
    "make me a landing page for the product",
    "build a dashboard for the sensor data",
])
def test_a_web_artifact_goal_can_reach_the_browser(goal):
    """These have no verify verb, so the old regex missed every one of them."""
    assert M._V5_WEB_ARTIFACT_RE.search(goal), goal


@pytest.mark.parametrize("goal", [
    "get the latest in AI and ML",
    "Create a small Python package at /workspace/statkit with stats.py",
    "Write a 200-word explainer of what a race condition is",
])
def test_a_non_visual_goal_does_not_drag_in_a_browser(goal):
    """Seeding it everywhere would be its own kind of noise."""
    assert not M._V5_WEB_ARTIFACT_RE.search(goal), goal


def test_an_explicit_verify_goal_still_matches_the_original_route():
    assert M._V5_UI_VERIFY_STEP_RE.search(
        "Create form.html and verify in a browser that the error appears")
