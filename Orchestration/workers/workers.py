"""
vera_workers.py  —  Worker Cluster Tab
=======================================
Add to _module_files in capability_orchestration.py:
    os.path.join(_here, "workers.py"),

Fixes vs previous version:
- Jobs: reads top-level Redis stream fields (id/capability/ts), not a data sub-key
- Resources: scheduled psutil collector pushes CPU/RAM/disk into vera:workers:{id}
- Auto-load: hooks the tab button click so the panel refreshes on first activation
- Totals: aggregate resource bars across all workers
"""
from __future__ import annotations
import asyncio, json, logging, os, time
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse

from Vera.Orchestration.capability_orchestration import (
    APP, WORKER_REGISTRY, emit_event, now_iso, register_ui, schedule,
)
import Vera.Orchestration.capability_orchestration as _orch

log = logging.getLogger("vera.workers")

WORKER_META: dict = {}
COMPLETED_JOBS: list = []
_VERA_VENV = os.getenv("VERA_VENV", "~/vera-env")
_CODE_PATH  = Path(__file__).parent


# ── resource metrics collector (runs on every node that loads this module) ────
async def _push_local_metrics():
    """Push this host's CPU/RAM/disk into its Redis worker entry every 10s."""
    try:
        import psutil  # type: ignore
    except ImportError:
        log.debug("psutil not installed — no local resource metrics")
        return

    while True:
        try:
            cpu  = psutil.cpu_percent(interval=1)
            ram  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            metrics = {
                "cpu_pct":      round(cpu, 1),
                "ram_used_gb":  round(ram.used  / 1e9, 2),
                "ram_total_gb": round(ram.total / 1e9, 2),
                "ram_pct":      round(ram.percent, 1),
                "disk_used_gb": round(disk.used  / 1e9, 2),
                "disk_total_gb":round(disk.total / 1e9, 2),
                "disk_pct":     round(disk.percent, 1),
            }
            r = _orch.REDIS
            if r:
                # Write into every local worker entry
                for wid in list(WORKER_REGISTRY.keys()):
                    try:
                        await r.hset(f"vera:workers:{wid}", mapping={
                            "cpu_pct":       str(metrics["cpu_pct"]),
                            "ram_used_gb":   str(metrics["ram_used_gb"]),
                            "ram_total_gb":  str(metrics["ram_total_gb"]),
                            "ram_pct":       str(metrics["ram_pct"]),
                            "disk_used_gb":  str(metrics["disk_used_gb"]),
                            "disk_total_gb": str(metrics["disk_total_gb"]),
                            "disk_pct":      str(metrics["disk_pct"]),
                        })
                    except Exception:
                        pass
        except Exception as e:
            log.debug("metrics push: %s", e)
        await asyncio.sleep(10)


async def _start_metrics():
    asyncio.create_task(_push_local_metrics())

schedule(_start_metrics, interval=999999, name="worker_metrics")
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_start_metrics())
except Exception:
    pass


