"""A `done` that says only "goal complete" carries what the page showed (prod A/B
2026-10-03: 3 of 6 working-countdown runs reported no values)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.operator import completion as C  # noqa: E402

HIST = [{"action": "click", "seen": "00:30 Start"}, {"action": "wait", "seen": "00:30  Start"},
        {"action": "wait", "seen": "00:27 Start"}]


def test_a_generic_done_gets_the_observed_trail():
    s = C.evidence_summary("goal complete", HIST, "00:25 Start")
    assert s == "goal complete. Observed on the page, in order: '00:30 Start' -> '00:27 Start' -> '00:25 Start'"
    assert "Observed on the page" in C.evidence_summary("", HIST)
    assert "Observed on the page" in C.evidence_summary("Done.", HIST)


def test_a_real_summary_is_kept_untouched():
    real = "Countdown went from 00:30 to 00:27 after 3 s"
    assert C.evidence_summary(real, HIST) == real
    assert C.evidence_summary("The error 'Please enter a valid email' appeared", HIST).startswith("The error")


def test_no_history_no_change_and_bounded():
    assert C.evidence_summary("goal complete", []) == "goal complete"
    many = [{"seen": "v%d" % i} for i in range(10)]
    assert C.evidence_summary("done", many).count("->") == 3


def test_the_loop_uses_it():
    src = (ROOT / "vera" / "operator" / "operator_loop.py").read_text(encoding="utf-8")
    assert "_completion.evidence_summary(summary, history, _observed_text(obs))" in src
