"""node_runner_core.py — pure decisions about compute-worker processes.

No psutil, no HTTP, no Vera imports: shared verbatim by the node agent
(edge/vera_node_agent.py, which reads /proc) and by Vera's reaper capability
(vera/workers/node_agent_capabilities.py, which acts on what the agent
reports). Pinned by tests/test_node_runner_reap.py.

Why this exists (2026-09-16). An `ollama` runner on cpu-247 (CT130) was found
at 1200% CPU — twelve cores — with 16h03m of accumulated CPU time, having
started at 15:53. Vera's orchestrator had restarted at 16:23, so the client
that asked for that generation was gone: there was no ESTABLISHED connection
from the Vera host to any ollama node, and the socket the node still showed was
half-open. `llama-server` had no way to know. It only notices a dead client
when it finishes and tries to write, and at the ~0.05 tok/s a CPU node manages
with num_predict sized to a 24,576-token window, "finishes" was days away.

Vera already had `_sweep_stuck_running`, but that only fail-marks the job
RECORD. Nothing ever told the node to stop, and on CPU nodes — which are
ungated, capacity 0 — nothing tracked the work at all.

The rule below is Vera's own existing reasoning, applied to the process instead
of the bookkeeping: a generation cannot outlive the client that is waiting for
it, because that client aborts at OLLAMA_GEN_TIMEOUT. A runner still burning
CPU at twice that has, by definition, no one left to answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

#: Default: 2x OLLAMA_GEN_TIMEOUT (900s), matching _sweep_stuck_running's
#: threshold for the same reason. Never below this floor.
DEFAULT_STUCK_S = 1800.0

#: A runner must be doing real work to count as stuck. One that is merely
#: resident and idle is ollama's keep_alive doing its job, not a wedge.
MIN_CPU_SECONDS = 60.0


@dataclass
class Runner:
    """One compute-worker process as the agent sees it."""
    pid: int
    model: str = ""
    port: int = 0
    state: str = ""          # 'R' running, 'S' sleeping, ...
    cpu_seconds: float = 0.0  # total CPU time consumed
    age_s: float = 0.0        # wall-clock since start
    rss_mb: int = 0
    node: str = ""

    def to_dict(self) -> Dict:
        return {"pid": self.pid, "model": self.model, "port": self.port,
                "state": self.state, "cpu_seconds": round(self.cpu_seconds, 1),
                "age_s": round(self.age_s, 1), "rss_mb": self.rss_mb,
                "node": self.node}


@dataclass
class ReapVerdict:
    stuck: List[Runner] = field(default_factory=list)
    kept: List[Runner] = field(default_factory=list)
    reasons: Dict[int, str] = field(default_factory=dict)


def is_active(r: Runner, *, min_cpu_seconds: float = MIN_CPU_SECONDS) -> bool:
    """Is this runner actually computing, as opposed to loaded and idle?

    Two signals, both needed. `state == 'R'` alone is a single sample and can
    catch a runner mid-tick; accumulated CPU time alone cannot tell "busy now"
    from "was busy an hour ago". Together they mean work is happening.
    """
    if (r.state or "").upper() != "R":
        return False
    return float(r.cpu_seconds or 0) >= float(min_cpu_seconds)


def reap_plan(runners: Iterable[Runner], *, stuck_s: float = DEFAULT_STUCK_S,
              protected_pids: Optional[Iterable[int]] = None,
              min_cpu_seconds: float = MIN_CPU_SECONDS) -> ReapVerdict:
    """Which runners have outlived any client that could still be waiting.

    `protected_pids` are runners Vera knows it is *currently* waiting on — pass
    them and they are never reaped, whatever their age. That is the hook for a
    future lease/in-flight registry; with none supplied the age rule alone
    still holds, because a client cannot outlive its own timeout.

    `stuck_s <= 0` disables reaping entirely.
    """
    v = ReapVerdict()
    protect = {int(p) for p in (protected_pids or [])}
    if stuck_s <= 0:
        v.kept = [r for r in runners if r]
        return v
    for r in runners:
        if not r or not getattr(r, "pid", 0):
            continue
        if r.pid in protect:
            v.kept.append(r)
            v.reasons[r.pid] = "vera is still waiting on this one"
            continue
        if not is_active(r, min_cpu_seconds=min_cpu_seconds):
            v.kept.append(r)
            v.reasons[r.pid] = "resident but idle (keep_alive), not computing"
            continue
        if float(r.age_s or 0) < stuck_s:
            v.kept.append(r)
            v.reasons[r.pid] = f"active for {r.age_s:.0f}s, under the {stuck_s:.0f}s bound"
            continue
        v.stuck.append(r)
        v.reasons[r.pid] = (
            f"computing for {r.age_s:.0f}s ({r.cpu_seconds:.0f}s CPU) — past "
            f"{stuck_s:.0f}s, so every client that asked has already timed out")
    return v


def parse_model_from_cmdline(cmdline: str) -> str:
    """Pull the model blob/name out of a llama-server command line.

    Ollama runners are spawned as `llama-server --model <path-to-blob> --port N`,
    so the blob sha is all the node can see; Vera maps it back to a tag.
    """
    parts = (cmdline or "").split()
    for flag in ("--model", "-m"):
        if flag in parts:
            i = parts.index(flag)
            if i + 1 < len(parts):
                val = parts[i + 1]
                return val.rsplit("/", 1)[-1] if "/" in val else val
    return ""


def parse_port_from_cmdline(cmdline: str) -> int:
    parts = (cmdline or "").split()
    if "--port" in parts:
        i = parts.index("--port")
        if i + 1 < len(parts):
            try:
                return int(parts[i + 1])
            except ValueError:
                return 0
    return 0
