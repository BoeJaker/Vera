"""gpu_residency_core.py — pure decisions for GPU_inference.py's residency manager.

No torch, no CUDA, no FastAPI: importable and testable on any box
(tests/test_gpu_residency.py). GPU_inference.py holds the side effects — moving
weights, reading the driver — and asks this module *whether* to act.

Context (2026-09-16): this service and ollama share ONE Tesla V100-PCIE-12GB
(12,288 MiB) inside CT126. This service held 3,548 MiB continuously for 2d18h
while idle, leaving ollama 8,740 MiB against the ~10,560 MiB it needs at
OLLAMA_MAX_AUTO_CTX=28672 — so ollama silently offloaded layers to CPU and
loop_executor throughput fell to 2.53 tok/s (12-30 when it fits, 0.05 on a real
CPU node). The LLM is the priority tenant: media weights live in CPU RAM and
visit the card only for the duration of a job.

Deployed alongside GPU_inference.py to /home/Servers/StableDiffustionWhisper on
CT126 — both files must be copied together.
"""

from __future__ import annotations

from typing import Dict, List, Mapping

#: Shape of one model's residency record, for reference:
#:   {"resident": bool, "busy": int, "last": float, "mb": int}


def can_place(free_mb: int, need_mb: int, reserve_mb: int,
              default_need_mb: int = 1024) -> bool:
    """May a model take the card without cutting into the LLM's reserve?

    `need_mb` is what this model measured last time it was parked; 0 means "not
    measured yet", in which case `default_need_mb` stands in rather than
    guessing the model's size from nothing. The reserve is headroom the LLM must
    keep: ollama sizes its KV cache when it LOADS, so VRAM taken from under it
    afterwards does not shrink the cache, it just forces a partial CPU offload.
    """
    if free_mb <= 0:
        return False
    want = (int(need_mb) or int(default_need_mb)) + int(reserve_mb)
    return int(free_mb) >= want


def parkable(state: Mapping[str, Mapping], *, now: float, idle_s: float) -> List[str]:
    """Names that should be parked back to CPU RAM.

    A model is parked when it is resident, no job holds it, and it has been idle
    for at least `idle_s`. `idle_s <= 0` disables parking entirely (the old
    always-resident behaviour), and a model with a job in flight is never taken
    out from under it.
    """
    if idle_s <= 0:
        return []
    out: List[str] = []
    for name, st in (state or {}).items():
        if not st or not st.get("resident"):
            continue
        if int(st.get("busy") or 0) > 0:
            continue
        last = float(st.get("last") or 0)
        # Never parked anything with no stamp at all: treat it as just-used so a
        # model loaded a moment ago is not reaped before its first job.
        if not last:
            continue
        if now - last >= idle_s:
            out.append(name)
    return out


def new_state() -> Dict[str, object]:
    """A fresh residency record."""
    return {"resident": False, "busy": 0, "last": 0.0, "mb": 0}
