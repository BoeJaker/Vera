"""The executor prompt's call site must pass every block, unswapped â€” Phase 4.

`_v5_compose_executor_system` takes 24 keyword-only parameters, 22 of them
plain strings. Two failures are invisible at runtime: dropping a keyword (the
block silently vanishes from every executor prompt) and passing the wrong local
under a keyword (two blocks swap and the prompt still looks plausible). Neither
raises; both change what every step executor is told.

Parses the source by PATH rather than importing it, so it is pure and
deterministic â€” no app import, no `Vera.vera.X` namespace resolution â€” which is
what lets it sit in the critical gate alongside the other loop guards. The
byte-level golden lives in `test_executor_prompt_compose.py`.
"""
import ast
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "vera" / "dag" / "dag_workshop_capabilities.py")

COMPOSER = "_v5_compose_executor_system"
EXECUTOR = "_v5_run_step_inner"


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _find_def(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_to(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]


def test_composer_is_defined_at_module_level():
    """A nested def would put it out of reach of the golden test and reuse."""
    tree = _tree()
    assert any(isinstance(n, ast.FunctionDef) and n.name == COMPOSER
               for n in tree.body), "%s must be a module-level function" % COMPOSER


def test_call_site_passes_every_parameter_by_name():
    tree = _tree()
    fn = _find_def(tree, COMPOSER)
    executor = _find_def(tree, EXECUTOR)
    assert fn is not None and executor is not None

    declared = [a.arg for a in fn.args.kwonlyargs]
    assert declared, "composer must take keyword-only params"
    assert not fn.args.args, "composer must take no positional params"

    calls = _calls_to(executor, COMPOSER)
    assert len(calls) == 1, "expected exactly one call site, found %d" % len(calls)
    call = calls[0]

    assert not call.args, "call site must not pass positional args"
    passed = [kw.arg for kw in call.keywords]
    assert None not in passed, "call site must not use **kwargs â€” it hides drops"
    assert sorted(passed) == sorted(declared), (
        "call site/param mismatch â€” missing=%s unexpected=%s"
        % (sorted(set(declared) - set(passed)), sorted(set(passed) - set(declared))))


def test_no_block_is_passed_under_another_blocks_name():
    """Every keyword must be a bare pass-through of the identically-named local.

    `author_note=prose_note` type-checks, runs, and silently corrupts the prompt.
    Requiring `name=name` makes such a swap a test failure instead.
    """
    tree = _tree()
    call = _calls_to(_find_def(tree, EXECUTOR), COMPOSER)[0]
    swapped = [(kw.arg, ast.dump(kw.value)) for kw in call.keywords
               if not (isinstance(kw.value, ast.Name) and kw.value.id == kw.arg)]
    assert swapped == [], "keywords not passed through under their own name: %s" % [
        s[0] for s in swapped]


def test_composer_result_is_assigned_to_the_prompt_variable():
    """Guards against the call becoming dead code while a stale `sys` survives."""
    tree = _tree()
    executor = _find_def(tree, EXECUTOR)
    assigned = [
        n for n in ast.walk(executor)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "sys" for t in n.targets)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) == COMPOSER
    ]
    assert len(assigned) == 1, "the composed prompt must be assigned to `sys` once"


def test_executor_no_longer_inlines_the_prompt():
    """Phase 4's point: the prompt body left the 3,400-line function."""
    tree = _tree()
    executor = _find_def(tree, EXECUTOR)
    composer = _find_def(tree, COMPOSER)
    exec_lines = executor.end_lineno - executor.lineno
    comp_lines = composer.end_lineno - composer.lineno
    assert comp_lines > 80, "composer looks too small to hold the prompt (%d lines)" % comp_lines
    # The executor must not regrow its own copy of the opening line.
    body = "\n".join(SRC.read_text(encoding="utf-8").splitlines()
                     [executor.lineno - 1:executor.end_lineno])
    assert "You are a FOCUSED SPECIALIST sub-agent" not in body, (
        "executor re-inlined the system prompt (%d lines)" % exec_lines)
