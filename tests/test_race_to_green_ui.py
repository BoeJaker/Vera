"""The race-to-green strip must show a RACE, and a shorter green bar must show.

Reported from the live Loop Lab: "race to green appears not to be working and
we are not able to see the full history of unit tests over time".

Both are visible in the source. The strip read `/evolve/pipeline/list`, filtered
to code pipelines and drew one cell per PIPELINE coloured by `gate_passed` — a
single scalar on the pipeline record, overwritten every time that pipeline is
re-gated. A branch that went red, got fixed and went green was ONE green cell,
so the race was precisely the thing it could not display. And nothing persisted
the counts at all: the gate parsed {passed, failed, errors, skipped} on every
run, emitted an event and returned.

Source-level assertions, same approach as test_census_panel_modals: the panel is
a 300KB HTML file with no JS test harness. The inline script is also parsed with
node where available, which is what catches a dropped bracket in a
string-concatenated template.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.critical

PANEL = os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start)]


# ── it must read the history, not just the pipelines ────────────────────────
def test_the_strip_reads_the_unittest_history(src):
    assert "/evolve/unittest/history" in src, \
        "still reading only pipeline.list — one cell per pipeline cannot show a race"


def test_the_strip_renders_one_cell_per_run(src):
    body = _fn(src, "_utLanes")
    assert "uh.lanes" in body or "uh&&uh.lanes" in body


def test_the_race_and_the_trend_are_shown(src):
    body = _fn(src, "_utLanes")
    assert "race.still_red" in body, "a branch stuck red must say so"
    assert "went_green_at" in body, "the red→green transition is the headline"
    assert "total_delta" in body, "tests over time is the other half of the ask"


# ── the signal that would otherwise read as success ─────────────────────────
def test_a_green_run_on_fewer_tests_is_surfaced(src):
    """Coverage that stops being collected reports as PASS. The strip has to
    say it out loud or it looks like an ordinary green."""
    body = _fn(src, "_utLanes")
    assert "regressions" in body
    assert "FEWER TESTS" in body or "fewer tests" in body.lower()


def test_bar_height_carries_the_test_count(src):
    """Colour alone cannot distinguish '2799 passed' from '2744 passed' — both
    are green. Height is what makes a shrinking suite visible."""
    body = _fn(src, "_utLanes")
    assert "c.total" in body and "height:" in body


# ── a fresh deploy must not be a blank card ─────────────────────────────────
def test_it_falls_back_while_the_history_is_empty(src):
    """The history starts empty on the deploy that introduces it. Falling back
    to the old per-pipeline view beats an empty card that reads as broken."""
    body = _fn(src, "_utLanes")
    assert "pl.pipelines" in body, "no fallback — a fresh deploy shows nothing"
    assert "gate_passed" in body


def test_the_empty_state_explains_itself(src):
    body = _fn(src, "_utLanes")
    assert "next gate will start the history" in body


# ── the wiring ──────────────────────────────────────────────────────────────
def test_the_lane_renderer_is_actually_called(src):
    assert "_utLanes(" in _fn(src, "loadUnitTests")


def test_a_cell_still_opens_its_pipeline(src):
    body = _fn(src, "_utLanes")
    assert "openPipe(" in body and "pipeline_id" in body


# ── the whole panel still parses ────────────────────────────────────────────
def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    assert bodies, "no inline script found — the selector is wrong, not the panel"
    joined = "\n;\n".join(bodies)
    p = subprocess.run([node, "--check", "-"], input=joined, text=True,
                       capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
