"""Specialist models - the non-LLM models the estate runs, in one view.

    specialist.status   NLP on each node (nlp_server version vs the host's, each
                        task's model: in the store? loaded?), the GPU media
                        server(s) (STT/TTS/diffusion), and the host's entity NER
                        (spaCy / GLiNER). deep=true adds the media server's SD
                        model, LoRA and voice counts, and each media node's
                        deployed gpu_inference version over SSH.

    specialist.catalog  curated models per family (NLP task alternatives,
                        Whisper sizes, Kokoro, SD-1.x checkpoints, GLiNER),
                        each marked in use / in the store; hf=true adds a
                        Hugging Face search, flagged unvetted.
    specialist.install  put a catalog entry (or an HF pick) into the shared
                        store: a job on the builder CT, the one box with the
                        store mounted read-write.
    specialist.jobs     the builder's jobs (queued / running / done / failed + log).
    specialist.store    what the shared store holds, per family.
    specialist.node_models  models sitting in a node's OWN caches, outside the
                        store (read over SSH).

Putting a model in the store does not make a node serve it: that is choosing
the model per task / per server, a separate step. Re-provisioning nodes to the
host's version is `provision.component.sync`. Everything is reached through other capabilities, so the module never imports
a sibling capability module (see the Vera namespace trap: a second import runs
the module body again).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from fastapi.responses import HTMLResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, register_ui
from Vera.vera.catalog import specialist_catalog as _cat
from Vera.vera.catalog import specialist_core as _core
from Vera.vera.provisioning.components_core import compare_versions
from Vera.vera.research.nlp_dispatch_core import DEFAULT_MODELS, TASK_KIND

log = logging.getLogger("vera.catalog.specialist")


def _rawcap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


async def _call(name: str, **kw) -> Dict[str, Any]:
    fn = _rawcap(name)
    if not fn:
        return {"error": f"{name} unavailable"}
    try:
        return await fn(**kw) or {}
    except Exception as e:
        log.debug("specialist: %s failed: %s", name, e)
        return {"error": f"{name}: {type(e).__name__}: {e}"}


def _in_sandbox() -> bool:
    try:
        return bool(_orch.is_dev_sandbox())
    except Exception:
        return False


#: The builder's port (components_capabilities._COMPONENTS["model_builder"]).
BUILDER_PORT = 8773
BUILDER_TAG = "model-builder"


async def _builder_url() -> str:
    """The builder's base URL: VERA_MODEL_BUILDER_URL, else the stored SSH host
    tagged `model-builder` (CT 131, vera-model-builder)."""
    env = os.getenv("VERA_MODEL_BUILDER_URL", "").strip().rstrip("/")
    if env:
        return env
    res = await _call("exec.ssh.hosts.list")
    for h in res.get("hosts") or []:
        if BUILDER_TAG in (h.get("tags") or []) and h.get("host"):
            return f"http://{h['host']}:{BUILDER_PORT}"
    return ""


async def _builder(method: str, path: str, body: Dict[str, Any] = None,
                   timeout: float = 15) -> Dict[str, Any]:
    import httpx
    url = await _builder_url()
    if not url:
        return {"ok": False, "error": "no model builder: store an SSH host tagged "
                                      f"'{BUILDER_TAG}' or set VERA_MODEL_BUILDER_URL"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await (c.post(url + path, json=body) if method == "POST" else c.get(url + path))
        try:
            d = r.json()
        except Exception:
            d = {"error": r.text[:300]}
        if r.status_code != 200:
            return {"ok": False, "error": d.get("detail") or d.get("error") or f"HTTP {r.status_code}",
                    "builder": url}
        return {"ok": True, "builder": url, **(d if isinstance(d, dict) else {"data": d})}
    except Exception as e:
        return {"ok": False, "builder": url,
                "error": f"builder unreachable ({type(e).__name__}: {e}) - is model_builder "
                         f"deployed on the builder CT?"}


async def _host_versions() -> Dict[str, Dict[str, Any]]:
    out = {}
    for comp in ("nlp_server", "gpu_inference", "model_builder"):
        res = await _call("provision.component.version", component=comp)
        out[comp] = res.get("host") or {}
    return out


def _host_ner() -> Dict[str, Any]:
    """The host's in-process entity NER, from its config and what is importable.
    Deliberately NOT fabric.entity_graph.ner: that runs a self-test extraction,
    which on this 2-core host is a real inference, not a status read."""
    return {"backend": os.getenv("FABRIC_NER_BACKEND", "auto"),
            "spacy_model": os.getenv("FABRIC_NER_MODEL", "en_core_web_sm"),
            "gliner_model": os.getenv("FABRIC_GLINER_MODEL", "urchade/gliner_medium-v2.1"),
            "spacy_installed": importlib.util.find_spec("spacy") is not None,
            "gliner_installed": importlib.util.find_spec("gliner") is not None,
            "runs_on": "host (in-process, fabric entity graph)"}


async def _media_deep(row: Dict[str, Any]) -> Dict[str, Any]:
    """SD model + LoRA count and voice count from an online media server."""
    import httpx
    out: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{row['url']}/sd/capabilities")
            if r.status_code == 200:
                d = r.json() or {}
                out["sd_model"] = d.get("model", "")
                out["loras"] = d.get("loras")
                out["image_tiers"] = [k for k, v in d.items() if v is True]
            r = await c.get(f"{row['url']}/tts/voices")
            if r.status_code == 200:
                d = r.json() or {}
                out["tts_engine"] = d.get("engine", "")
                out["voices"] = len(d.get("voices") or [])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


async def _ssh_host_ids() -> Dict[str, str]:
    """LAN address -> stored SSH host id, to read a node's deployed version."""
    res = await _call("exec.ssh.hosts.list")
    return {str(h.get("host")): str(h.get("id")) for h in (res.get("hosts") or [])
            if h.get("host") and h.get("id")}


