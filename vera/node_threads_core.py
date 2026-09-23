"""How many threads a CPU node's runner should use.

Ollama sizes a runner's thread count from the HOST's physical cores - it reads
cpuinfo, not the cgroup - so inside a 12-CPU LXC on a 2 x 12-core host every
runner started with 24 threads on 12 hyperthreads, and those 12 were the SMT
siblings of the other CPU node's 12. llama.cpp's thread pool busy-waits, and
oversubscription is pathological for it. Measured on cpu-247 (2026-09-23), the
same 0.5b, prompt and window, Ollama's own timings:

    num_thread default (24)   45 s   0.24 tok/s
    num_thread 12             2.1 s  30 tok/s
    num_thread 6              2.0 s  60 tok/s

Every "slow embed" (4-6 s for a 137M model), every CPU-node stall behind a
long generation, and the nine-hour chat title were this. The runner line that
gives it away: `system_info: n_threads = 24 (n_threads_batch = 24) / 12`.

This is the one rule: what to send as `options.num_thread` for a request to a
node. GPU nodes are left alone (their runners barely use CPU threads).

Pure: numbers in, a number out.
"""
from __future__ import annotations

from typing import Any, Optional

#: Threads for a non-GPU node when neither the node nor the caller says.
#: Six, not twelve: the two CPU nodes share twelve physical cores (SMT
#: siblings), and six each measured fastest AND leaves the other node room.
DEFAULT_CPU_THREADS = 6


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def threads_for(*, has_gpu: bool, node_num_thread: Any = None,
                default: int = DEFAULT_CPU_THREADS, pinned: Any = None) -> int:
    """`options.num_thread` for this request, or 0 to send nothing.

    A caller that pinned a positive num_thread keeps it. A GPU node gets
    nothing. Otherwise the node's own configured `num_thread` wins, then the
    estate default.
    """
    p = _int(pinned)
    if p > 0:
        return p
    if has_gpu:
        return 0
    n = _int(node_num_thread)
    if n > 0:
        return n
    d = _int(default)
    return d if d > 0 else 0


def with_threads(options: Optional[dict], threads: int) -> dict:
    """`options` with num_thread set when `threads` is positive; a copy."""
    out = dict(options or {})
    if int(threads or 0) > 0:
        out["num_thread"] = int(threads)
    return out
