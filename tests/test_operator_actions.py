"""Actions: pure structural validation of the action space."""

import pytest

from vera.operator import actions as A


@pytest.mark.parametrize("action,args,ok", [
    ("click", {}, False),
    ("click", {"ref": "e1"}, True),
    ("click", {"x": 10, "y": 20}, True),
    ("hover", {}, False),
    ("type", {}, False),
    ("type", {"text": "hi"}, True),
    ("press", {}, False),
    ("press", {"key": "Enter"}, True),
    ("goto", {}, False),
    ("goto", {"url": "/x"}, True),
    ("select", {"ref": "e1"}, False),
    ("select", {"ref": "e1", "value": "v"}, True),
    ("select", {"ref": "e1", "label": "L"}, True),
    ("nav", {"direction": "sideways"}, False),
    ("nav", {"direction": "back"}, True),
    ("wait", {}, True),
    ("done", {}, True),
    ("screenshot", {}, True),
    ("bogus", {}, False),
])
def test_validate_action(action, args, ok):
    assert A.validate_action(action, args)["ok"] is ok


def test_action_space_text_lists_all():
    txt = A.action_space_text()
    for name in A.ACTIONS:
        assert name in txt


def test_mutating_set():
    assert "click" in A.MUTATING_ACTIONS
    assert "scroll" not in A.MUTATING_ACTIONS  # read-ish, never gated
