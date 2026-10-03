"""A numeric bar the planner invented reaches the verifier as a guide, not a requirement
(probe 3 Oct, 850d0600: ">1500 words" failed a fetch step three times; 30 min, no report)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("cbc", ROOT / "vera" / "dag" / "criterion_bars_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

GOAL = "Fetch and extract detailed comparison data for 4 vector databases"
CRIT = "Two web.fetch calls each returning >1500 words of structured text covering at least 4 databases"


def test_invented_bars_only():
    assert C.invented_bars(CRIT, GOAL) == [">1500 words"]
    assert C.invented_bars("at least 300 characters", "write a 300 character summary") == []
    assert C.invented_bars("file exists", GOAL) == []


def test_note_names_them_and_is_empty_otherwise():
    n = C.note(CRIT, GOAL)
    assert "'>1500 words'" in n and "rough guide" in n
    assert C.note("report.md exists", GOAL) == ""


def test_the_verifier_gets_the_note():
    v = SRC[SRC.index("async def _v6_verify_step("):]
    v = v[:v.index("Was the criterion met?")]
    assert "_criterion_bars_core.note(crit, str(step.get('goal') or ''))" in v
