"""USER role-profile overrides must not silently discard declared tuning.

Pins the 2026-08-24 prod incident: overriding only the MODEL on `loop/coder` also
dropped its declared `{"temperature": 0.7, "top_p": 0.9}` (7e0e974), and the
sibling loop roles lost `num_ctx: 16384` — because a user role rule replaces the
declared one wholesale. Two landed tuning fixes were inert on prod as a result.

Pure (no I/O, no app import), so it runs in the critical gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.role_profile_merge import inherit_declared_fields as merge  # noqa: E402


DECLARED_CODER = {
    "pattern": "loop/coder", "label": "loop · coder", "declared_by": "loop",
    "role": "coder", "job_type": "loop_coder", "prefer_gpu": True,
    "deny_gpu": False, "pin": "", "model": "qwen2.5-coder:14b",
    "options": {"temperature": 0.7, "top_p": 0.9},
    "escalate_chars": 0, "escalate": {},
}


# ── the incident ─────────────────────────────────────────────────────────────
def test_model_only_override_keeps_declared_options():
    """The exact shape the routing panel's 'add role' path sends: no options."""
    supplied = {"job_type": "", "prefer_gpu": True, "deny_gpu": False,
                "pin": "gpu-250", "model": "jaahas/qwen3.5-uncensored:9b",
                "escalate_chars": 0}
    out = merge(DECLARED_CODER, supplied)
    assert out["model"] == "jaahas/qwen3.5-uncensored:9b"   # caller's choice wins
    assert out["pin"] == "gpu-250"
    assert out["options"] == {"temperature": 0.7, "top_p": 0.9}   # NOT discarded


def test_num_ctx_survives_a_partial_override():
    declared = {"model": "m", "options": {"temperature": 0.2, "num_ctx": 16384}}
    out = merge(declared, {"prefer_gpu": True})
    assert out["options"]["num_ctx"] == 16384


# ── explicit values always win, including falsy ones ─────────────────────────
def test_explicitly_sent_false_is_honoured():
    """An operator turning prefer_gpu OFF must not have True inherited back."""
    out = merge(DECLARED_CODER, {"prefer_gpu": False})
    assert out["prefer_gpu"] is False


def test_explicitly_sent_empty_options_is_honoured():
    """Sending options explicitly, even empty, is a deliberate clear."""
    out = merge(DECLARED_CODER, {"options": {}})
    assert out["options"] == {}


def test_explicit_empty_model_is_honoured():
    out = merge(DECLARED_CODER, {"model": ""})
    assert out["model"] == ""


# ── identity fields are never inherited ──────────────────────────────────────
def test_identity_fields_are_not_inherited():
    out = merge(DECLARED_CODER, {"model": "x"})
    for k in ("pattern", "label", "declared_by", "role"):
        assert k not in out


# ── boundaries ───────────────────────────────────────────────────────────────
def test_no_declared_rule_passes_supplied_through():
    supplied = {"model": "x"}
    assert merge({}, supplied) == supplied
    assert merge(None, supplied) == supplied


def test_inputs_are_not_mutated():
    declared = dict(DECLARED_CODER)
    supplied = {"model": "x"}
    merge(declared, supplied)
    assert supplied == {"model": "x"}
    assert declared == DECLARED_CODER


def test_empty_supplied_inherits_the_whole_declared_rule():
    out = merge(DECLARED_CODER, {})
    assert out["model"] == "qwen2.5-coder:14b"
    assert out["options"] == {"temperature": 0.7, "top_p": 0.9}
    assert out["job_type"] == "loop_coder"
