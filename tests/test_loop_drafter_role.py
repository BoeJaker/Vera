"""First drafts on the executor's model, fixes on the coder (owner, 12 Oct 2026: "GPU 9B drafts,
30b fixes"). Imports the monolith module, so it runs in-container (see test_loop_cancel.py)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402
from vera import role_profile_merge as M             # noqa: E402

pytestmark = pytest.mark.critical


def test_code_gen_call_without_a_fix_is_a_draft():
    args = {"prompt": "Write a Python script that parses data.csv", "output_format": "code",
            "model": "pinned:run"}
    assert W._v5_apply_code_routing("llm.generate", args)
    assert args["role"] == "drafter" and "model" not in args


def test_code_gen_call_that_fixes_goes_to_the_coder():
    args = {"prompt": "Fix the bug in parser.py so the script handles empty rows", "output_format": "code"}
    assert W._v5_apply_code_routing("llm.generate", args)
    assert args["role"] == "coder"


def test_drafter_follows_the_executor_model():
    roles = {"executor": {"model": "jaahas/qwen3.5-uncensored:9b", "pin": "gpu-250"},
             "drafter": {"job_type": "loop_drafter", "prefer_gpu": True, "model": "@executor"},
             "coder": {"model": "qwen3-coder:30b", "pin": "cpu-246"}}
    out = M.follow_model_refs(roles)
    assert out["drafter"]["model"] == "jaahas/qwen3.5-uncensored:9b"
    assert out["drafter"]["prefer_gpu"] is True and out["coder"]["model"] == "qwen3-coder:30b"


def test_author_first_pass_uses_the_drafter():
    src = open(W.__file__, encoding="utf-8").read()
    i = src.index('caller="code.author"')
    assert 'role="drafter"' in src[i - 900:i]