@capability(
    "specialist.status",
    http_method="GET", http_path="/specialist/status", http_tags=["models", "nlp", "media"],
    memory="off", silent=True,
    description="The non-LLM ('specialist') models across the estate, per node: NLP "
                "(nlp_server's deployed version against the version this host would "
                "deploy - current|behind|modified|unversioned - and each task's model, "
                "whether it is in the shared store and loaded), the GPU media servers "
                "(STT/TTS/diffusion services), and the host's entity NER (spaCy / "
                "GLiNER). Read-only. Inputs: refresh (bool - re-probe the NLP nodes), "
                "deep (bool - also the media server's SD model/LoRA/voice counts and "
                "each media node's gpu_inference version over SSH). Output: {ok, "
                "versions, nlp:{placement, registry, nodes}, media:{nodes}, host_ner, "
                "summary}.",
)
async def cap_specialist_status(refresh: bool = False, deep: bool = False,
                                trace_id=None) -> Dict[str, Any]:
    versions = await _host_versions()

    nlp = await _call("nlp.nodes", refresh=bool(refresh))
    nlp_rows = [_core.nlp_node_row(n, versions.get("nlp_server") or {}, compare_versions)
                for n in (nlp.get("nodes") or [])]

    media_rows = [_core.media_node_row(iid, inst) for iid, inst in
                  sorted((getattr(_orch, "MEDIA_INSTANCES", {}) or {}).items())]

    if deep:
        online = [r for r in media_rows if r["status"] == "online"]
        for r, extra in zip(online, await asyncio.gather(*(_media_deep(r) for r in online))):
            r["serves"].update(extra)
        ids = await _ssh_host_ids()
        for r in media_rows:
            hid = ids.get(urlparse(r["url"]).hostname or "")
            if not hid:
                r["component"] = {"state": "unknown", "note": "no stored SSH host"}
                continue
            v = await _call("provision.component.version", component="gpu_inference",
                            host_id=hid)
            r["component"] = {"state": v.get("state", "unknown"),
                              "version": (v.get("node") or {}).get("version", ""),
                              "changed": v.get("changed") or [],
                              "error": v.get("error", "")}

    sandbox = _in_sandbox()
    bh = await _builder("GET", "/health", timeout=5)
    builder = {"url": bh.get("builder", ""), "ok": bool(bh.get("ok")),
               "error": bh.get("error", ""), "free_gb": bh.get("free_gb"),
               "writable": bh.get("writable"), "running": bh.get("running") or [],
               "queued": bh.get("queued") or [],
               "version": (bh.get("component") or {}).get("version", "")}
    return {"ok": True, "sandbox": sandbox, "builder": builder,
            "versions": {k: v.get("version", "") for k, v in versions.items()},
            "nlp": {"placement": {"where": nlp.get("where"), "node": nlp.get("node"),
                                  "reason": nlp.get("reason"),
                                  "nlp_local": nlp.get("nlp_local")},
                    "registry": _core.registry_rows(DEFAULT_MODELS, TASK_KIND),
                    "nodes": nlp_rows, "error": nlp.get("error", "")},
            "media": {"nodes": media_rows},
            "host_ner": _host_ner(),
            "summary": _core.summarize(nlp_rows, media_rows)}


