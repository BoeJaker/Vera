"""The loop's generations carry a default output bound (plan item 16).

Census run72 (2026-09-24), research-web: one controller call generated the
whole 16,384-token window in 1,055 s. Pure: the bound core, and a
source-level check that the loop's shared generate wrapper applies it.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag.loop_output_bound_core import DEFAULT_LOOP_NUM_PREDICT, bound_options  # noqa: E402


def test_the_default_is_above_every_legitimate_output_seen():
    # largest loop output in ~1,330 calls before the fix was 3,407 (executor);
    # writer p95 1,979. The bound must not touch any of them.
    assert DEFAULT_LOOP_NUM_PREDICT == 4096


def test_an_unbounded_call_gets_the_default():
    assert bound_options(None) == {"num_predict": 4096}
    assert bound_options({}) == {"num_predict": 4096}
    assert bound_options({"seed": 7}) == {"seed": 7, "num_predict": 4096}


def test_a_callers_own_bound_wins_smaller_or_larger():
    assert bound_options({"num_predict": 512})["num_predict"] == 512        # the thinker's
    assert bound_options({"num_predict": 12000})["num_predict"] == 12000    # a deliberate long one


def test_zero_or_junk_pins_are_not_pins():
    assert bound_options({"num_predict": 0})["num_predict"] == 4096
    assert bound_options({"num_predict": "x"})["num_predict"] == 4096


def test_the_bound_can_be_switched_off():
    assert "num_predict" not in bound_options({"seed": 1}, default=0)
    assert "num_predict" not in bound_options({"num_predict": 0}, default=0)


def test_the_argument_is_not_mutated():
    src = {"seed": 1}
    bound_options(src)
    assert src == {"seed": 1}


def test_the_loop_wrapper_applies_the_bound():
    src_path = os.path.join(os.path.dirname(__file__), "..", "vera", "dag", "dag_workshop_capabilities.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_safe_ollama_generate_dw")
    called = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in ast.walk(fn)
              if isinstance(c, ast.Call)}
    assert "_bound_loop_options" in called, sorted(called)
