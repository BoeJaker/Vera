"""DEEP-21 D21-1 (compute-roles, 11 Oct 2026): every schedule kind except the census launch starts in the
background - the tick never waits on the work it starts (RF-09 complete)."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "evolve" / "schedule_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_no_kind_awaits_its_call_inline():
    i = SRC.index("async def _start(")
    body = SRC[i:SRC.index("\ndef _result_word", i)]
    for cap in ("evolve.suite.start", "evolve.task.run", "evolve.pipeline.test", "evolve.pipeline.adopt",
                "evolve.pipeline.promote", "board.dispatch", "loops.run", "evolve.unittest.run"):
        assert ('await _call("%s"' % cap) not in body, cap
    assert body.count("return _bg(run_id, rec, _call(") >= 6
    assert 'exclusive=bool(rec.get("exclusive", True))' in SRC
