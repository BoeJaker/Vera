"""How long ollama_generate waits on an Ollama read. Pure.

The read timeout covers the wait for Ollama's first byte: queueing, a model load, the
prompt pass. On a CPU node that pass alone can take many minutes for a long prompt, so the
full generation timeout (OLLAMA_GEN_TIMEOUT, 900 s) stays. On a GPU node a working request
starts within a minute - a cold load plus a 28k-token prompt is ~40 s - so a GPU that has
not produced a byte in OLLAMA_GPU_FIRST_BYTE_S (300 s) is stuck, not busy: give up and let
the existing CPU fallback run. 4 Oct 2026: two census coder calls and a probe each waited
the full 900 s on gpu-250 while it sat at 0% with a runner Ollama thought was busy.
Between-chunk stalls are still caught earlier by OLLAMA_STALL_TIMEOUT (240 s).
"""


def read_timeout(gen_timeout: float, has_gpu: bool, gpu_first_byte_s: float) -> float:
    try:
        g = float(gen_timeout)
    except (TypeError, ValueError):
        g = 900.0
    try:
        cap = float(gpu_first_byte_s)
    except (TypeError, ValueError):
        cap = 0.0
    if has_gpu and cap > 0:
        return min(g, cap)
    return g
