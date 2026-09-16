"""vera_node_agent.py — Vera's control/monitor plane on a compute worker.

One small agent per compute node (the ollama containers CT126/129/130 today),
giving Vera a generic way to SEE and CONTROL what a worker is actually doing —
rather than inferring it from the outside and being wrong.

Why it exists (2026-09-16). An ollama runner on cpu-247 sat at 1200% CPU —
twelve of the Proxmox host's forty-eight cores — for eighty minutes, with no
client left waiting: Vera's orchestrator had restarted half an hour after the
generation began, and `llama-server` cannot tell. Nothing in Vera could see it,
because ollama's HTTP API reports which models are RESIDENT, never which are
COMPUTING, and the CPU nodes are ungated so nothing tracked the work either.
Diagnosing it needed root on the Proxmox host and `pct exec`; killing it needed
the same. Neither is something Vera can do for itself, and neither generalises
past Proxmox.

Deliberately dependency-light — stdlib plus FastAPI, reading /proc directly. It
must run on the CPU-only ollama nodes, which have no torch and no GPU, so it is
NOT merged into GPU_inference.py (that one needs torch, diffusers and a card).
Where a media service IS present on the same node, the agent proxies to it, so
Vera has one address per node for both "what are you doing" and "do this".

Endpoints
---------
  GET  /node/status            host facts: cores, load, memory, uptime, gpu
  GET  /node/runners           compute-worker processes + whether each is stuck
  POST /node/runner/kill       terminate one runner by pid (token-gated)
  GET  /node/media             is a local media service present, and healthy
  POST /node/media/{path}      proxy an inference task to it

Auth: set VERA_NODE_TOKEN and send it as X-Vera-Node-Token. Mutating routes
refuse without it; read-only routes are open on the private network so Vera can
monitor a node it has not been given a token for yet.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from typing import Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from node_runner_core import (
    DEFAULT_STUCK_S, Runner, parse_model_from_cmdline, parse_port_from_cmdline,
    reap_plan,
)

NODE_NAME = os.getenv("VERA_NODE_NAME", os.uname().nodename)
NODE_TOKEN = os.getenv("VERA_NODE_TOKEN", "").strip()
STUCK_S = float(os.getenv("VERA_RUNNER_STUCK_S", str(DEFAULT_STUCK_S)))
MEDIA_URL = os.getenv("VERA_NODE_MEDIA_URL", "http://127.0.0.1:8765").rstrip("/")
#: Process names treated as compute workers. ollama's runner is llama-server;
#: add others (vllm, sglang) here as they appear on the estate.
RUNNER_NAMES = tuple(
    n.strip() for n in
    os.getenv("VERA_RUNNER_NAMES", "llama-server,ollama runner").split(",")
    if n.strip()
)

_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

app = FastAPI(title="Vera Node Agent", version="1.0")


# ─────────────────────────────── /proc reading ───────────────────────────────

def _boot_time() -> float:
    try:
        with open("/proc/stat", "r") as fh:
            for line in fh:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except Exception:
        pass
    return 0.0


def _read_runner(pid: int, boot: float) -> Optional[Runner]:
    """One /proc entry → Runner, or None if it is not a compute worker.

    Reads `stat` rather than shelling out to ps: the containers here report a
    nonsense ELAPSED through ps (their btime is skewed, which also zeroes
    ps's %CPU), and that is exactly the reading that made the wedged runner look
    idle. utime+stime and starttime from /proc/<pid>/stat are unaffected.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if not cmdline or not any(n in cmdline for n in RUNNER_NAMES):
            return None
        with open(f"/proc/{pid}/stat", "r") as fh:
            raw = fh.read()
        # comm may contain spaces/parens — split after the last ')'
        rest = raw[raw.rindex(")") + 2:].split()
        state = rest[0]
        utime, stime = float(rest[11]), float(rest[12])
        starttime = float(rest[19])
        rss_pages = float(rest[21])
    except Exception:
        return None
    cpu_seconds = (utime + stime) / float(_CLK or 100)
    started = (boot + starttime / float(_CLK or 100)) if boot else 0.0
    age = (time.time() - started) if started else 0.0
    return Runner(
        pid=pid, model=parse_model_from_cmdline(cmdline),
        port=parse_port_from_cmdline(cmdline), state=state,
        cpu_seconds=cpu_seconds, age_s=max(0.0, age),
        rss_mb=int(rss_pages * 4096 / (1024 * 1024)), node=NODE_NAME,
    )


def _runners() -> List[Runner]:
    boot = _boot_time()
    out: List[Runner] = []
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            r = _read_runner(int(name), boot)
            if r is not None:
                out.append(r)
    except Exception:
        pass
    return sorted(out, key=lambda r: -r.cpu_seconds)


def _gpu() -> Optional[Dict]:
    """nvidia-smi if this node has a card, else None. Never raises."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        q = ("name,memory.total,memory.used,memory.free,utilization.gpu")
        res = subprocess.run(
            [exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if res.returncode != 0 or not res.stdout.strip():
            return None
        name, tot, used, free, util = [
            p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
        return {"name": name, "total_mb": int(float(tot)), "used_mb": int(float(used)),
                "free_mb": int(float(free)), "util_pct": int(float(util))}
    except Exception:
        return None


def _require_token(tok: Optional[str]) -> None:
    if not NODE_TOKEN:
        raise HTTPException(503, "VERA_NODE_TOKEN is not set on this node — "
                                 "refusing to mutate anything without it")
    if (tok or "") != NODE_TOKEN:
        raise HTTPException(403, "bad or missing X-Vera-Node-Token")


# ──────────────────────────────── endpoints ──────────────────────────────────

@app.get("/node/status")
async def node_status():
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    mem: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.strip().split()[0]) // 1024
    except Exception:
        pass
    return {
        "node": NODE_NAME,
        "cores": os.cpu_count(),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "mem_total_mb": mem.get("MemTotal", 0),
        "mem_available_mb": mem.get("MemAvailable", 0),
        "gpu": _gpu(),
        "runners": len(_runners()),
        "stuck_after_s": STUCK_S,
        "can_control": bool(NODE_TOKEN),
    }


@app.get("/node/runners")
async def node_runners(stuck_s: float = 0):
    """Every compute worker on this node, and which have outlived their client.

    `stuck` is advisory — the agent never kills on its own. Vera decides, so the
    policy lives in one place and a node cannot surprise the estate.
    """
    runners = _runners()
    v = reap_plan(runners, stuck_s=(stuck_s or STUCK_S))
    stuck_pids = {r.pid for r in v.stuck}
    return {
        "node": NODE_NAME,
        "stuck_after_s": (stuck_s or STUCK_S),
        "runners": [{**r.to_dict(), "stuck": r.pid in stuck_pids,
                     "reason": v.reasons.get(r.pid, "")} for r in runners],
        "stuck_count": len(v.stuck),
    }


class KillReq(BaseModel):
    pid: int
    force: bool = False
    reason: str = ""


@app.post("/node/runner/kill")
async def node_runner_kill(req: KillReq,
                           x_vera_node_token: Optional[str] = Header(None)):
    """Terminate one runner. SIGTERM first — ollama reaps it and unloads the
    model cleanly; the wedged runner on cpu-247 went on SIGTERM alone."""
    _require_token(x_vera_node_token)
    r = _read_runner(int(req.pid), _boot_time())
    if r is None:
        raise HTTPException(404, f"pid {req.pid} is not a compute worker on this node")
    before = r.to_dict()
    try:
        os.kill(req.pid, signal.SIGKILL if req.force else signal.SIGTERM)
    except ProcessLookupError:
        raise HTTPException(404, f"pid {req.pid} is gone")
    except PermissionError:
        raise HTTPException(403, f"not permitted to signal pid {req.pid}")
    gone = False
    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(req.pid, 0)
        except OSError:
            gone = True
            break
    return {"ok": True, "pid": req.pid, "gone": gone,
            "signal": "SIGKILL" if req.force else "SIGTERM",
            "reason": req.reason, "runner": before, "node": NODE_NAME}


@app.get("/node/media")
async def node_media():
    """Is a specialised-inference service (Whisper/TTS/Stable Diffusion) here?

    Ollama covers text generation; this is how Vera finds the work it does not
    cover, at the same address as everything else about the node.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"{MEDIA_URL}/health", timeout=5) as resp:
            import json as _json
            return {"present": True, "url": MEDIA_URL,
                    "health": _json.loads(resp.read().decode("utf-8"))}
    except Exception as e:
        return {"present": False, "url": MEDIA_URL, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("VERA_NODE_BIND", "0.0.0.0"),
                port=int(os.getenv("VERA_NODE_PORT", "8770")))
