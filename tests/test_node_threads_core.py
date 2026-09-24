"""A 137M embedding took 4-6 seconds; a 0.5b decoded at 0.24 tokens a second.

CT130's runner line: `system_info: n_threads = 24 (n_threads_batch = 24) / 12`.
Ollama sizes threads from the host's 24 physical cores and ignores the
container's 12-CPU cgroup; llama.cpp's busy-waiting pool collapses under 2:1
oversubscription. Same call, same node, Ollama's own timings (2026-09-23):
default 45 s at 0.24 tok/s; num_thread 12 -> 2.1 s at 30 tok/s; num_thread 6 ->
2.0 s at 60 tok/s.

Pure: numbers in, a number out; plus a source pin that both request builders use it.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera import node_threads_core as T                       # noqa: E402

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_a_cpu_node_gets_the_estate_default():
    assert T.threads_for(has_gpu=False) == T.DEFAULT_CPU_THREADS == 6


def test_a_gpu_node_is_left_alone():
    assert T.threads_for(has_gpu=True) == 0
    assert T.threads_for(has_gpu=True, node_num_thread=4) == 0


def test_the_nodes_own_setting_wins_over_the_default():
    assert T.threads_for(has_gpu=False, node_num_thread=12) == 12
    assert T.threads_for(has_gpu=False, node_num_thread="8") == 8


def test_a_callers_pin_wins_over_everything():
    assert T.threads_for(has_gpu=False, node_num_thread=12, pinned=3) == 3
    assert T.threads_for(has_gpu=True, pinned=2) == 2


def test_nonsense_is_safe():
    assert T.threads_for(has_gpu=False, node_num_thread="x", default="y") == 0
    assert T.threads_for(has_gpu=False, node_num_thread=-1, default=0) == 0
    assert T.threads_for(has_gpu=False, pinned=None, node_num_thread=None) == 6


def test_with_threads_copies_and_sets():
    src = {"num_ctx": 4096}
    out = T.with_threads(src, 6)
    assert out == {"num_ctx": 4096, "num_thread": 6} and "num_thread" not in src
    assert T.with_threads(None, 0) == {}


def test_a_failover_to_a_cpu_node_refits_threads_and_window():
    """run73 (2026-09-24): the GPU's body - no num_thread, num_ctx 28672 - was
    re-sent to both CPU nodes; each started the 9b at 24 threads for 4 min."""
    gpu_body_opts = {"num_ctx": 28672, "num_predict": 4096, "temperature": 0.2}
    out = T.refit_for_node(gpu_body_opts, has_gpu=False, node_ctx_max=8192)
    assert out == {"num_ctx": 8192, "num_predict": 4096, "temperature": 0.2, "num_thread": 6}
    assert gpu_body_opts == {"num_ctx": 28672, "num_predict": 4096, "temperature": 0.2}  # a copy


def test_a_failover_to_a_gpu_node_drops_the_thread_count():
    out = T.refit_for_node({"num_ctx": 4096, "num_thread": 6}, has_gpu=True)
    assert out == {"num_ctx": 4096}


def test_a_node_without_a_known_cap_keeps_the_window():
    out = T.refit_for_node({"num_ctx": 28672}, has_gpu=False, node_ctx_max=0)
    assert out == {"num_ctx": 28672, "num_thread": 6}
    assert T.refit_for_node(None, has_gpu=False, node_num_thread=4) == {"num_thread": 4}
    assert T.refit_for_node(None, has_gpu=True) == {}


def test_the_generate_failover_refits_its_body():
    src = (ROOT / "vera/capability_orchestration.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    gen = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "ollama_generate")
    body = ast.get_source_segment(src, gen)
    assert "_node_threads_core.refit_for_node(" in body
    assert 'json={**fb_body, "stream": True}' in body
    assert 'json={**body, "stream": True}' not in body


def test_both_request_builders_send_it():
    """Generate and embed - the embed runner was the one starving the census."""
    src = (ROOT / "vera/capability_orchestration.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    gen = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "ollama_generate")
    body = ast.get_source_segment(src, gen)
    assert "_node_threads_core.threads_for(" in body
    assert '_merged_opts["num_thread"] = _nt' in body
    assert "def _embed_body(" in src
    assert 'json=_embed_body(mdl, text, inst)' in src
    assert 'json=_embed_body(mdl, text, fb_inst)' in src


def test_the_embed_body_shape():
    """The helper is small enough to exercise by extracting it."""
    src = (ROOT / "vera/capability_orchestration.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_embed_body")
    from typing import Optional
    ns = {"_node_threads_core": T, "_CPU_NODE_THREADS": 6, "Optional": Optional}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "x", "exec"), ns)
    cpu = ns["_embed_body"]("nomic-embed-text", "hello", {"has_gpu": False})
    gpu = ns["_embed_body"]("nomic-embed-text", "hello", {"has_gpu": True})
    assert cpu == {"model": "nomic-embed-text", "input": "hello", "options": {"num_thread": 6}}
    assert gpu == {"model": "nomic-embed-text", "input": "hello"}
    assert ns["_embed_body"]("m", "x" * 5000, None)["input"] == "x" * 4096
