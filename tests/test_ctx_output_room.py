"""A five-word chat title got a 24,576-token window.

2026-09-23: prod's `naming` rule was switched to qwen2.5:0.5b and a 237-char
title prompt still took 54 s, all of it node-side - the node's journal shows the
0.5b loaded with `n_ctx_slot = 24576` and an 8 GB prompt cache, for eight output
tokens. The auto-fit reserved the flat global output maximum (16,384) on every
call, so any prompt at all rounded up to 24,576; the node's own output ceiling
(3,072 on a CPU box) and the caller's pinned num_predict were only applied to
num_predict afterwards, once the window was already sized for a report.

Pure: numbers in, a number out.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.capabilities import ctx_policy_core as C           # noqa: E402

pytestmark = pytest.mark.critical

GLOBAL_MAX = 16384      # _OUTPUT_MAX_TOKENS
CPU_CEIL = 3072         # _OUTPUT_MAX_TOKENS_CPU
RESERVE = 1024          # _CTX_RESERVE_OUT


def _round_ctx(n):
    for step in (4096, 8192, 16384, 24576, 32768):
        if n <= step:
            return step
    return n


def test_a_title_on_a_cpu_node_gets_a_small_window():
    """The census case: 237 chars (~100 tokens), num_predict pinned small by the
    output budget, on a CPU node."""
    room = C.output_room(global_max=GLOBAL_MAX, node_ceiling=CPU_CEIL, want_predict=24,
                         reserve=RESERVE)
    assert room == RESERVE                        # 24 + margin is under the floor
    assert _round_ctx(100 + room) == 4096         # not 24576


def test_the_old_rule_is_what_produced_24576():
    """Pinned to the failure so nobody reintroduces the flat reservation."""
    assert _round_ctx(100 + GLOBAL_MAX) == 24576


def test_the_node_ceiling_bounds_an_unstated_intent():
    """A call that states no num_predict on a CPU node cannot produce more than
    the node's ceiling, so the window need not hold more."""
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=CPU_CEIL, reserve=RESERVE) == CPU_CEIL
    assert _round_ctx(100 + CPU_CEIL) == 4096


def test_a_gpu_node_with_no_ceiling_keeps_the_global_max():
    """_OUTPUT_MAX_TOKENS_GPU is 0 (= the global max applies): a report on the
    GPU still gets its full room, so long generations are not squeezed."""
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=GLOBAL_MAX, reserve=RESERVE) == GLOBAL_MAX
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=0, reserve=RESERVE) == GLOBAL_MAX


def test_a_pinned_num_predict_plus_margin_wins_when_smaller():
    """The operator's thinker pins 1,024: it needs ~1,280 of room, not 16k. The
    role's pinned num_ctx is a floor applied by the caller, so the loop's window
    on the GPU does not change - only what a small call reserves does."""
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=GLOBAL_MAX, want_predict=1024,
                         reserve=RESERVE) == 1024 + C.DEFAULT_MARGIN


def test_a_pin_larger_than_the_ceiling_does_not_raise_it():
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=CPU_CEIL, want_predict=9000,
                         reserve=RESERVE) == CPU_CEIL


def test_the_reserve_is_a_floor_and_nonsense_is_safe():
    assert C.output_room(global_max=0, node_ceiling=0, reserve=RESERVE) == RESERVE
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=CPU_CEIL, want_predict=-5,
                         reserve=RESERVE) == CPU_CEIL
    assert C.output_room(global_max=GLOBAL_MAX, node_ceiling=CPU_CEIL, want_predict=None,
                         reserve=RESERVE) == CPU_CEIL


def test_the_default_naming_rule_names_a_model_every_node_carries():
    """With no model, a sandbox took the instance default - the 9b - onto a CPU
    node and held it for nine hours (judgement 18). The estate's CPU nodes carry
    qwen2.5:0.5b; the rule must name it."""
    from vera import capability_orchestration as orch
    rule = orch.DEFAULT_ROUTING_RULES["naming"]
    assert rule.get("model") == "qwen2.5:0.5b"
    assert rule.get("deny_gpu") is True
    # on the GPU node's CPU sibling with the embedder (user, 2026-09-28): the
    # 0.5b and the embed model are separate runners with two slots there, and a
    # naming generation takes a gate slot embeddings never use - so it does not
    # queue behind them; the CPU nodes stay free for the large models
    assert rule.get("prefer") == "gpu-250-cpu"
