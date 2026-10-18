"""
node_compute_core.py — model training on the node workers (no app imports)

Vera's NLP, embeddings, LLM and media calls already run on the nodes. What was
left on the host was model TRAINING: the worldview JEPA (torch), the ML Lab
trainer (numpy) and the markets models (scikit-learn). Each is a long numeric
loop over arrays the host assembles from its own stores - so the split is
"the host gathers, a node computes, the host keeps the result".

The contract, end to end:

  host   SET  vera:compute:in:<job>    packed inputs (arrays + JSON, no pickle)
         XADD vera:tasks:node:cpu_compute  {capability: compute.run, job, fn}
  node   compute.run: GET inputs -> run the allow-listed function in a thread
         RPUSH vera:compute:prog:<job>  progress dicts, as they happen
         SET  vera:compute:hb:<job>     every few seconds while it runs
         SET  vera:compute:out:<job>    packed outputs
         SET  vera:compute:done:<job>   {ok, error, node, seconds}
  host   polls done + progress, re-emits progress as the host's own events,
         reads the outputs and applies them (load weights, store the model).
  stop   host SET vera:compute:stop:<job>; the function checks between epochs.

The task stream is one only node workers read. The host reads every class
stream (so nothing is stranded), which is exactly why compute.run cannot ride
the cpu_compute class stream: the host would take its own job back.

Pure: names in, decisions out. Tested without a cluster.
"""
from __future__ import annotations

import importlib.util
import io
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The stream compute jobs are queued on. Only node workers holding the
#: cpu_compute class read it (worker_placement_core.streams_to_read).
NODE_COMPUTE_STREAM = "vera:tasks:node:cpu_compute"
#: The capability a node worker runs for a job.
RUN_CAP = "compute.run"
#: Config (json): {"host_fallback": bool, "threads": int}.
CONFIG_KEY = "vera:compute:config"
#: Recent jobs, newest first (json records), for the status view.
JOBS_KEY = "vera:compute:jobs"
JOBS_KEEP = 50

DEFAULT_CONFIG: Dict[str, Any] = {
    # Off: the point is that training runs on the nodes. When no node worker can
    # take a job the caller gets an error naming why, not a silent host run.
    "host_fallback": False,
    # torch / scikit-learn threads for one job on a node. The worker's own
    # BLAS pools stay at its unit's size; this is the training loop's share.
    "threads": 4,
    # What a node always keeps for its own services (Ollama runners, the NLP
    # and media servers): a job is admitted only on top of this.
    "reserve_cpu": 2.0,
    "reserve_mem_gb": 8.0,
    # How long a job waits for a node with room before it gives up (or falls
    # back to the host, when that is on).
    "max_wait_s": 900,
}

#: What one job of each function takes from its node, in cores and GB. A job
#: is admitted on a node only when the node's measured free CPU and memory,
#: less its reserve and the jobs this host already sent it, cover this.
NEEDS: Dict[str, Dict[str, float]] = {
    "worldview.train":  {"cpu": 4.0, "mem_gb": 6.0},
    "ml.train":         {"cpu": 1.0, "mem_gb": 1.0},
    "markets.ml.train": {"cpu": 4.0, "mem_gb": 2.0},
    # a chunk runs as test_pool node_parallel (4) pytest processes
    "pytest.shard":     {"cpu": 4.0, "mem_gb": 6.0},
    # ML Workshop inference (ml-workshop-compute MW-1)
    "ml.forward":        {"cpu": 1.0, "mem_gb": 1.0},
    "ml.module_forward": {"cpu": 1.0, "mem_gb": 1.0},
    "onnx.run":          {"cpu": 1.0, "mem_gb": 1.0},
    # web-ingress WI-12 crawl class: fetch-only batches a crawl worker reserves for itself while it runs one
    # (ingress_crawl_core.EXTRA_RES_KEY; the class stream has no target host to reserve against - vera-44, 18 Oct)
    "ingress.fetch.batch": {"cpu": 0.25, "mem_gb": 0.25},
}
def threads_for(fn: str, default: int) -> int:
    """Threads a job of `fn` may use: the cores it was admitted for (NEEDS).
    Every job used the config-wide 4 while ml.train reserved 1 core, so
    parallel trials would oversubscribe a node (2026-10-11, MW-4)."""
    need = (NEEDS.get(fn) or {}).get("cpu")
    return max(1, int(round(need))) if need else max(1, int(default or 1))