# ── /cluster/jobs ──────────────────────────────────────────────────────────────
async def _load_jobs(*, limit: int = 500, offset: int = 0):
    r = _orch.REDIS
    pending = []
    if r:
        try:
            # Build set of completed and running task IDs to exclude
            running_ids = set()
            for wid, w in WORKER_REGISTRY.items():
                if w.get("current_task") and str(w.get("status","")).startswith("running"):
                    tid = w.get("current_task_id","")
                    if tid: running_ids.add(tid)
            done_ids = set(j.get("id","") for j in COMPLETED_JOBS[-500:] if j.get("id"))

            # Use xpending to get truly unacknowledged entries
            try:
                pinfo = await r.xpending(_orch.TASK_STREAM, _orch.GROUP_WORKERS)
                pending_count = pinfo.get("pending", 0) if pinfo else 0
                if pending_count > 0:
                    # Fetch detailed pending entries — get ALL of them
                    details = await r.xpending_range(
                        _orch.TASK_STREAM, _orch.GROUP_WORKERS,
                        min="-", max="+", count=min(pending_count, 1000)
                    )
                    for entry in (details or []):
                        msg_id = entry.get("message_id", b"")
                        if isinstance(msg_id, bytes): msg_id = msg_id.decode()
                        consumer = entry.get("consumer", b"")
                        if isinstance(consumer, bytes): consumer = consumer.decode()
                        idle_ms = entry.get("time_since_delivered", 0)
                        # Read the actual message data
                        msgs = await r.xrange(_orch.TASK_STREAM, min=msg_id, max=msg_id, count=1)
                        for eid, data in msgs:
                            def dec(v):
                                return v.decode() if isinstance(v, bytes) else str(v)
                            task_id = dec(data.get(b"id", data.get("id", b"")))
                            if task_id not in running_ids and task_id not in done_ids:
                                pending.append({
                                    "id":         task_id,
                                    "capability": dec(data.get(b"capability", data.get("capability", b"?"))),
                                    "ts":         dec(data.get(b"ts", data.get("ts", b""))),
                                    "trace_id":   dec(data.get(b"trace_id", data.get("trace_id", b""))),
                                    "consumer":   consumer,
                                    "idle_ms":    idle_ms,
                                    "msg_id":     msg_id,
                                })
            except Exception:
                # Fallback: xrange but filter out known running/done
                entries = await r.xrange(_orch.TASK_STREAM, count=500)
                for eid, data in entries:
                    def dec(v):
                        return v.decode() if isinstance(v, bytes) else str(v)
                    task_id = dec(data.get(b"id", data.get("id", b"")))
                    if task_id not in running_ids and task_id not in done_ids:
                        pending.append({
                            "id":         task_id,
                            "capability": dec(data.get(b"capability", data.get("capability", b"?"))),
                            "ts":         dec(data.get(b"ts", data.get("ts", b""))),
                            "trace_id":   dec(data.get(b"trace_id", data.get("trace_id", b""))),
                        })
        except Exception as e:
            log.debug("load_jobs pending: %s", e)

    running = [
        {"id": w.get("current_task_id",""), "capability": w.get("current_task",""),
         "worker_id": wid, "ts": w.get("task_started","")}
        for wid, w in WORKER_REGISTRY.items()
        if w.get("current_task") and str(w.get("status","")).startswith("running")
    ]
    done_total = len(COMPLETED_JOBS)
    done_slice = list(reversed(COMPLETED_JOBS[max(0, done_total - offset - limit):max(0, done_total - offset)]))
    return {"pending": pending, "running": running,
            "done": done_slice, "done_total": done_total,
            "pending_total": len(pending)}

@APP.get("/cluster/jobs")
async def route_jobs(limit: int = 500, offset: int = 0):
    return await _load_jobs(limit=min(limit, 2000), offset=max(offset, 0))


