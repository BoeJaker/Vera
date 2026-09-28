"""
The columns are the user's (Notes/42 defect 49): the message measure is the column's — never wider than the space the
chat has — so the block and the composer flow; the grown graph's width is dragged on the rail's handle and kept; a
handle between the chat and the canvas sets the canvas's width and keeps it; the chat stack floors at 360 px. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_the_measure_flows_into_the_column():
    src = _read("vera", "chat", "chat_panel.html")
    assert "#chatColumn{--measure:min(736px, calc(100% - 32px))}" in src
    assert "body.ctx-grown #chatColumn,body.ctx-remote #chatColumn{--measure:min(736px, calc(100% - var(--ctx-gutter,16px) - 32px))}" in src
    assert "body.has-cols #chatStack{flex:1 1 0;min-width:360px}" in src


def test_the_graph_and_canvas_widths_are_dragged_and_kept():
    src = _read("vera", "chat", "chat_panel.html")
    assert "localStorage.setItem('vera_ctx_grown_w', String(nw))" in src and "localStorage.getItem('vera_ctx_grown_w')" in src
    assert "function _cvResizeMount(){" in src and "h.id='cvResize'" in src
    assert "col.style.setProperty('--cv-w', nw+'px'); col.classList.add('sized');" in src
    assert "localStorage.setItem('vera_canvas_col_w'" in src and "#canvasColumn.sized{flex:0 0 var(--cv-w,520px)!important" in src