#: Kept for the record: MW-7 stopped counting a job this long after the node's
#: metrics passed its send time, assuming the metrics then show its load. They
#: do not - a node CT's psutil reads the whole Proxmox host - so a node took 4
#: four-core gate chunks at once on 12 cores (2026-10-12). Reservations now
#: count for the job's whole run again (reserved_now).
RESERVE_LAG_S = 15
#: A worker that reports cpu_scope "pinned" measures its own cores (P0-1, 18 Oct 2026), so its 60 s average shows
#: a job once it has run that long: a reservation older than this stops counting toward CPU. Memory keeps the
#: whole-run reservation: a chunk's memory grows long after its first reading.
PINNED_CPU_SEEN_S = 75


def compute_slots_default(ncpu: int) -> int:
    """Compute jobs a node worker runs side by side (VERA_COMPUTE_SLOTS
    overrides): an upper bound - free CPU / memory admit each job."""
    return max(1, int(ncpu or 2) - 2)


def run_detached(cap_name: str, is_worker: bool, slots: int) -> bool:
    """Does a node worker run this task beside others instead of one at a
    time? Only compute jobs, and only with more than one slot (MW-7)."""
    return bool(is_worker and int(slots or 0) > 1 and cap_name == "compute.run")


def reserved_now(res: Optional[Dict[str, Any]], metrics_at: float, scope: str = "host") -> Tuple[float, float]:
    """(cpu, mem_gb) this host has in flight on a node: every job it sent and
    has not heard back about. A node whose metrics read the Proxmox host
    (scope "host") cannot show its own jobs, so its reservations count for the
    job's whole run (2026-10-12, see RESERVE_LAG_S). A node that measures its
    own pinned cores (scope "pinned", P0-1 18 Oct 2026) shows a job once its
    average has covered it: only jobs sent too recently for that still count."""
    res = res or {}
    if scope != "pinned" or not metrics_at or not isinstance(res.get("sent"), list):
        return float(res.get("cpu", 0.0)), float(res.get("mem_gb", 0.0))
    cpu = 0.0
    for e in res["sent"]:
        try:
            t, c = float(e[0]), float(e[1])
        except (TypeError, ValueError, IndexError):
            continue
        if t > metrics_at - PINNED_CPU_SEEN_S:
            cpu += c
    # memory keeps its whole-run reservation (vera-d2's review, 18 Oct 2026): a pytest chunk's memory keeps growing
    # long after its first reading, and Ollama-B is already short of memory
    return cpu, float(res.get("mem_gb", 0.0))


#: Work a NODE takes for itself (P0d, 18 Oct 2026): a class-stream task (ingress.fetch.batch on vera:tasks:cls:crawl)
#: is read by whichever idle worker gets it, so the host cannot reserve for it. The worker writes its own reservation
#: into a hash per node - field = task id, value = extra_entry(...) - and HDELs it when the task ends. A field past
#: its `until` is ignored and pruned, so a killed worker cannot hold capacity. Agreed with vera-6e (web-ingress).
KEY_EXTRA_RES = "vera:node_compute:extra_res:"


def extra_entry(cap: str, timeout_s: float, now: float) -> str:
    """The hash value a worker writes while it runs `cap`: its NEEDS size, until now + the task's timeout."""
    need = NEEDS.get(cap, {"cpu": 1.0, "mem_gb": 1.0})
    return json.dumps({"cpu": float(need["cpu"]), "mem_gb": float(need["mem_gb"]),
                       "until": float(now) + max(1.0, float(timeout_s or 0)), "cap": str(cap)})


