"""One context window per (GPU node, model): the runner must not be re-created
call after call because the prompt length changed.

Census run58 author-then-edit: 24 llama-server starts for 54 generate calls;
the 2026-09-22 verification probe with every loop role pinned to 16384: 34
starts for 82 calls (the pin is a floor under the per-prompt fit, so the
window still jittered 24576 <-> 28672). On a 12 GB V100 each change is a
30-100 s cold load. Pure: the helper takes the fitted window and the
node-safe cap.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.capability_orchestration import _stable_ctx  # noqa: E402


def test_gpu_node_gets_the_node_safe_cap_every_call():
    # two prompts of different length, same model, same GPU node -> same window
    assert _stable_ctx(24576, 28672, has_gpu=True) == 28672
    assert _stable_ctx(8192, 28672, has_gpu=True) == 28672
    assert _stable_ctx(28672, 28672, has_gpu=True) == 28672


def test_cpu_node_keeps_the_fit():
    assert _stable_ctx(8192, 32768, has_gpu=False) == 8192
    assert _stable_ctx(24576, 32768, has_gpu=False) == 24576


def test_no_cap_or_stability_off_leaves_the_fit_alone():
    assert _stable_ctx(8192, 0, has_gpu=True) == 8192
    assert _stable_ctx(8192, 28672, has_gpu=True, stable=False) == 8192
    # a cap below the fit (already applied upstream) is never raised past what was asked
    assert _stable_ctx(28672, 16384, has_gpu=True) == 28672
