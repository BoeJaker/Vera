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
}

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
}

#: What a worker reports it can run (registration field `libs`).
PROBED_LIBS = ("numpy", "torch", "sklearn", "scipy")


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


def merge_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (raw or {}).items():
        if k == "host_fallback":
            cfg[k] = bool(v)
        elif k == "threads":
            try:
                cfg[k] = max(1, min(32, int(v)))
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


def decide(fn: str, workers: Iterable[Dict[str, Any]], *, host_fallback: bool,
           is_sandbox: bool = False) -> Tuple[str, str]:
    """('node' | 'host' | 'refuse', why) for one job.

    A node is chosen when every live compute worker can run the function: the
    stream hands a job to whichever worker reads first, so one worker without
    torch would fail a worldview job it happened to take. Busy workers count -
    the job waits in the stream, which is the point of a queue."""
    if fn not in FUNCTIONS:
        return "refuse", f"unknown compute function {fn!r}"
    if is_sandbox:
        # a dev sandbox has its own Redis: no node worker reads it
        return "host", "dev sandbox - no node workers on its Redis"
    ws = compute_workers(workers)
    need = requirements(fn)
    if ws:
        lacking = sorted({str(w.get("host") or w.get("id") or "?") for w in ws
                          if not all(_libs_of(w).get(m) for m in need)})
        if not lacking:
            return "node", f"{len(ws)} compute worker(s)"
        why = f"compute worker(s) without {'/'.join(need)}: {', '.join(lacking)}"
    else:
        why = "no node worker has the CPU compute class"
    if host_fallback:
        return "host", why + " - host fallback is on"
    return "refuse", why + " (turn on host fallback to run it on the Vera host)"


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
    for pkg in ("torch", "scikit-learn"):
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
