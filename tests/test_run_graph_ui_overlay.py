from pathlib import Path

import pytest


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_chat_context_graph_exposes_session_scoped_run_layer():
    panel = (ROOT / "vera/chat/chat_panel.html").read_text(encoding="utf-8")
    assert 'data-ctx-layer="run"' in panel
    assert "/run/shadow/graph?session_id=" in panel
    assert "source:'run'" in panel
    assert panel.count("session_id:SID||''") >= 2


def test_memory_graph_loads_and_streams_non_authoritative_runs():
    panel = (ROOT / "vera/fabric/memory_graph_panel.html").read_text(encoding="utf-8")
    assert "/run/shadow/graph?" in panel
    assert "t === 'run.event'" in panel
    assert "non_authoritative:true" in panel
    assert "record_type:run.parent_run_id?'run_node':'run'" in panel
