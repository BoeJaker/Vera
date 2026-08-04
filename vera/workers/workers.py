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

from Vera.vera.capability_orchestration import (
    APP, WORKER_REGISTRY, emit_event, now_iso, register_ui, schedule,
)
import Vera.vera.capability_orchestration as _orch

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

    # Prime the CPU sampler. cpu_percent(interval=1) BLOCKS the thread with an
    # internal time.sleep(1) to measure over a window — on the event loop that
    # froze every WebSocket for 1s every 10s. interval=None is non-blocking:
    # it returns %CPU since the PREVIOUS call, and this loop's own 10s cadence
    # is the measurement window. The first priming call returns 0.0 (no baseline).
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass

    while True:
        try:
            cpu  = psutil.cpu_percent(interval=None)
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
                        # Keep SSH-provisioned entries (workers.py's own
                        # registrations — the main heartbeat loop in
                        # capability_orchestration.py refreshes its own TTL
                        # separately) alive in Redis past their 120s TTL as
                        # long as this process still has them locally; once a
                        # restart wipes WORKER_REGISTRY, this stops firing and
                        # the entry expires out cleanly instead of lingering
                        # with stale data forever.
                        meta = WORKER_META.get(wid)
                        if meta:
                            await r.hset(f"vera:workers:{wid}", mapping={
                                "host": meta.get("host", ""),
                            })
                            await r.expire(f"vera:workers:{wid}", 120)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("metrics push: %s", e)
        await asyncio.sleep(10)


async def _start_metrics():
    asyncio.create_task(_push_local_metrics())
    asyncio.create_task(_restore_worker_meta())   # hydrate persisted on/off flags

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
                                    "args_preview": dec(data.get(b"payload", data.get("payload", b"")))[:400],
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
                            "args_preview": dec(data.get(b"payload", data.get("payload", b"")))[:400],
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
    # "provisioned", not "starting" — this flow uploads code/deps but never
    # launches a persistent remote worker process, so there is no live
    # heartbeat to promote it out of a transient state. A human (or a future
    # start-process step) is what would flip this to a real running worker.
    WORKER_REGISTRY[wid] = {"worker_id":wid,"status":"provisioned","in_use":0,"current_task":"","capabilities":[]}
    WORKER_META[wid] = {"worker_class":cls,"label":label,"host":host,"ssh_port":port,"ssh_user":user,"registered_at":now_iso()}
    r = _orch.REDIS
    if r:
        try: await r.hset("vera:worker_meta", wid, json.dumps(WORKER_META[wid]))
        except Exception: pass
        # Also write the durable vera:workers:{wid} hash — obs.workers/obs.cluster
        # (and so the main dashboard's Workers tile) only ever scan THIS key, not
        # vera:worker_meta. Without it, an SSH-provisioned worker is invisible to
        # every Vera instance except the one that ran this SSE stream, and even
        # there it silently disappears once _push_local_metrics's periodic TTL
        # refresh below stops finding it after a restart. Match the field shape
        # the main worker heartbeat writes (capability_orchestration.py) so
        # obs.workers doesn't have to fall back to its setdefault() placeholders.
        try:
            await r.hset(f"vera:workers:{wid}", mapping={
                "id": wid, "status": "provisioned", "host": host,
                "capabilities": "[]", "cap_count": "0",
                "tasks_done": "0", "tasks_failed": "0",
                "started": WORKER_META[wid]["registered_at"],
                "pid": "", "current_task": "", "task_started": "",
                "ollama_instance": "",
            })
            await r.expire(f"vera:workers:{wid}", 120)
        except Exception as e:
            log.warning("worker registry push (ssh init) failed: %s", e)
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
    # Enforce the persisted on/off flag: a disabled worker is forced to
    # 'disabled' regardless of what it reports, and told to stand down so a
    # cooperative worker stops pulling jobs.
    enabled = m.get("enabled", True)
    w["enabled"] = enabled
    if not enabled:
        w["status"] = "disabled"
    return {"ok":True, "enabled": enabled}

@APP.post("/cluster/workers/{wid}/drain")
async def route_drain(wid: str):
    w = WORKER_REGISTRY.get(wid)
    if not w: return JSONResponse({"error":"not found"},404)
    w["status"] = "draining"
    await emit_event({"type":"worker.draining","worker_id":wid})
    return {"ok":True}


async def _set_worker_enabled(wid: str, enabled: bool):
    """Toggle a worker on/off. Disabled workers are marked 'disabled' (excluded
    from dispatch) and the flag is persisted to vera:worker_meta so it survives a
    reboot; the heartbeat re-applies it on every check-in."""
    m = WORKER_META.setdefault(wid, {})
    m["enabled"] = bool(enabled)
    w = WORKER_REGISTRY.get(wid)
    if w:
        w["enabled"] = bool(enabled)
        w["status"]  = "idle" if enabled else "disabled"
    r = _orch.REDIS
    if r:
        try: await r.hset("vera:worker_meta", wid, json.dumps(m))
        except Exception: pass
    await emit_event({"type": "worker.enabled" if enabled else "worker.disabled",
                      "worker_id": wid})
    return {"ok": True, "worker_id": wid, "enabled": bool(enabled)}

@APP.post("/cluster/workers/{wid}/enable")
async def route_enable(wid: str):
    return await _set_worker_enabled(wid, True)

@APP.post("/cluster/workers/{wid}/disable")
async def route_disable(wid: str):
    return await _set_worker_enabled(wid, False)


async def _restore_worker_meta():
    """Hydrate WORKER_META (incl. the persisted `enabled` flag) from Redis on
    boot, so a worker turned off before a restart stays off.

    Also re-seeds WORKER_REGISTRY for each restored id: WORKER_REGISTRY is a
    plain in-process dict (unlike WORKER_META, which is Redis-backed), so a
    restart of the instance that ran _ssh_init_gen wipes it — and with it,
    _push_local_metrics's periodic TTL refresh for that worker's
    vera:workers:{wid} hash, which then silently expires out of obs.workers
    120s later even though the SSH-provisioned host is still perfectly real.
    Re-seeding here means that as soon as this instance is back up, its own
    metrics loop picks the id back up and keeps it alive."""
    for _ in range(60):
        if _orch.REDIS:
            break
        await asyncio.sleep(1)
    r = _orch.REDIS
    if not r:
        return
    try:
        raw = await r.hgetall("vera:worker_meta")
        for k, v in (raw or {}).items():
            wid = k.decode() if isinstance(k, bytes) else k
            try:
                meta = json.loads(v.decode() if isinstance(v, bytes) else v)
            except Exception:
                continue
            WORKER_META.setdefault(wid, {}).update(meta)
            WORKER_REGISTRY.setdefault(wid, {
                "worker_id": wid, "status": "provisioned",
                "in_use": 0, "current_task": "", "capabilities": [],
            })
    except Exception as e:
        log.debug("restore worker meta: %s", e)

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
  backendBase = backendBase.replace(/\/$/, '') || window._veraBase || (window.__VERA_BASE__||('http://'+location.hostname+':8999'));
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
        "cluster.job.stop",
        # Unified provisioning (embedded Provision sub-tab) — one cap per group
        # is enough for the chat's live cap-activity mirror to route the whole
        # group's activity into this panel.
        "nodes.list", "nodes.provision", "provision.overview",
        "provision.apply", "provision.node.new", "docker.stack.deploy",
        "secprov.deploy",
    ],
    mode="tab",
    tab_order=1,
    specialist_agent="infra-operator",
    specialist_loop_profile="devops",
)

log.info("vera_workers loaded")