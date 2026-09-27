"""A step that needs step N also reads whatever recovered step N (step_deps_core).

Before: `needs: [3]` gave a step step 3's FAILED attempt, never the recovery step
(new id) that finished step 3's work. Pure tests use the lowercase import with
the repo root on the path; the constructor tests import the app module and so
run in-container, where the merge gate runs.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import step_deps_core as D  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None


def _r(i, **kw):
    return dict({"id": i, "title": f"step {i}", "ok": True}, **kw)


def test_a_needed_step_brings_the_recovery_that_finished_it():
    bb = {1: _r(1), 2: _r(2, ok=False, met=False), 3: _r(3), 7: _r(7, _recovers=2)}
    got = D.dependency_results(bb, [2])
    assert [r["id"] for r in got] == [2, 7]


def test_every_recovery_in_the_line_is_included_in_the_order_they_ran():
    bb = {2: _r(2, ok=False), 7: _r(7, ok=False, _recovers=2), 8: _r(8, _recovers=2)}
    assert [r["id"] for r in D.dependency_results(bb, [2])] == [2, 7, 8]


def test_a_recovery_of_another_step_is_not_pulled_in():
    bb = {1: _r(1), 2: _r(2), 7: _r(7, _recovers=1)}
    assert [r["id"] for r in D.dependency_results(bb, [2])] == [2]


def test_without_recoveries_the_result_is_exactly_what_it_was():
    bb = {1: _r(1), 2: _r(2), 3: _r(3)}
    assert D.dependency_results(bb, [1, 3]) == [bb[1], bb[3]]


def test_no_needed_step_has_run_so_the_caller_keeps_its_fallback():
    assert D.dependency_results({1: _r(1)}, [5]) == []
    assert D.dependency_results({1: _r(1)}, []) == []


def test_a_duplicate_need_or_shared_recovery_is_listed_once():
    bb = {1: _r(1), 2: _r(2), 9: _r(9, _recovers=1)}
    got = D.dependency_results(bb, [1, 1, 2])
    assert [r["id"] for r in got] == [1, 9, 2]


def test_recovers_id_names_the_original_step_through_a_chain():
    assert D.recovers_id({"id": 3}) == 3
    assert D.recovers_id({"id": 8, "_recovers": 3}) == 3
    assert D.recovers_id(None) is None


def test_the_step_runner_reads_dependencies_through_the_helper():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ex = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v5_run_step_inner")
    assert any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "dependency_results"
               for n in ast.walk(ex)), "the step runner must pick its deps via dependency_results"


def test_a_recovery_result_is_stamped_with_the_step_it_recovers():
    """The link lives on the RESULT (what the blackboard holds), not only the step."""
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert 'res["_recovers"] = step["_recovers"]' in src


needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


@needs_app
def test_a_plain_recovery_step_names_the_step_it_recovers():
    rec = M._v6_make_recovery_step({"id": 3, "title": "t", "goal": "g", "caps": []},
                                   {"summary": "half done"}, 7)
    assert rec["id"] == 7 and rec["_recovers"] == 3


@needs_app
def test_a_recovery_of_a_recovery_still_names_the_original():
    first = M._v6_make_recovery_step({"id": 3, "title": "t", "goal": "g", "caps": []}, {}, 7)
    second = M._v6_make_recovery_step(first, {}, 8)
    assert second["_recovers"] == 3


@needs_app
def test_an_adjusted_recovery_step_names_the_step_it_recovers(monkeypatch):
    async def _stub(prompt, system=None, **kw):
        return '{"title":"retry","goal":"do it differently","caps":[],"success":"s"}'
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", _stub)
    import asyncio
    adj = asyncio.run(M._v6_adjust_step(
        {"id": 4, "title": "t", "goal": "g", "caps": [], "success": "s"}, {"summary": "x"},
        "goal", catalog_names=[], valid_skill_ids=set(), new_id=9,
        model="", instance_id="", prefer_gpu=True))
    assert adj.get("_adjusted") and adj["_recovers"] == 4