# ── SSE helpers ────────────────────────────────────────────────────────────────
def _evt(t, m): return f"data: {json.dumps({'type':t,'msg':m})}\n\n".encode()
def _sse(gen):  return StreamingResponse(gen, media_type="text/event-stream",
                                         headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ── SSH init ───────────────────────────────────────────────────────────────────
async def _ssh_init_gen(p):
    wid, host = p.get("worker_id",""), p.get("host","")
    port, user = int(p.get("port",22)), p.get("user","")
    auth, pkgs = p.get("auth",""), p.get("packages",[])
    cls, label = p.get("worker_class","CPU"), p.get("label", p.get("worker_id",""))
    yield _evt("info", f"SSH init {wid} ({cls}) -> {user}@{host}:{port}")
    try: import asyncssh
    except ImportError: yield _evt("err","asyncssh not installed — pip install asyncssh"); return
    kw = {"host":host,"port":port,"username":user,"known_hosts":None}
    if auth: kw["client_keys" if Path(auth).exists() else "password"] = auth
    try: conn = await asyncssh.connect(**kw)
    except Exception as e: yield _evt("err",f"SSH failed: {e}"); return
    yield _evt("ok", f"Connected to {host}")
    async def run(cmd):
        r = await conn.run(cmd, check=False); return r.stdout.strip(), r.returncode
    _, rc = await run("python3 --version 2>&1")
    if rc: yield _evt("err","Python not found"); conn.close(); return
    await run(f"python3 -m venv {_VERA_VENV} --system-site-packages 2>&1")
    yield _evt("info", f"Venv ready at {_VERA_VENV}")
    pip = f"{_VERA_VENV}/bin/pip"
    for pkg in ["fastapi","uvicorn","httpx","redis[asyncio]","asyncssh","psutil"] + list(pkgs):
        _, rc = await run(f"{pip} install --quiet {pkg} 2>&1 | tail -1")
        yield _evt("info" if rc==0 else "warn", ("+ " if rc==0 else "! ")+pkg)
    try:
        async with conn.start_sftp_client() as sftp:
            remote = f"/home/{user}/vera"
            await sftp.makedirs(remote, exist_ok=True)
            for f in _CODE_PATH.glob("*.py"):
                await sftp.put(str(f), f"{remote}/{f.name}")
                yield _evt("info", f"  up {f.name}")
        yield _evt("ok","Files uploaded")
    except Exception as e: yield _evt("warn",f"Upload: {e}")
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"http://{host}:11435/api/version")
            yield _evt("ok" if r.status_code==200 else "info",
                       f"Ollama {'v'+r.json().get('version','?') if r.status_code==200 else 'not found'}")
    except Exception: yield _evt("info","Ollama not found")
    conn.close()
    WORKER_REGISTRY[wid] = {"worker_id":wid,"status":"starting","in_use":0,"current_task":"","capabilities":[]}
    WORKER_META[wid] = {"worker_class":cls,"label":label,"host":host,"ssh_port":port,"ssh_user":user,"registered_at":now_iso()}
    r = _orch.REDIS
    if r:
        try: await r.hset("vera:worker_meta", wid, json.dumps(WORKER_META[wid]))
        except Exception: pass
    await emit_event({"type":"worker.registered","worker_id":wid,"class":cls})
    yield _evt("ok", f"Worker {wid} registered")

@APP.post("/cluster/workers/init")
async def route_init(req: Request): return _sse(_ssh_init_gen(await req.json()))


# ── capability sync ────────────────────────────────────────────────────────────
async def _sync_gen(p):
    targets, caps, extra = p.get("targets",[]), p.get("capabilities",[]), p.get("packages",[])
    yield _evt("info", f"Syncing {len(caps)} caps to {len(targets)} worker(s)")
    try: import asyncssh
    except ImportError: yield _evt("err","asyncssh not installed"); return
    files = list(_CODE_PATH.glob("*.py"))
    for tid in targets:
        m = WORKER_META.get(tid,{})
        host, user, port = m.get("host",""), m.get("ssh_user",""), int(m.get("ssh_port",22))
        if not host or not user: yield _evt("warn",f"[{tid}] No SSH info"); continue
        try: conn = await asyncssh.connect(host=host,port=port,username=user,known_hosts=None)
        except Exception as e: yield _evt("err",f"[{tid}] SSH failed: {e}"); continue
        try:
            async with conn.start_sftp_client() as sftp:
                remote = f"/home/{user}/vera"
                await sftp.makedirs(remote, exist_ok=True)
                for f in files: await sftp.put(str(f), f"{remote}/{f.name}")
            yield _evt("ok",f"[{tid}] {len(files)} files uploaded")
        except Exception as e: yield _evt("warn",f"[{tid}] {e}")
        for pkg in extra:
            r = await conn.run(f"{_VERA_VENV}/bin/pip install --quiet {pkg}", check=False)
            yield _evt("info" if r.returncode==0 else "warn", f"[{tid}] {'+ ' if r.returncode==0 else '! '}{pkg}")
        conn.close(); yield _evt("ok",f"[{tid}] Done")
        tm = WORKER_META.setdefault(tid,{})
        tm["capabilities"] = sorted(set(tm.get("capabilities",[])) | set(caps))
        r = _orch.REDIS
        if r:
            try: await r.hset("vera:worker_meta", tid, json.dumps(tm))
            except Exception: pass
    await emit_event({"type":"worker.sync_done","targets":targets})
    yield _evt("ok","Sync complete")

