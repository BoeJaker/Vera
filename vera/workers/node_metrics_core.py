"""A node's own CPU, measured on the cores it is pinned to (P0-1/P0-2, looplab-control §9.0, 18 Oct 2026).

The test nodes are LXC containers on one Proxmox host (PVE01 'corp'), each pinned to 12 of its 48 cores (Ollama
12-23, Ollama-B 0-11, Ollama-C 24-35). Checked inside each with the worker's own venv (18 Oct 2026): affinity is the
host-numbered cpuset, and psutil's per-core list has 12 entries - the container's own view (lxcfs). Only load1 is
host-wide (16.24 on all three). The trouble was the SAMPLE: one cpu_percent() per tick, so a 4-core chunk starting
or stopping moved free cores by 4 at once (12 -> 8 -> 4 -> 0 within 30 s) and admission was a coin toss.

`own_cpu` reports the cores the process may run on - whether /proc/stat lists the host's cores (keep ours by
affinity) or only the container's (all of them) - and says so ("pinned"); `Window` averages the last minute so one
busy tick does not refuse a chunk. Pure: the worker feeds it psutil's numbers; tests feed it lists.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Iterable, Optional, Sequence, Tuple

#: seconds the reported average covers
WINDOW_S = 60.0


def own_cpu(percpu: Optional[Sequence[float]], affinity: Optional[Iterable[int]]) -> Tuple[float, int, str]:
    """(busy %, cores, scope) for the cores this process is pinned to.

    scope "pinned": the figure is over the node's own cores, so it shows the node's own jobs (and anything else
    scheduled on those physical cores); "host": no pinning could be read, the figure is every core /proc/stat lists.
    """
    per = [max(0.0, min(100.0, float(x))) for x in (percpu or [])]
    cores = sorted({int(c) for c in (affinity or [])})
    if per and cores and len(cores) < len(per) and cores[-1] < len(per):
        own = [per[c] for c in cores]                     # /proc/stat lists the host's cores: keep ours
        return round(sum(own) / len(own), 1), len(own), "pinned"
    if per and cores and len(cores) == len(per):          # the container's /proc/stat is already its own
        return round(sum(per) / len(per), 1), len(per), "pinned"
    if per:
        return round(sum(per) / len(per), 1), len(per), "host"
    return 0.0, max(1, len(cores)), "host"


class Window:
    """The mean of the samples of the last `span` seconds."""

    def __init__(self, span: float = WINDOW_S):
        self.span = float(span)
        self._s: Deque[Tuple[float, float]] = deque()

    def add(self, value: float, now: Optional[float] = None) -> float:
        t = float(now if now is not None else time.time())
        self._s.append((t, float(value)))
        while self._s and self._s[0][0] < t - self.span:
            self._s.popleft()
        return self.mean()

    def mean(self) -> float:
        return round(sum(v for _, v in self._s) / len(self._s), 1) if self._s else 0.0

    def __len__(self) -> int:
        return len(self._s)
