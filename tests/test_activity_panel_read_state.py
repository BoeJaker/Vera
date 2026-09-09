"""Static contract for Activity's shared read-state adoption."""

from pathlib import Path


PANEL = Path(__file__).resolve().parents[1] / "vera/activity/activity_panel.html"


def test_activity_loads_shared_ui_before_using_read_state():
    html = PANEL.read_text(encoding="utf-8")
    shared = html.index('<script src="/ui/vera-ui.js"></script>')
    consumer = html.index("function renderPipelineState")
    assert shared < consumer
    assert "window.veraUI.readState({loading:" in html
    assert "window.veraUI.renderReadState" in html


def test_activity_distinguishes_loading_empty_and_error_with_retry():
    html = PANEL.read_text(encoding="utf-8")
    assert "renderPipelineState({loading:true})" in html
    assert "renderPipelineState({empty:true})" in html
    assert "renderPipelineState({error:" in html
    assert "onRetry:loadPipelines" in html
    assert "hasData:kind!=='empty'" in html
    assert "if(!res.ok)throw new Error('HTTP '+res.status)" in html
    assert "catch(e){\n    renderPipelineState({error:" in html


def test_activity_no_longer_fakes_a_healthy_default_scope_on_failure():
    html = PANEL.read_text(encoding="utf-8")
    assert "||[{scope:'all',label:'Everything',kind:'all'}]" not in html
    assert "const pls=r.pipelines" in html
    assert "$('tl').setScope(_scope)" in html
