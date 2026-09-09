"""Static contract for honest produced-file inventory states in the loop UI."""

from pathlib import Path

import pytest


pytestmark = pytest.mark.critical
SOURCE = Path(__file__).resolve().parents[1] / "vera" / "agent_loop_ouput.js"


def _artifact_reader() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("async _renderFilesCard()")
    end = source.index("// Plain JSON POST", start)
    return source[start:end]


def test_artifact_card_uses_shared_safe_read_state_with_retry():
    source = SOURCE.read_text(encoding="utf-8")
    assert "window.veraUI.readState" in source
    assert "window.veraUI.renderReadState" in source
    assert "onRetry:()=>this._renderFilesCard()" in source
    assert "target.textContent = label" in source
    assert "kind === 'error' ? 'alert' : 'status'" in source


def test_artifact_inventory_distinguishes_loading_empty_and_failure():
    reader = _artifact_reader()
    assert "_renderFilesReadState('loading','Loading produced files…')" in reader
    assert "if(!r.ok) throw new Error('Artifact inventory returned HTTP ' + r.status)" in reader
    assert "if(!data || data.ok === false)" in reader
    assert "if(!Array.isArray(data.files))" in reader
    assert "_renderFilesReadState('error'" in reader
    assert "_renderFilesReadState('empty','This run produced no files.')" in reader
    assert "catch(e){ return; }" not in reader


def test_latest_artifact_refresh_owns_the_visible_state():
    source = SOURCE.read_text(encoding="utf-8")
    reader = _artifact_reader()
    assert "this._filesLoadToken = 0" in source
    assert "const token = ++this._filesLoadToken" in reader
    assert reader.count("if(token !== this._filesLoadToken) return") == 2


def test_populated_inventory_keeps_existing_artifact_actions():
    source = SOURCE.read_text(encoding="utf-8")
    reader = _artifact_reader()
    assert "data.files.filter(f => f && !f.is_dir)" in reader
    assert 'data-act="preview"' in reader
    assert 'data-act="source"' in reader
    assert "this._fileUrl(f.name)" in reader
    assert "files-refresh" in source
