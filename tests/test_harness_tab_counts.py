"""Counts on tabs with live work (the Harness board): Activity carries the feed's count, Workers & Ollama the busy instances."""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_tabs_with_live_work_carry_a_count():
    assert "function _tabCounts(){" in HTML and "_tabCounts(); setInterval(_tabCounts, 5000);" in HTML
    assert "set(/activity/i, isFinite(act)?act:0);" in HTML and "set(/workers|ollama/i, busy?parseInt(busy,10):0);" in HTML
    assert ".tab .ct{font-family:var(--f-mono" in HTML, "the board's count chip on the tab"
