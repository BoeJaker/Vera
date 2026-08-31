from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


def test_trigger_ui_distinguishes_pause_cancel_and_delete():
    panel = (Path(__file__).parents[1] / "vera" / "dream" / "dream_panel.html").read_text(
        encoding="utf-8")

    assert "/dream/trigger/'+(state==='active'?'resume':'pause')" in panel
    assert "/dream/trigger/cancel" in panel
    assert "Its record will be preserved and cannot be resumed" in panel
    assert "Delete trigger" in panel
    assert "enabled:t.enabled" not in panel
    assert "enabled:_teEnabled" in panel
