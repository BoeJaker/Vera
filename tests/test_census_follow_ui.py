"""Following a census must survive the gaps between its goals.

Reported: "i hate i have to keep refreshing the loop lab for the census to stay
up to date" and "it'd be good if it was easier to monitor the runs and their
cycles overall - at the moment i have to refresh all the elements and load the
current run into the comparison table to see each steps stats".

Three defects in the source, and together they are exactly that experience:

  1. loadCensusLive re-armed its own timer at the END of its render path, and
     returned early when no loop was active. Between two goals there is no
     active loop for 30-60s, and a 12-goal census has ELEVEN such gaps — so
     following stopped at the first one and never resumed. An API hiccup did
     the same thing permanently.
  2. Only the live card polled at all. The run-history, operator and template
     tables never refreshed on their own, so a goal that FINISHED did not
     appear until a manual refresh.
  3. /census/live has always returned `recent` — the goals this run has already
     finished, with status and wall clock — and nothing rendered it. That is
     why the Compare table was the only way to see how a run was going, and
     the payload capped it at the last SIX of twelve.

Source-level, same approach as the other panel tests.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.critical

PANEL = os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html")
CAPS = os.path.join(os.path.dirname(__file__), "..", "vera", "census", "census_capabilities.py")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def caps():
    with open(CAPS, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start)]


# ── the timer cannot die ────────────────────────────────────────────────────
def test_the_poll_timer_is_rearmed_in_a_finally(src):
    """Any early return, thrown error or slow tick must still schedule the
    next one. This is the whole fix."""
    body = _fn(src, "censusPoll")
    assert "finally" in body and "_cenArm()" in body


def test_a_failed_tick_does_not_stop_the_next(src):
    assert "catch" in _fn(src, "censusPoll")


def test_the_render_path_no_longer_arms_its_own_timer(src):
    """It armed only on the branch that rendered an ACTIVE goal, which is why
    the gap between goals killed it."""
    assert "setTimeout(loadCensusLive" not in src


def test_no_active_loop_is_not_the_end_of_following(src):
    """Between goals there is no active loop. Returning null there would make
    the poller treat a normal gap as unreadable."""
    body = _fn(src, "loadCensusLive")
    assert "return doneN" in body


def test_ticks_do_not_stack(src):
    """A tick slower than the interval must not pile up behind itself."""
    body = _fn(src, "censusPoll")
    assert "_cenBusy" in body


def test_following_stops_when_you_leave_the_section(src):
    """Polling a hidden tab forever is its own bug."""
    body = _fn(src, "_cenFollowing")
    assert "_curSec()" in body and "census" in body
    assert "census-follow" in body


def test_the_follow_checkbox_actually_rearms(src):
    assert 'id="census-follow"' in src
    assert 'onchange="_cenArm()"' in src


# ── the tables refresh when there is news ───────────────────────────────────
def test_a_landed_goal_refreshes_the_heavy_tables(src):
    """Not every tick — only when the completed-goal count CHANGES, which is
    exactly when a goal finished."""
    body = _fn(src, "censusPoll")
    assert "_cenLastDone" in body
    assert "loadCensusRuns()" in body


def test_the_run_history_can_refresh_without_reentering_the_poller(src):
    """loadCensus used to own the history table inline, so refreshing it meant
    calling loadCensus, which calls the live card, which polls."""
    assert "async function loadCensusRuns(" in src
    assert "loadCensusRuns()" in _fn(src, "loadCensus")


# ── monitoring the run without the Compare table ────────────────────────────
def test_the_goals_finished_so_far_are_rendered(src):
    assert "function _cenRecent(" in src
    assert "_cenRecent(r)" in _fn(src, "loadCensusLive")


def test_the_strip_shows_the_wall_clock_and_the_outcome(src):
    body = _fn(src, "_cenRecent")
    assert "_cenSec(g.wall_s)" in body
    assert "wall-cap" in body


def test_a_goal_that_hit_the_cap_is_distinguishable(src):
    """A wall-cap is not a failure and not a pass; half the default goal set
    finishes within a minute of the cap, so it needs its own colour."""
    body = _fn(src, "_cenRecent")
    assert "cap" in body and "#c9a35a" in body


def test_the_strip_is_visible_between_goals_too(src):
    """The gap between goals is when you most want to see what just landed."""
    body = _fn(src, "loadCensusLive")
    idx = body.index("if(!a){")
    assert "_cenRecent(r)" in body[idx:idx + 400]


def test_a_goal_in_the_strip_opens_its_steps(src):
    assert "openCensusGoal(" in _fn(src, "_cenRecent")


# ── the cap is read, not assumed ────────────────────────────────────────────
def test_the_wall_cap_comes_from_the_server(caps):
    assert "CENSUS_WALL_CAP_S" in caps
    assert '"wall_cap_s": CENSUS_WALL_CAP_S' in caps


def test_every_finished_goal_is_returned_not_just_six(caps):
    """A 12-goal census was showing half its own progress."""
    assert "for r in done]" in caps, "still slicing `recent` to the last few"
    assert "done[-6:]" not in caps


# ── the panel still parses ──────────────────────────────────────────────────
def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies),
                       text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
