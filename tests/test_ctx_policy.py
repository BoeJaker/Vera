"""Output must fit the window it is generated into.

Pins vera/capabilities/ctx_policy_core.py.

The defect (capabilities.py:1986): `num_predict = num_ctx`. The prompt already
occupies part of the window, so that asks for more output than can physically
fit. Measured on prod 2026-09-16: loop_executor sends ~43,254 prompt chars
(11-13k tokens) into a 28,672 window and is told it may emit 28,672 tokens.

Overflow does not truncate. Every ollama runner launches with
`--context-shift --keep 4`, so a generation that outruns its window keeps going
with its instructions evicted — at ~0.05 tok/s on a CPU node that is days of
work nobody is waiting for, which is what was found burning twelve cores on
cpu-247.

The `fit` bound cannot truncate valid output: anything past it could only have
been produced by evicting the prompt.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.capabilities.ctx_policy_core import (  # noqa: E402
    CHARS_PER_TOKEN, DEFAULT_MARGIN, FIT, LONGFORM, MIN_PREDICT, CtxPlan,
    did_shift, estimate_tokens, keep_tokens, measured_chars_per_token,
    output_bound, resolve, safe_chars_per_token, window_ceiling,
)


# ── window ceiling: CPU RAM is not VRAM ──────────────────────────────────────

def test_a_cpu_node_keeps_the_models_full_window():
    """These CPU nodes have 50 GB of system RAM and no spill cliff — the KV
    cache just lives in RAM. A big prompt that would have to shift on the 12 GB
    card runs UNSHIFTED here, which is the point of routing it to CPU."""
    assert window_ceiling(has_gpu=False, vram_gb=0, detected_max=262144,
                          gpu_ceiling=28672) == 262144


def test_a_gpu_node_is_held_to_the_ceiling():
    """12 GB of VRAM holds ~28k tokens of KV for a 9B model; the model's own
    window is 262,144."""
    assert window_ceiling(has_gpu=True, vram_gb=12.0, detected_max=262144,
                          gpu_ceiling=28672) == 28672


def test_unknown_vram_on_a_gpu_means_the_ceiling_not_no_ceiling():
    """THE BUG (2026-09-16). gpu-250 reports vram_gb=None from the catalog, and
    the old code treated that as the CPU case — offering a 12.3 GB card the
    model's full 262,144 window, where the KV cache alone would be 32 GB."""
    assert window_ceiling(has_gpu=True, vram_gb=0, detected_max=262144,
                          gpu_ceiling=28672) == 28672
    assert window_ceiling(has_gpu=True, vram_gb=None or 0, detected_max=262144,
                          gpu_ceiling=65536) == 65536


def test_the_ceiling_never_inflates_a_small_model_window():
    """A model whose own window is smaller than the ceiling keeps its own."""
    assert window_ceiling(has_gpu=True, vram_gb=12.0, detected_max=8192,
                          gpu_ceiling=28672) == 8192
    assert window_ceiling(has_gpu=False, vram_gb=0, detected_max=8192,
                          gpu_ceiling=28672) == 8192

CTX = 28672          # OLLAMA_MAX_AUTO_CTX on prod


# ── shift detection: the only detector available ─────────────────────────────

def test_shift_detector_matches_the_validated_measurements():
    """Measured live 2026-09-16. llama.cpp logs the shift nowhere we can reach —
    a deliberately forced shift logged nothing on all three nodes — but both
    counts come back on every response."""
    assert did_shift(prompt_tokens=23, eval_count=700, num_ctx=320) is True
    assert did_shift(prompt_tokens=23, eval_count=200, num_ctx=4096) is False


def test_shift_detector_is_quiet_without_data():
    assert did_shift(0, 700, 320) is False          # no prompt count
    assert did_shift(23, 700, 0) is False           # no window known


def test_exactly_filling_the_window_is_not_a_shift():
    assert did_shift(prompt_tokens=100, eval_count=200, num_ctx=300) is False
    assert did_shift(prompt_tokens=100, eval_count=201, num_ctx=300) is True


# ── chars/token: the measurement that stops the overrun ──────────────────────

def test_measured_ratio_reproduces_the_live_numbers():
    """prompt_eval_count on qwen3.5-uncensored:9b, 2026-09-16."""
    assert round(measured_chars_per_token(2700, 610), 2) == 4.43   # prose
    assert round(measured_chars_per_token(2180, 730), 2) == 2.99   # json
    assert round(measured_chars_per_token(2490, 1059), 2) == 2.35  # python
    assert measured_chars_per_token(0, 10) is None


