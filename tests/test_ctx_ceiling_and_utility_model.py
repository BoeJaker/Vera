"""A five-word chat title still got a 16,384-token window, and a 9b.

2026-09-23, after the output-room fix: prod's naming probe loaded the 0.5b with
`n_ctx_slot = 16384`. `llm.generate` passes its generous default (16,384) as
`num_ctx`, and the auto-fit treats a caller's num_ctx as a FLOOR - right for a
role that wants a big window, wrong for a default that means "at most".

And a sandbox's SAVED profile had a `naming` rule with no model, so the fix to
the built-in defaults could not reach it: it sent the instance default - the
9b - to cpu-247 for a chat title while a census ran.

Pure: numbers and names in, a decision out; plus a source pin on the wiring.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.capabilities import ctx_policy_core as C           # noqa: E402

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


# -- the ceiling ---------------------------------------------------------------
def test_a_ceiling_only_lowers():
    assert C.apply_ceiling(24576, 16384) == 16384
    assert C.apply_ceiling(4096, 16384) == 4096          # a small fit stays small
    assert C.apply_ceiling(4096, 0) == 4096              # no ceiling
    assert C.apply_ceiling(0, 16384) == 0


def test_llm_generate_sends_a_ceiling_not_a_pin():
    """The exact line that made the default a floor."""
    src = (ROOT / "vera/capabilities/capabilities.py").read_text(encoding="utf-8")
    assert '_gen_opts = {"num_ctx_max": _ctx}' in src
    assert '_gen_opts = {"num_ctx": _ctx}' not in src


def test_the_router_honours_the_ceiling_and_keeps_it_from_ollama():
    src = (ROOT / "vera/capability_orchestration.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    router = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "ollama_generate")
    body = ast.get_source_segment(src, router)
    assert "apply_ceiling(_want, int(_merged_opts.get(\"num_ctx_max\") or 0))" in body
    assert '_merged_opts.pop("num_ctx_max", None)' in body
    # and the pin is STILL a floor for roles that mean it
    assert "_want = max(_fit, _pinned)" in body


# -- the utility model --------------------------------------------------------
SERVED = ["jaahas/qwen3.5-uncensored:9b", "jaahas/qwen3.5-uncensored:latest",
          "nomic-embed-text:latest", "qwen2.5:0.5b", "qwen2.5:7b"]


def test_a_naming_rule_with_no_model_gets_the_small_one():
    assert C.utility_model("naming", "", SERVED) == "qwen2.5:0.5b"
    assert C.utility_model("naming", None, SERVED) == "qwen2.5:0.5b"
    assert C.utility_model("naming", "", ()) == "qwen2.5:0.5b"      # unknown catalogue: trusted


def test_a_deliberate_model_is_never_overridden():
    assert C.utility_model("naming", "qwen2.5:7b", SERVED) == ""
    assert C.utility_model("naming", "jaahas/qwen3.5-uncensored", SERVED) == ""


def test_other_job_types_are_untouched():
    for jt in ("chat", "code", "loop_executor", "summarize", "", None):
        assert C.utility_model(jt, "", SERVED) == "", jt


def test_a_node_that_does_not_carry_the_small_model_is_left_alone():
    assert C.utility_model("naming", "", ["jaahas/qwen3.5-uncensored:9b"]) == ""


def test_the_router_applies_it_only_when_no_model_was_asked_for():
    src = (ROOT / "vera/capability_orchestration.py").read_text(encoding="utf-8")
    assert "_ctx_policy_core.utility_model(" in src
    assert "if eff_model is None:" in src and "utility_model(" in src