# ── the catalog and the store ─────────────────────────────────────────────────
@capability(
    "specialist.store",
    http_method="GET", http_path="/specialist/store", http_tags=["models"],
    memory="off", silent=True,
    description="What the shared specialist-model store holds, per family (nlp, whisper, "
                "sd, tts, gliner, spacy, hf): each model's directory and size, the NLP "
                "export manifest, free space. Read from the builder CT. Output: {ok, store, "
                "free_gb, families:{family:[{name, model, size_mb, onnx}]}, whisper_files, "
                "nlp_manifest}.",
)
async def cap_specialist_store(trace_id=None) -> Dict[str, Any]:
    return await _builder("GET", "/store", timeout=60)


async def _hf_search(family: str, task: str, query: str, limit: int) -> Dict[str, Any]:
    import httpx
    try:
        params = _cat.search_params(family, task=task, query=query, limit=limit)
    except ValueError as e:
        return {"error": str(e), "results": []}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://huggingface.co/api/models", params=params)
            r.raise_for_status()
            rows = r.json() or []
    except Exception as e:
        return {"error": f"Hugging Face search failed: {type(e).__name__}: {e}", "results": []}
    curated_ids = {e["id"] for e in _cat.curated(family)}
    out = []
    for m in rows:
        mid = m.get("modelId") or m.get("id") or ""
        try:
            e = _cat.hf_pick(family, mid, task=task)
        except ValueError:
            continue
        e.update(downloads=m.get("downloads"), likes=m.get("likes"),
                 curated=e["id"] in curated_ids)
        out.append(e)
    return {"results": out}


@capability(
    "specialist.catalog",
    http_method="GET", http_path="/specialist/catalog", http_tags=["models"],
    memory="off", silent=True,
    description="The specialist-model catalog. Curated entries per family - NLP "
                "alternatives per nlp_server task, Whisper sizes, Kokoro TTS, SD-1.x "
                "checkpoints, GLiNER - each marked in_use (a node loads it today) and "
                "built (already in the shared store). 'Curated' is a known-good candidate, "
                "not a claim it was exported here; the store says what was built. With "
                "hf=true (and task for nlp) adds a Hugging Face search, flagged unvetted. "
                "Inputs: family (str - nlp|whisper|tts|sd|gliner, default all), task (str - "
                "nlp task), query (str), hf (bool), limit (int=20). Output: {ok, families, "
                "entries[], hf{results[]|error}, store_error}.",
)
async def cap_specialist_catalog(family: str = "", task: str = "", query: str = "",
                                 hf: bool = False, limit: int = 20,
                                 trace_id=None) -> Dict[str, Any]:
    if family and family not in _cat.FAMILIES:
        return {"ok": False, "error": f"family must be one of {list(_cat.FAMILIES)}"}
    entries = [e for e in _cat.curated(family) if not task or e.get("task") == task]
    store = await _builder("GET", "/store", timeout=60)
    _cat.mark_built(entries, store if store.get("ok") else {})
    out: Dict[str, Any] = {"ok": True, "families": _cat.FAMILIES,
                           "nlp_tasks": sorted(_cat.NLP_TASK_PIPELINE) + ["rerank"],
                           "entries": entries,
                           "store_error": "" if store.get("ok") else store.get("error", "")}
    if hf and family:
        out["hf"] = await _hf_search(family, task, query, limit)
    return out


@capability(
    "specialist.install",
    http_method="POST", http_path="/specialist/install", http_tags=["models"],
    memory="off",
    description="Put a specialist model into the shared store - a job on the builder "
                "CT (the one box with the store mounted read-write; nodes read it ro). "
                "Either entry (a specialist.catalog id) or an unvetted Hugging Face pick: "
                "family (nlp|sd|gliner) + model (owner/name) + task (for nlp). NLP models "
                "are exported to ONNX with the exporter that built the store; others are "
                "downloaded. Does NOT switch any node to the model. Refused from a dev "
                "sandbox (the store is prod's). Output: {ok, job:{id, state, ...}, builder}.",
)
async def cap_specialist_install(entry: str = "", family: str = "", model: str = "",
                                 task: str = "", trace_id=None) -> Dict[str, Any]:
    if _in_sandbox():
        return {"ok": False, "error": "this is a dev sandbox: the shared model store is "
                                      "prod's - install from the host"}
    try:
        e = _cat.find(entry) if entry else _cat.hf_pick(family, model, task=task)
        if e is None:
            return {"ok": False, "error": f"no catalog entry {entry!r}"}
        job = _cat.job_for(e)
    except ValueError as ex:
        return {"ok": False, "error": str(ex)}
    res = await _builder("POST", "/jobs", job, timeout=30)
    if res.get("ok"):
        await _orch.emit_event({"type": "specialist.install", "entry": e["id"],
                                "vetted": bool(e.get("vetted")),
                                "job": (res.get("job") or {}).get("id")})
    return res


