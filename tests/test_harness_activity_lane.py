"""
The activity ticker, its lane and the run flow as the Harness board draws them (design landing, step 5 / B2):
activity_overlay.js is the one owner — the pill after the header's meters, the panel in the column flow under the
header, kind chips with colour and count, the lane of cards ending at "now", the run flow beneath (steps · in/out ·
duration · state, Re-run · Open in Loop Lab). The harness drops its step-4 duplicate ticker and guards the old
ollama.health read. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


OV = _read("vera", "activity_overlay.js")
HTML = _read("vera", "capability_orchestration.html")


def test_the_overlay_is_the_ticker_the_lane_in_flow_and_the_run_flow():
    assert "const anchor = header.querySelector('.hsp') || header.querySelector('.hdr-right');" in OV and "header.insertAdjacentElement('afterend', panel);" in OV
    assert "panel.classList.add('float');" in OV and "if (!panel.classList.contains('float')) return;" in OV, "floats only on a page with no header"
    for s in ("#vao-pill{display:inline-flex;align-items:center;gap:8px;height:30px", "#vao-panel{display:none;flex-direction:column;flex-shrink:0;margin:8px 16px 10px", ".vao-lane{display:flex;gap:9px;padding:0 12px 12px;align-items:stretch;height:132px",
              ".vao-chip i{", ".vao-kind i{", ".vao-flow{", ".vao-fs.run .vao-fdot{", ".vao-fio .in{", ".vao-fio .out{", "<div class=\"vao-flowhost\"></div>"):
        assert s in OV, s
    assert "(LABEL[e.kind] || e.kind || '') + ' · ' + String(e.title || '').slice(0, 60)" in OV, "the pill reads kind · title"
    assert "<i></i>${esc(LABEL[k] || k)}<span class=\"vao-n\">${counts[k] || 0}</span>" in OV, "a kind chip carries its colour and count"
    for fn in ("function stepsFromLoopEvents(evs)", "async function loadFlow(e)", "function renderFlow()", "function tagsOf(e)"):
        assert fn in OV, fn
    assert "/workshop/agent_loop/session_state?session_id=" in OV and "window.VeraGraphFamilies" in OV, "a loop's steps from its persisted events through the families"
    assert "x.children.map((child, ci) =>" in OV and "x.loop_plan.map((lp, i) =>" in OV, "a run's children and a program's loop plan"
    assert "Re-run · attach ↗" in OV and "Open in Loop Lab ↗" in OV and "Open in DAG workshop ↗" in OV
    assert "narratorStatus" in OV and "toggleNarrator" in OV and "vao-narr" in OV, "the narrator switch stays"


def test_the_harness_uses_the_overlay_as_its_ticker_and_guards_the_old_ollama_read():
    assert 'id="hdrAct"' not in HTML and "function _hdrActToggle" not in HTML and "function _hdrActSync" not in HTML, "the step-4 duplicate ticker is gone"
    assert "header #vao-pill,header .vao{" in HTML
    assert '<script src="/ui/elements/activity_overlay.js"></script>' in HTML
    assert "const po=document.getElementById('panel-ollama'); if(po&&po.classList.contains('active')) renderInstCards();" in HTML
    assert "document.getElementById('panel-ollama').classList" not in HTML
