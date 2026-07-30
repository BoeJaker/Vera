"""Thinker: prompt building + robust decision parsing (no LLM call)."""

from vera.operator import perception as P
from vera.operator import thinker as T


def test_parse_plain_json():
    d = T.parse_decision('{"thought":"go","action":"click","args":{"ref":"e1"},"done":false}')
    assert d["action"] == "click"
    assert d["args"] == {"ref": "e1"}
    assert d["done"] is False


def test_parse_fenced_json():
    d = T.parse_decision('```json\n{"action":"done","done":true}\n```')
    assert d["action"] == "done" and d["done"] is True


def test_parse_json_with_prose_around():
    d = T.parse_decision('Sure, here you go: {"action":"type","args":{"text":"hi"}} done.')
    assert d["action"] == "type"


def test_parse_fallback_regex():
    d = T.parse_decision("I will use action: click on the button")
    assert d["action"] == "click"


def test_parse_unrecoverable():
    assert "error" in T.parse_decision("no json and no action word here ...")
    assert "error" in T.parse_decision("")


def test_split_provider():
    assert T._split_provider("ollama") == ("ollama", "")
    assert T._split_provider("anthropic:claude-opus") == ("anthropic", "claude-opus")


def test_build_prompt_has_goal_and_actions():
    obs = P.build_observation({"url": "u", "title": "t",
                               "elements": [{"ref": "e1", "role": "button", "name": "Run"}]})
    pr = T.build_prompt("open the panel", obs, [], canvas=False)
    assert "open the panel" in pr["user"]
    assert "ACTION SPACE" in pr["user"]
    assert "e1: button" in pr["user"]
    assert "operator" in pr["system"].lower()