def extra_from(fields: Optional[Dict[Any, Any]], now: float) -> Tuple[Dict[str, Any], List[str]]:
    """(sum of a node's live self-reservations {cpu, mem_gb, n, caps}, the ids of fields past their `until` or
    unreadable - the caller HDELs them)."""
    out = {"cpu": 0.0, "mem_gb": 0.0, "n": 0, "caps": []}
    dead: List[str] = []
    for k, v in (fields or {}).items():
        k = k.decode() if isinstance(k, bytes) else str(k)
        try:
            e = json.loads(v.decode() if isinstance(v, bytes) else v)
            until, cpu, mem = float(e.get("until") or 0), float(e.get("cpu") or 0), float(e.get("mem_gb") or 0)
        except (TypeError, ValueError, AttributeError):
            dead.append(k)
            continue
        if until <= now:
            dead.append(k)
            continue
        out["cpu"] += max(0.0, cpu)
        out["mem_gb"] += max(0.0, mem)
        out["n"] += 1
        cap = str(e.get("cap") or "")
        if cap and cap not in out["caps"]:
            out["caps"].append(cap)
    return out, dead


def with_extra(reserved: Optional[Dict[str, Dict[str, Any]]], extra: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """`reserved` per host_id with each node's self-reservations alongside (extra_cpu / extra_mem_gb / extra_n /
    extra_caps), for admit(). The host's own entries are not changed."""
    out = {h: dict(v) for h, v in (reserved or {}).items()}
    for h, x in (extra or {}).items():
        if not x or not x.get("n"):
            continue
        row = out.setdefault(h, {"cpu": 0.0, "mem_gb": 0.0, "jobs": 0.0, "sent": []})
        row.update(extra_cpu=float(x["cpu"]), extra_mem_gb=float(x["mem_gb"]), extra_n=int(x["n"]),
                   extra_caps=list(x.get("caps") or []))
    return out


#: Node metrics older than this are not trusted for admission.
METRICS_STALE_S = 60

#: Each node's pinned test venv (hash: host_id -> {"sha", "ok", "at", "error"})
#: and the freeze sha the gate image needs now. A node runs a pytest shard only
#: when its venv matches (test_runner_core, nodes.testenv.sync).
TESTENV_KEY = "vera:nodes:testenv"
TESTENV_WANT_KEY = "vera:nodes:testenv:want"

#: Seconds a key lives. Inputs can be tens of MB, so they never outlive a job
#: by much; the done record stays a little longer for a late reader.
TTL_IN = 3 * 3600
TTL_OUT = 3600
HEARTBEAT_EVERY = 5
#: No heartbeat for this long after one was seen: the node lost the job.
HEARTBEAT_LOST = 60
#: Queued with no worker alive to take it for this long: give up.
QUEUE_ORPHAN = 120


def key(kind: str, job: str) -> str:
    return f"vera:compute:{kind}:{job}"


def in_key(job: str) -> str:   return key("in", job)
def out_key(job: str) -> str:  return key("out", job)
def done_key(job: str) -> str: return key("done", job)
def hb_key(job: str) -> str:   return key("hb", job)
def stop_key(job: str) -> str: return key("stop", job)
def prog_key(job: str) -> str: return key("prog", job)


# ── the functions a node may run ──────────────────────────────────────────────
#: name -> (module basename, attribute, libraries it needs). The module is
#: looked up among the modules the worker has ALREADY loaded (cap modules load
#: under their bare file name), so nothing is imported twice and no name from
#: the wire is ever imported. Each function takes (inputs, progress,
#: should_stop) and returns a dict of outputs; it must touch no store.
FUNCTIONS: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    "worldview.train": ("worldview_jepa", "node_train_stages", ("torch", "numpy")),
    "ml.train":        ("ml_training", "node_train_epochs", ("numpy",)),
    "markets.ml.train": ("markets_lab_capabilities", "node_train_model",
                         ("numpy", "sklearn")),
    # a shard of a pipeline gate's pytest run, in the node's pinned test venv
    # (`testenv`: the node's venv matches the gate image's packages)
    "pytest.shard":    ("test_runner_core", "node_run_shard", ("testenv",)),
    # ML Workshop inference off the host (ml-workshop-compute MW-1): a trained
    # module's forward pass, a module's per-node introspection pass, and an
    # exported ONNX model (the artifact travels by reference, _blob_refs)
    "ml.forward":        ("ml_training", "node_forward", ("numpy",)),
    "ml.module_forward": ("ml_workshop", "node_module_forward", ("numpy",)),
    "onnx.run":          ("ml_onnx", "node_onnx_run", ("numpy", "onnxruntime")),
}