def test_an_unmeasured_route_gets_the_conservative_default():
    """Assume dense content until proven otherwise: under-counting overruns."""
    assert safe_chars_per_token(None) == CHARS_PER_TOKEN
    assert safe_chars_per_token(0) == CHARS_PER_TOKEN


def test_a_measured_route_is_trusted_discounted_not_clamped_to_the_default():
    """Clamping down to the default would make measuring pointless — a prose
    route (4.43) would be charged the dense-code rate and lose most of its
    window. The discount covers per-call variance around the EMA."""
    assert safe_chars_per_token(4.43) == pytest.approx(4.43 * 0.9)
    assert safe_chars_per_token(4.43) > CHARS_PER_TOKEN, "measurement must win"
    assert safe_chars_per_token(2.35) < 2.35, "but always discounted"


def test_the_discount_errs_towards_over_counting_tokens():
    """Over-counting wastes a little window; under-counting shifts."""
    for measured in (2.35, 2.99, 3.14, 4.43):
        assert safe_chars_per_token(measured) < measured


def test_a_pathological_ratio_cannot_shrink_the_window_to_nothing():
    assert safe_chars_per_token(0.1) == 1.2                   # floored


def test_the_old_constant_would_have_under_counted_code():
    """The regression in one line: 3.4 against a real 2.35."""
    real = measured_chars_per_token(2490, 1059)
    old_est = 2490 / 3.4
    assert old_est < 1059, "3.4 under-counts python source"
    assert 2490 / safe_chars_per_token(real) >= 1059


# ── the output bound ─────────────────────────────────────────────────────────

def test_output_bound_reserves_the_margin_the_old_code_lacked():
    assert output_bound(num_ctx=28672, prompt_tokens=12000, ceiling=99999,
                        margin=256) == 28672 - 12000 - 256


def test_output_bound_respects_the_device_ceiling():
    """CPU nodes take a smaller ceiling: 16,384 tokens is ~16 min on the GPU and
    ~91 HOURS at a CPU node's measured 0.05 tok/s."""
    assert output_bound(num_ctx=28672, prompt_tokens=1000, ceiling=3072) == 3072
    assert output_bound(num_ctx=28672, prompt_tokens=1000, ceiling=16384) == 16384


def test_output_bound_never_returns_a_useless_stub():
    assert output_bound(num_ctx=1024, prompt_tokens=2000, ceiling=16384) == 512


# ── num_keep ─────────────────────────────────────────────────────────────────

def test_keep_is_a_pure_function_of_the_window():
    """MEASURED 2026-09-16: num_keep is baked in when the runner is first
    spawned for a (model, num_ctx) and silently IGNORED thereafter — sending
    8, then 512, then 8 at the same model+ctx produced one runner at keep=8.

    So it must not depend on the current call's system prompt, or the value
    would be decided by whichever request happened to spawn the runner."""
    assert keep_tokens(24576) == 3072          # ctx // 8
    assert keep_tokens(24576) == keep_tokens(24576), "must be deterministic"
    assert keep_tokens(0) is None


def test_keep_is_capped_so_it_cannot_eat_the_window():
    assert keep_tokens(262144) == 4096         # the cap, not ctx // 8
    assert keep_tokens(16) <= 8                # never more than half


def test_keep_comfortably_exceeds_a_real_system_prompt():
    """Vera's system prompts run to a few hundred tokens; keep must cover them
    at the windows actually in use."""
    for ctx in (8192, 16384, 24576, 28672):
        assert keep_tokens(ctx) >= 1024


# ── the defect, in its own numbers ───────────────────────────────────────────

def test_output_is_bounded_to_what_actually_fits():
    """loop_executor's real shape: ~12k prompt tokens in a 28,672 window."""
    p = resolve(num_ctx=CTX, prompt_tokens=12000)
    assert p.num_predict == CTX - 12000 - DEFAULT_MARGIN   # 16,416
    assert p.num_predict < CTX, "must never permit the whole window as output"


def test_prompt_plus_output_never_exceeds_the_window():
    """The invariant. Violating it is what triggers a context shift."""
    for ptoks in (0, 100, 5000, 12000, 20000, 27000):
        p = resolve(num_ctx=CTX, prompt_tokens=ptoks)
        assert ptoks + p.num_predict <= CTX, (ptoks, p.num_predict)


def test_the_old_behaviour_would_have_overflowed():
    """Guards the regression directly: num_predict = num_ctx overflows by the
    entire length of the prompt."""
    ptoks = 12000
    old = CTX                      # what capabilities.py:1986 set
    assert ptoks + old > CTX       # ...which cannot fit
    new = resolve(num_ctx=CTX, prompt_tokens=ptoks).num_predict
    assert ptoks + new <= CTX


