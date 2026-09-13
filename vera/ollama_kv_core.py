"""KV-cache bytes per token from an Ollama /api/show model_info block.

The cache holds a key and a value per ATTENTION layer per token:
    bytes/token = attention_layers * 2 (K and V) * kv_heads * head_dim * 2 (fp16)

Hybrid models do not attend in every layer. qwen3.5 reports block_count=32 and
full_attention_interval=4: only every fourth layer is ordinary attention; the
other 24 keep a fixed-size state (its ssm.* fields) that does not grow with the
window. Counting all 32 gave 131,072 B/token where the node measured 32,768
(896 MiB of KV at 28,672 tokens, 2026-09-11), so the up-front VRAM fit divided
free memory by a number four times too big and offered the GPU node a window a
quarter of what the card holds.

A missing head_count_kv means UNKNOWN; substituting head_count would be wrong by
construction for every grouped-query model, so the result is 0.0 (= do not cap).
"""
from typing import Any, Dict, Optional


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def attention_layers(block_count: int, full_attention_interval: int) -> int:
    """How many of the model's layers hold a growing KV cache."""
    if block_count <= 0:
        return 0
    if full_attention_interval and full_attention_interval > 1:
        return max(1, block_count // full_attention_interval)
    return block_count


def kv_bytes_per_token(model_info: Optional[Dict[str, Any]]) -> float:
    mi = model_info or {}
    arch = str(mi.get("general.architecture") or "")
    if not arch:
        return 0.0

    def g(suffix: str) -> Any:
        return mi.get(f"{arch}.{suffix}")

    layers = attention_layers(_int(g("block_count")), _int(g("full_attention_interval")))
    n_kv = _int(g("attention.head_count_kv"))
    head_dim = _int(g("attention.key_length"))
    if not head_dim:
        emb, n_head = _int(g("embedding_length")), _int(g("attention.head_count"))
        head_dim = (emb // n_head) if (emb and n_head) else 0
    if not (layers and n_kv and head_dim):
        return 0.0
    return float(layers * 2 * n_kv * head_dim * 2)