#: Modules that are not capability modules (so not loaded by a worker at
#: start): imported on first use, by these fixed names only.
IMPORTS: Dict[str, str] = {"test_runner_core": "Vera.vera.evolve.test_runner_core"}

#: What a worker reports it can run (registration field `libs`).
PROBED_LIBS = ("numpy", "torch", "sklearn", "scipy", "onnxruntime")


def available_libs(find=importlib.util.find_spec) -> Dict[str, bool]:
    out = {}
    for m in PROBED_LIBS:
        try:
            out[m] = find(m) is not None
        except (ImportError, ValueError):
            out[m] = False
    return out


def requirements(fn: str) -> Tuple[str, ...]:
    spec = FUNCTIONS.get(fn)
    return spec[2] if spec else ()


#: run_on_node(serial=<name>): a cluster-wide one-at-a-time slot per name.
SERIAL_KEY = "vera:compute:serial:{name}"
#: The slot's TTL; its holder refreshes it every third of this while it runs.
SERIAL_TTL_S = 120


def serial_name(name: Any) -> str:
    """A slot name, or '' for none: lowercase letters, digits, '-', '_', '.'."""
    s = str(name or "").strip().lower()
    return s if s and len(s) <= 64 and all(c.isalnum() or c in "-_." for c in s) else ""


def serial_key(name: str) -> str:
    return SERIAL_KEY.format(name=name)


def node_missing(fn: str, libs: Dict[str, bool]) -> List[str]:
    """What a node worker lacks to run `fn`, judged on the node itself. Only
    importable libraries: a requirement that is not one (`testenv`, a venv on
    disk) is the function's own check."""
    return [m for m in requirements(fn) if m in PROBED_LIBS and not (libs or {}).get(m)]


def merge_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (raw or {}).items():
        try:
            if k == "host_fallback":
                cfg[k] = bool(v)
            elif k == "threads":
                cfg[k] = max(1, min(32, int(v)))
            elif k in ("reserve_cpu", "reserve_mem_gb"):
                cfg[k] = max(0.0, min(64.0, float(v)))
            elif k == "max_wait_s":
                cfg[k] = max(0, min(6 * 3600, int(v)))
        except (TypeError, ValueError):
            pass
    return cfg


def _libs_of(w: Dict[str, Any]) -> Dict[str, bool]:
    v = w.get("libs") or {}
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


def _classes_of(w: Dict[str, Any]) -> Tuple[str, ...]:
    v = w.get("classes") or ()
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [s for s in v.split(",") if s]
    return tuple(str(c) for c in v or ())


