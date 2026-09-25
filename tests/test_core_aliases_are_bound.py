"""Every `import X as _alias` the loop module relies on is bound on the normal path.

25 Sep 2026, op-run4: a merge had folded `verify_evidence_core as _verify_evidence`
into another import's except-branch (a try with two except clauses parses fine),
so the alias was bound only when steer_core failed to import - never - and every
step verify died with `NameError: _verify_evidence`. Four goals hit the wall cap
with one step executed each. No test imported the module and called the verify,
so nothing caught it. Two checks here: a pure AST one (an alias imported in an
except handler must also be imported in a try body somewhere), and, where the app
module imports, the module itself has every alias bound.
"""
import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.join(os.path.dirname(__file__), "..")
MODULES = [
    os.path.join("vera", "dag", "dag_workshop_capabilities.py"),
]
ALIAS_RE = re.compile(r"^\s*from\s+(?:Vera\.)?vera\.[\w.]+\s+import\s+\w+\s+as\s+(_\w+)\s*$", re.M)


def _aliases_in(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.asname and a.asname.startswith("_"):
                    out.add(a.asname)
    return out


def _handler_only_aliases(src: str):
    """Aliases imported inside an except handler that no try BODY (or plain
    module-level import) also binds."""
    tree = ast.parse(src)
    bound_normally, in_handlers = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Try):
            for stmt in n.body:
                bound_normally |= _aliases_in(stmt)
            for h in n.handlers:
                for stmt in h.body:
                    in_handlers |= _aliases_in(stmt)
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            bound_normally |= _aliases_in(stmt)
    return sorted(in_handlers - bound_normally)


@pytest.mark.parametrize("rel", MODULES)
def test_no_alias_is_bound_only_in_an_except_handler(rel):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    offenders = _handler_only_aliases(src)
    assert not offenders, (
        f"{rel}: these aliases are imported only inside an except handler, so on the "
        f"normal path they are never bound: {offenders}")


def test_the_scan_catches_the_op_run4_shape():
    bad = (
        "try:\n    from Vera.vera.dag import steer_core as _steer_core\n"
        "except ImportError:\n    from vera.dag import steer_core as _steer_core\n"
        "    from Vera.vera.dag import verify_evidence_core as _verify_evidence\n"
        "except ImportError:\n    from vera.dag import verify_evidence_core as _verify_evidence\n"
    )
    assert _handler_only_aliases(bad) == ["_verify_evidence"]
    good = (
        "try:\n    from Vera.vera.dag import verify_evidence_core as _verify_evidence\n"
        "except ImportError:\n    from vera.dag import verify_evidence_core as _verify_evidence\n"
    )
    assert _handler_only_aliases(good) == []


def test_the_loop_module_binds_every_alias_it_imports():
    try:
        from Vera.vera.dag import dag_workshop_capabilities as M
    except Exception:                                    # pragma: no cover
        pytest.skip("app module not importable here")
    here = os.path.realpath(ROOT)
    if not os.path.realpath(getattr(M, "__file__", "") or "").startswith(here):
        pytest.skip("Vera.vera.* resolves to another checkout here (host venv); the gate container binds it to this worktree")
    src = open(os.path.join(ROOT, MODULES[0]), encoding="utf-8").read()
    aliases = sorted(set(ALIAS_RE.findall(src)))
    assert aliases, "no aliases found - the scan regex is broken"
    missing = [a for a in aliases if not hasattr(M, a)]
    assert not missing, f"the loop module never binds: {missing}"
