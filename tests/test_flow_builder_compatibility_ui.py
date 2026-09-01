from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


SOURCE = (Path(__file__).parents[1] / "vera" / "elements" /
          "flow_builder_element.js").read_text(encoding="utf-8")


def test_builder_exposes_clear_compatibility_states_and_details():
    assert 'data-role="compat-badge"' in SOURCE
    assert 'data-role="compat-panel"' in SOURCE
    assert "Portable" in SOURCE
    assert "Native details " in SOURCE
    assert "Blocked " in SOURCE
    assert "Execution blocked" in SOURCE
    assert "This analysis does not execute the flow." in SOURCE


def test_builder_uses_shared_analysis_and_rejects_invalid_responses():
    assert "workflow.flow_builder.analyze" in SOURCE
    assert "typeof report.ok!=='boolean'" in SOURCE
    assert "report.executes!==false" in SOURCE
    assert "analysis_unavailable" in SOURCE
    assert "blocking:true" in SOURCE


def test_graph_changes_invalidate_inflight_analysis_and_debounce_refresh():
    schedule = SOURCE[SOURCE.index("_scheduleCompatibility(){"):]
    assert schedule.index("this._compatSeq++") < schedule.index("this._compat = null")
    assert "setTimeout(()=>this.getCompatibility({refresh:true}), 180)" in schedule
    assert "if(seq!==this._compatSeq)" in SOURCE
    assert "disconnectedCallback(){ clearTimeout(this._compatTimer); this._compatSeq++; }" in SOURCE


def test_generic_dag_execution_fails_closed_before_native_request():
    run = SOURCE[SOURCE.index("async runAsDag(opts){"):]
    guard = run.index("await this.requireCompatible({operation:'run'})")
    conversion = run.index("const { dag, state } = this.toDag()")
    request = run.index("/workshop/dag/run_stream")

    assert guard < conversion < request
    assert "compatibility_blocked" in run
    assert "return;" in run[guard:conversion]


def test_compatibility_panel_escapes_server_supplied_paths_and_details():
    panel = SOURCE[SOURCE.index("_renderCompatibilityPanel(){"):]
    assert "_esc(g.path||'workflow')" in panel
    assert "_esc(g.detail||g.code||'Compatibility detail')" in panel
    assert "_esc(d.path||'$')" in panel
    assert "_esc(d.kind||'changed')" in panel


def test_product_ui_contains_no_internal_delivery_stage_language():
    lowered = SOURCE.lower()
    for forbidden in ("w4-04", "p5-w17", "roadmap slice", "plan stage"):
        assert forbidden not in lowered
