from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


SOURCE = (Path(__file__).parents[1] / "vera" / "elements" /
          "flow_builder_element.js").read_text(encoding="utf-8")


def _section(start: str, end: str) -> str:
    offset = SOURCE.index(start)
    return SOURCE[offset:SOURCE.index(end, offset)]


def test_toolbar_exposes_explicit_workflow_ir_import_and_export():
    assert 'data-act="ir-export"' in SOURCE
    assert 'data-act="ir-import"' in SOURCE
    assert 'data-role="ir-text"' in SOURCE
    assert 'data-act="ir-preview"' in SOURCE
    assert 'data-act="ir-apply" disabled' in SOURCE


def test_export_uses_non_executing_shared_conversion_contract():
    export = _section("async exportWorkflowIR(){", "async previewWorkflowIR(workflow){")
    assert "workflow.flow_builder.to_ir" in export
    assert "!report.ok || !report.workflow" in export
    assert "flow:workflow-export" in export
    call = _section("async _workflowCall(name, args){", "async exportWorkflowIR(){")
    assert "report.executes!==false" in call
    assert "invalid Workflow IR response" in call


def test_preview_is_non_mutating_and_apply_requires_exact_evidence():
    preview = _section("async previewWorkflowIR(workflow){", "applyWorkflowIR(preview){")
    assert "workflow.flow_builder.from_ir" in preview
    assert "this.setGraph" not in preview
    apply = _section("applyWorkflowIR(preview){", "loadFromSource(doc){")
    assert "preview===this._irPreview" in apply
    assert "JSON.stringify(preview)===this._irPreviewJSON" in apply
    assert "preview.executes!==false" in apply
    assert "blocking || changed" in apply
    assert "!preview.graph" in apply
    assert apply.index("if(!unchanged") < apply.index("this.setGraph")


def test_text_edits_invalidate_a_previously_approved_preview():
    wiring = _section("_wire(){", "_inspInput(e){")
    assert "this._$('ir-text').addEventListener('input'" in wiring
    assert "this._irPreview=null" in wiring
    assert "querySelector('[data-act=\"ir-apply\"]')" in wiring
    assert "disabled=true" in wiring


def test_late_import_and_export_responses_cannot_restore_stale_state():
    assert "disconnectedCallback(){ clearTimeout(this._compatTimer); this._compatSeq++; this._irSeq++; }" in SOURCE
    export = _section("async _openIRExport(){", "_openIRImport(){")
    preview = _section("async _previewIRText(){", "_applyIRPreview(){")
    assert "const seq=++this._irSeq" in export
    assert "if(seq!==this._irSeq)return" in export
    assert "const seq=++this._irSeq" in preview
    assert "if(seq!==this._irSeq){" in preview
    assert "if(this._irPreview===report){ this._irPreview=null; this._irPreviewJSON=''; }" in preview
    assert "_closeIRPanel(){ this._irSeq++" in SOURCE


def test_import_panel_escapes_server_supplied_evidence():
    status = _section("_setIRStatus(message, state, report){", "_closeIRPanel(){")
    assert "_esc(message)" in status
    assert "_esc(g.path||'workflow')" in status
    assert "_esc(g.detail||g.code||'Compatibility issue')" in status
    assert "_esc(d.path||'$')" in status
    assert "_esc(d.kind||'changed')" in status


def test_product_ui_does_not_expose_internal_delivery_vocabulary():
    lowered = SOURCE.lower()
    for forbidden in ("w4-04", "p5-w17", "roadmap slice", "plan stage"):
        assert forbidden not in lowered
