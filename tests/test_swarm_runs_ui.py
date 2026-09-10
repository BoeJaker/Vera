"""The Swarm's session list was empty by construction, and Runs said nothing.

Two reports:

  "the swarm page is displaying the codex agents operational are but im not
   sure its displaying yours"
  "im not sure what the runs ui of the loop lab is for"

The first is not a truncation problem. Swarm filtered sessions to
['live','resumable','stalled','declared-block'] — and EVERY ingested session on
this instance classifies as `untracked`, because untracked simply means "no
board claim and no pipeline". 90 of 90, measured 2026-09-07. So the list was
empty no matter what was running, and no session of yours could ever appear in
it. It also asked for 40 of 87 sessions, and named itself "Active Claude
sessions" now that codex transcripts are ingested too.

The second is a view with a title, a refresh button and no explanation: Runs is
the raw per-execution log every other view summarises, and without filters or a
line saying so it reads as redundant with Suite.
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


# ── the swarm can actually show a session ───────────────────────────────────
def _live_sess_filter(src):
    """Just the expression that decides which sessions the card shows.

    Scoped deliberately: asserting `age_s` appears anywhere in loadSwarm passed
    against the BROKEN version too, because the function already rendered
    `_csessAge(s.age_s)` in a row. A test that passes before the fix guards
    nothing.
    """
    i = src.index("const liveSess=")
    return src[i:src.index(";", src.index("filter(", i))]


def test_an_untracked_session_is_not_excluded(src):
    """`untracked` means no board claim — not "not running". Excluding it
    emptied the list on an instance where all 90 sessions are untracked."""
    f = _live_sess_filter(src)
    assert "age_s" in f, "the FILTER still tests state alone"


def test_recency_decides_what_counts_as_active(src):
    assert "stalled_after_s" in _fn(src, "loadSwarm")
    assert "_stale" in _live_sess_filter(src)


def test_the_swarm_asks_for_more_than_forty_sessions(src):
    assert "claude_sessions/watch?max_sessions=40" not in src


def test_each_session_row_names_its_agent(src):
    """"whose session is this" was the question the card could not answer."""
    body = _fn(src, "loadSwarm")
    assert "s.agent" in body
    assert "codex" in body


def test_the_section_is_no_longer_claude_only_by_name(src):
    assert "Active agent sessions" in src
    assert "Active Claude sessions" not in src


# ── the swarm's follow cannot die ───────────────────────────────────────────
def test_the_swarm_timer_is_rearmed_in_a_finally(src):
    body = _fn(src, "swarmPoll")
    assert "finally" in body and "_swArm()" in body


def test_the_swarm_render_no_longer_arms_its_own_timer(src):
    assert "setTimeout(loadSwarm" not in src


def test_swarm_ticks_do_not_stack(src):
    assert "_swBusy" in _fn(src, "swarmPoll")


def test_navigating_to_swarm_starts_the_poller(src):
    assert "swarm:swarmPoll" in src


# ── runs says what it is, and can be sifted ─────────────────────────────────
# Since flattening slice 3 the Runs page is the runs view of Work: every
# driver run (census, suite, improvement session, single run) in one table,
# rendered by renderCensusTable. The same things must still be possible.
def test_runs_explains_what_it_holds(src):
    assert "a row is one driver run" in src
    assert "census" in src and "single run" in src, "say what kinds of run share the table"


def test_runs_can_be_filtered_by_source(src):
    body = _fn(src, "renderCensusTable")
    assert "setWorkKind" in body and "setWorkSrc" in body
    for src_name in ("manual", "goal", "captest", "ide"):
        assert src_name in body


def test_runs_can_be_filtered_by_task(src):
    body = _fn(src, "renderCensusTable")
    assert "cen-text" in body and "x.task" in body, "the text filter reaches a single run's task"


def test_runs_can_show_problems_only(src):
    """A run that returned cleanly having failed half its checks is exactly
    what this view is for finding — so 'problems' is not just `error`."""
    body = _fn(src, "renderCensusTable")
    assert "'problems'" in body
    assert "x.failed" in body and "x.capped" in body and "s.wall_capped" in body


def test_filtering_does_not_refetch(src):
    """A round trip per keystroke would make sifting unpleasant."""
    body = _fn(src, "renderCensusTable")
    assert "_workRows" in body and "_cenRunsAll" in body
    assert "await api" not in body


def test_the_count_shows_the_denominator(src):
    """"12 runs" after filtering must not read as "12 runs exist"."""
    assert "_workMeta.total" in _fn(src, "renderCensusTable")


def test_the_commit_filter_still_works(src):
    """The git graph's "runs of this commit" link; census runs on that code
    count too."""
    body = _fn(src, "renderCensusTable")
    assert "filterCommit" in body and "x.commits" in body and "s.code" in body


# ── logs belong with the sandboxes they came from ───────────────────────────
def test_logs_is_not_its_own_rail_entry(src):
    """Not merged with Activity — those are different subjects (an audit of
    what CHANGED vs what a process PRINTED). Logs moved to Sandbox, where the
    containers it reports on already live."""
    assert 'data-sec="logs"' not in src


def test_every_logs_element_survived_the_move(src):
    """A flatten must not cost an element. Each control is still declared."""
    ids = set(re.findall(r'id="([a-z0-9-]+)"', src))
    for want in ("logs-body", "logs-status", "logs-container", "logs-follow",
                 "logs-perf"):
        assert want in ids, "lost #%s in the move" % want


def test_the_logs_card_is_inside_the_sandbox_section(src):
    start = src.index('id="sec-sandbox"')
    end = src.index('<div class="sec" id=', start + 10)
    for want in ("logs-body", "logs-status", "logs-container"):
        assert start < src.index('id="%s"' % want) < end


def test_opening_sandbox_loads_the_logs(src):
    assert "loadLogs()" in src
    assert "sandbox:()=>{loadSandbox();loadLogs()}" in src


def test_an_old_logs_deeplink_still_lands_somewhere_real(src):
    """Injected nav items and bookmarks still say 'logs'; they must not open a
    blank panel."""
    assert "logs:()=>nav('sandbox')" in src


def test_the_logs_follow_timer_follows_its_new_section(src):
    """It was keyed to _curSec()==='logs', a section that no longer exists —
    which would have stopped it following the moment it moved."""
    assert "_curSec()==='logs'" not in src


# ── the panel still parses ──────────────────────────────────────────────────
def test_the_panel_script_parses(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bodies = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
    p = subprocess.run([node, "--check", "-"], input="\n;\n".join(bodies),
                       text=True, capture_output=True)
    assert p.returncode == 0, "panel JS does not parse:\n" + (p.stderr or "")[-2000:]
