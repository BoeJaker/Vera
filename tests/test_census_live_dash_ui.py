"""While a census runs, Work shows a live dash: the loop's own output and the
goal's numbers as they move.

Asked 2026-09-10: "if a census is running there should be a live dash with
the live agentic loop ui output and live infographics". Source-level pins:
the dash renders under the live strip from the one evolve.work.live poll
(nothing on it fetches on its own), the loop output follows the running
session keyed by element (so the pop-out modal and the dash can coexist),
the infographics cover goals / elapsed vs cap / steps x cycles / counters /
this goal's routing / quality so far, and the dash goes away when no goal is
in flight.
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


def test_the_dash_sits_under_the_live_strip_and_renders_from_the_one_poll(src):
    i = src.index('<div class="sec" id="sec-work"')
    sec = src[i:src.index("\n</div>\n", i)]
    assert 'id="work-live"' in sec and 'id="census-dash"' in sec
    assert sec.index('id="work-live"') < sec.index('id="census-dash"') < sec.index('id="census-runs"')
    poll = _fn(src, "censusPoll")
    assert "_cenDash(_workLive&&_workLive.census)" in poll
    for fn in ("_cenDash", "_cenDashInfo"):
        assert "await api(" not in _fn(src, fn), "%s fetches on its own" % fn


def test_the_dash_shows_the_loop_output_and_goes_away_with_the_goal(src):
    body = _fn(src, "_cenDash")
    assert "cenAloBlock(a.session_id,'cen-dash-alo')" in body and "cenAloStart(a.session_id,'cen-dash-alo')" in body
    assert "cenAloStop('cen-dash-alo')" in body, "no goal in flight: the follow stops and the dash clears"
    assert "openCensusLiveLoop(" in body, "pop out to the larger modal"
    # the follow is keyed by element id, so the modal ('cen-alo') and the dash coexist
    assert "const _cenAloS={};" in src
    assert "function cenAloStart(sid,id)" in src and "function cenAloStop(id)" in src
    assert "if(_cenAloS[id]!==s)return;" in _fn(src, "cenAloPoll"), "a restart for another session must not be fed the old one's events"
    assert "try{cenAloStop()}catch(_){}" in _fn(src, "closeModal"), "closing the modal stops only the modal's follow"


def test_the_infographics_cover_the_goal_as_it_runs(src):
    body = _fn(src, "_cenDashInfo")
    for signal in ("goals_total", "p.remaining", "elapsed_s", "near the cap", "steps × cycles", "tool_calls",
                   "unaccounted_steps", "by_node", "by_model", "tok_s_median", "spill_calls", "reroutes",
                   "ro.last", "quality", "wall time per finished goal", "_cenSpark("):
        assert signal in body, signal
    assert "animation:pulse" in body and "@keyframes pulse" in src


def test_census_live_carries_the_goals_routing():
    here = os.path.join(os.path.dirname(__file__), "..", "vera", "census")
    with open(os.path.join(here, "census_capabilities.py"), encoding="utf-8") as fh:
        cap = fh.read()
    assert '"routing": routing,' in cap and "_ctl.live_routing(" in cap and '"ollama.request_log"' in cap
    with open(os.path.join(here, "control.py"), encoding="utf-8") as fh:
        ctl = fh.read()
    assert "def live_routing(" in ctl


def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies), text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
