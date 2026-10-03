"""A page that stays the same after a click is the answer to "does it change?" (prod
2026-10-03: a broken countdown clicked six times, repeat guard at 370 s, no answer)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.operator import completion as C  # noqa: E402


def h(action, seen, thought="t"):
    return {"action": action, "seen": seen, "thought": thought, "args": {}}


def test_three_unchanged_observations_after_a_click_nudge_done():
    n = C.nudge_for([h("click", "00:30 Start"), h("wait", "00:30  Start"), h("click", "00:30 Start")])
    assert "it did NOT change" in n and "00:30 Start" in n and "a click" in n


def test_no_nudge_while_the_page_moves_or_too_early():
    assert C.nudge_for([h("click", "00:30"), h("wait", "00:29"), h("wait", "00:27")]) == ""
    assert C.nudge_for([h("click", "00:30"), h("wait", "00:30")]) == ""


def test_typing_and_waiting_alone_never_nudge():
    assert C.nudge_for([h("type", "Name Email"), h("type", "Name Email"), h("type", "Name Email")]) == ""
    assert C.nudge_for([h("wait", "x"), h("wait", "x"), h("scroll", "x")]) == ""


def test_done_and_the_claim_nudge_keep_precedence():
    assert C.nudge_for([h("click", "a"), h("click", "a"), h("done", "a")]) == ""
    n = C.nudge_for([h("click", "a"), h("wait", "a"), h("click", "a", "We have verified it works")])
    assert n == C.NUDGE