def compute_workers(workers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Live node workers that read the compute stream. A registration with no
    `libs` is a worker on code from before the compute stream existed: it
    cannot take a job, so it neither counts nor blocks (seen 2026-10-02: for
    the 2 min an old registration outlives its restart, every job was refused)."""
    return [w for w in workers or ()
            if str(w.get("role") or "") == "node-worker"
            and "cpu_compute" in _classes_of(w) and _libs_of(w)]


def capable(fn: str, workers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compute workers that have everything `fn` needs."""
    need = requirements(fn)
    return [w for w in compute_workers(workers) if all(_libs_of(w).get(m) for m in need)]


def decide(fn: str, workers: Iterable[Dict[str, Any]], *, host_fallback: bool,
           is_sandbox: bool = False) -> Tuple[str, str]:
    """('node' | 'host' | 'refuse', why) for one job.

    A job is sent to ONE node chosen by plan(), so it needs one worker that can
    run it - not all of them. Whether that node has room right now is plan()'s
    question; this one is whether any node could ever take it."""
    if fn not in FUNCTIONS:
        return "refuse", f"unknown compute function {fn!r}"
    if is_sandbox:
        # a dev sandbox has its own Redis: no node worker reads it
        return "host", "dev sandbox - no node workers on its Redis"
    ws = compute_workers(workers)
    need = requirements(fn)
    ok = capable(fn, ws)
    if ok:
        return "node", f"{len(ok)} of {len(ws)} compute worker(s) can run it"
    if ws:
        why = f"no compute worker has {'/'.join(need)}"
    else:
        why = "no node worker has the CPU compute class"
    if host_fallback:
        return "host", why + " - host fallback is on"
    return "refuse", why + " (turn on host fallback to run it on the Vera host)"


# ── scheduling: which node, and is there room ─────────────────────────────────
#: A node worker's own queue: the host sends a job to the node it chose.
NODE_STREAM = "vera:tasks:node:host:{host_id}"


def node_stream(host_id: str) -> str:
    return NODE_STREAM.format(host_id=host_id)


def _f(w: Dict[str, Any], k: str, default: float = 0.0) -> float:
    try:
        v = w.get(k)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def capacity(w: Dict[str, Any]) -> Dict[str, float]:
    """A worker's node, as its last metrics tick saw it: cores, free cores
    (from the CPU % across all cores) and free memory (available, not just
    unused - page cache counts as free)."""
    cores = _f(w, "ncpu") or 1.0
    # the node's own cores averaged over a minute when the worker reports them (P0-1/P0-2), else the old reading
    pinned = str(w.get("cpu_scope") or "") == "pinned" and w.get("cpu_pct_avg") not in (None, "")
    busy = max(0.0, min(100.0, _f(w, "cpu_pct_avg") if pinned else _f(w, "cpu_pct"))) / 100.0
    mem_total = _f(w, "ram_total_gb")
    mem_free = _f(w, "mem_avail_gb") or max(0.0, mem_total - _f(w, "ram_used_gb"))
    return {"cores": cores, "cpu_free": round(cores * (1.0 - busy), 2),
            "mem_free_gb": round(mem_free, 2), "mem_total_gb": round(mem_total, 2),
            "load1": _f(w, "load1"), "scope": "pinned" if pinned else "host"}


def admit(fn: str, w: Dict[str, Any], cfg: Dict[str, Any],
          reserved: Optional[Dict[str, float]] = None, *,
          now: Optional[float] = None) -> Tuple[bool, str, float]:
    """(room for this job on this worker's node?, why, score). The score ranks
    admitted nodes: the smaller of the CPU and memory headroom left after the
    job, in units of what the job needs."""
    need = NEEDS.get(fn, {"cpu": 1.0, "mem_gb": 1.0})
    status = str(w.get("status") or "")
    res = reserved or {}
    slots = int(_f(w, "compute_slots") or 0)
    if slots > 0:
        # a worker that runs compute jobs side by side (MW-7): a free slot, and
        # then free CPU / memory below, decide
        busy = max(int(res.get("jobs", 0) or 0), int(_f(w, "compute_running") or 0))
        if busy >= slots:
            return False, f"all {slots} compute slots in use", 0.0
        if status == "starting":
            return False, "worker starting", 0.0
    elif status != "idle":
        return False, f"worker busy ({status or 'unknown'})", 0.0
    if now is not None and _f(w, "metrics_at") and now - _f(w, "metrics_at") > METRICS_STALE_S:
        return False, "no fresh metrics", 0.0
    if slots <= 0 and res.get("jobs", 0) >= 1:
        # a worker on older code runs one task at a time: a second job sent
        # now would only queue behind the first
        return False, "a job was just sent to it", 0.0
    cap = capacity(w)
    r_cpu = float(cfg.get("reserve_cpu", 2.0))
    r_mem = float(cfg.get("reserve_mem_gb", 8.0))
    rc, rm = reserved_now(res, _f(w, "metrics_at"), cap.get("scope", "host"))
    # what the node reserved for itself (P0d): on top, for the task's whole run - at 0.25 cores a crawl batch's own
    # CPU may show in a pinned node's average too, a small double count accepted rather than special-cased
    xc, xm, xn = float(res.get("extra_cpu") or 0.0), float(res.get("extra_mem_gb") or 0.0), int(res.get("extra_n") or 0)
    cpu_left = cap["cpu_free"] - r_cpu - rc - xc
    mem_left = cap["mem_free_gb"] - r_mem - rm - xm
    own = (f", {'/'.join(res.get('extra_caps') or []) or 'node'} {xn} task(s) holding {xc:.2f} cores / {xm:.2f} GB"
           if xn else "")
    # P0-8: a refusal names everything it counted - the reading, its kind, and what this host has in flight there
    src = "own cores, 60 s avg" if cap.get("scope") == "pinned" else "host reading"
    inflight = f", {rc:.0f} held by {int(res.get('jobs', 0) or 0)} job(s) sent here" if rc else ""
    if cpu_left < need["cpu"]:
        return False, (f"CPU: {cap['cpu_free']:.1f} of {cap['cores']:.0f} cores free ({src}){inflight}{own}; "
                       f"needs {need['cpu']:.1f} + {r_cpu:.0f} kept spare"), 0.0
    if mem_left < need["mem_gb"]:
        held = f", {rm:.0f} GB held by jobs sent here" if rm else ""
        return False, (f"memory: {cap['mem_free_gb']:.0f} GB free{held}{own}; needs "
                       f"{need['mem_gb']:.0f} + {r_mem:.0f} GB kept spare"), 0.0
    score = min(cpu_left / need["cpu"], mem_left / need["mem_gb"])
    return True, f"{cap['cpu_free']:.1f} cores, {cap['mem_free_gb']:.0f} GB free", score


def plan(fn: str, workers: Iterable[Dict[str, Any]], cfg: Dict[str, Any], *,
         reserved: Optional[Dict[str, Dict[str, float]]] = None,
         now: Optional[float] = None, exclude: Iterable[str] = ()) -> Dict[str, Any]:
    """Where one job goes now: {pick (worker | None), why, nodes: [{host, ok,
    why}]}. `reserved` is per host_id: what this host already sent there that
    the node's metrics may not show yet. `exclude`: host_ids to skip (a node
    that just lost this job)."""
    ex = set(exclude or ())
    rows, best, best_score = [], None, -1.0
    for w in capable(fn, workers):
        hid = str(w.get("host_id") or "")
        if not hid:
            rows.append({"host": w.get("host"), "ok": False,
                         "why": "worker reports no host id (older code)"})
            continue
        if hid in ex:
            rows.append({"host": w.get("host"), "host_id": hid, "ok": False, "why": "excluded"})
            continue
        ok, why, score = admit(fn, w, cfg, (reserved or {}).get(hid), now=now)
        rows.append({"host": w.get("host"), "host_id": hid, "ok": ok, "why": why})
        if ok and score > best_score:
            best, best_score = w, score
    if best is not None:
        return {"pick": best, "why": f"{best.get('host')}: room now", "nodes": rows}
    why = "; ".join(f"{r['host']}: {r['why']}" for r in rows) or "no capable node"
    return {"pick": None, "why": why, "nodes": rows}


def liveness(*, now: float, dispatched_at: float, hb_seen_at: float,
             hb_alive: bool, workers_alive: int) -> Tuple[str, str]:
    """('wait' | 'lost' | 'orphan', why) while a job has no done record."""
    if hb_alive:
        return "wait", "running"
    if hb_seen_at:
        if now - hb_seen_at > HEARTBEAT_LOST:
            return "lost", f"no heartbeat for {int(now - hb_seen_at)} s"
        return "wait", "heartbeat late"
    if workers_alive <= 0 and now - dispatched_at > QUEUE_ORPHAN:
        return "orphan", "queued, and no compute worker has been alive to take it"
    return "wait", "queued"


# ── the wire format: arrays and bytes ride as .npy entries, the rest as JSON ──
_ND = "__nd__"
_BY = "__bytes__"


def pack(obj: Any) -> bytes:
    """Inputs/outputs to bytes. numpy arrays and bytes travel as array entries
    of an .npz (allow_pickle off on both ends); everything else must be JSON.
    No pickle: a node never unpickles what the host sends, nor the reverse."""
    import numpy as np
    arrays: Dict[str, Any] = {}

    def enc(v):
        if isinstance(v, np.ndarray):
            if v.dtype == object:
                raise TypeError("object arrays cannot travel")
            name = f"a{len(arrays)}"
            arrays[name] = v
            return {_ND: name}
        if isinstance(v, (bytes, bytearray, memoryview)):
            name = f"a{len(arrays)}"
            arrays[name] = np.frombuffer(bytes(v), dtype=np.uint8)
            return {_BY: name}
        if isinstance(v, dict):
            return {str(k): enc(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [enc(x) for x in v]
        if isinstance(v, np.generic):
            return v.item()
        return v

    manifest = json.dumps(enc(obj))
    arrays["__manifest__"] = np.frombuffer(manifest.encode("utf-8"), dtype=np.uint8)
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def unpack(blob: bytes) -> Any:
    import numpy as np
    with np.load(io.BytesIO(blob), allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    manifest = json.loads(bytes(arrays.pop("__manifest__")).decode("utf-8"))

    def dec(v):
        if isinstance(v, dict):
            if len(v) == 1 and _ND in v:
                return arrays[v[_ND]]
            if len(v) == 1 and _BY in v:
                return bytes(arrays[v[_BY]])
            return {k: dec(x) for k, x in v.items()}
        if isinstance(v, list):
            return [dec(x) for x in v]
        return v

    return dec(manifest)


# ── reading a pickle a node wrote ─────────────────────────────────────────────
# Two outputs are pickles by nature: a torch checkpoint (torch.save) and a
# fitted scikit-learn model. The nodes' venvs carry numpy 2, whose pickles name
# `numpy._core`; a numpy 1 host has only `numpy.core` and cannot load them.
# numpy 2 reads numpy 1 pickles, so only this direction needs help.

def _numpy_major() -> int:
    try:
        import numpy
        return int(str(numpy.__version__).split(".")[0])
    except Exception:
        return 0


def compat_module_name(module: str, numpy_major: int) -> str:
    """The module a pickle's global resolves to on this host."""
    if numpy_major and numpy_major < 2 and (module == "numpy._core"
                                            or module.startswith("numpy._core.")):
        return "numpy.core" + module[len("numpy._core"):]
    return module


def compat_pickle_module():
    """A pickle module (for torch.load(pickle_module=...) or .loads) that reads
    a numpy 2 pickle on a numpy 1 host."""
    import pickle
    import types
    major = _numpy_major()

    class Unpickler(pickle.Unpickler):
        def find_class(self, module, name):
            return super().find_class(compat_module_name(module, major), name)

    mod = types.ModuleType("vera_compat_pickle")
    mod.__dict__.update({k: getattr(pickle, k) for k in dir(pickle) if not k.startswith("__")})
    mod.Unpickler = Unpickler
    mod.load = lambda f, **kw: Unpickler(f, **kw).load()
    mod.loads = lambda b, **kw: Unpickler(io.BytesIO(b), **kw).load()
    return mod


def ml_pins(versions: Dict[str, str]) -> Dict[str, str]:
    """pip pins for a worker's training libraries from the host's versions
    (`{"torch": "2.7.1+cu126", "scikit-learn": "1.7.1"}`): a model the node
    pickles must load on the host, so the libraries match. The local build tag
    goes - the worker takes torch's CPU build of the same release."""
    out = {}
    for pkg in ("torch", "scikit-learn", "onnxruntime"):
        v = str((versions or {}).get(pkg) or "").split("+", 1)[0].strip()
        if v and all(p.isdigit() for p in v.split(".")):
            out[pkg] = v
    return out


def job_record(job: str, fn: str, *, where: str, why: str, label: str = "",
               state: str = "queued", **extra) -> Dict[str, Any]:
    rec = {"job": job, "fn": fn, "where": where, "why": why, "label": label,
           "state": state}
    rec.update(extra)
    return rec


async def resolve_blob_refs(inputs: Dict[str, Any], get) -> Optional[str]:
    """Replace `_blob_refs` ({name: redis key}) in `inputs` with the stored
    bytes, using `get` (an async Redis get). Returns an error, or None. One
    place for the worker path and the host fallback - the host path used to
    hand a function the bare key (2026-10-11)."""
    for name, k in (inputs.pop("_blob_refs", None) or {}).items():
        data = await get(str(k))
        if data is None:
            return f"input {name!r} expired or was withdrawn"
        inputs[name] = data
    return None