@capability(
    "specialist.jobs",
    http_method="GET", http_path="/specialist/jobs", http_tags=["models"],
    memory="off", silent=True,
    description="The builder's jobs, newest first (state queued|running|done|failed, "
                "result, error, last log lines); with job_id, one job and its full log. "
                "Jobs live in the builder's memory - a builder restart forgets finished "
                "ones; the store is the lasting record. Inputs: job_id (str). Output: "
                "{ok, jobs[]} | {ok, id, state, log[], ...}.",
)
async def cap_specialist_jobs(job_id: str = "", trace_id=None) -> Dict[str, Any]:
    if job_id:
        if not all(ch.isalnum() for ch in job_id):
            return {"ok": False, "error": "bad job id"}
        return await _builder("GET", f"/jobs/{job_id}")
    return await _builder("GET", "/jobs")


@capability(
    "specialist.node_models",
    http_method="GET", http_path="/specialist/node_models", http_tags=["models", "nodes"],
    memory="off", silent=True,
    description="Specialist models sitting in each node's OWN caches, outside the shared "
                "store: Hugging Face hub caches (diffusion, IP-Adapter, ControlNet ...), "
                "Whisper checkpoints, Kokoro, rembg, Coqui - with every copy's path and "
                "the total size, so duplicates across user caches show. Read over SSH. "
                "Inputs: host_ids (list - default: every Ollama node with a stored SSH "
                "credential). Output: {ok, nodes:[{host_id, host, models[], total_mb, error}]}.",
)
async def cap_specialist_node_models(host_ids: List[str] = None, trace_id=None) -> Dict[str, Any]:
    hosts = await _call("exec.ssh.hosts.list")
    known = {h.get("id"): h.get("host", "") for h in hosts.get("hosts") or []}
    if not host_ids:
        addrs = {urlparse(str(i.get("url") or "")).hostname
                 for i in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).values()}
        host_ids = [hid for hid, a in known.items() if a in addrs]
    cmd = _core.node_cache_probe_cmd()
    nodes = []
    for hid in host_ids:
        res = await _call("exec.ssh.run", command=cmd, host_id=hid, timeout=120)
        rows = _core.parse_node_cache(res.get("stdout") or "")
        nodes.append({"host_id": hid, "host": known.get(hid, ""), "models": rows,
                      "total_mb": sum(r["size_mb"] for r in rows),
                      "error": "" if res.get("ok") else str(res.get("stderr") or res.get("error") or "")[:300]})
    return {"ok": True, "nodes": nodes}


def _node_addrs() -> List[str]:
    return sorted({urlparse(str(i.get("url") or "")).hostname or ""
                   for i in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).values()} - {""})


