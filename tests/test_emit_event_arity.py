"""`emit_event` takes ONE dict — a name plus a payload is a TypeError.

    async def emit_event(event: dict)

The OpenClaw bridge called it as `emit_event("openclaw.connected", {...})` at
all ten of its call sites. Nothing caught it for as long as the module existed,
because every one of those lines sat behind a handshake that could never
succeed (2026-09-05). The moment the handshake was fixed, the first successful
connect died on `emit_event() takes 1 positional argument but 2 were given` and
reported itself as a connection failure.

A grep would miss the same mistake written across two lines, so this walks the
AST of every module instead. It is deliberately repo-wide: the failure mode is
silent until the surrounding code first runs, which may be years later.
"""
import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERA = os.path.join(REPO, "vera")


def _python_files():
    for root, dirs, files in os.walk(VERA):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "node_modules"}]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _emit_event_calls(tree):
    """Every call whose callee is named `emit_event`, however it was reached."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        if name == "emit_event":
            yield node


def test_emit_event_is_never_called_with_a_name_and_a_payload():
    offenders = []
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        try:
            tree = ast.parse(source)
        except SyntaxError:            # not this test's business
            continue
        for call in _emit_event_calls(tree):
            if len(call.args) > 1:
                offenders.append(
                    f"{os.path.relpath(path, REPO)}:{call.lineno} "
                    f"({len(call.args)} positional args)")
    assert not offenders, (
        "emit_event takes a single dict — pass the event name as its 'type' "
        "key:\n  " + "\n  ".join(offenders))


def test_every_emit_event_payload_carries_a_type():
    """A dict literal with no `type` publishes an unnamed event: subscribers
    (the panels read `msg.event || msg.type`) can never match it."""
    offenders = []
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for call in _emit_event_calls(tree):
            if len(call.args) != 1 or not isinstance(call.args[0], ast.Dict):
                continue           # a variable or a build-up dict — can't tell statically
            keys = {k.value for k in call.args[0].keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if not ({"type", "event"} & keys):
                offenders.append(f"{os.path.relpath(path, REPO)}:{call.lineno}")
    assert not offenders, (
        "these emit_event payloads name no event type:\n  " + "\n  ".join(offenders))


def test_the_openclaw_bridge_still_names_its_events():
    """The specific regression: the bridge's own events must survive a connect."""
    path = os.path.join(VERA, "openclaw", "openclaw_capabilities.py")
    if not os.path.exists(path):
        pytest.skip("openclaw module not present")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    calls = list(_emit_event_calls(tree))
    assert calls, "expected the bridge to emit events"
    names = set()
    for call in calls:
        assert len(call.args) == 1, f"line {call.lineno} passes {len(call.args)} args"
        payload = call.args[0]
        assert isinstance(payload, ast.Dict), f"line {call.lineno} is not a dict literal"
        for key, value in zip(payload.keys, payload.values):
            if isinstance(key, ast.Constant) and key.value == "type":
                if isinstance(value, ast.Constant):
                    names.add(value.value)
                elif isinstance(value, ast.JoinedStr):   # f"openclaw.gw.{event}"
                    names.add("openclaw.gw.*")
    assert {"openclaw.connected", "openclaw.disconnected"} <= names
