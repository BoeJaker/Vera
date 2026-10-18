"""A role's model may name another role of the same profile ("@executor") - owner 12 Oct 2026: the loop's
coder uses the executor's model, so changing one changes both."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera import role_profile_merge as M  # noqa: E402

ORCH = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_ref_resolves_to_the_target_model_and_is_kept_visible():
    roles = {"executor": {"model": "jaahas/qwen3.5-uncensored:9b", "pin": "gpu-250"},
             "coder": {"model": "@executor", "pin": "gpu-250", "options": {"temperature": 0.7}}}
    out = M.follow_model_refs(roles)
    assert out["coder"]["model"] == "jaahas/qwen3.5-uncensored:9b" and out["coder"]["model_ref"] == "@executor"
    assert out["coder"]["options"] == {"temperature": 0.7} and out["executor"] == roles["executor"]
    roles["executor"]["model"] = "qwen3:14b"                      # change ONE place
    assert M.follow_model_refs(roles)["coder"]["model"] == "qwen3:14b"


def test_one_hop_no_loops_missing_target_falls_back():
    assert M.follow_model_ref({"model": "@a"}, {"a": {"model": "@b"}}.get)["model"] == ""
    assert M.follow_model_ref({"model": "@nope"}, {}.get)["model"] == ""
    assert M.follow_model_ref({"model": "qwen2.5:7b"}, {}.get) == {"model": "qwen2.5:7b"}
    assert M.model_ref({"model": "@"}) == "" and M.model_ref({}) == ""


def test_wired_into_both_resolvers():
    assert 'merged["roles"] = _follow_model_refs(merged["roles"])' in ORCH
    assert "return _follow_model_ref(rule, lambda r: _resolve_role_rule_raw(profile, r))" in ORCH
