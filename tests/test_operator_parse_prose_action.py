"""A prose reply never becomes a made-up action (edge mirror 2026-10-02, run
fa706e575d: "Action: I will click..." executed as action 'I')."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.operator.thinker import parse_decision  # noqa: E402


def test_prose_after_action_is_not_an_action():
    d = parse_decision("Thinking Process:\n\n1. **Analyze**\nAction: I will click the Start button")
    assert "error" in d and "action" not in d


def test_a_real_action_on_its_own_line_still_parses():
    d = parse_decision("I should wait.\naction: wait")
    assert d["action"] == "wait" and not d.get("error")
    d = parse_decision('Action: I think so.\n"action": "Click"')
    assert d["action"] == "click"


def test_json_is_unaffected():
    d = parse_decision('ok {"thought": "t", "action": "wait", "args": {"ms": 500}}')
    assert d["action"] == "wait" and d["args"] == {"ms": 500}
