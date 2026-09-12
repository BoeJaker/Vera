"""
The unified graph's families (UI redesign, Notes/40 §7) and the chat's context
graph (the Graph board's column). The memory graph panel is out of scope and
untouched; the chat's graph column is the chat's own element. The files are
text, so this runs anywhere (the layouts run under node in
tests/test_graph_families.cjs and tests/test_context_graph_element.cjs).
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


PANEL = _read("vera", "fabric", "memory_graph_panel.html")
ROUTES = _read("vera", "vera_graph_panels.py")
CHAT_ROUTES = _read("vera", "chat", "chat_panels_capabilities.py")
CHAT = _read("vera", "chat", "chat_panel.html")
FAM = _read("vera", "graph", "families.js")
EL = _read("vera", "chat", "context_graph_element.js")


def test_families_are_served():
    assert '@APP.get("/ui/graph/families.js", include_in_schema=False)' in ROUTES and 'Path(__file__).parent / "graph" / "families.js"' in ROUTES
    assert "root.VeraGraphFamilies = api;" in FAM and "module.exports = api;" in FAM


def test_the_memory_graph_panel_is_untouched():
    # out of scope: nothing of the unified graph's UI lives in it, and nothing embeds it in the chat's columns
    for marker in ('id="family-chips"', 'id="vchip-galaxy"', "function ingestFamily(", '<script src="/ui/graph/families.js"></script>', "vera:graph:family"):
        assert marker not in PANEL, marker
    assert "_resolvePanel('memory-graph')" not in CHAT and "graphColumnFrame" not in CHAT


def test_the_chats_context_graph_is_its_own_element():
    assert '@APP.get("/ui/context_graph_element.js", include_in_schema=False)' in CHAT_ROUTES and 'Path(__file__).parent / "context_graph_element.js"' in CHAT_ROUTES
    assert "root.customElements.define('vera-context-graph', VeraContextGraph);" in EL and "root.VeraContextGraph = api;" in EL
    for view in ("'galaxy'", "'iso'", "'flow'", "'time'"):
        assert view in EL
    assert "vera:ctx:rendered" in EL and "vera:ctx:pick" in EL and "vera:ctx:toggle" in EL and "vera:ctx:focus-turn" in EL
    assert "positions()" in EL, "the host draws the runs to the message from the positions the element reports"
    assert "VeraGraph" not in EL.replace("VeraGraphFamilies", "").replace("VeraContextGraph", ""), "not built on any other graph's engine"
    assert "createGraph(" not in EL and "vera_graph.js" not in EL
    assert '<script src="/ui/context_graph_element.js"></script>' in CHAT and '<script src="/ui/graph/families.js"></script>' in CHAT
    assert "_ctxCol=document.createElement('vera-context-graph')" in CHAT
    # the chat's graphs integrate: context (fed), memory (the rail's session graph on the arc), dag (the loop lane's fallback), loop (teed)
    for api in ("setContext(nodes, edges, o)", "setMemory(nodes, edges, o)", "setDag(nodes)", "appendLoopEvent(ev)", "setPlan(goals)", "allEdges(on)"):
        assert api in EL, api
    assert "data-a=\"alledges\"" in EL and "if (!S.allEdges) {" in EL, "the design's All edges toggle"
    assert "data-a=\"collapse\"" in EL and "vera:ctx:collapse" in EL, "Collapse folds the graph back into the quick menu"
    assert "out.families.push({ name: 'memory'" in EL and "out.families.push({ name: 'dag'" in EL and "out.families.push({ name: 'loop'" in EL


def test_the_lane_and_the_plan_row_come_from_the_runs_own_events_for_any_run():
    # pass B: families.js reads the planner's plan, sub-plans, replans and every record kind
    assert "if (/\\.(plan|replan)$/.test(t) || /master_plan_piece_planned$/.test(t)) {" in FAM and "if (/\\.subplan$/.test(t)) {" in FAM
    assert "edges.push(edge(pid, id, 'EXECUTED_BY', 'loop'));" in FAM, "the board's EXECUTED_BY — a plan step to the loop steps that ran it"
    assert "const RECORD = /(assess|verify|ledger|clarif|recover|gate|deliverable|finaliz|finalis|journal|question)/;" in FAM
    # the element: the run's plan is the row while a run is in the lane; the lane's structure; the runs picker
    assert "const runPlan = (S.loop && S.loop.length && S.runPlan && S.runPlan.length) ? S.runPlan : [];" in EL
    assert "(planIsRun ? 'plan · ' : 'goals · ')" in EL and "st === 'run' ? 'mem' : 'exec'" in EL
    assert "loopStems" in EL and "' pruned'" in EL and "<span class=\"m\">" in EL, "sub-steps, pruned branches, marks"
    assert "setRuns(list, o) {" in EL and 'data-r="runs"' in EL and "vera:ctx:run" in EL
    # the chat: the live loop first, else this session's last run replayed from session_state; the picker's run, tailed
    assert "function _ctxColRuns(force){" in CHAT and "function _ctxColShowRun(sid){" in CHAT
    assert "/workshop/agent_loop/sessions?limit=40" in _fn("_ctxColRuns") and "/workshop/agent_loop/session_state?session_id=" in _fn("_ctxColShowRun")
    assert "_reattachLoop(sid, (st.count||events.length), null, null, ev=>{ if(_ctxRunSid===sid&&_ctxCol) _ctxCol.appendLoopEvent(ev); }, ac.signal);" in _fn("_ctxColShowRun")
    assert "_ctxCol.addEventListener('vera:ctx:run'" in CHAT
    assert "if(t==='done'||/\\.done$/.test(t)){ _ctxColPlanT=0; _ctxRunsT=0; }" in _fn("_ctxColLoopEv"), "a run ending refreshes the goals and the runs"
    assert "async function _reattachLoop(sid, since, aloEl, gEl, onEv, signal){" in CHAT and "try{ const ev=JSON.parse(raw); tee(ev); }catch(_){}" in CHAT


def _fn(name):
    i = CHAT.index("function %s(" % name)
    j = CHAT.find("\n  function ", i + 10)
    return CHAT[i:j if j > 0 else i + 20000]
