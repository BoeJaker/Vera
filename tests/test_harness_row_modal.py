"""Every dashboard row opens its own detail modal (B4): the board's .mbox — title · meta · Copy · Open · ✕, the stats strip, the rows."""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_every_row_opens_the_boards_modal_and_rows_with_their_own_handler_keep_it():
    for s in ("function _rowOf(t){", "function _rowFields(row){", "function _rowDetail(row){", "function _rowDetailClose(){", "#rowModal .mbox{", "#rowModal .mstats{", "#rowModal .mrow{"):
        assert s in HTML, s
    assert "if(t.closest('button, a, input, select, textarea, [contenteditable], .w-head, .w-actions, .w-resize, [onclick]')) return;" in HTML, "a row with its own handler keeps it"
    assert "if(ev.key === 'Escape') _rowDetailClose();" in HTML
    assert "dashWidgetWindow(wid)" in HTML, "Open takes the widget to its own window"
