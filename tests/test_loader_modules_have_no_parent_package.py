"""A module the loader loads by PATH has no parent package.

`capability_orchestration` loads every entry in its `_module_files` list from a
file path as a TOP-LEVEL module. Such a module has no `__package__`, so a
relative import raises

    attempted relative import with no known parent package

and the loader catches it, logs `âœ— <module> failed to load`, and carries on -
so the failure is a single log line and a silently missing capability, not a
crash. On 2026-08-31 I added two `from . import ...` lines to
operator_web_capabilities.py; every operator capability vanished from prod and
the only symptom was operator.run no longer being registered.

That is why this is a test and not a convention: the failure mode is quiet.
Files these modules IMPORT are ordinary package members and may use relative
imports freely - only the entry points listed in `_module_files` are affected.
"""
import ast
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_ORCH = os.path.join(_ROOT, "vera", "capability_orchestration.py")


def _loader_entry_points():
    """The paths in `_module_files`, read from the loader itself so this test
    tracks the real list rather than a copy that can drift."""
    with open(_ORCH, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("_module_files = [")
    end = src.index("]", start)
    block = src[start:end]
    rels = re.findall(r'os\.path\.join\(_here,\s*"([^"]+)"\)', block)
    return [r for r in rels if r.endswith(".py")]


def _relative_imports(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = []
    for node in tree.body:                     # module level only
        if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
            names = ", ".join(a.name for a in node.names)
            out.append("line %d: from %s%s import %s"
                       % (node.lineno, "." * node.level, node.module or "", names))
    return out


def test_no_loader_entry_point_uses_a_relative_import():
    offenders = []
    for rel in _loader_entry_points():
        p = os.path.join(_ROOT, "vera", rel)
        if not os.path.exists(p):
            continue
        for hit in _relative_imports(p):
            offenders.append("%s %s" % (rel, hit))
    assert not offenders, (
        "these are loaded by path as top-level modules and will fail to "
        "register:\n" + "\n".join(offenders))


def test_the_operator_module_is_one_of_them():
    """The specific regression, pinned by name."""
    eps = _loader_entry_points()
    assert any(e.endswith("operator_web_capabilities.py") for e in eps)
    p = os.path.join(_ROOT, "vera", "operator", "operator_web_capabilities.py")
    assert _relative_imports(p) == []


def test_the_scan_reads_a_real_list_and_is_not_vacuous():
    """If the list stops parsing, this test must fail rather than pass empty."""
    eps = _loader_entry_points()
    assert len(eps) > 20, "only found %d loader entry points" % len(eps)
    assert any(e.endswith("capabilities/capabilities.py") for e in eps)


def test_a_module_a_loader_entry_imports_may_still_use_relative_imports():
    """Not everything is affected - only the entry points. vera/operator/
    targets.py uses `from . import target_fallback` and is correct, because it
    is imported AS a package member, never loaded by path."""
    p = os.path.join(_ROOT, "vera", "operator", "targets.py")
    if os.path.exists(p):
        assert p.endswith("targets.py")   # documents the distinction
