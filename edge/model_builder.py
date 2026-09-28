"""
model_builder.py  -  fills the shared specialist-model store, on demand
=====================================================================

Runs on ONE box: the builder CT (vera-model-builder), which mounts the shared
store (/tank_sdh/vera-store/models on the Proxmox host) READ-WRITE at
/opt/vera-store/models. The serving nodes mount the same store read-only, so
this is the only place a model is ever downloaded or exported; every node then
reads the same bytes.

    python model_builder.py serve --host 0.0.0.0 --port 8773

Deployed by `provision.deploy(component="model_builder")`; driven by Vera's
`specialist.install` / `specialist.jobs` / `specialist.store`.

JOB KINDS
---------
nlp_export    export a Hugging Face model to ONNX for one nlp_server task, with
              the same exporter that built the store (nlp_export_models.
              build_one), into <store>/nlp/<slug>, then MERGE its package into
              <store>/nlp/manifest.json (a partial build must never drop the
              other models' entries).
hf_snapshot   download a Hugging Face repo into <store>/<family>/<slug>
              (GLiNER, diffusers checkpoints ...), optionally filtered.
whisper       fetch an openai-whisper checkpoint into <store>/whisper.
url_fetch     fetch fixed files into <store>/<family>/<slug>. Vera only sends
              URLs from its own curated catalog; there is no free-form URL path.

One job at a time (a CPU export of a large model takes the whole CT), in
arrival order. Jobs and their logs live in memory: a restart forgets finished
jobs, and the store itself is the record of what was built.

NO `from __future__ import annotations` (see nlp_server.py: FastAPI would bind
the request bodies as query parameters).
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

STORE = os.getenv("VERA_STORE_DIR", "/opt/vera-store/models")
#: Families the builder may write. Anything else is refused: the store also
#: holds ollama's blobs, which this process must never touch.
FAMILIES = ("nlp", "whisper", "sd", "tts", "gliner", "spacy", "hf")
MAX_LOG = 400

_JOBS: Dict[str, Dict[str, Any]] = {}
_ORDER: List[str] = []
_LOCK = threading.Lock()
_WAKE = threading.Event()


def slug(model_id: str) -> str:
    return str(model_id).replace("/", "__")


def family_dir(family: str) -> str:
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}")
    return os.path.join(STORE, family)


def _dir_size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1e6, 1)


# ── component version (same record provision.deploy writes) ──────────────────
_COMPONENT = None


def component_record() -> Dict[str, Any]:
    global _COMPONENT
    if _COMPONENT is None:
        try:
            with open(os.path.join(_HERE, "model_builder.version.json"), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            rec = {}
        if not isinstance(rec, dict) or not rec.get("version"):
            _COMPONENT = {"version": "", "intact": None,
                          "note": "deployed before component versions"}
        else:
            changed = []
            for name, digest in (rec.get("files") or {}).items():
                if name.startswith("<"):
                    continue
                try:
                    with open(os.path.join(_HERE, name), "rb") as fh:
                        ok = hashlib.sha256(fh.read()).hexdigest() == digest
                except OSError:
                    ok = False
                if not ok:
                    changed.append(name)
            _COMPONENT = {"version": rec["version"], "intact": not changed, "changed": changed}
    return _COMPONENT


# ── store inventory ───────────────────────────────────────────────────────────
def store_inventory() -> Dict[str, Any]:
    out: Dict[str, Any] = {"store": STORE, "families": {}}
    try:
        st = shutil.disk_usage(STORE)
        out["free_gb"] = round(st.free / 1e9, 1)
    except OSError as e:
        out["error"] = str(e)
    for fam in FAMILIES:
        d = os.path.join(STORE, fam)
        rows = []
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for name in names:
            p = os.path.join(d, name)
            if not os.path.isdir(p) or name.startswith("."):
                continue
            rows.append({"name": name, "model": name.replace("__", "/"),
                         "size_mb": _dir_size_mb(p),
                         "onnx": any(f.endswith(".onnx") for f in os.listdir(p)),
                         "mtime": int(os.path.getmtime(p))})
        out["families"][fam] = rows
    # whisper keeps loose checkpoints (<name>.pt) at the family root
    try:
        out["whisper_files"] = sorted(f for f in os.listdir(os.path.join(STORE, "whisper"))
                                      if f.endswith(".pt"))
    except OSError:
        out["whisper_files"] = []
    try:
        with open(os.path.join(STORE, "nlp", "manifest.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        out["nlp_manifest"] = {"built_at": m.get("built_at"),
                               "models": [{k: r.get(k) for k in ("task", "model", "status", "dir")}
                                          for r in (m.get("models") or [])]}
    except (OSError, ValueError):
        out["nlp_manifest"] = None
    return out


# ── jobs ─────────────────────────────────────────────────────────────────────
class _JobLog(io.TextIOBase):
    """Captures a job's stdout/stderr into its log (last MAX_LOG lines)."""

    def __init__(self, job: Dict[str, Any]):
        self.job = job
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.job["log"].append(line[-500:])
                del self.job["log"][:-MAX_LOG]
        return len(s)


