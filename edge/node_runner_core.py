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
from typing import Dict, Iterable, List, Optional, Tuple

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


# ─────────────────────────────────────────────────────────────────────────────
# THE OTHER FAILURE: a runner nothing is WRONG with, that nothing reaches
# ─────────────────────────────────────────────────────────────────────────────
# `reap_plan` above answers "is this runner burning the box for nobody". On
# 2026-09-20 gpu-250 failed the opposite way and this module reported all-clear
# throughout, correctly by its own rule and uselessly in practice.
#
# ollama's scheduler deadlocked. Its HTTP server kept answering, its runner
# stayed healthy, idle and resident with the model in VRAM — and generation
# requests were accepted and never dispatched to it. Talking to the runner
# directly returned in 0.41s; the same request through ollama timed out at 30s
# having produced no journal line and never moved the runner's slot off
# `is_processing: false`.
#
# Every signal Vera owned said the node was fine, because /api/ps, /api/tags and
# /api/version do NOT go through the scheduler: obs.health, /ollama/cluster,
# in_use, gpu_gate and this reaper all read "online, idle". Chat hung for eight
# hours with no reply and nothing to look at.
#
# Killing the runner is NOT the remedy and must never be inferred from this: the
# runner is the healthy part, and `reap_plan` already keeps it (idle, no CPU).
# The wedged component is `ollama serve` itself, and only restarting it clears
# the deadlock. So this classifies and NAMES the state; it never reaps.


@dataclass
class DispatchProbe:
    """What one node's ollama API did when asked to do a trivial generation.

    `dispatched` is three-valued on purpose: True (it answered), False (it did
    not, within the probe's bound), None (not probed — see `skipped`). None must
    never read as a wedge; an unprobed node is an unknown, not a finding.
    """
    node: str = ""
    metadata_ok: bool = False          # /api/ps answered at all
    resident_models: int = 0           # how many models it reports loaded
    dispatched: Optional[bool] = None  # the node ANSWERED the probe at all
    probe_s: float = 0.0
    skipped: str = ""                  # why the probe did not run
    probe_model: str = ""              # which resident model was asked

    def to_dict(self) -> Dict:
        return {"node": self.node, "metadata_ok": self.metadata_ok,
                "resident_models": self.resident_models,
                "dispatched": self.dispatched, "probe_model": self.probe_model,
                "probe_s": round(self.probe_s, 2), "skipped": self.skipped,
                "wedged": is_dispatch_wedged(self)}


def is_dispatch_wedged(p: Optional[DispatchProbe]) -> bool:
    """ollama is answering metadata but will not hand work to its own runner.

    All four conditions are required, and each one excludes a different
    innocent explanation:

      * probed at all        — an unprobed node is unknown, not wedged
      * metadata answered    — a node that is simply DOWN is a different finding
                               (and `unreachable` already reports it)
      * a model is resident  — with nothing loaded, a slow reply is a cold model
                               load, which is normal and can take minutes
      * the node never       — `dispatched` is False ONLY when the probe got no
        answered at all        reply whatsoever. Any HTTP response, including a
                               4xx, proves the scheduler is alive and answering;
                               reading a non-200 as a wedge flagged a healthy
                               gpu-250 in 0.06s because the resident model was
                               an embedding one that cannot serve /api/generate.
    """
    if p is None or p.skipped:
        return False
    if not p.metadata_ok:
        return False
    if p.resident_models <= 0:
        return False
    return p.dispatched is False


def is_embedding_model(m: Dict) -> bool:
    """Would ollama refuse /api/generate for this resident model?

    An embedding model cannot complete, so a 4xx from one says something about
    the MODEL and nothing about whether the node dispatches work.
    """
    if not isinstance(m, dict):
        return False
    name = str(m.get("name") or m.get("model") or "").lower()
    details = m.get("details") or {}
    fam = str(details.get("family") or "").lower()
    fams = [str(f).lower() for f in (details.get("families") or [])]
    return ("embed" in name or "bert" in fam
            or any("bert" in f for f in fams))


def probe_call(models: Optional[List[Dict]]) -> Tuple[str, str, Dict]:
    """(model, path, payload) for the cheapest call a RESIDENT model can serve.

    Prefers a generative model: a completion exercises the same scheduler path
    real work uses. Falls back to /api/embed for an embedding-only node, so a
    node serving only nomic-embed-text is still probed meaningfully instead of
    being asked to do something it cannot and failing for the wrong reason.
    """
    rows = [m for m in (models or []) if isinstance(m, dict)]
    if not rows:
        return "", "", {}
    gen = [m for m in rows if not is_embedding_model(m)]
    if gen:
        name = str(gen[0].get("name") or gen[0].get("model") or "")
        if name:
            return name, "/api/generate", {
                "model": name, "prompt": "ping", "stream": False,
                "options": {"num_predict": 1},
            }
    name = str(rows[0].get("name") or rows[0].get("model") or "")
    if not name:
        return "", "", {}
    return name, "/api/embed", {"model": name, "input": "ping"}


def dispatch_finding(p: Optional[DispatchProbe]) -> Optional[Dict]:
    """A reportable finding for a wedged node, or None when it is healthy.

    Carries the remedy because the obvious reading of "runner idle, node not
    serving" is to kill the runner, and that is the wrong move: it destroys a
    loaded model and leaves the actual deadlock in place.
    """
    if not is_dispatch_wedged(p):
        return None
    return {
        "node": p.node,
        "severity": "crit",
        "title": f"{p.node}: ollama accepts generations but never dispatches them",
        "detail": (
            f"/api/ps answers and reports {p.resident_models} resident model(s), "
            f"but a 1-token generation did not return within {p.probe_s:.0f}s. "
            "With the model already in memory that is milliseconds of real work. "
            "Metadata endpoints bypass ollama's scheduler, so health checks that "
            "only poll /api/ps, /api/tags or /api/version will keep reporting "
            "this node online and free while nothing can generate on it."),
        # Two causes produce this from outside, and they have OPPOSITE remedies,
        # so name both rather than assert the one we saw. A node saturated by
        # other work is slow; a deadlocked scheduler never answers at all.
        "discriminator": (
            f"On {p.node}, ask the runner directly: `nodes.runner.list` gives its "
            "pid and port, then `curl 127.0.0.1:<port>/health` and `/slots` ON "
            "that node. A runner that answers /health in milliseconds with its "
            "slot idle, while ollama's own API will not generate, is a WEDGED "
            "SCHEDULER. A runner at high CPU with a busy slot is a SATURATED "
            "NODE — that one needs load shed, not a restart."),
        "remedy": (
            f"If the runner is idle and healthy: restart ollama on {p.node} "
            "(systemctl restart ollama-vera in its container) — only that clears "
            "the deadlock. Do NOT kill the runner: it is the working part, and "
            "killing it loses the loaded model without fixing anything."),
    }


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
