"""Chat must not evict the runner it could have reused (vera/agents/chat_ctx_core.py).

The regression these guard, observed on gpu-250 (Tesla V100-PCIE-12GB) on
2026-09-20: `run`/`run_stream` asked ollama for `effective_num_ctx(...)` — the
node MAXIMUM — on every chat turn, while `ollama_generate` sizes its window to
the prompt. ollama keys a runner by (model, num_ctx), so the two callers evicted
each other's runner on a node whose VRAM fits exactly one. Chat never completed
a single generation for eight hours; repeated unsatisfiable reloads finally
deadlocked ollama's scheduler, and because /api/ps, /api/tags and /api/version
do not go through that scheduler every health probe still read "online, idle".

The load-bearing property is the FIRST test: a small turn, with a usable runner
already resident, must ask for exactly that runner's window.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.agents import chat_ctx_core as cc  # noqa: E402


# ── the thrash itself ────────────────────────────────────────────────────────

def test_small_turn_reuses_the_resident_runner_instead_of_asking_for_the_cap():
    """The bug, stated: a 6-token question must not request the node maximum."""
    got = cc.stable_chat_num_ctx(needed=900, cap=28672, resident=24576)
    assert got == 24576, "must adopt the loaded window, not the node cap"


def test_repeated_identical_turns_ask_for_the_same_window():
    """A window that drifts per-turn reloads the runner per-turn."""
    windows = {
        cc.stable_chat_num_ctx(needed=n, cap=28672, resident=0)
        for n in (620, 731, 802, 915, 1040)
    }
    assert len(windows) == 1, f"turn-to-turn drift forces a reload each time: {windows}"


def test_resident_window_is_not_adopted_when_it_cannot_hold_the_turn():
    """Reuse must never cost correctness — a short window gets rejected."""
    got = cc.stable_chat_num_ctx(needed=20000, cap=28672, resident=8192)
    assert got >= 20000
    assert got <= 28672


def test_resident_window_above_the_cap_is_not_adopted():
    """Another caller's larger runner must not raise our own ceiling."""
    got = cc.stable_chat_num_ctx(needed=900, cap=8192, resident=24576)
    assert got <= 8192


# ── clamping ─────────────────────────────────────────────────────────────────

def test_result_never_exceeds_the_cap():
    for needed in (1, 5_000, 50_000, 10 ** 7):
        got = cc.stable_chat_num_ctx(needed=needed, cap=16384, resident=0)
        assert 0 < got <= 16384, (needed, got)


def test_tiny_turn_still_gets_the_floor():
    assert cc.stable_chat_num_ctx(needed=1, cap=65536, resident=0) >= cc.CTX_FLOOR


def test_a_cap_below_the_floor_is_honoured_not_overridden():
    """A deliberately tiny agent window is the operator's choice."""
    assert cc.stable_chat_num_ctx(needed=10, cap=2048, resident=0) == 2048


def test_absent_cap_does_not_clamp_to_zero():
    """cap=0 means 'unknown', not 'no context at all'."""
    assert cc.stable_chat_num_ctx(needed=9000, cap=0, resident=0) >= 9000


def test_rounds_up_never_down():
    """Rounding down would silently truncate the prompt it was sized for."""
    for needed in (4097, 8193, 12289):
        got = cc.stable_chat_num_ctx(needed=needed, cap=65536, resident=0)
        assert got >= needed, (needed, got)
        assert got % cc.CTX_STEP == 0


def test_garbage_inputs_do_not_raise():
    for bad in (None, 0, -5):
        got = cc.stable_chat_num_ctx(needed=bad, cap=16384, resident=bad)
        assert got > 0


# ── reading the resident window out of /api/ps ───────────────────────────────

def test_resident_ctx_exact_tag():
    ps = {"models": [{"name": "jaahas/qwen3.5-uncensored:9b", "context_length": 24576}]}
    assert cc.resident_ctx_from_ps(ps, "jaahas/qwen3.5-uncensored:9b") == 24576


def test_resident_ctx_matches_another_tag_of_the_same_blob():
    """`:9b` and `:latest` were digest 155911794292... — one runner, two names."""
    ps = {"models": [{"name": "jaahas/qwen3.5-uncensored:9b", "context_length": 24576}]}
    assert cc.resident_ctx_from_ps(ps, "jaahas/qwen3.5-uncensored") == 24576
    assert cc.resident_ctx_from_ps(ps, "jaahas/qwen3.5-uncensored:latest") == 24576


def test_resident_ctx_prefers_the_exact_tag_over_a_sibling():
    ps = {"models": [
        {"name": "qwen3.5:2b", "context_length": 8192},
        {"name": "qwen3.5:9b", "context_length": 24576},
    ]}
    assert cc.resident_ctx_from_ps(ps, "qwen3.5:9b") == 24576


def test_resident_ctx_unrelated_model_is_not_adopted():
    ps = {"models": [{"name": "gemma3:27b", "context_length": 8192}]}
    assert cc.resident_ctx_from_ps(ps, "jaahas/qwen3.5-uncensored") == 0


def test_resident_ctx_tolerates_unusable_payloads():
    for bad in (None, {}, {"models": None}, {"models": []}, {"models": [None]},
                {"models": [{"name": "m"}]},
                {"models": [{"name": "m", "context_length": "not-a-number"}]},
                {"models": "nope"}):
        assert cc.resident_ctx_from_ps(bad, "m") == 0


# ── end-to-end: the observed numbers ─────────────────────────────────────────

def test_observed_gpu250_case_does_not_reload():
    """The exact shape seen on 2026-09-20.

    Runner resident at 24576 (ollama's own fit for the 12GB card); agent cap
    16384; a one-line question. Before the fix chat asked for 16384 and evicted
    the 24576 runner. 24576 is above this agent's cap, so it must NOT be
    adopted — but the request must still be stable turn to turn.
    """
    first = cc.stable_chat_num_ctx(needed=1100, cap=16384, resident=24576)
    second = cc.stable_chat_num_ctx(needed=1240, cap=16384, resident=first)
    assert first <= 16384
    assert second == first, "the second turn must reuse the runner the first loaded"
