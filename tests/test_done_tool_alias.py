"""A tool named `done` ends the step (plan item 24b).

run70-73 (24 Sep 2026): five executor turns called a tool literally named
`done`, were told "There is NO capability called 'done'", and spent the
following cycles on refused calls. Pure test of the alias set plus a source pin
that the executor turn honours it before tool-name resolution.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src():
    return open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()


def test_the_alias_set_is_the_words_a_finished_model_reaches_for():
    src = _src()
    i = src.index("_V5_DONE_TOOL_ALIASES = frozenset(")
    line = src[i:src.index("\n", i)]
    for w in ("done", "finish", "stop", "complete", "end"):
        assert f'"{w}"' in line
    assert '"exec' not in line and '"code' not in line


def test_the_executor_turn_takes_done_as_a_tool_name_before_resolving_it():
    src = _src()
    i = src.index("if tool.lower() in _V5_DONE_TOOL_ALIASES:")
    j = src.index("tool = _v5_resolve_tool_name(tool, allowed, catalog_set)", i)
    assert i < j                                            # handled before name resolution
    block = src[i:j]
    assert "result_summary = str(_dsum)[:_V5_DONE_SUMMARY]" in block
    assert "ok = bool(had_useful or outputs)" in block
    assert "break" in block
    ast.parse(src)
