"""
The exploded scene's + Place opens the WidgetConfig sheet — the same sheet as the LHM, the canvas and the dashboard —
and the record it resolves lands on the station's plate with its own form (content: form · size · record · placed);
the old list picker stays as the fallback; the DAG's edges reach the context graph. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_place_opens_the_sheet_and_lands_the_record():
    src = _read("vera", "chat", "chat_panel.html")
    assert "rec=await S.open({ mode:'add', into:'canvas', title:'Place on the plate'+(pm?' · turn '+pm:''), templates:true, sizes:['s','m','l'] })" in src
    assert "if(rec) await _xplPlaceRecord(rec, pm); return; }" in src
    assert "async function _xplPlaceRecord(rec, mid){" in src
    assert "record:rec, placed:{ where:'iso plate', turn:mid }" in src
    assert "anchor:{ mid, turn:mid }" in src
    # the fallback picker stays
    assert "async function _xplPlacePick(kind, id){" in src and "id=\"xplPlaceList\"" in src


def test_dag_edges_reach_the_graph():
    src = _read("vera", "chat", "chat_panel.html")
    assert "_ctxCol.setDag(_lastDagNodes, _lastDagEdges);" in src