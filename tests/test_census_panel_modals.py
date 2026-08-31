"""A modal must appear on the click, and a compare row describes two runs.

Reported from the live Loop Lab census panel: modals "take a long time to load,
often not loading at all", and in the compare table "the entire line is
clickable but should have 2 records".

Both are visible in the source. Every census modal opened only AFTER its fetch
returned, so a slow endpoint was indistinguishable from a dead button and an
error opened nothing at all (it toasted). And the compare row - which describes
a BASE run and a HEAD run side by side - carried a single onclick that silently
opened the head record, with no way to reach the base one.

These are source-level assertions because the panel is a 4000-line HTML file
with no JS test harness; the same approach caught the operator loader-import
regression. The inline script is also parsed with node when it is available,
which is what catches a dropped bracket in a string-concatenated template.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.critical

PANEL = os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "evolve_panel.html")


@pytest.fixture(scope="module")
def src():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    """The body of a top-level function, up to the next column-0 close brace."""
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start)]


# ── a modal must appear on the click ────────────────────────────────────────

@pytest.mark.parametrize("fn", ["openCensusGoal", "openOperatorTrace"])
def test_the_modal_opens_before_the_fetch(src, fn):
    body = _fn(src, fn)
    assert "openModalLoading(" in body, f"{fn} must open a frame first"
    assert body.index("openModalLoading(") < body.index("await api("), (
        f"{fn} still awaits its data before showing anything")


@pytest.mark.parametrize("fn", ["openCensusGoal", "openOperatorTrace"])
def test_a_failed_fetch_reports_inside_the_modal(src, fn):
    """A toast with no modal is the "nothing happened" the report describes."""
    body = _fn(src, fn)
    assert "modalFail(" in body
    assert "toast(" not in body.split("modalFail(")[0].split("await api(")[-1]


def test_a_late_response_cannot_paint_over_a_newer_modal(src):
    """Two clicks in a row, the first slower: without a token the stale answer
    wins."""
    assert "_modalSeq" in src
    body = _fn(src, "modalFill")
    assert "tok!==_modalSeq" in body


# ── the loop's own output ───────────────────────────────────────────────────

def test_the_goal_modal_carries_the_agent_loop_output(src):
    body = _fn(src, "openCensusGoal")
    assert "cenAloBlock(" in body
    assert "cenAloStart(" in body
    assert "record.session_id" in body, "the loop is keyed by the goal's session"


def test_the_live_card_can_open_the_running_loop(src):
    assert "openCensusLiveLoop(" in src
    assert "function openCensusLiveLoop" in src


def test_the_output_element_is_the_shared_one(src):
    """Not a bespoke renderer - the panel already loads this element."""
    assert "vera-agent-loop-output" in _fn(src, "cenAloBlock")
    assert '/ui/elements/agent_loop_output.js' in src


def test_the_poller_uses_the_index_exact_endpoint(src):
    """session_state?since=N replays from 0 and cannot silently miss events;
    the SSE/event-bus route is what the Test section abandoned."""
    body = _fn(src, "cenAloPoll")
    assert "/workshop/agent_loop/session_state" in body
    assert "since=" in body


def test_the_poller_stops_when_the_run_is_terminal(src):
    body = _fn(src, "cenAloPoll")
    assert "setTimeout(cenAloPoll" in body
    assert "return" in body.split("!=='running'")[1][:200], (
        "a finished run must stop polling, not tick forever")


def test_closing_the_modal_stops_the_poller(src):
    """Otherwise it polls a hidden element for the life of the page."""
    assert "cenAloStop()" in _fn(src, "closeModal") or "cenAloStop()" in src[
        src.index("function closeModal("):src.index("function closeModal(") + 200]


def test_the_poller_gives_up_if_the_element_is_gone(src):
    body = _fn(src, "cenAloPoll")
    assert "cen-alo" in body and "cenAloStop()" in body


# ── a compare row describes two runs ────────────────────────────────────────

def test_the_compare_row_is_not_one_click_target(src):
    body = _fn(src, "loadCensusCompare")
    assert "'<tr class=\"clk\" onclick=\"openCensusGoal(" not in body, (
        "the whole line still opens a single record")


def test_each_run_cell_opens_its_own_record(src):
    body = _fn(src, "loadCensusCompare")
    opens = re.findall(r"openCensusGoal\(\\'\'\+esc\((\w)\)", body)
    assert set(opens) == {"b", "h"}, f"expected a base and a head target, got {opens}"


def test_the_cells_say_which_run_they_open(src):
    """"Not clear selecting from one run to another" - the affordance has to
    name the run, not just be clickable."""
    body = _fn(src, "loadCensusCompare")
    assert "open the '+esc(b)+' record" in body
    assert "open the '+esc(h)+' record" in body


# ── the file still parses ───────────────────────────────────────────────────

def test_the_inline_script_parses():
    """String-concatenated HTML templates hide a dropped bracket from review;
    one slipped through while making this change."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    with open(PANEL, encoding="utf-8") as fh:
        html = fh.read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "no inline script found - the extraction is wrong, not the panel"
    js = "\n;\n".join(blocks)
    tmp = os.path.join(os.path.dirname(__file__), "_panel_check.js")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:2000]
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
