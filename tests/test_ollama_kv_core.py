"""kv_bytes_per_token against a real /api/show block and its edge cases.

Imports lowercase `vera.ollama_kv_core` with the repo root on sys.path so the
WORKTREE copy is exercised.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera import ollama_kv_core as kv  # noqa: E402

pytestmark = pytest.mark.critical

# Exactly what gpu-250 reported for jaahas/qwen3.5-uncensored:9b on 2026-09-13.
QWEN35 = {"general.architecture": "qwen35", "qwen35.block_count": 32,
          "qwen35.attention.head_count": 16, "qwen35.attention.head_count_kv": 4,
          "qwen35.attention.key_length": 256, "qwen35.attention.value_length": 256,
          "qwen35.embedding_length": 4096, "qwen35.full_attention_interval": 4,
          "qwen35.context_length": 262144}


def test_a_hybrid_model_counts_only_its_attention_layers():
    """32 layers, one in four attends: 8 * 2 * 4 * 256 * 2 = 32,768 - the figure
    the node itself measured (896 MiB at 28,672 tokens)."""
    assert kv.kv_bytes_per_token(QWEN35) == 32768.0


def test_a_plain_transformer_counts_every_layer():
    mi = {"general.architecture": "llama", "llama.block_count": 32,
          "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
          "llama.attention.key_length": 128}
    assert kv.kv_bytes_per_token(mi) == 32 * 2 * 8 * 128 * 2


def test_head_dim_falls_back_to_embedding_over_heads():
    mi = {"general.architecture": "llama", "llama.block_count": 2,
          "llama.attention.head_count": 4, "llama.attention.head_count_kv": 4,
          "llama.embedding_length": 512}
    assert kv.kv_bytes_per_token(mi) == 2 * 2 * 4 * 128 * 2


def test_unknown_kv_heads_is_unknown_not_head_count():
    mi = dict(QWEN35)
    mi.pop("qwen35.attention.head_count_kv")
    assert kv.kv_bytes_per_token(mi) == 0.0


def test_no_architecture_is_unknown():
    assert kv.kv_bytes_per_token({}) == 0.0
    assert kv.kv_bytes_per_token(None) == 0.0


def test_attention_layers_never_zero_for_a_real_model():
    assert kv.attention_layers(3, 4) == 1
    assert kv.attention_layers(32, 1) == 32
    assert kv.attention_layers(0, 4) == 0
