"""A sync mirror of an async helper loses writes silently.

`a35fd55` (2026-08-11) made the instance-store helpers async in
`ide_remote_capabilities.py` to get file I/O off the event loop - a live trace
had caught a 3.3s stall there causing WS flapping. `vscode_capabilities.py`
kept SYNC mirrors of `_load_instances`/`_save_instances` that delegated into
that module and never awaited, and two other modules called them the same way.

The read half failed loudly: `ide.vscode.instances` and
`ide.claude_sessions.sources` both returned 500 with `TypeError: 'coroutine'
object is not iterable`. **The write half failed silently** - `_save_instances`
built a coroutine and dropped it, so every VS Code instance save was a no-op
and nothing raised. It went unnoticed for nineteen days.

That asymmetry is why this guard is shape-based rather than behavioural: the
loud half would eventually be reported by a user, the silent half would not.
The rule is simply that a call to one of these helpers is always awaited.
"""
import ast
import os

import pytest

_IDE = os.path.join(os.path.dirname(__file__), "..", "vera", "ide")

#: The instance-store helpers. Async in ide_remote_capabilities.py, and
#: mirrored in vscode_capabilities.py, which is where the mirrors drifted.
HELPERS = {"_load_instances", "_save_instances", "_get_instance",
           "_upsert_instance", "_delete_instance", "_pick_port",
           "_resolve_target"}


def _ide_modules():
    for name in sorted(os.listdir(_IDE)):
        if name.endswith(".py"):
            yield name, os.path.join(_IDE, name)


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _called_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _unawaited_calls(tree):
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _called_name(n) in HELPERS:
            if id(n) not in awaited:
                out.append((_called_name(n), n.lineno))
    return out


def _defs_of_helpers(tree):
    return [(n.name, n.lineno, isinstance(n, ast.AsyncFunctionDef))
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in HELPERS]


# --- the guard --------------------------------------------------------------

def test_every_call_to_an_instance_store_helper_is_awaited():
    """The bug itself: a call that builds a coroutine and drops it."""
    offenders = []
    for name, path in _ide_modules():
        for helper, line in _unawaited_calls(_tree(path)):
            offenders.append("%s:%d calls %s() without await" % (name, line, helper))
    assert not offenders, "\n".join(offenders)


def test_every_definition_of_an_instance_store_helper_is_async():
    """A sync def of one of these names is a mirror waiting to drift again."""
    offenders = []
    for name, path in _ide_modules():
        for helper, line, is_async in _defs_of_helpers(_tree(path)):
            if not is_async:
                offenders.append("%s:%d def %s is sync" % (name, line, helper))
    assert not offenders, "\n".join(offenders)


def test_the_write_path_is_covered_and_not_merely_absent():
    """The silent half specifically - assert _save_instances is really called.

    Without this, the guard above passes vacuously if the write path is ever
    refactored out from under it, which is the failure mode that hid the
    original bug for nineteen days.
    """
    saves = 0
    for _name, path in _ide_modules():
        tree = _tree(path)
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _called_name(n) == "_save_instances":
                saves += 1
                assert id(n) in awaited
    assert saves >= 4, "expected the upsert/delete/register writes, found %d" % saves


def test_the_scan_actually_reads_the_modules_this_is_about():
    """Guard against the guard passing because it looked at nothing."""
    names = {n for n, _ in _ide_modules()}
    assert {"vscode_capabilities.py", "ide_remote_capabilities.py",
            "ide_claude_sessions_capabilities.py"} <= names
    assert len(names) >= 4


@pytest.mark.parametrize("module,helper", [
    ("vscode_capabilities.py", "_load_instances"),
    ("vscode_capabilities.py", "_save_instances"),
    ("ide_remote_capabilities.py", "_load_instances"),
    ("ide_remote_capabilities.py", "_save_instances"),
])
def test_the_helpers_that_drifted_are_defined_where_expected(module, helper):
    """Pins the pairing: if a mirror is deleted or moved, this test says so
    rather than silently covering one side only."""
    tree = _tree(os.path.join(_IDE, module))
    found = [d for d in _defs_of_helpers(tree) if d[0] == helper]
    assert found, "%s no longer defines %s" % (module, helper)
    assert all(is_async for _n, _l, is_async in found)