def submit(spec: Dict[str, Any]) -> Dict[str, Any]:
    kind = spec.get("kind")
    if kind not in ("nlp_export", "hf_snapshot", "whisper", "url_fetch"):
        raise ValueError("kind must be nlp_export|hf_snapshot|whisper|url_fetch")
    family = {"nlp_export": "nlp", "whisper": "whisper"}.get(kind) or spec.get("family", "")
    family_dir(family)                       # validates
    if not spec.get("model"):
        raise ValueError("model is required")
    job = {"id": uuid.uuid4().hex[:12], "kind": kind, "family": family,
           "model": spec["model"], "spec": spec, "state": "queued",
           "queued_at": time.time(), "started_at": None, "ended_at": None,
           "result": None, "error": "", "log": []}
    with _LOCK:
        _JOBS[job["id"]] = job
        _ORDER.append(job["id"])
        del _ORDER[:-200]
    _WAKE.set()
    return job


def _next_job() -> Optional[Dict[str, Any]]:
    with _LOCK:
        for jid in _ORDER:
            j = _JOBS.get(jid)
            if j and j["state"] == "queued":
                j["state"] = "running"
                j["started_at"] = time.time()
                return j
    return None


def _merge_nlp_manifest(result: Dict[str, Any], package: Optional[Dict[str, Any]],
                        package_error: str) -> None:
    """Add/replace ONE model's entries in the store's manifest.json, keeping
    everything else - the exporter's own `build` rewrites the whole file with
    only what that run built."""
    path = os.path.join(STORE, "nlp", "manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        m = {"schema": "vera.nlp-export-manifest/v2", "models": [],
             "model_packages": {}, "package_errors": {}}
    models = [r for r in (m.get("models") or [])
              if not (r.get("task") == result["task"] and r.get("model") == result["model"])]
    models.append(result)
    m["models"] = models
    # model_packages is keyed by task: it describes what the node serves for
    # that task, so it only moves when the registry model for the task is built.
    if package is not None:
        m.setdefault("model_packages", {})[result["task"]] = package
        (m.get("package_errors") or {}).pop(result["task"], None)
    elif package_error:
        m.setdefault("package_errors", {})[result["task"]] = package_error
    m["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2)
    os.replace(tmp, path)


def _run_nlp_export(job: Dict[str, Any]) -> Dict[str, Any]:
    import nlp_export_models as exp
    task, model = job["spec"].get("task", ""), job["model"]
    if task not in exp.TASK_KIND:
        raise ValueError(f"unknown nlp task {task!r} (known: {sorted(exp.TASK_KIND)})")
    out_root = family_dir("nlp")
    result = exp.build_one(task, model, out_root)
    if result.get("status") != "ok":
        raise RuntimeError(result.get("error") or result.get("status") or "export failed")
    package, perr = None, ""
    if exp.DEFAULT_MODELS.get(task) == model and result.get("dir"):
        try:
            from vmodels.nlp_inventory import package_nlp_directory
            from importlib import metadata
            package = package_nlp_directory(
                task=task, model=model, kind=result.get("kind") or "unknown",
                directory=os.path.join(out_root, result["dir"]),
                framework_version=metadata.version("onnxruntime")).to_dict()
        except Exception as e:
            perr = f"{type(e).__name__}: {e}"
    _merge_nlp_manifest(result, package, perr)
    return result


def _run_hf_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    from huggingface_hub import snapshot_download
    spec = job["spec"]
    target = os.path.join(family_dir(job["family"]), slug(job["model"]))
    os.makedirs(target, exist_ok=True)
    path = snapshot_download(repo_id=job["model"], revision=spec.get("revision") or None,
                             local_dir=target,
                             allow_patterns=spec.get("allow_patterns") or None,
                             ignore_patterns=spec.get("ignore_patterns") or None)
    return {"dir": os.path.relpath(path, STORE), "size_mb": _dir_size_mb(target)}


def _run_whisper(job: Dict[str, Any]) -> Dict[str, Any]:
    import whisper
    name = job["model"]
    if name not in whisper._MODELS:
        raise ValueError(f"unknown whisper model {name!r} (known: {sorted(whisper._MODELS)})")
    root = family_dir("whisper")
    path = whisper._download(whisper._MODELS[name], root, False)
    return {"file": os.path.relpath(path, STORE), "size_mb": round(os.path.getsize(path) / 1e6, 1)}


def _run_url_fetch(job: Dict[str, Any]) -> Dict[str, Any]:
    target = os.path.join(family_dir(job["family"]), slug(job["model"]))
    os.makedirs(target, exist_ok=True)
    got = []
    for url in job["spec"].get("urls") or []:
        dest = os.path.join(target, os.path.basename(url.split("?", 1)[0]))
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            print(f"HAVE {os.path.basename(dest)}")
        else:
            print(f"GET  {url}")
            tmp = dest + ".part"
            urllib.request.urlretrieve(url, tmp)
            os.replace(tmp, dest)
        got.append(os.path.basename(dest))
    return {"dir": os.path.relpath(target, STORE), "files": got, "size_mb": _dir_size_mb(target)}


_RUNNERS = {"nlp_export": _run_nlp_export, "hf_snapshot": _run_hf_snapshot,
            "whisper": _run_whisper, "url_fetch": _run_url_fetch}


def _worker():
    while True:
        job = _next_job()
        if job is None:
            _WAKE.wait(5)
            _WAKE.clear()
            continue
        sink = _JobLog(job)
        try:
            with redirect_stdout(sink), redirect_stderr(sink):
                job["result"] = _RUNNERS[job["kind"]](job)
            job["state"] = "done"
        except Exception as e:
            job["state"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            job["log"].extend(traceback.format_exc().splitlines()[-20:])
        job["ended_at"] = time.time()


def job_view(job: Dict[str, Any], log_lines: int = 40) -> Dict[str, Any]:
    v = {k: job[k] for k in ("id", "kind", "family", "model", "state", "queued_at",
                             "started_at", "ended_at", "result", "error")}
    v["task"] = job["spec"].get("task", "")
    v["log"] = job["log"][-log_lines:] if log_lines else []
    return v


# ── HTTP ─────────────────────────────────────────────────────────────────────
def build_app():
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    app = FastAPI(title="vera-model-builder")

    class JobReq(BaseModel):
        kind: str
        model: str
        family: str = ""
        task: str = ""
        revision: str = ""
        allow_patterns: Optional[List[str]] = None
        ignore_patterns: Optional[List[str]] = None
        urls: Optional[List[str]] = None

    @app.get("/health")
    async def health():
        running = [j["id"] for j in _JOBS.values() if j["state"] == "running"]
        queued = [j["id"] for j in _JOBS.values() if j["state"] == "queued"]
        inv = {}
        try:
            inv["free_gb"] = round(shutil.disk_usage(STORE).free / 1e9, 1)
            inv["writable"] = os.access(os.path.join(STORE, "nlp"), os.W_OK)
        except OSError as e:
            inv["error"] = str(e)
        return {"ok": True, "service": "vera-model-builder", "store": STORE,
                "component": component_record(), "running": running, "queued": queued, **inv}

    @app.get("/store")
    async def store():
        return store_inventory()

    @app.post("/jobs")
    async def jobs_submit(req: JobReq):
        try:
            job = submit(req.model_dump() if hasattr(req, "model_dump") else req.dict())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "job": job_view(job, 0)}

    @app.get("/jobs")
    async def jobs_list():
        with _LOCK:
            ids = list(reversed(_ORDER))
        return {"jobs": [job_view(_JOBS[i], 3) for i in ids if i in _JOBS]}

    @app.get("/jobs/{job_id}")
    async def jobs_get(job_id: str):
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="no such job")
        return job_view(job, MAX_LOG)

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8773)
    sub.add_parser("store")
    args = ap.parse_args()
    if args.cmd == "store":
        print(json.dumps(store_inventory(), indent=1))
        return 0
    if args.cmd == "serve":
        import uvicorn
        threading.Thread(target=_worker, name="builder-jobs", daemon=True).start()
        uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