@APP.post("/cluster/workers/sync")
async def route_sync(req: Request): return _sse(_sync_gen(await req.json()))

@APP.post("/cluster/workers/heartbeat")
async def route_heartbeat(req: Request):
    d = await req.json(); wid = d.get("worker_id")
    if not wid: return JSONResponse({"error":"worker_id required"},400)
    w = WORKER_REGISTRY.setdefault(wid,{"worker_id":wid,"in_use":0})
    for k in ("status","current_task","in_use","capabilities","task_started","tasks_done","tasks_failed"):
        if k in d: w[k] = d[k]
    m = WORKER_META.setdefault(wid,{})
    m["last_heartbeat"] = now_iso()
    for k in ("worker_class","label","has_ollama","metrics","cpu_pct","ram_used_gb","ram_total_gb","disk_used_gb","disk_total_gb"):
        if k in d: m[k] = d[k]
    if d.get("completed_task"):
        COMPLETED_JOBS.append({"id":d.get("completed_task_id",""),"capability":d.get("completed_task_cap",""),
                                "worker_id":wid,"success":d.get("success",True),"ts":now_iso()})
        if len(COMPLETED_JOBS)>500: del COMPLETED_JOBS[:-500]
    return {"ok":True}

@APP.post("/cluster/workers/{wid}/drain")
async def route_drain(wid: str):
    w = WORKER_REGISTRY.get(wid)
    if not w: return JSONResponse({"error":"not found"},404)
    w["status"] = "draining"
    await emit_event({"type":"worker.draining","worker_id":wid})
    return {"ok":True}

@APP.delete("/cluster/workers/{wid}/remove")
async def route_remove_worker(wid: str):
    """Remove a stale/offline worker from the registry and Redis."""
    # Remove from local registry
    WORKER_REGISTRY.pop(wid, None)
    WORKER_META.pop(wid, None)
    # Remove from Redis
    r = _orch.REDIS
    if r:
        try:
            await r.delete(f"vera:workers:{wid}")
            await r.hdel("vera:worker_meta", wid)
        except Exception as e:
            log.debug("remove worker redis: %s", e)
    await emit_event({"type":"worker.removed","worker_id":wid})
    return {"ok":True}



# ── UI panel ───────────────────────────────────────────────────────────────────
# Workers & Ollama combined panel — standalone HTML served via iframe.
# Replaces the old inline Workers-only panel with a richer dashboard that
# includes Workers, Ollama cluster, and live Jobs in configurable sub-panes.

_WOL_MOUNT_JS = r"""
(function mountWolPanel() {
  var mount = document.getElementById('panel-wol');
  if (!mount || mount._wolMounted) return;
  mount._wolMounted = true;
  var frame = document.createElement('iframe');
  var backendBase = (document.getElementById('backendUrl') || {}).value || '';
  backendBase = backendBase.replace(/\/$/, '') || window._veraBase || 'http://llm.int:8999';
  frame.src = backendBase + '/ui/panels/workers-ollama';
  frame.style.cssText = 'width:100%;height:100%;border:none;display:block;background:#181614';
  frame.allow = 'clipboard-read; clipboard-write';
  mount.appendChild(frame);
  // Relay base URL and theme to the iframe
  var urlInput = document.getElementById('backendUrl');
  if (urlInput) {
    urlInput.addEventListener('change', function() {
      try { frame.contentWindow.postMessage({type:'vera:base', url: urlInput.value.replace(/\/$/, '')}, '*'); } catch(_) {}
    });
  }
})();
"""

register_ui(
    "workers-ollama",
    "Workers & Ollama",
    "",
    '<div id="panel-wol" style="height:100%;overflow:hidden;background:var(--bg0)"></div>',
    _WOL_MOUNT_JS,
    ui_caps=[
        "obs.cluster", "ollama.instances", "ollama.ping",
        "ollama.pull", "ollama.generate", "cluster.nodes",
        "cluster.jobs", "worker.init", "worker.sync", "worker.drain",
    ],
    mode="tab",
    tab_order=1,
)

log.info("vera_workers loaded")