# ── it must not restrict valid work ──────────────────────────────────────────

def test_a_short_prompt_still_gets_a_huge_budget():
    """The point of not lowering num_ctx: a small prompt keeps almost all of it."""
    p = resolve(num_ctx=CTX, prompt_tokens=500)
    assert p.num_predict == CTX - 500 - DEFAULT_MARGIN     # 27,916
    assert p.num_predict > 27000


def test_typical_generations_are_nowhere_near_the_bound():
    """Measured averages: chat 244 tok, loop_executor 380, loop_coder 2135.
    The bound must be slack for all of them, or it is a restriction."""
    p = resolve(num_ctx=CTX, prompt_tokens=12000)
    for observed in (244, 380, 747, 2135):
        assert observed < p.num_predict


def test_a_caller_may_ask_for_less_but_never_for_more():
    assert resolve(num_ctx=CTX, prompt_tokens=1000, want_predict=500).num_predict == 500
    # asking for more than fits is capped to what fits
    big = resolve(num_ctx=CTX, prompt_tokens=20000, want_predict=99999)
    assert big.num_predict == CTX - 20000 - DEFAULT_MARGIN


# ── degenerate windows ───────────────────────────────────────────────────────

def test_a_prompt_that_fills_the_window_hits_the_floor_and_says_why():
    p = resolve(num_ctx=4096, prompt_tokens=4000)
    assert p.num_predict == MIN_PREDICT
    assert "raise num_ctx or shorten the prompt" in p.reason


def test_unknown_window_leaves_the_caller_alone():
    """Better to change nothing than to invent a bound from a window we do not
    know — that is how a silent truncation gets shipped."""
    p = resolve(num_ctx=0, prompt_tokens=500, want_predict=1234)
    assert p.num_predict == 1234 and p.num_ctx == 0


# ── num_keep: the prompt's survival across a shift ───────────────────────────

def test_num_keep_is_set_from_the_system_prefix():
    """Runners default to --keep 4, which discards the instructions on the first
    shift. Anything we know is strictly better."""
    p = resolve(num_ctx=CTX, prompt_tokens=12000, system_tokens=900)
    assert p.num_keep == 900


def test_num_keep_never_swallows_the_window():
    p = resolve(num_ctx=1024, prompt_tokens=100, system_tokens=9000)
    assert p.num_keep <= 512


def test_no_system_prefix_means_no_opinion():
    assert resolve(num_ctx=CTX, prompt_tokens=100).num_keep is None


# ── longform: shifting used on purpose ───────────────────────────────────────

def test_longform_bounds_by_intent_not_by_the_window():
    """Shifting is the supported mechanism for output longer than the window
    ('context shift on infinite text generation'). In this mode the bound is
    what the caller actually wants, and the instructions are kept."""
    p = resolve(num_ctx=4096, prompt_tokens=500, mode=LONGFORM,
                want_predict=20000, system_tokens=200)
    assert p.num_predict == 20000        # deliberately exceeds num_ctx
    assert p.num_keep == 200
    assert p.mode == LONGFORM


def test_longform_is_opt_in_and_fit_is_what_you_get_by_default():
    """The keep-efficacy question is unsettled (see PLAN), so shifting is never
    the default — `fit` prevents the overrun rather than relying on num_keep to
    make one safe."""
    assert resolve(num_ctx=CTX, prompt_tokens=1000).mode == FIT
    assert resolve(num_ctx=CTX, prompt_tokens=1000, mode="").mode == FIT
    assert resolve(num_ctx=CTX, prompt_tokens=1000,
                   mode="nonsense-value").mode != LONGFORM


def test_longform_still_terminates_without_a_stated_intent():
    """Even asked for 'as much as you like', it must not be unbounded — ollama's
    own default of -1 is what let a runner aim at days of output."""
    p = resolve(num_ctx=4096, prompt_tokens=100, mode=LONGFORM)
    assert p.num_predict == 4096


# ── helpers ──────────────────────────────────────────────────────────────────

def test_token_estimate_is_conservative():
    """Over-counting tokens shrinks num_predict, which is the safe direction."""
    text = "x" * 4000
    assert estimate_tokens(text) >= 4000 / 4.0
    assert estimate_tokens("") == 0


def test_options_carries_only_ollama_keys():
    o = resolve(num_ctx=CTX, prompt_tokens=100, system_tokens=50).options()
    assert set(o) == {"num_ctx", "num_predict", "num_keep"}
    assert set(resolve(num_ctx=CTX, prompt_tokens=100).options()) == {
        "num_ctx", "num_predict"}