@capability(
    "specialist.store.mount",
    http_method="POST", http_path="/specialist/store/mount", http_tags=["models", "nodes"],
    memory="off",
    description="Give every Ollama node the shared specialist-model store, READ-ONLY, at "
                f"{_core.STORE_NODE_PATH} (the Proxmox host's {_core.STORE_HOST_PATH}), so "
                "every node sees the same models. Finds each node's container on the "
                "Proxmox host by its IP, adds the bind mount on the first free mpN (never "
                "touching existing mounts), then checks inside the container whether it "
                "is live or needs a restart of the CT. A node already mounted is left "
                "alone; one mounted WRITABLE is reported, not changed. Inputs: dry_run "
                "(bool=true). Output: {ok, nodes:[{host, vmid, state, key, live, error}]}.",
)
async def cap_specialist_store_mount(dry_run: bool = True, trace_id=None) -> Dict[str, Any]:
    if not dry_run and _in_sandbox():
        return {"ok": False, "error": "this is a dev sandbox: node mounts are prod's"}
    clusters = (await _call("proxmox.cluster.list")).get("clusters") or []
    if not clusters:
        return {"ok": False, "error": "no Proxmox cluster registered"}
    out = []
    for addr in _node_addrs():
        row: Dict[str, Any] = {"host": addr}
        for cl in clusters:
            for node in (cl.get("node_hosts") or {}):
                find = await _call("proxmox.node.exec", cluster_id=cl["id"], node=node,
                                   command=f"grep -l 'ip={addr}/' /etc/pve/lxc/*.conf 2>/dev/null | head -1")
                conf = (find.get("stdout") or "").strip()
                if not conf:
                    continue
                vmid = os.path.basename(conf).split(".", 1)[0]
                cfg = await _call("proxmox.node.exec", cluster_id=cl["id"], node=node,
                                  command=f"pct config {vmid}")
                plan = _core.store_mount_plan(cfg.get("stdout") or "")
                row.update(vmid=vmid, cluster_id=cl["id"], node=node, **plan)
                if plan["state"] == "missing" and not dry_run:
                    res = await _call("proxmox.node.exec", cluster_id=cl["id"], node=node,
                                      command=f"pct set {vmid} {plan['cmd']}")
                    # exit code, not stderr: this host's pvesm prints warnings
                    # (an absent 'mypool') on every command
                    if res.get("error") or res.get("exit_code") not in (0, None):
                        row["error"] = str(res.get("error") or res.get("stderr"))[-300:]
                    else:
                        row["state"] = "added"
                if plan["state"] != "missing" or not dry_run:
                    chk = await _call("proxmox.node.exec", cluster_id=cl["id"], node=node,
                                      command=f"pct exec {vmid} -- test -d {_core.STORE_NODE_PATH}/nlp "
                                              f"&& echo LIVE || echo NOT_LIVE")
                    row["live"] = "LIVE" in (chk.get("stdout") or "")
                    if not row["live"] and row.get("state") in ("added", "mounted"):
                        row["note"] = "configured - takes effect when the CT restarts"
                break
            if "vmid" in row:
                break
        if "vmid" not in row:
            row["error"] = "no container with this IP on any registered Proxmox node"
        out.append(row)
    return {"ok": all(not r.get("error") for r in out), "dry_run": bool(dry_run), "nodes": out}


# ── the element ───────────────────────────────────────────────────────────────
_EL = Path(__file__).resolve().parent / "specialist_models_element.js"


@APP.get("/ui/elements/specialist_models.js", include_in_schema=False)
async def _specialist_element_js():
    try:
        body = _EL.read_text(encoding="utf-8")
    except OSError:
        body = "console.error('specialist_models_element.js not found')"
    return Response(body, media_type="application/javascript")


@APP.get("/specialist/panel", include_in_schema=False)
async def _specialist_panel():
    """The element as a page of its own (the registered panel embeds this)."""
    return HTMLResponse("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<script>(function(){try{var d=document.documentElement,S=window.localStorage;
var t=S.getItem('vera:ui:theme');if(t)d.setAttribute('data-theme',t);
var vf=S.getItem('vera:ui:themeVarsFor');if(t&&vf!==t)return;var v=JSON.parse(S.getItem('vera:ui:themeVars')||'null');
if(v)for(var k in v)d.style.setProperty(k,v[k]);}catch(e){}})();</script>
<title>Vera - Specialist models</title>
<style>:root{--bg:#0d0f12;--bg1:#14181d;--bg2:#1a1f26;--border:#232a33;--border2:#2e3742;--fg:#d8dde3;
--dim:#5f6975;--acc:#4a9eff;--acc2:#28c28a;--warn:#f5b341;--err:#ef5b5b}
html,body{margin:0;background:var(--bg0,var(--bg));color:var(--fg);height:100%}</style></head>
<body><vera-specialist-models></vera-specialist-models>
<script src="/ui/vera-ui.js"></script><script src="/ui/elements/specialist_models.js"></script></body></html>""")


register_ui(
    "specialist-models", "Specialist models", "◇",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/specialist/panel" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"></iframe>
</div>""",
    "",
    ui_caps=["specialist.status", "provision.component.sync", "specialist.catalog",
             "specialist.install", "specialist.jobs", "specialist.store",
             "specialist.node_models", "specialist.store.mount"],
    # an element of the Models view (and any dashboard), not a tab of its own
    mode="element",
    tab_order=75,
)
