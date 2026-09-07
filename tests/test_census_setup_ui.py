"""The census must be startable and editable from the Loop Lab, not a shell script.

Asked for twice: "need to add a way for the user to trigger them from the ui
also for the user to configure them from the UI like create a set of questions
as a template, each template records its own timeline from run 0 so only runs of
the same questions are comparable" — and then, when it had not happened, "i think
the loop lab UI should let you setup censuses - i did ask for this but you
havent acted on it."

Before this the Census section was five read-only cards. The only way to start a
run was `python3 run_census.py` in a directory outside the repo, which is also
why the templates lived outside the repo.

The assertions that matter most are the ones about what a number MEANS:

  * the timeline is filtered by the template's own tag — a run of a different
    question set is not a comparable point and must never share the chart;
  * the run is started with assess=false — a census score is its declared
    checks, and adding a critic's opinion would silently redefine it.

Source-level, same approach as test_census_panel_modals and
test_race_to_green_ui: the panel is a 300KB HTML file with no JS harness.
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


# ── you can start one ───────────────────────────────────────────────────────
def test_a_census_can_be_started_from_the_panel(src):
    assert "/evolve/suite/start" in _fn(src, "ctRun"), \
        "no way to trigger a census from the UI — the original request"


def test_starting_one_asks_first(src):
    """It takes the GPU for hours. A misclick is expensive."""
    assert "confirm(" in _fn(src, "ctRun")


def test_the_run_does_not_add_a_critic(src):
    """A census score is its DECLARED checks — does clock.html contain a timer,
    does the report cite a source. Letting the suite's critic score it too
    would change what every historical number means."""
    assert "assess:false" in _fn(src, "ctRun").replace(" ", "")


# ── you can configure one ───────────────────────────────────────────────────
def test_a_template_can_be_seeded_into_tasks(src):
    assert "/evolve/census/template/seed" in _fn(src, "ctSeed")


def test_a_template_can_be_created_and_saved(src):
    assert "/evolve/census/template/save" in _fn(src, "ctSave")
    assert "ctNew" in src and "ctEdit" in src


def test_the_editor_reports_what_would_be_meaningless(src):
    """save refuses duplicate goal ids, goals with no checks, and checks that
    assert nothing. The person editing has to see why."""
    body = _fn(src, "ctSave")
    assert "_ctShowProblems" in body
    assert "problems" in body


def test_a_forced_save_is_a_separate_button(src):
    """Overriding the refusal must be deliberate, not the default path."""
    assert "ctSave(true)" in src and "ctSave(false)" in src


def test_an_edit_that_rebases_the_timeline_says_so(src):
    body = _fn(src, "ctSave")
    assert "breaks_comparability" in body


# ── the timeline means what it says ─────────────────────────────────────────
def test_the_timeline_is_filtered_to_this_templates_tag(src):
    """The whole point of templates: only runs of the same questions share a
    chart. An unfiltered evolve.suites call would mix them."""
    body = _fn(src, "ctTimeline")
    assert "tag=" in body and "_ctCur.tag" in body


def test_the_timeline_counts_from_run_zero_per_template(src):
    body = _fn(src, "ctTimeline")
    assert "reverse()" in body, "oldest first, or run 0 is not run 0"
    assert "Run 0" in body or "run 0" in body


def test_a_contended_run_is_flagged_in_the_timeline(src):
    """Its timings queued behind someone else's GPU work and are not
    comparable with an uncontended run."""
    assert "contended" in _fn(src, "ctTimeline")


# ── the wiring ──────────────────────────────────────────────────────────────
def test_the_card_loads_with_the_census_section(src):
    assert "loadCensusTemplates()" in _fn(src, "loadCensus")


def test_every_element_the_script_touches_exists(src):
    ids = set(re.findall(r'id="([a-z0-9-]+)"', src))
    for want in ("ct-pick", "ct-model", "ct-meta", "ct-timeline", "ct-editor",
                 "ct-json", "ct-save-msg", "ct-problems", "ct-status"):
        assert want in ids, "script references #%s but no element declares it" % want


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies),
                       text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
