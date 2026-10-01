"""
nodes_capabilities.py — unified Vera node estate (Iteration 1)
==============================================================
Add to _module_files in capability_orchestration.py:
    os.path.join(_here, "workers/nodes_capabilities.py"),

The Workers & Ollama surfaces grew up around *backends* (an Ollama pane, a
vLLM pane, a Docker pane, a Proxmox pane, a Provision pane…), each with its
own notion of a "host". This module inverts that: **every reachable machine
is simply a Vera NODE of varying capability**, and everything Vera can run
(inference workers, data stores, the worker agent) is a COMPONENT that can be
provisioned onto a node through whichever management plane the node offers —
Docker first, Proxmox second, plain SSH as the fallback.

Nothing here re-implements installs: every step delegates to caps that
already exist (docker.stack.deploy / docker.run / provision.install /
provision.serve / provision.connect / provision.deploy / provision.worker /
pxstore.backend.provision_vllm / pxstore.fs.sync / ollama.add_instance /
vllm.instances.add). This module contributes the *unified model* on top:

  Estate         nodes.list                 — one row per machine, with its SSH /
                                              Docker / Proxmox identities linked,
                                              detected hardware + software facts,
                                              and the ollama/vllm instances it runs
  Detection      nodes.detect / detect_all  — one SSH probe: GPU/RAM/cores/disk +
                                              docker/ollama/vllm/zfs/pve presence;
                                              feeds the catalog's NODE_HW too
  Components     nodes.components           — the unified provisionable catalog
  Provisioning   nodes.provision.plan       — resolve components → backend + steps
                 nodes.provision            — execute the plan (docker → proxmox →
                                              ssh fallback), register endpoints
  Storage        nodes.storage              — estate-wide storage: ZFS pools,
                                              datasets, NON-ZFS mounts, guest disks
                                              (Proxmox) + volumes/images (Docker)
  Backup         nodes.backup.get/.set/.run — vzdump guests to a PVE storage +
                                              tar docker volumes to a backup dir,
                                              on a configurable schedule
  Share sync     nodes.sync.get/.set/.run   — keep the pxstore share tree in sync
                                              on a configurable (default daily)
                                              schedule

Redis layout
────────────
  vera:nodes:facts        str  {ssh_host_id: {…facts, detected_at}}
  vera:nodes:sync         str  share-tree autosync config (+ last_run)
  vera:nodes:backup       str  backup config (+ last_run)
  vera:nodes:backup:log   str  JSON list of recent backup runs (capped)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex

import httpx
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, schedule,
)

log = logging.getLogger("vera.nodes")

KEY_FACTS = "vera:nodes:facts"
KEY_SYNC = "vera:nodes:sync"
KEY_BACKUP = "vera:nodes:backup"
KEY_BACKUP_LOG = "vera:nodes:backup:log"

FACTS: Dict[str, dict] = {}
_HYDRATED = {"v": False}


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────
def _redis():
    return getattr(_orch, "REDIS", None)


def _rawcap(name: str):
    """Another capability's undecorated function (no double activity records)."""
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


def _mod(name: str):
    m = sys.modules.get(name)
    if m is not None:
        return m
    for k, v in list(sys.modules.items()):
        if v is not None and k.endswith(name):
            return v
    return None


async def _hydrate() -> None:
    if _HYDRATED["v"]:
        return
    _HYDRATED["v"] = True
    r = _redis()
    if not r:
        return
    try:
        raw = await r.get(KEY_FACTS)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                FACTS.update(doc)
    except Exception as e:
        log.debug("hydrate facts: %s", e)


async def _persist_facts() -> None:
    r = _redis()
    if r:
        try:
            await r.set(KEY_FACTS, json.dumps(FACTS))
        except Exception as e:
            log.warning("persist facts: %s", e)


async def _json_cfg(key: str, default: dict) -> dict:
    out = dict(default)
    r = _redis()
    if r:
        try:
            raw = await r.get(key)
            if raw:
                out.update(json.loads(raw))
        except Exception:
            pass
    return out


async def _json_cfg_put(key: str, cfg: dict) -> None:
    r = _redis()
    if r:
        try:
            await r.set(key, json.dumps(cfg))
        except Exception as e:
            log.warning("persist %s: %s", key, e)


async def _ssh_hosts() -> List[Dict]:
    fn = _rawcap("exec.ssh.hosts.list")
    if not fn:
        return []
    try:
        return (await fn() or {}).get("hosts", []) or []
    except Exception:
        return []


async def _ssh(host_id: str, command: str, timeout: int = 60) -> Dict:
    run = _rawcap("exec.ssh.run")
    if not run:
        return {"ok": False, "error": "exec.ssh.run unavailable", "rc": -1,
                "stdout": "", "stderr": ""}
    return await run(command=command, host_id=host_id, timeout=timeout) or \
        {"ok": False, "error": "no response", "rc": -1, "stdout": "", "stderr": ""}


async def _docker_hosts() -> List[Dict]:
    fn = _rawcap("docker.hosts.list")
    if not fn:
        return []
    try:
        return (await fn() or {}).get("hosts", []) or []
    except Exception:
        return []


async def _pxstore_cfgs() -> Dict[str, dict]:
    """All pxstore cluster configs {cluster_id: cfg}."""
    r = _redis()
    out: Dict[str, dict] = {}
    if not r:
        return out
    try:
        raw = await r.hgetall("vera:pxstore:cfg")
        for cid, blob in (raw or {}).items():
            cid = cid.decode() if isinstance(cid, bytes) else cid
            try:
                blob = blob.decode() if isinstance(blob, bytes) else blob
                out[cid] = json.loads(blob)
            except Exception:
                continue
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# NODE MODEL  — merge SSH hosts, Docker hosts, Proxmox links, instances, facts
# ─────────────────────────────────────────────────────────────────────────────
def _addr_of_url(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""


async def _build_nodes() -> List[Dict]:
    await _hydrate()
    nodes: Dict[str, Dict] = {}          # node key -> node
    by_addr: Dict[str, str] = {}         # addr -> node key

    def _new(key: str, label: str, addr: str) -> Dict:
        n = {"id": key, "label": label or addr or key, "addr": addr,
             "ssh_host_id": "", "docker_host_id": "", "docker_kind": "",
             "proxmox": None, "ollama": [], "vllm": [],
             "hw": {}, "facts": {}, "tags": [], "backends": []}
        nodes[key] = n
        if addr:
            by_addr.setdefault(addr, key)
        return n

    def _find(addr: str) -> Optional[Dict]:
        k = by_addr.get(addr)
        return nodes.get(k) if k else None

    # 1) SSH hosts — the canonical identities
    for h in await _ssh_hosts():
        n = _new(h.get("id", ""), h.get("label", ""), h.get("host", ""))
        n["ssh_host_id"] = h.get("id", "")
        n["user"] = h.get("user", "")
        n["tags"] = h.get("tags", []) or []

    # 2) Docker hosts
    for d in await _docker_hosts():
        kind = d.get("kind", "local")
        target = None
        if kind == "ssh" and d.get("ssh_host_id") in nodes:
            target = nodes[d["ssh_host_id"]]
        else:
            addr = ("localhost" if kind == "local"
                    else _addr_of_url(d.get("url", "")) or d.get("id", ""))
            target = _find(addr) or _new(f"docker-{d.get('id','')}",
                                         d.get("label", ""), addr)
        target["docker_host_id"] = d.get("id", "")
        target["docker_kind"] = kind

    # 3) Ollama instances (catalog NODE_SSH mapping wins, else URL host match)
    cat = _mod("catalog_capabilities")
    node_ssh = dict(getattr(cat, "NODE_SSH", {}) or {}) if cat else {}
    node_hw = dict(getattr(cat, "NODE_HW", {}) or {}) if cat else {}
    for iid, i in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).items():
        addr = _addr_of_url(i.get("url", ""))
        target = None
        if node_ssh.get(iid) and node_ssh[iid] in nodes:
            target = nodes[node_ssh[iid]]
        target = target or _find(addr) or _new(f"inst-{iid}", i.get("label", iid), addr)
        target["ollama"].append({
            "id": iid, "label": i.get("label", iid), "url": i.get("url", ""),
            "status": i.get("status", ""), "enabled": i.get("enabled", True),
            "has_gpu": i.get("has_gpu", False),
            "models": (i.get("models") or [])[:20]})
        if node_hw.get(iid) and not target["hw"]:
            target["hw"] = {k: node_hw[iid].get(k) for k in
                            ("vram_gb", "ram_gb", "gpu_name", "gpu_count", "cpu_cores")
                            if node_hw[iid].get(k) is not None}

    # 4) vLLM instances
    vmod = _mod("vllm_capabilities")
    for iid, inst in (getattr(vmod, "VLLM_INSTANCES", {}) or {}).items() if vmod else []:
        url = getattr(inst, "url", "")
        addr = _addr_of_url(url)
        target = None
        if node_ssh.get(iid) and node_ssh[iid] in nodes:
            target = nodes[node_ssh[iid]]
        target = target or _find(addr) or _new(f"inst-{iid}",
                                               getattr(inst, "label", iid), addr)
        target["vllm"].append({
            "id": iid, "label": getattr(inst, "label", iid), "url": url,
            "status": getattr(inst, "status", ""),
            "models": (getattr(inst, "models", []) or [])[:10]})
        if node_hw.get(iid) and not target["hw"]:
            target["hw"] = {k: node_hw[iid].get(k) for k in
                            ("vram_gb", "ram_gb", "gpu_name", "gpu_count", "cpu_cores")
                            if node_hw[iid].get(k) is not None}

    # 5) Proxmox links — enrolled guests (label pve-<vmid>, tags proxmox,<cluster>)
    #    and PVE nodes themselves (pxstore node→SSH mapping).
    px = await _pxstore_cfgs()
    host_to_pve: Dict[str, Dict] = {}
    for cid, cfg in px.items():
        for pve_node, hid in (cfg.get("node_hosts") or {}).items():
            host_to_pve[hid] = {"kind": "node", "cluster_id": cid, "node": pve_node}
    # The Proxmox cluster record's node map is the one every caller shares; it wins.
    pm = sys.modules.get("proxmox_capabilities")
    if pm is not None and hasattr(pm, "_all_raw"):
        try:
            for crec in await pm._all_raw():
                for pve_node, hid in (crec.get("node_hosts") or {}).items():
                    host_to_pve[str(hid)] = {"kind": "node", "cluster_id": crec.get("id", ""),
                                             "node": pve_node}
        except Exception as e:
            log.debug("cluster record node_hosts: %s", e)
    px_ids = list(px.keys())
    for n in nodes.values():
        hid = n.get("ssh_host_id", "")
        if hid in host_to_pve:
            n["proxmox"] = host_to_pve[hid]
            continue
        # enroll labels: canonical 'pve:<vmid>@<node>', legacy 'pve-<vmid>'
        m = re.match(r"^pve[-:](\d+)(?:@([\w.-]+))?$", str(n.get("label", "")))
        tags = [str(t) for t in (n.get("tags") or [])]
        if m and "proxmox" in tags:
            cid = next((t for t in tags if t in px_ids), "")
            if not cid and len(px_ids) == 1:
                cid = px_ids[0]
            n["proxmox"] = {"kind": "guest", "cluster_id": cid,
                            "vmid": int(m.group(1)),
                            "node": m.group(2) or ""}

    # 6) Cached facts + backends summary
    for n in nodes.values():
        f = FACTS.get(n.get("ssh_host_id") or n["id"], {})
        if f:
            n["facts"] = f
            hwf = {k: f.get(k) for k in
                   ("vram_gb", "ram_gb", "gpu_name", "gpu_count", "cpu_cores",
                    "disk_total_gb", "disk_free_gb") if f.get(k) is not None}
            n["hw"] = {**hwf, **n["hw"]}
        backs = []
        if n.get("docker_host_id") or (f.get("docker_running")):
            backs.append("docker")
        if n.get("proxmox"):
            backs.append("proxmox")
        if n.get("ssh_host_id"):
            backs.append("ssh")
        n["backends"] = backs
    return sorted(nodes.values(),
                  key=lambda n: (0 if n["backends"] else 1,
                                 -(n["hw"].get("vram_gb") or 0),
                                 str(n["label"]).lower()))


@capability(
    "nodes.list",
    http_method="GET", http_path="/nodes", http_tags=["nodes"],
    memory="off", silent=True,
    description="Unified Vera node estate: one row per machine, merging the SSH "
                "credential store, Docker hosts, Proxmox links (PVE nodes + "
                "enrolled guests), Ollama + vLLM instances and cached detection "
                "facts. Every machine is 'a Vera node of varying capability'. "
                "Output: {nodes:[{id,label,addr,ssh_host_id,docker_host_id,"
                "proxmox,ollama[],vllm[],hw,facts,backends[]}]}.",
)
async def cap_nodes_list(trace_id=None) -> Dict:
    return {"nodes": await _build_nodes()}


# ─────────────────────────────────────────────────────────────────────────────
# DETECTION  — one SSH probe for hardware AND software facts
# ─────────────────────────────────────────────────────────────────────────────
_DETECT_SCRIPT = r"""
echo "OS|$(. /etc/os-release 2>/dev/null && echo "$ID $VERSION_ID")"
echo "ARCH|$(uname -m 2>/dev/null)"
echo "CPU|$(nproc 2>/dev/null)"
echo "RAM_MB|$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')"
df -P -B1G / 2>/dev/null | awk 'NR==2{print "DISK_GB|"$2"|"$4}'
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | while IFS= read -r l; do echo "GPU|$l"; done
command -v docker >/dev/null 2>&1 && echo "DOCKER|yes" || echo "DOCKER|no"
docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 && echo "DOCKER_RUN|yes" || echo "DOCKER_RUN|no"
s=$(systemctl is-active ollama 2>/dev/null); echo "OLLAMA_SVC|${s:-none}"
curl -s -m 3 http://localhost:11434/api/version >/dev/null 2>&1 && echo "OLLAMA_API|yes" || echo "OLLAMA_API|no"
s=$(systemctl is-active vera-vllm 2>/dev/null); echo "VLLM_SVC|${s:-none}"
[ -f /etc/systemd/system/vera-vllm.service ] && echo "VLLM_UNIT|yes" || echo "VLLM_UNIT|no"
command -v zfs >/dev/null 2>&1 && echo "ZFS|yes" || echo "ZFS|no"
command -v pveversion >/dev/null 2>&1 && echo "PVE|yes" || echo "PVE|no"
command -v python3 >/dev/null 2>&1 && echo "PY|$(python3 -V 2>&1 | awk '{print $2}')" || echo "PY|no"
"""


def _parse_detect(txt: str) -> Dict:
    out: Dict[str, Any] = {}
    gpus: List[str] = []
    vram = 0.0
    for ln in (txt or "").splitlines():
        if "|" not in ln:
            continue
        k, _, v = ln.partition("|")
        k, v = k.strip(), v.strip()
        try:
            if k == "OS":
                out["os"] = v
            elif k == "ARCH":
                out["arch"] = v
            elif k == "CPU" and v:
                out["cpu_cores"] = int(re.sub(r"[^\d]", "", v) or 0)
            elif k == "RAM_MB" and v:
                out["ram_gb"] = round(float(v) / 1024.0, 1)
            elif k == "DISK_GB":
                tot, _, free = v.partition("|")
                out["disk_total_gb"] = float(re.sub(r"[^\d.]", "", tot) or 0)
                out["disk_free_gb"] = float(re.sub(r"[^\d.]", "", free) or 0)
            elif k == "GPU" and v:
                parts = [p.strip() for p in v.split(",")]
                gpus.append(parts[0])
                if len(parts) > 1:
                    vram += float(re.sub(r"[^\d.]", "", parts[1]) or 0)
            elif k == "DOCKER":
                out["docker"] = v == "yes"
            elif k == "DOCKER_RUN":
                out["docker_running"] = v == "yes"
            elif k == "OLLAMA_SVC":
                out["ollama_service"] = v
            elif k == "OLLAMA_API":
                out["ollama_api"] = v == "yes"
            elif k == "VLLM_SVC":
                out["vllm_service"] = v
            elif k == "VLLM_UNIT":
                out["vllm_unit"] = v == "yes"
            elif k == "ZFS":
                out["zfs"] = v == "yes"
            elif k == "PVE":
                out["pve"] = v == "yes"
            elif k == "PY":
                out["python"] = v
        except Exception:
            continue
    if gpus:
        out["gpu_name"] = gpus[0]
        out["gpu_count"] = len(gpus)
        out["vram_gb"] = round(vram / 1024.0, 1)
    return out


@capability(
    "nodes.detect",
    http_method="POST", http_path="/nodes/detect", http_tags=["nodes"],
    memory="off",
    description="Probe a node over SSH in ONE pass: hardware (GPU/VRAM/RAM/cores/"
                "disk) + software facts (docker present+running, ollama service/"
                "API, vera-vllm unit, ZFS, Proxmox, python3). Caches the facts "
                "and feeds hardware into the model catalog for every ollama/vllm "
                "instance mapped to this host. Inputs: host_id (str! — SSH host "
                "id). Output: {ok, host_id, facts}.",
)
async def cap_nodes_detect(host_id: str = "", trace_id=None) -> Dict:
    if not host_id:
        return {"error": "host_id required"}
    await _hydrate()
    r = await _ssh(host_id, _DETECT_SCRIPT, timeout=45)
    if not (r.get("stdout") or "").strip():
        return {"error": r.get("error") or r.get("stderr", "")[:400]
                         or "no output from probe"}
    facts = _parse_detect(r.get("stdout", ""))
    facts["detected_at"] = now_iso()
    FACTS[host_id] = facts
    await _persist_facts()

    # feed the catalog's per-instance hardware store for mapped instances
    cat = _mod("catalog_capabilities")
    if cat is not None and any(k in facts for k in ("vram_gb", "ram_gb", "cpu_cores")):
        try:
            node_ssh = getattr(cat, "NODE_SSH", {}) or {}
            node_hw = getattr(cat, "NODE_HW", {}) or {}
            for iid, hid in node_ssh.items():
                if hid != host_id:
                    continue
                prev = node_hw.get(iid, {})
                for k in ("vram_gb", "ram_gb", "gpu_name", "gpu_count", "cpu_cores"):
                    if facts.get(k) is not None:
                        prev[k] = facts[k]
                prev["source"] = "auto"
                prev["detected_at"] = facts["detected_at"]
                node_hw[iid] = prev
            if hasattr(cat, "_persist") and hasattr(cat, "KEY_NODE_HW"):
                await cat._persist(cat.KEY_NODE_HW, node_hw)
        except Exception as e:
            log.debug("catalog hw sync: %s", e)

    await emit_event({"type": "nodes.detected", "host_id": host_id, "facts": facts})
    return {"ok": True, "host_id": host_id, "facts": facts}


@capability(
    "nodes.detect_all",
    http_method="POST", http_path="/nodes/detect_all", http_tags=["nodes"],
    memory="off",
    description="Run nodes.detect for every stored SSH host (bounded "
                "concurrency). Output: {ok, results:{host_id:{ok|error}}}.",
)
async def cap_nodes_detect_all(trace_id=None) -> Dict:
    hosts = await _ssh_hosts()
    sem = asyncio.Semaphore(4)
    results: Dict[str, Dict] = {}

    async def _one(hid: str):
        async with sem:
            try:
                res = await cap_nodes_detect(host_id=hid)
            except Exception as e:
                res = {"error": str(e)}
            results[hid] = ({"ok": True} if res.get("ok")
                            else {"error": res.get("error", "failed")})

    await asyncio.gather(*[_one(h.get("id", "")) for h in hosts if h.get("id")])
    return {"ok": True, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# COMPONENT CATALOG  — everything Vera can put on a node, backend-agnostic
# ─────────────────────────────────────────────────────────────────────────────
_COMPONENTS: Dict[str, Dict[str, Any]] = {
    # Inference workers
    "ollama": {
        "group": "workers", "label": "Ollama",
        "backends": ["docker", "proxmox", "ssh"], "gpu": "optional",
        "desc": "LLM inference server. Docker: vera-ollama container (GPU via "
                "--gpus all). SSH: official install script + systemd. Registered "
                "into the cluster automatically.",
    },
    "vllm": {
        "group": "workers", "label": "vLLM",
        "backends": ["docker", "proxmox", "ssh"], "gpu": "recommended",
        "needs_model": True,
        "desc": "OpenAI-compatible high-throughput server. Needs a model (pick "
                "one from the HF catalog). Weights cache under hf_home — point "
                "it at the central model store mount to share weights.",
    },
    "gpu_inference": {
        "group": "workers", "label": "SD · TTS · STT (GPU inference)",
        "backends": ["ssh"], "gpu": "required", "heavy": True,
        "desc": "Whisper STT + Stable Diffusion + TTS server (edge/"
                "GPU_inference.py). Heavy python deps — long first install.",
    },
    "onnx_runtime": {
        "group": "workers", "label": "ONNX Runtime",
        "backends": ["ssh"], "gpu": "optional",
        "desc": "Edge ONNX model server (CUDA→DML→CPU).",
    },
    "nlp_server": {
        "group": "workers", "label": "NLP (NER · classify · embed)",
        "backends": ["ssh"], "heavy": True,
        "desc": "Text NLP on the node (edge/nlp_server.py): NER, sentiment, "
                "zero-shot, QA, langid, embeddings, rerank - ONNX from the shared "
                "read-only model store. Installed as a systemd service.",
    },
    # Data stores / resources
    "redis": {"group": "stores", "label": "Redis", "backends": ["docker"],
              "desc": "Event streams, task queues, caching."},
    "postgres": {"group": "stores", "label": "PostgreSQL", "backends": ["docker"],
                 "desc": "Persistent memory + data-fabric archive."},
    "chromadb": {"group": "stores", "label": "ChromaDB", "backends": ["docker"],
                 "desc": "Vector embeddings store."},
    "neo4j": {"group": "stores", "label": "Neo4j", "backends": ["docker"],
              "desc": "Memory graph database."},
    "garage": {"group": "stores", "label": "Garage (S3)", "backends": ["docker"],
               "desc": "S3 blob store for the data fabric (ring auto-bootstrap)."},
    # Platform
    "docker": {
        "group": "platform", "label": "Docker Engine", "backends": ["ssh"],
        "desc": "Container runtime. Installed over SSH, then the node is "
                "registered as a Docker host — unlocking the docker path for "
                "every other component.",
    },
    "vera-worker": {
        "group": "platform", "label": "Vera Worker",
        "backends": ["docker", "ssh"], "gpu": "optional",
        "desc": "A Vera orchestrator joined to the cluster as a worker "
                "(docker container or native git-clone install).",
    },
    "mesh_gateway": {
        "group": "platform", "label": "Mesh Gateway", "backends": ["ssh"],
        "desc": "LAN→Vera forwarder for firewalled ESP32 mesh nodes.",
    },
}

_BACKEND_ORDER = ["docker", "proxmox", "ssh"]


@capability(
    "nodes.components",
    http_method="GET", http_path="/nodes/components", http_tags=["nodes"],
    memory="off", silent=True,
    description="Unified catalog of everything provisionable onto a node "
                "(inference workers, data stores, platform pieces) with the "
                "backends each supports (docker preferred, proxmox, ssh "
                "fallback). Output: {components:[{key,group,label,backends,"
                "gpu,needs_model,heavy,desc}]}.",
)
async def cap_nodes_components(trace_id=None) -> Dict:
    return {"components": [
        {"key": k, "group": c["group"], "label": c["label"],
         "backends": c["backends"], "gpu": c.get("gpu", ""),
         "needs_model": bool(c.get("needs_model")),
         "heavy": bool(c.get("heavy")), "desc": c["desc"]}
        for k, c in _COMPONENTS.items()]}


# ─────────────────────────────────────────────────────────────────────────────
# PROVISION PLANNING  — pick the management plane per component
# ─────────────────────────────────────────────────────────────────────────────
async def _node_by_id(node_id: str) -> Optional[Dict]:
    for n in await _build_nodes():
        if n["id"] == node_id or (node_id and n.get("ssh_host_id") == node_id):
            return n
    return None


def _resolve_backend(comp: Dict, node: Dict, want: str,
                     will_have_docker: bool) -> tuple[str, str]:
    """→ (backend, warning). 'auto' prefers docker, then proxmox, then ssh."""
    supported = comp["backends"]
    has_docker = bool(node.get("docker_host_id")) or \
        bool((node.get("facts") or {}).get("docker_running")) or will_have_docker
    is_ct = (node.get("proxmox") or {}).get("kind") == "guest"
    has_ssh = bool(node.get("ssh_host_id"))

    if want and want != "auto":
        if want not in supported:
            return "", f"{comp['label']} does not support the {want} backend"
        if want == "docker" and not has_docker:
            return "docker", "no Docker on this node yet — add the 'docker' " \
                             "component or it will be auto-installed first"
        if want == "proxmox" and not is_ct:
            return "", "proxmox backend needs the node to be an enrolled LXC guest"
        if want == "ssh" and not has_ssh:
            return "", "no SSH credential stored for this node"
        return want, ""

    for b in _BACKEND_ORDER:
        if b not in supported:
            continue
        if b == "docker" and has_docker:
            return "docker", ""
        if b == "proxmox" and is_ct:
            return "proxmox", ""
        if b == "ssh" and has_ssh:
            return "ssh", ""
    # docker-only component on a node without docker → docker with auto-install
    if "docker" in supported and has_ssh:
        return "docker", "Docker will be installed over SSH first (fallback chain)"
    return "", "no usable backend (store an SSH credential for this node first)"


async def _default_hf_home(node: Dict) -> str:
    """Central model store integration: prefer the pxstore store mount when the
    node belongs to a Proxmox cluster that has one provisioned."""
    px = await _pxstore_cfgs()
    cid = (node.get("proxmox") or {}).get("cluster_id", "")
    for c, cfg in px.items():
        if cid and c != cid:
            continue
        if cfg.get("store_mount"):
            return "/models/hf" if (node.get("proxmox") or {}).get("kind") == "guest" \
                else cfg["store_mount"] + "/hf"
    return ""


@capability(
    "nodes.provision.plan",
    http_method="POST", http_path="/nodes/provision/plan", http_tags=["nodes"],
    memory="off", silent=True,
    description="Dry-run a unified provision: resolve each requested component "
                "to a backend (docker → proxmox → ssh fallback) + the concrete "
                "delegate step, with warnings — no side effects. Inputs: node_id "
                "(str! — from nodes.list; ssh host id also accepted), components "
                "(list! of keys from nodes.components), backend (str='auto' — "
                "force docker|proxmox|ssh for all), options (dict — gpus:'all', "
                "model:'HF id' for vllm, hf_home, port overrides {component: "
                "port}, quantization, install_deps:bool, num_thread:int — a CPU "
                "Ollama node's runner threads, default 6, worker:bool=true — an "
                "Ollama node also gets a native vera-worker, worker_source "
                "'host'|'git', worker_threads:int). Output: {ok, node, "
                "steps:[{component,backend,action,warning}], warnings}.",
)
async def cap_nodes_provision_plan(node_id: str = "",
                                   components: Optional[List[str]] = None,
                                   backend: str = "auto",
                                   options: Optional[Dict] = None,
                                   trace_id=None) -> Dict:
    components = [c for c in (components or []) if c in _COMPONENTS]
    if not components:
        return {"error": "components required (see nodes.components)",
                "available": list(_COMPONENTS)}
    node = await _node_by_id(node_id)
    if not node:
        return {"error": f"node not found: {node_id}"}
    opt = options or {}
    # An Ollama node is also a Vera worker: it takes the node-safe tasks off
    # the shared stream (worker_placement_core). Native over SSH - these nodes
    # are unprivileged LXC containers, where Docker is not an option.
    # options.worker=false opts out.
    worker_added = False
    if ("ollama" in components and "vera-worker" not in components
            and opt.get("worker", True) is not False):
        components = components + ["vera-worker"]
        worker_added = True
    ports = opt.get("ports") or {}
    warnings: List[str] = []
    steps: List[Dict] = []

    # docker runtime auto-install: needed when any docker-backend step lands on
    # a node without docker
    will_have_docker = "docker" in components

    ordered = sorted(components, key=lambda c: 0 if c == "docker" else 1)
    for key in ordered:
        comp = _COMPONENTS[key]
        b, warn = _resolve_backend(comp, node, backend, will_have_docker)
        if key == "vera-worker" and worker_added:
            if node.get("ssh_host_id"):
                b, warn = "ssh", ""
            else:
                b, warn = "", ("the node worker installs natively over SSH — "
                               "store an SSH credential for this node")
        if not b:
            steps.append({"component": key, "backend": "", "action": "SKIP",
                          "warning": warn})
            warnings.append(f"{key}: {warn}")
            continue
        if warn:
            warnings.append(f"{key}: {warn}")
        if b == "docker" and not (node.get("docker_host_id")
                                  or (node.get("facts") or {}).get("docker_running")
                                  or will_have_docker):
            # prepend the implicit docker install (once)
            if not any(s["component"] == "docker" for s in steps):
                steps.insert(0, {"component": "docker", "backend": "ssh",
                                 "action": "provision.install target=docker + "
                                           "register docker host",
                                 "auto_added": True, "warning": ""})
            will_have_docker = True

        gpu_req = comp.get("gpu")
        has_gpu = bool((node.get("hw") or {}).get("vram_gb")) or \
            bool((node.get("facts") or {}).get("gpu_name"))
        if gpu_req == "required" and not has_gpu:
            warnings.append(f"{key}: needs a GPU — none detected on this node")
        if comp.get("needs_model") and not (opt.get("model") or "").strip():
            steps.append({"component": key, "backend": b, "action": "SKIP",
                          "warning": "vLLM needs a model — pick one from the "
                                     "HF catalog (options.model)"})
            warnings.append(f"{key}: model required")
            continue

        action = {
            ("ollama", "docker"): "docker.stack.deploy service=ollama"
                                  + (" gpus=" + opt.get("gpus", "")
                                     if opt.get("gpus") else "")
                                  + " → register instance",
            ("ollama", "ssh"): "provision.install target=ollama → register instance",
            ("ollama", "proxmox"): "pct exec: install ollama + enable service "
                                   "→ register instance",
            ("vllm", "docker"): f"docker.run vllm/vllm-openai --model "
                                f"{opt.get('model','')} → register instance",
            ("vllm", "ssh"): f"provision.install target=vllm → provision.serve "
                             f"{opt.get('model','')} → register instance",
            ("vllm", "proxmox"): f"pxstore.backend.provision_vllm model="
                                 f"{opt.get('model','')} → backend.switch",
            ("gpu_inference", "ssh"): "provision.deploy component=gpu_inference "
                                      "(install deps + launch)",
            ("onnx_runtime", "ssh"): "provision.deploy component=onnx_runtime",
            ("nlp_server", "ssh"): "provision.deploy component=nlp_server "
                                   "(install deps + systemd service)",
            ("mesh_gateway", "ssh"): "provision.deploy component=mesh_gateway",
            ("docker", "ssh"): "provision.install target=docker + register "
                               "docker host",
            ("vera-worker", "docker"): "provision.worker mode=docker",
            ("vera-worker", "ssh"): "provision.worker mode=native",
        }.get((key, b))
        if action is None and comp["group"] == "stores":
            action = f"docker.stack.deploy service={key}"
        steps.append({"component": key, "backend": b,
                      "action": action or f"{key} via {b}",
                      "port": ports.get(key), "warning": "",
                      **({"auto_added": True} if key == "vera-worker" and worker_added
                         else {})})

    if any(s["component"] == "vllm" and s["action"] != "SKIP" for s in steps):
        hf = opt.get("hf_home") or await _default_hf_home(node)
        if hf:
            warnings.append(f"vllm: weights cache → {hf} (central model store)")

    return {"ok": True, "node": {"id": node["id"], "label": node["label"],
                                 "addr": node["addr"],
                                 "backends": node["backends"]},
            "steps": steps, "warnings": warnings}


# ─────────────────────────────────────────────────────────────────────────────
# PROVISION EXECUTION
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Vera.vera.provisioning import ollama_node_core as _ollama_core
except Exception:                                    # worktree / app-free import
    from vera.provisioning import ollama_node_core as _ollama_core


async def _ensure_docker_host(node: Dict) -> Dict:
    """Make sure the node is a registered Docker host; install docker over SSH
    if it is missing. Returns {ok, docker_host_id} or {error}."""
    if node.get("docker_host_id"):
        return {"ok": True, "docker_host_id": node["docker_host_id"]}
    hid = node.get("ssh_host_id")
    if not hid:
        return {"error": "no docker host and no SSH credential for this node"}
    facts = node.get("facts") or {}
    if not facts.get("docker_running"):
        inst = _rawcap("provision.install")
        if not inst:
            return {"error": "provision.install unavailable"}
        res = await inst(host_id=hid, target="docker", sudo=True, timeout=900)
        if not res.get("ok"):
            return {"error": "docker install failed: "
                             + str(res.get("error", ""))[:300]}
    save = _rawcap("docker.hosts.save")
    if not save:
        return {"error": "docker.hosts.save unavailable"}
    reg = await save(kind="ssh", ssh_host_id=hid,
                     label=f"{node.get('addr','')} (node)")
    dhid = (reg.get("host") or {}).get("id") if isinstance(reg, dict) else None
    if not dhid:
        return {"error": "could not register node as a Docker host"}
    node["docker_host_id"] = dhid
    return {"ok": True, "docker_host_id": dhid, "installed": True}


async def _register_ollama(node: Dict, port: int, has_gpu: bool,
                           num_thread: Any = None) -> Dict:
    add = _rawcap("ollama.add_instance")
    if not add:
        return {"error": "ollama.add_instance unavailable"}
    addr = node.get("addr") or "localhost"
    # Reuse the id already serving this URL. ollama.add_instance keys on the id
    # alone, so a second id for one Ollama lets the GPU gate — whose capacity is
    # counted per instance id — hand the same card to two callers at once.
    plan = _ollama_core.registration_plan(
        getattr(_orch, "OLLAMA_INSTANCES", {}) or {}, addr, port, has_gpu,
        preferred_id=f"node-{re.sub(r'[^a-zA-Z0-9]+', '-', addr)}-{port}")
    # The runner thread count is recorded on the node itself: Ollama has no
    # server-side setting, so every request carries it from the registry.
    nt = _ollama_core.registration_threads(has_gpu, num_thread)
    res = await add(id=plan["instance_id"], url=plan["url"], has_gpu=has_gpu,
                    label=f"{node.get('label', addr)} (ollama)", num_thread=nt)
    return {"ok": True, "instance_id": plan["instance_id"], "url": plan["url"],
            "reused": plan["action"] == "reuse", "reason": plan["reason"],
            "num_thread": nt, "result": res}


async def _ollama_layout(reg: Dict, opt: Dict) -> Dict:
    """The rest of an Ollama node, once it is registered: the concurrency
    layout (a CPU node's 2 slots; a GPU node's CPU-only sibling on :11436,
    registered as '<id>-cpu' - the dual CPU+GPU node) and the activity tap in
    front of each. The same caps an operator runs by hand (nodes.ollama.tune,
    nodes.ollama.tap), so a node Vera provisions comes up the way the fleet
    runs. options.ollama_layout=false skips it; a step that cannot run (no
    stored SSH login for the address) says so and the install still counts."""
    if not (reg or {}).get("instance_id") or opt.get("ollama_layout", True) is False:
        return {"skipped": "ollama_layout=false" if reg.get("instance_id") else "not registered"}
    iid = reg["instance_id"]
    out: Dict[str, Any] = {}
    try:
        out["tune"] = await cap_nodes_ollama_tune(dry_run=False, instance_ids=[iid])
    except Exception as e:
        out["tune"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    ids = [iid]
    for row in (out["tune"] or {}).get("nodes") or []:
        sib = ((row.get("result") or {}).get("registered") or "")
        if sib:
            ids.append(sib)
    if opt.get("tap", True) is not False:
        try:
            out["tap"] = await cap_nodes_ollama_tap(dry_run=False, instance_ids=ids)
        except Exception as e:
            out["tap"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["instances"] = ids
    return out


async def _register_vllm(node: Dict, port: int, api_key: str = "") -> Dict:
    add = _rawcap("vllm.instances.add")
    if not add:
        return {"error": "vllm.instances.add unavailable"}
    addr = node.get("addr") or "localhost"
    iid = f"vllm-{re.sub(r'[^a-zA-Z0-9]+', '-', addr)}-{port}"
    url = f"http://{addr}:{port}"
    res = await add(id=iid, url=url, label=f"{node.get('label', addr)} (vLLM)",
                    has_gpu=True, api_key=api_key)
    return {"ok": bool(res.get("ok", True)), "instance_id": iid, "url": url,
            "result": res}


async def _prov_step(node: Dict, key: str, b: str, opt: Dict) -> Dict:
    """Execute one component provision on `node` via backend `b`."""
    comp = _COMPONENTS[key]
    ports = opt.get("ports") or {}
    gpus = opt.get("gpus", "")
    hid = node.get("ssh_host_id", "")

    # ── data stores + ollama via docker ─────────────────────────────────────
    if b == "docker" and (comp["group"] == "stores" or key == "ollama"):
        ok = await _ensure_docker_host(node)
        if not ok.get("ok"):
            return {"error": ok.get("error")}
        dep = _rawcap("docker.stack.deploy")
        if not dep:
            return {"error": "docker.stack.deploy unavailable"}
        res = await dep(host_id=ok["docker_host_id"], service=key, gpus=gpus)
        out = {"ok": bool(res.get("ok")), "deploy": res}
        if key == "ollama" and res.get("ok"):
            out["register"] = await _register_ollama(
                node, int(ports.get("ollama") or 11434), bool(gpus),
                num_thread=opt.get("num_thread"))
        return out

    # ── vLLM ─────────────────────────────────────────────────────────────────
    if key == "vllm":
        model = (opt.get("model") or "").strip()
        if not model:
            return {"error": "vLLM needs options.model (HF id from the catalog)"}
        port = int(ports.get("vllm") or 8000)
        hf_home = opt.get("hf_home") or await _default_hf_home(node)
        if b == "docker":
            ok = await _ensure_docker_host(node)
            if not ok.get("ok"):
                return {"error": ok.get("error")}
            run = _rawcap("docker.run")
            if not run:
                return {"error": "docker.run unavailable"}
            vols = (f"{hf_home}:/root/.cache/huggingface" if hf_home
                    else "vera-hf-cache:/root/.cache/huggingface")
            import os as _os
            env = {}
            tok = _os.environ.get("HF_TOKEN") or _os.environ.get("HUGGINGFACE_TOKEN")
            if tok:
                env["HUGGING_FACE_HUB_TOKEN"] = tok
            extra = "--ipc=host" + (f" --gpus {shlex.quote(gpus)}" if gpus else "")
            cmdline = f"--model {shlex.quote(model)} --host 0.0.0.0 --port 8000"
            if opt.get("quantization"):
                cmdline += f" --quantization {shlex.quote(opt['quantization'])}"
            if opt.get("extra_args"):
                cmdline += " " + str(opt["extra_args"])
            res = await run(host_id=ok["docker_host_id"],
                            image="vllm/vllm-openai:latest",
                            name=f"vera-vllm-{port}", ports=f"{port}:8000",
                            env=env, volumes=vols, extra_args=extra,
                            command=cmdline, pull=True)
            out = {"ok": bool(res.get("ok")), "run": res, "hf_home": hf_home}
            if res.get("ok"):
                out["register"] = await _register_vllm(node, port)
            return out
        if b == "proxmox":
            pmx = node.get("proxmox") or {}
            prov = _rawcap("pxstore.backend.provision_vllm")
            sw = _rawcap("pxstore.backend.switch")
            if not prov:
                return {"error": "pxstore.backend.provision_vllm unavailable"}
            pve_node = opt.get("pve_node") or pmx.get("node") or ""
            if not pve_node:
                return {"error": "options.pve_node required for the proxmox "
                                 "backend (the PVE node hosting this CT)"}
            res = await prov(cluster_id=pmx.get("cluster_id", ""),
                             node=pve_node, vmid=int(pmx.get("vmid") or 0),
                             model=model, port=port,
                             hf_home=hf_home or "/models/hf",
                             pip_spec=opt.get("pip_spec", "vllm"),
                             extra_args=str(opt.get("extra_args", "")))
            out = {"ok": bool(res.get("ok")), "provision": res}
            if res.get("ok") and sw and opt.get("start", True):
                out["switch"] = await sw(
                    cluster_id=pmx.get("cluster_id", ""), node=pve_node,
                    vmid=int(pmx.get("vmid") or 0), backend="vllm",
                    vllm_port=port)
                out["ok"] = bool(out["switch"].get("ok"))
            return out
        # ssh
        inst = _rawcap("provision.install")
        serve = _rawcap("provision.serve")
        if not (inst and serve):
            return {"error": "provision.install/serve unavailable"}
        ires = await inst(host_id=hid, target="vllm", sudo=True, timeout=1800)
        if not ires.get("ok"):
            return {"error": "vllm install failed: "
                             + str(ires.get("error", ""))[:300], "install": ires}
        extra = f"--download-dir {shlex.quote(hf_home)}" if hf_home else ""
        sres = await serve(host_id=hid, model=model, port=port, extra=extra)
        out = {"ok": bool(sres.get("ok")), "install": ires, "serve": sres,
               "hf_home": hf_home}
        if sres.get("ok"):
            out["register"] = await _register_vllm(node, port)
        return out

    # ── Ollama via ssh / proxmox ─────────────────────────────────────────────
    if key == "ollama":
        port = int(ports.get("ollama") or 11434)
        if b == "proxmox":
            pmx = node.get("proxmox") or {}
            pve_node = opt.get("pve_node") or pmx.get("node") or ""
            if not pve_node:
                return {"error": "options.pve_node required for the proxmox backend"}
            pxm = _mod("pxstore_capabilities")
            if not pxm:
                return {"error": "pxstore module not loaded"}
            # The recipe binds 0.0.0.0:port and proves the node ANSWERS there.
            # The stock unit binds loopback, so `systemctl is-active` passed for a
            # node Vera could never reach.
            r = await pxm._node_ssh(pmx.get("cluster_id", ""), pve_node,
                                    pxm._sh(pxm._pct_exec(int(pmx.get("vmid") or 0),
                                                          _ollama_core.ct_install_script(port))),
                                    timeout=900)
            active = _ollama_core.install_succeeded(r.get("stdout", ""))
            out = {"ok": active, "log": (r.get("stdout", "") or "")[-800:]}
            if active:
                out["register"] = await _register_ollama(node, port, bool(gpus),
                                                         num_thread=opt.get("num_thread"))
                out["layout"] = await _ollama_layout(out["register"], opt)
            return out
        inst = _rawcap("provision.install")
        if not inst:
            return {"error": "provision.install unavailable"}
        # Without `port`, Ollama installs on 11434 while the node is registered
        # on the port that was asked for.
        ires = await inst(host_id=hid, target="ollama", sudo=True, port=port,
                          timeout=900)
        out = {"ok": bool(ires.get("ok")), "install": ires}
        if ires.get("ok"):
            out["register"] = await _register_ollama(node, port, bool(gpus),
                                                     num_thread=opt.get("num_thread"))
            out["layout"] = await _ollama_layout(out["register"], opt)
        return out

    # ── docker runtime ───────────────────────────────────────────────────────
    if key == "docker":
        res = await _ensure_docker_host(node)
        return res if res.get("ok") else {"error": res.get("error")}

    # ── vera worker ──────────────────────────────────────────────────────────
    if key == "vera-worker":
        wk = _rawcap("provision.worker")
        if not wk:
            return {"error": "provision.worker unavailable"}
        mode = "docker" if b == "docker" else "native"
        res = await wk(host_id=hid, mode=mode, gpus=gpus,
                       image=str(opt.get("image", "")),
                       repo_url=str(opt.get("repo_url", "")),
                       source=str(opt.get("worker_source", "")),
                       threads=int(opt.get("worker_threads") or 0))
        return {"ok": bool(res.get("ok")), "worker": res}

    # ── bundled edge components over ssh ────────────────────────────────────
    if key in ("gpu_inference", "onnx_runtime", "nlp_server", "mesh_gateway"):
        dep = _rawcap("provision.deploy")
        if not dep:
            return {"error": "provision.deploy unavailable"}
        # nlp_server runs as vera-nlp_server.service on every node; redeploying
        # it any other way would leave that unit and a second copy both running.
        kwargs: Dict[str, Any] = {
            "host_id": hid, "component": key,
            "install_deps": bool(opt.get("install_deps", True)),
            "launch": True, "systemd": bool(opt.get("systemd", key == "nlp_server"))}
        if (opt.get("ports") or {}).get(key):
            kwargs["port"] = int(opt["ports"][key])
        if key == "mesh_gateway":
            kwargs["vera_url"] = str(opt.get("vera_url", ""))
        res = await dep(**kwargs)
        return {"ok": bool(res.get("ok")), "deploy": res}

    return {"error": f"no executor for {key} via {b}"}


@capability(
    "nodes.provision",
    http_method="POST", http_path="/nodes/provision", http_tags=["nodes"],
    memory="off",
    description="Execute a unified provision: run the resolved plan (see "
                "nodes.provision.plan) — Docker first, Proxmox for enrolled "
                "LXC guests, SSH as the fallback — and register every new "
                "endpoint (ollama/vllm instances, docker hosts, workers) into "
                "Vera's cluster. An Ollama node is registered with its runner "
                "thread count (CPU nodes; default 6), gets the fleet's layout - a CPU "
                "node 2 slots, a GPU node its CPU-only sibling Ollama on :11436 (the dual "
                "CPU+GPU node) - and the activity tap, unless options.ollama_layout=false, "
                "and also becomes a native Vera node worker unless options.worker=false. Inputs: node_id "
                "(str!), components (list!), backend (str='auto'), options (dict "
                "— gpus, model (HF id, required for vllm), hf_home, ports{}, "
                "quantization, pve_node, install_deps, vera_url, start, "
                "num_thread, worker, worker_source, worker_threads, ollama_layout, tap). Emits nodes.provision.progress "
                "events per step. Output: {ok, node_id, results:[{component,"
                "backend,ok,…}]}.",
)
async def cap_nodes_provision(node_id: str = "",
                              components: Optional[List[str]] = None,
                              backend: str = "auto",
                              options: Optional[Dict] = None,
                              trace_id=None) -> Dict:
    plan = await cap_nodes_provision_plan(node_id=node_id, components=components,
                                          backend=backend, options=options)
    if not plan.get("ok"):
        return plan
    node = await _node_by_id(node_id)
    opt = options or {}
    results: List[Dict] = []
    overall = True
    for step in plan["steps"]:
        key, b = step["component"], step["backend"]
        if step.get("action") == "SKIP":
            results.append({"component": key, "backend": b, "ok": False,
                            "skipped": True, "error": step.get("warning", "")})
            continue
        await emit_event({"type": "nodes.provision.progress",
                          "node_id": node["id"], "component": key,
                          "backend": b, "stage": "start"})
        try:
            res = await _prov_step(node, key, b, opt)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"}
        ok = bool(res.get("ok"))
        overall = overall and ok
        results.append({"component": key, "backend": b, "ok": ok, **res})
        await emit_event({"type": "nodes.provision.progress",
                          "node_id": node["id"], "component": key,
                          "backend": b, "stage": "done", "ok": ok,
                          "error": str(res.get("error", ""))[:300]})
        # a successful docker install unlocks the docker path for later steps
        if key == "docker" and ok:
            node["facts"] = {**(node.get("facts") or {}), "docker_running": True}
    await emit_event({"type": "nodes.provisioned", "node_id": node["id"],
                      "ok": overall,
                      "components": [r["component"] for r in results]})
    return {"ok": overall, "node_id": node["id"], "results": results,
            "warnings": plan.get("warnings", [])}


# ─────────────────────────────────────────────────────────────────────────────
# ESTATE STORAGE OVERVIEW  — Proxmox (pools/datasets/NON-ZFS mounts/guests)
#                            + Docker (volumes/images/containers)
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "nodes.storage",
    http_method="POST", http_path="/nodes/storage", http_tags=["nodes"],
    memory="off", silent=True,
    description="Estate-wide storage overview. Proxmox: per node — ZFS pools, "
                "datasets, NON-ZFS mounts (explicitly included), PVE storages "
                "and per-guest (VM/CT) disk usage via pxstore.inventory. "
                "Docker: per host — volumes (with sizes), images and container "
                "disk usage via the engine's /system/df. Inputs: cluster_id "
                "(str — blank = every saved cluster), docker (bool=true). "
                "Output: {proxmox:[{cluster_id,node,pools,datasets,mounts,"
                "storages,guests}], docker:[{host_id,volumes,images_gb,"
                "containers_gb}], errors}.",
)
async def cap_nodes_storage(cluster_id: str = "", docker: bool = True,
                            trace_id=None) -> Dict:
    out: Dict[str, Any] = {"proxmox": [], "docker": [], "errors": []}
    inv = _rawcap("pxstore.inventory")
    clist = _rawcap("proxmox.cluster.list")
    if inv and clist:
        try:
            clusters = (await clist() or {}).get("clusters", [])
        except Exception:
            clusters = []
        for c in clusters:
            cid = c.get("id", "")
            if cluster_id and cid != cluster_id:
                continue
            try:
                first = await inv(cluster_id=cid)
                if first.get("error"):
                    out["errors"].append(f"proxmox {cid}: {first['error']}")
                    continue
                for pve_node in first.get("nodes") or [first.get("node", "")]:
                    if not pve_node:
                        continue
                    r = first if pve_node == first.get("node") else \
                        await inv(cluster_id=cid, node=pve_node)
                    if r.get("error"):
                        out["errors"].append(f"{cid}/{pve_node}: {r['error']}")
                        continue
                    out["proxmox"].append({
                        "cluster_id": cid, "node": pve_node,
                        "pools": r.get("pools", []),
                        "datasets": (r.get("datasets") or [])[:200],
                        "mounts": r.get("mounts", []),        # non-ZFS included
                        "storages": r.get("storages", []),
                        "guests": [{
                            "vmid": g.get("vmid"), "name": g.get("name"),
                            "type": g.get("type"), "status": g.get("status"),
                            "disk": g.get("disk", 0),
                            "maxdisk": g.get("maxdisk", 0)}
                            for g in r.get("guests", [])],
                        "unallocated_bytes": r.get("unallocated_bytes", 0)})
            except Exception as e:
                out["errors"].append(f"proxmox {cid}: {e}")

    if docker:
        dk = _mod("docker_capabilities")
        if dk is not None:
            for d in await _docker_hosts():
                rec = dk._get_host(d.get("id", ""))
                if not rec:
                    continue
                try:
                    st, body, err = await dk._engine_request(
                        rec, "GET", "/system/df", timeout=30)
                    if st != 200:
                        out["errors"].append(
                            f"docker {d.get('id')}: {err or ('HTTP ' + str(st))}")
                        continue
                    df = json.loads(body or b"{}")
                    vols = [{"name": v.get("Name", ""),
                             "size": ((v.get("UsageData") or {}).get("Size") or 0),
                             "refs": ((v.get("UsageData") or {}).get("RefCount") or 0)}
                            for v in (df.get("Volumes") or [])]
                    vols.sort(key=lambda v: -v["size"])
                    out["docker"].append({
                        "host_id": d.get("id", ""), "label": d.get("label", ""),
                        "volumes": vols[:100],
                        "volumes_gb": round(sum(v["size"] for v in vols) / 1e9, 2),
                        "images_gb": round(sum((i.get("Size") or 0)
                                               for i in (df.get("Images") or [])) / 1e9, 2),
                        "containers_gb": round(sum((c.get("SizeRw") or 0)
                                                   for c in (df.get("Containers") or [])) / 1e9, 2)})
                except Exception as e:
                    out["errors"].append(f"docker {d.get('id')}: {e}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# BACKUP  — vzdump guests to a PVE storage + tar docker volumes to a dir
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_BACKUP = {
    "enabled": False,
    "interval_hours": 24,
    "last_run": 0,
    "proxmox": {"cluster_id": "", "storage": "", "mode": "snapshot",
                "compress": "zstd", "all": True, "vmids": []},
    "docker": {"enabled": False, "hosts": [], "dest": "/var/backups/vera",
               "include": "vera-", "exclude": ["vera-ollama"], "keep": 3},
}


@capability(
    "nodes.backup.get",
    http_method="GET", http_path="/nodes/backup", http_tags=["nodes"],
    memory="off", silent=True,
    description="Get the estate backup config + recent run log. Every schedule "
                "and each guest's latest backup together: backup.status. Output: "
                "{config, log:[…]}.",
)
async def cap_backup_get(trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_BACKUP, _DEFAULT_BACKUP)
    entries = []
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_BACKUP_LOG)
            if raw:
                entries = json.loads(raw)[-20:]
        except Exception:
            pass
    return {"config": cfg, "log": entries}


@capability(
    "nodes.backup.set",
    http_method="POST", http_path="/nodes/backup/set", http_tags=["nodes"],
    memory="off",
    description="Configure estate backups. Inputs: enabled (bool), "
                "interval_hours (int=24), proxmox (dict — cluster_id, storage "
                "(PVE storage id that accepts 'backup' content — the dedicated "
                "backup target), mode snapshot|suspend|stop, compress, all "
                "(bool), vmids (list)), docker (dict — enabled, hosts (list — "
                "blank = all), dest (dir on each host), include (name prefix), "
                "exclude (list), keep (int)). Output: {ok, config}.",
)
async def cap_backup_set(enabled: Optional[bool] = None,
                         interval_hours: Optional[int] = None,
                         proxmox: Optional[Dict] = None,
                         docker: Optional[Dict] = None, trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_BACKUP, _DEFAULT_BACKUP)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if interval_hours:
        cfg["interval_hours"] = max(1, int(interval_hours))
    if isinstance(proxmox, dict):
        cfg["proxmox"] = {**cfg.get("proxmox", {}), **proxmox}
    if isinstance(docker, dict):
        cfg["docker"] = {**cfg.get("docker", {}), **docker}
    await _json_cfg_put(KEY_BACKUP, cfg)
    return {"ok": True, "config": cfg}


async def _backup_proxmox(pcfg: Dict) -> List[Dict]:
    """vzdump every configured guest to the dedicated backup storage."""
    results: List[Dict] = []
    pm = _mod("proxmox_capabilities")
    if not pm:
        return [{"error": "proxmox module not loaded"}]
    cid = pcfg.get("cluster_id", "")
    storage = pcfg.get("storage", "")
    if not (cid and storage):
        return [{"error": "proxmox backup needs cluster_id + storage"}]
    rec = await pm._get_cluster(cid, opened=True)
    if not rec:
        return [{"error": f"cluster not found: {cid}"}]
    res, err = await pm._pve(rec, "GET", "/cluster/resources")
    if res is None:
        return [{"error": err}]
    nodes = sorted({g.get("node", "") for g in res
                    if g.get("type") in ("qemu", "lxc") and g.get("node")})
    want = [int(v) for v in (pcfg.get("vmids") or [])]
    for pve_node in nodes:
        body: Dict[str, Any] = {"storage": storage,
                                "mode": pcfg.get("mode", "snapshot"),
                                "compress": pcfg.get("compress", "zstd")}
        if pcfg.get("all", True) and not want:
            body["all"] = 1
        else:
            here = [g for g in res if g.get("node") == pve_node
                    and int(g.get("vmid", 0)) in want]
            if not here:
                continue
            body["vmid"] = ",".join(str(g["vmid"]) for g in here)
        upid, err = await pm._pve(rec, "POST", f"/nodes/{pve_node}/vzdump",
                                  data=body)
        results.append({"node": pve_node, "ok": err == "" or err is None,
                        "upid": upid if isinstance(upid, str) else "",
                        "error": err or ""})
    return results


async def _backup_docker(dcfg: Dict) -> List[Dict]:
    """Tar each matching named volume into dest on its own host (rotated)."""
    results: List[Dict] = []
    dk = _mod("docker_capabilities")
    if not dk:
        return [{"error": "docker module not loaded"}]
    hosts = dcfg.get("hosts") or [d.get("id", "") for d in await _docker_hosts()]
    include = dcfg.get("include", "vera-")
    exclude = set(dcfg.get("exclude") or [])
    dest = dcfg.get("dest", "/var/backups/vera")
    keep = max(1, int(dcfg.get("keep", 3)))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for hid in hosts:
        rec = dk._get_host(hid)
        if not rec:
            results.append({"host_id": hid, "error": "unknown docker host"})
            continue
        try:
            st, body, err = await dk._engine_request(rec, "GET", "/volumes",
                                                     timeout=20)
            vols = [v.get("Name", "") for v in
                    (json.loads(body or b"{}").get("Volumes") or [])] \
                if st == 200 else []
        except Exception as e:
            results.append({"host_id": hid, "error": f"volume list: {e}"})
            continue
        targets = [v for v in vols
                   if v.startswith(include) and v not in exclude]
        done, errs = [], []
        for vol in targets:
            # tar the volume + prune old copies beyond `keep`
            sh = (f"mkdir -p /dst && "
                  f"tar czf /dst/{vol}_{stamp}.tgz -C /src . && "
                  f"ls -1t /dst/{vol}_*.tgz 2>/dev/null | tail -n +{keep + 1} "
                  f"| xargs -r rm -f")
            args = ["run", "--rm", "-v", f"{vol}:/src:ro", "-v", f"{dest}:/dst",
                    "alpine:3.20", "sh", "-c", sh]
            try:
                argv = await dk._docker_argv(rec, args)
                ok, reason = dk._sandbox_gate(" ".join(argv))
                if not ok:
                    errs.append(f"{vol}: sandbox: {reason}")
                    continue
                r = await dk._run_local(argv, timeout=1800)
                if r.get("ok"):
                    done.append(vol)
                else:
                    errs.append(f"{vol}: "
                                + (r.get("stderr") or r.get("error") or "?")[:200])
            except Exception as e:
                errs.append(f"{vol}: {e}")
        results.append({"host_id": hid, "dest": dest, "backed_up": done,
                        "errors": errs, "ok": not errs})
    return results


@capability(
    "nodes.backup.run",
    http_method="POST", http_path="/nodes/backup/run", http_tags=["nodes"],
    memory="off",
    description="Run the estate backup now: vzdump all configured Proxmox "
                "guests (VMs + CTs) to the dedicated backup storage, and tar "
                "matching docker named volumes into the per-host backup dir "
                "(rotated, keep-N). Uses the saved config (nodes.backup.set); "
                "pass proxmox/docker dicts to override one-off. One guest now: "
                "backup.run. Output: {ok, proxmox:[…], docker:[…]}.",
)
async def cap_backup_run(proxmox: Optional[Dict] = None,
                         docker: Optional[Dict] = None, trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_BACKUP, _DEFAULT_BACKUP)
    pcfg = {**cfg.get("proxmox", {}), **(proxmox or {})}
    dcfg = {**cfg.get("docker", {}), **(docker or {})}
    await emit_event({"type": "nodes.backup.start"})
    p_res = await _backup_proxmox(pcfg) if pcfg.get("storage") else \
        [{"skipped": "no proxmox backup storage configured"}]
    d_res = await _backup_docker(dcfg) if dcfg.get("enabled") else \
        [{"skipped": "docker volume backup disabled"}]
    ok = all(x.get("ok", True) for x in p_res + d_res if "skipped" not in x)
    entry = {"ts": now_iso(), "ok": ok, "proxmox": p_res, "docker": d_res}
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_BACKUP_LOG)
            lst = json.loads(raw) if raw else []
            lst.append(entry)
            await r.set(KEY_BACKUP_LOG, json.dumps(lst[-40:]))
        except Exception:
            pass
    cfg["last_run"] = time.time()
    await _json_cfg_put(KEY_BACKUP, cfg)
    await emit_event({"type": "nodes.backup.done", "ok": ok})
    return {"ok": ok, "proxmox": p_res, "docker": d_res}


# ─────────────────────────────────────────────────────────────────────────────
# SHARE-TREE AUTO-SYNC  — keep the pxstore file fabric fresh (default daily)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_SYNC = {
    "enabled": False,
    "interval_hours": 24,          # "daily at the least" — configurable
    "last_run": 0,
    "clusters": {},                # cluster_id -> [pve node names] ([] = all mapped)
    "legacy_share": False,         # also rebuild the hypervisor Samba tree (pxstore.fs.sync)
}


@capability(
    "nodes.sync.get",
    http_method="GET", http_path="/nodes/sync", http_tags=["nodes"],
    memory="off", silent=True,
    description="Get the estate-tree sync schedule. VFS-02 rebuilds its own "
                "estate tree every 5 minutes; this is the extra trigger from "
                "Vera, plus whether the legacy hypervisor share is rebuilt too. "
                "Output: {config}.",
)
async def cap_sync_get(trace_id=None) -> Dict:
    return {"config": await _json_cfg(KEY_SYNC, _DEFAULT_SYNC)}


@capability(
    "nodes.sync.set",
    http_method="POST", http_path="/nodes/sync/set", http_tags=["nodes"],
    memory="off",
    description="Configure share-tree auto-sync. Inputs: enabled (bool), "
                "interval_hours (int=24 — daily default, any interval), "
                "clusters (dict — {cluster_id:[pve nodes]} ; empty node list = "
                "every node mapped in that cluster's pxstore settings; omit to "
                "keep), legacy_share (bool — also rebuild the legacy hypervisor "
                "share). Output: {ok, config}.",
)
async def cap_sync_set(enabled: Optional[bool] = None,
                       interval_hours: Optional[int] = None,
                       clusters: Optional[Dict] = None,
                       legacy_share: Optional[bool] = None, trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_SYNC, _DEFAULT_SYNC)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if interval_hours:
        cfg["interval_hours"] = max(1, int(interval_hours))
    if isinstance(clusters, dict):
        cfg["clusters"] = {str(k): [str(n) for n in (v or [])]
                           for k, v in clusters.items()}
    if legacy_share is not None:
        cfg["legacy_share"] = bool(legacy_share)
    await _json_cfg_put(KEY_SYNC, cfg)
    return {"ok": True, "config": cfg}


@capability(
    "nodes.sync.run",
    http_method="POST", http_path="/nodes/sync/run", http_tags=["nodes"],
    memory="off",
    description="Rebuild the estate tree now. The file fabric (VFS-02) is "
                "the estate's file server: this triggers vfs.estate.sync, which "
                "VFS-02 otherwise runs every 5 minutes on its own timer. The "
                "legacy hypervisor share (pxstore.fs.sync per mapped node) is "
                "rebuilt too only when legacy_share is on (nodes.sync.set). "
                "Output: {ok, results:[{target,cluster_id,node,ok,linked,error}]}.",
)
async def cap_sync_run(trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_SYNC, _DEFAULT_SYNC)
    results: List[Dict] = []
    vfs_sync = _rawcap("vfs.estate.sync")
    if vfs_sync:
        try:
            r = await vfs_sync()
            results.append({"target": "vfs-02", "cluster_id": "", "node": "VFS-02",
                            "ok": bool(r.get("ok")) and not r.get("error"),
                            "linked": int(r.get("mounted") or 0),
                            "failed": len(r.get("failed") or []),
                            "error": str(r.get("error", ""))[:300]})
        except Exception as e:
            results.append({"target": "vfs-02", "cluster_id": "", "node": "VFS-02",
                            "ok": False, "linked": 0, "error": str(e)[:300]})
    fs_sync = _rawcap("pxstore.fs.sync") if cfg.get("legacy_share") else None
    if fs_sync:
        wanted = cfg.get("clusters") or {}
        for cid, pcfg in (await _pxstore_cfgs()).items():
            if wanted and cid not in wanted:
                continue
            for n in wanted.get(cid) or list((pcfg.get("node_hosts") or {}).keys()):
                try:
                    r = await fs_sync(cluster_id=cid, node=n)
                    results.append({"target": "legacy", "cluster_id": cid, "node": n,
                                    "ok": bool(r.get("ok")),
                                    "linked": len(r.get("linked") or []),
                                    "error": str(r.get("error", ""))[:300]})
                except Exception as e:
                    results.append({"target": "legacy", "cluster_id": cid, "node": n,
                                    "ok": False, "linked": 0, "error": str(e)[:300]})
    if not results:
        return {"ok": False, "results": [],
                "error": "nothing to sync — the file fabric capabilities (vfs.*) "
                         "are not loaded and the legacy share is off"}
    cfg["last_run"] = time.time()
    await _json_cfg_put(KEY_SYNC, cfg)
    ok = all(r["ok"] for r in results)
    await emit_event({"type": "nodes.sync.done", "ok": ok, "results": results})
    return {"ok": ok, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED MAINTENANCE  — one 5-min heartbeat drives both timers
# ─────────────────────────────────────────────────────────────────────────────
async def _maintenance_tick():
    try:
        sync_cfg = await _json_cfg(KEY_SYNC, _DEFAULT_SYNC)
        if sync_cfg.get("enabled"):
            due = float(sync_cfg.get("last_run") or 0) + \
                max(1, int(sync_cfg.get("interval_hours", 24))) * 3600
            if time.time() >= due:
                log.info("nodes: scheduled share-tree sync starting")
                await cap_sync_run()
    except Exception as e:
        log.warning("nodes sync tick: %s", e)
    try:
        bk_cfg = await _json_cfg(KEY_BACKUP, _DEFAULT_BACKUP)
        if bk_cfg.get("enabled"):
            due = float(bk_cfg.get("last_run") or 0) + \
                max(1, int(bk_cfg.get("interval_hours", 24))) * 3600
            if time.time() >= due:
                log.info("nodes: scheduled backup starting")
                await cap_backup_run()
    except Exception as e:
        log.warning("nodes backup tick: %s", e)


try:
    schedule(_maintenance_tick, 300.0, name="nodes_maintenance")
except Exception as e:
    log.debug("schedule nodes maintenance: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CORE TEMPERATURES  — SSH probe across three layers, richest-available wins
# per layer (they're complementary, not alternatives):
#   1. sensors -A -u (CPU package/core), falling back to /sys/class/thermal
#   2. ipmitool sdr type temperature — the iLO/BMC's full sensor list (inlet
#      ambient, per-CPU, DIMM zones, PSU inlet, fan-adjacent, and on servers
#      with a smart-array backplane, per-bay drive temps too) — this is what
#      actually gets "down to the drives" on real server hardware (HPE
#      ProLiant etc.) without any vendor-specific tooling.
#   3. smartctl per block device — SMART temperature attribute, as a direct
#      per-drive reading independent of whatever the backplane exposes to IPMI.
# On its own lighter/heavier tick from the 5-min maintenance timer above:
# SSH is comparatively expensive and temps move slowly, so this doesn't share
# _maintenance_tick's due-time logic.
#
# Exposed as obs.node_temps — named/tagged into the obs.* umbrella the same
# way obs.cluster lives in cluster.py rather than the observability section
# of capability_orchestration.py: the implementation stays here because this
# module already owns the SSH channel and per-host fact cache.
# ─────────────────────────────────────────────────────────────────────────────
TEMP_PROBE_SEC = 60.0
_TEMP_INSTALL_BACKOFF = 3600.0 * 6   # don't hammer apt on a host that keeps failing

# ...and don't hammer the CONNECTION either. Measured on prod 2026-09-04: of 28
# registered hosts 21 answered "no route to host" and 4 "PermissionDenied", and
# all 28 were dialled every 60s regardless - 92,928 cumulative SSH opens, ~64 a
# minute, with the host process at 57% CPU and nothing running. The install step
# above has had a backoff for ages; the dialling never did, and the dialling is
# the expensive part. See probe_backoff.
try:
    from Vera.vera.workers import probe_backoff as _probe_backoff
except ImportError:                                   # pragma: no cover
    from vera.workers import probe_backoff as _probe_backoff
try:
    from Vera.vera.workers import node_temps_core as _temps_core
except ImportError:                                   # pragma: no cover
    from vera.workers import node_temps_core as _temps_core

#: host_id -> backoff state (see probe_backoff). Empty means "probe normally".
_TEMP_BACKOFF: Dict[str, dict] = {}

_TEMP_SCRIPT = r"""
v=$(systemd-detect-virt --container 2>/dev/null); [ -z "$v" ] && [ -r /run/systemd/container ] && v=$(cat /run/systemd/container 2>/dev/null)
echo "VIRT|${v:-none}"
if command -v sensors >/dev/null 2>&1; then
  echo "SENSORS_BEGIN"
  sensors -A -u 2>/dev/null
  echo "SENSORS_END"
elif [ -d /sys/class/thermal ]; then
  for z in /sys/class/thermal/thermal_zone*/temp; do
    [ -f "$z" ] || continue
    d=$(dirname "$z")
    zt=$(cat "$d/type" 2>/dev/null || basename "$d")
    v=$(cat "$z" 2>/dev/null)
    [ -n "$v" ] && echo "THERMAL_ZONE|$zt|$v"
  done
else
  echo "NO_SENSORS"
fi
if command -v ipmitool >/dev/null 2>&1; then
  echo "IPMI_BEGIN"
  (sudo -n ipmitool sdr elist full 2>/dev/null || ipmitool sdr elist full 2>/dev/null)
  echo "IPMI_END"
else
  echo "NO_IPMI"
fi
if command -v smartctl >/dev/null 2>&1; then
  echo "SMART_BEGIN"
  for d in $(lsblk -d -n -o NAME 2>/dev/null | grep -E '^(sd|nvme|hd)'); do
    echo "SMART_DEV|/dev/$d"
    (sudo -n smartctl -a /dev/$d 2>/dev/null || smartctl -a /dev/$d 2>/dev/null)
    echo "SMART_DEV_END"
  done
  echo "SMART_END"
else
  echo "NO_SMART"
fi
if [ -r /proc/stat ]; then
  echo "PERCPU_BEGIN"
  grep '^cpu[0-9]' /proc/stat > /tmp/.vera_pc1 2>/dev/null
  sleep 1
  grep '^cpu[0-9]' /proc/stat > /tmp/.vera_pc2 2>/dev/null
  awk '
    NR==FNR{t=0;for(i=2;i<=NF;i++)t+=$i;idle1[$1]=$5;tot1[$1]=t;next}
    {t=0;for(i=2;i<=NF;i++)t+=$i;dt=t-tot1[$1];di=$5-idle1[$1];
     pct=(dt>0)?100*(1-di/dt):0; printf "PERCPU|%s|%.1f\n",$1,pct}
  ' /tmp/.vera_pc1 /tmp/.vera_pc2 2>/dev/null
  rm -f /tmp/.vera_pc1 /tmp/.vera_pc2
  echo "PERCPU_END"
else
  echo "NO_PERCPU"
fi
echo "DISK_BEGIN"
df -P -B1G 2>/dev/null | awk 'NR>1 && $1 ~ /^\// && $6 !~ /^\/(boot|snap|dev|run)/ {print "DISK|"$6"|"$2"|"$3"|"$5}'
echo "DISK_END"
"""

_TEMP_INSTALL_SCRIPT = (
    "(sudo -n apt-get install -y lm-sensors ipmitool smartmontools "
    "|| apt-get install -y lm-sensors ipmitool smartmontools) >/dev/null 2>&1; "
    "(sudo -n sensors-detect --auto || sensors-detect --auto) >/dev/null 2>&1; "
    "echo INSTALL_DONE"
)

_TEMP_CACHE: Dict[str, dict] = {}            # host_id -> {label, pve, temps, max_c, updated_at, error}
_TEMP_INSTALL_TRIED: Dict[str, float] = {}   # host_id -> time.time() of last install attempt


def _parse_sensors_u(txt: str) -> Dict[str, float]:
    """Parse `sensors -A -u` machine-readable output into {sensor_label: celsius}.
    Section headers ('Package id 0:', 'Core 0:') are unindented lines ending in
    ':'; the tempN_input values under them are indented 'tempN_input: NN.NNN'."""
    temps: Dict[str, float] = {}
    label = ""
    for raw in (txt or "").splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            s = raw.strip()
            if s.endswith(":"):
                label = s[:-1].strip()
            continue   # chip name line, or a section-header line just captured
        key, sep, val = raw.strip().partition(":")
        if sep and key.strip().endswith("_input"):
            try:
                temps[label or key.strip()] = round(float(val.strip()), 1)
            except ValueError:
                continue
    return temps


def _parse_ipmi_sensors(txt: str) -> Dict[str, Dict[str, float]]:
    """Parse `ipmitool sdr elist full` — the FULL sensor list (temperature,
    fan, voltage, current/power, everything the iLO/BMC exposes), not just
    the temperature subset — into {category: {name: value}}. Rows look like
    'Inlet Ambient | 01h | ok | 7.1 | 21 degrees C' or
    'Fan Block 1   | 30h | ok | 7.1 | 22400 RPM' or
    'VCORE         | 60h | ok | 7.1 | 1.20 Volts'; category is inferred from
    the reading's unit suffix. Skips sensors with no reading ('No Reading',
    'Disabled', non-numeric — e.g. discrete/state sensors elist also lists)."""
    out: Dict[str, Dict[str, float]] = {"temp": {}, "fan": {}, "voltage": {}, "power": {}}
    for ln in (txt or "").splitlines():
        if "|" not in ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 5:
            continue
        name, reading = parts[0], parts[-1]
        if not name:
            continue
        m = re.match(r"(-?\d+(?:\.\d+)?)\s*(.*)", reading)
        if not m:
            continue
        try:
            val = round(float(m.group(1)), 2)
        except ValueError:
            continue
        unit = m.group(2).strip().lower()
        if "degrees" in unit or unit == "c":
            out["temp"][name] = round(val, 1)
        elif "rpm" in unit:
            out["fan"][name] = val
        elif "volt" in unit:
            out["voltage"][name] = val
        elif "watt" in unit or "amp" in unit:
            out["power"][name] = val
        # else: discrete/unitless sensor (e.g. a state bitmask) — not a metric, skip
    return out


def _parse_smartctl_temp(txt: str) -> Optional[float]:
    """Extract one temperature reading from `smartctl -a` output, ATA (SMART
    attribute table, RAW_VALUE column) or NVMe ('Temperature: NN Celsius')."""
    m = re.search(
        r"^\s*\d+\s+(?:Temperature_Celsius|Airflow_Temperature_Cel)\s+\S+"
        r"\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        txt or "", re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"^Temperature:\s*(-?\d+)\s*Celsius", txt or "", re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"^Temperature Sensor \d+:\s*(-?\d+)\s*Celsius", txt or "", re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _parse_smart_health(txt: str) -> Optional[str]:
    m = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", txt or "")
    if m:
        return m.group(1)
    m = re.search(r"^SMART Health Status:\s*(.+)$", txt or "", re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def _parse_smart_poh(txt: str) -> Optional[int]:
    """Power-on hours — ATA attribute table (RAW_VALUE) or NVMe log line."""
    m = re.search(
        r"^\s*9\s+Power_On_Hours\s+\S+(?:\s+\d+){5}\s+\S+\s+\S+\s+\S+\s+(\d+)",
        txt or "", re.MULTILINE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.search(r"Power On Hours:\s*([\d,]+)", txt or "")
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _parse_smart_devices(txt: str) -> Dict[str, Dict]:
    """Parse the SMART_BEGIN..SMART_END block (one smartctl -a dump per
    SMART_DEV|<path> .. SMART_DEV_END section) into per-drive health, not
    just temperature — {"sda": {"temp_c":34.0,"health":"PASSED",
    "power_on_hours":12345}} — all pulled from the SAME smartctl -a dump the
    temp probe already fetches, no extra SSH round-trip."""
    out: Dict[str, Dict] = {}
    dev = None
    buf: List[str] = []
    for ln in (txt or "").splitlines():
        if ln.startswith("SMART_DEV|"):
            dev = ln.split("|", 1)[1].strip()
            buf = []
        elif ln.strip() == "SMART_DEV_END":
            if dev:
                body = "\n".join(buf)
                out[dev.rsplit("/", 1)[-1]] = {
                    "temp_c": _parse_smartctl_temp(body),
                    "health": _parse_smart_health(body),
                    "power_on_hours": _parse_smart_poh(body),
                }
            dev = None
        elif dev is not None:
            buf.append(ln)
    return out


def _parse_disk_usage(txt: str) -> List[Dict]:
    """Parse the DISK_BEGIN..DISK_END block: 'DISK|/mount|totalGB|usedGB|NN%'
    (df -P -B1G, real filesystems only — tmpfs/overlay and /boot|/snap|/dev|
    /run mounts are filtered out in the shell already, not worth showing)."""
    out: List[Dict] = []
    for ln in (txt or "").splitlines():
        if not ln.startswith("DISK|"):
            continue
        parts = ln.split("|")
        if len(parts) != 5:
            continue
        _, mount, total, used, pct = parts
        try:
            out.append({
                "mount": mount, "total_gb": float(total), "used_gb": float(used),
                "used_pct": float(pct.rstrip("%")),
            })
        except ValueError:
            continue
    return out


def _parse_thermal_zones(txt: str) -> Dict[str, float]:
    temps: Dict[str, float] = {}
    for ln in (txt or "").splitlines():
        if not ln.startswith("THERMAL_ZONE|"):
            continue
        _, _, rest = ln.partition("|")
        zt, _, raw = rest.partition("|")
        try:
            temps[zt.strip() or "zone"] = round(float(raw.strip()) / 1000.0, 1)
        except ValueError:
            continue
    return temps


def _parse_percpu(txt: str) -> Dict[str, float]:
    """Parse the PERCPU_BEGIN..PERCPU_END block: 'PERCPU|cpu0|23.4' -> {"cpu0": 23.4}.
    Computed from two /proc/stat samples 1s apart — no extra package (sysstat/
    mpstat) required, works on any Linux box. Note: /proc/stat's cpuN is a
    LOGICAL cpu (thread) index, which on hyperthreaded hardware does not
    necessarily line up 1:1 with lm-sensors' physical "Core N" temp labels —
    shown as its own per-CPU load table, not fused into the temp readings."""
    out: Dict[str, float] = {}
    for ln in (txt or "").splitlines():
        if not ln.startswith("PERCPU|"):
            continue
        _, _, rest = ln.partition("|")
        cpu, _, raw = rest.partition("|")
        try:
            out[cpu.strip()] = round(float(raw.strip()), 1)
        except ValueError:
            continue
    return out


async def _probe_host_temp(host: Dict) -> None:
    host_id = host.get("id", "")
    if not host_id:
        return
    label = host.get("label") or host.get("host") or host_id
    _now = time.time()
    _bo = _TEMP_BACKOFF.get(host_id)
    if _probe_backoff.should_skip(_bo, _now):
        # Leave the cached entry in place but SAY it is stale, so a skipped
        # host cannot be mistaken for one that was probed and had nothing.
        _prev = dict(_TEMP_CACHE.get(host_id) or {})
        _prev.update({"host_id": host_id, "label": label,
                      "error": _probe_backoff.describe(_bo, _now)})
        _TEMP_CACHE[host_id] = _prev
        return
    try:
        r = await _ssh(host_id, _TEMP_SCRIPT, timeout=25)
        _rerr = (r or {}).get("error") or ""
        if _rerr:
            _TEMP_BACKOFF[host_id] = _probe_backoff.record_failure(_bo, _now, _rerr)
        else:
            _TEMP_BACKOFF[host_id] = _probe_backoff.record_success(_bo)
        out = r.get("stdout", "") or ""
        temps: Dict[str, float] = {}
        missing_tools = []

        if "SENSORS_BEGIN" in out:
            body = out.split("SENSORS_BEGIN", 1)[1].split("SENSORS_END", 1)[0]
            temps.update(_parse_sensors_u(body))
        elif "THERMAL_ZONE|" in out:
            temps.update(_parse_thermal_zones(out))
        elif "NO_SENSORS" in out:
            missing_tools.append("lm-sensors")

        health: Dict[str, Dict[str, float]] = {"fan": {}, "voltage": {}, "power": {}}
        if "IPMI_BEGIN" in out:
            body = out.split("IPMI_BEGIN", 1)[1].split("IPMI_END", 1)[0]
            ipmi = _parse_ipmi_sensors(body)
            temps.update(ipmi["temp"])
            health["fan"] = ipmi["fan"]; health["voltage"] = ipmi["voltage"]; health["power"] = ipmi["power"]
        elif "NO_IPMI" in out:
            missing_tools.append("ipmitool")

        drives: Dict[str, Dict] = {}
        if "SMART_BEGIN" in out:
            body = out.split("SMART_BEGIN", 1)[1].split("SMART_END", 1)[0]
            drives = _parse_smart_devices(body)
            for name, d in drives.items():
                if d.get("temp_c") is not None:
                    temps[f"drive {name}"] = d["temp_c"]
        elif "NO_SMART" in out:
            missing_tools.append("smartmontools")

        percpu: Dict[str, float] = {}
        if "PERCPU_BEGIN" in out:
            body = out.split("PERCPU_BEGIN", 1)[1].split("PERCPU_END", 1)[0]
            percpu = _parse_percpu(body)

        disk_usage: List[Dict] = []
        if "DISK_BEGIN" in out:
            body = out.split("DISK_BEGIN", 1)[1].split("DISK_END", 1)[0]
            disk_usage = _parse_disk_usage(body)

        # a container's sensors are its host's (see node_temps_core): no tool
        # installed inside one could ever read anything of its own
        virt = _temps_core.parse_virt(out)
        if missing_tools and not virt:
            # Try installing everything missing at once, with a long backoff
            # on repeat failure — never blocks this tick; the next tick picks
            # up real values once the tools (and, for lm-sensors, the kernel
            # modules via sensors-detect) are actually in place.
            last_try = _TEMP_INSTALL_TRIED.get(host_id, 0.0)
            if time.time() - last_try > _TEMP_INSTALL_BACKOFF:
                _TEMP_INSTALL_TRIED[host_id] = time.time()
                log.info("nodes: %s missing on %s, attempting install", ",".join(missing_tools), host_id)
                await _ssh(host_id, _TEMP_INSTALL_SCRIPT, timeout=120)

        error = "" if temps else (r.get("error") or (
            f"no sensors available ({', '.join(missing_tools)} not installed)" if missing_tools
            else "no temperature readings returned"))
        facts = FACTS.get(host_id, {})
        _TEMP_CACHE[host_id] = _temps_core.attribute({
            "host_id": host_id, "label": label, "pve": bool(facts.get("pve")),
            "temps": temps, "max_c": max(temps.values()) if temps else None,
            "percpu": percpu, "health": health, "drives": drives, "disk_usage": disk_usage,
            "updated_at": now_iso(), "error": error,
        }, virt)
    except Exception as e:
        _TEMP_BACKOFF[host_id] = _probe_backoff.record_failure(_bo, _now, e)
        _TEMP_CACHE[host_id] = {
            "host_id": host_id, "label": label, "pve": False,
            "temps": {}, "max_c": None, "percpu": {}, "health": {"fan": {}, "voltage": {}, "power": {}},
            "drives": {}, "disk_usage": [], "updated_at": now_iso(), "error": str(e)[:200],
        }


async def _temp_probe_tick():
    hosts = await _ssh_hosts()
    if not hosts:
        return
    await asyncio.gather(*(_probe_host_temp(h) for h in hosts), return_exceptions=True)


try:
    schedule(_temp_probe_tick, TEMP_PROBE_SEC, name="nodes_temp_probe")
except Exception as e:
    log.debug("schedule nodes temp probe: %s", e)


@capability(
    "obs.node_temps",
    http_method="GET", http_path="/nodes/temps", http_tags=["obs"],
    memory="off", silent=True,
    description="Core temperatures, fan/voltage/power health, AND per-logical-"
                f"CPU load for every SSH-registered node, probed every "
                f"{int(TEMP_PROBE_SEC)}s. Temps across three complementary "
                "layers: sensors -A -u (CPU package/physical-core, falling "
                "back to /sys/class/thermal), ipmitool sdr elist full (the "
                "iLO/BMC's FULL sensor list, not just temperature — inlet "
                "ambient, DIMM zones, PSU, per-bay drive temps on smart-array "
                "backplanes, PLUS fan RPM/voltage rails/power draw, split into "
                "the `health` field), and smartctl per block device (direct "
                "per-drive SMART temperature). Per-CPU load is computed from "
                "two /proc/stat samples 1s apart (no extra package needed) — "
                "note its cpuN is a LOGICAL cpu/thread index, which doesn't "
                "necessarily line up 1:1 with the temp side's physical "
                "'Core N' sensor labels on hyperthreaded hardware, so they're "
                "kept as separate tables, not fused per-core. Installs "
                "whichever of lm-sensors/ipmitool/smartmontools is missing, "
                "once per host with a long backoff. Named/tagged into the "
                "obs.* umbrella like obs.cluster, though implemented here "
                "since this module owns the SSH probe. Also carries drives "
                "(per-device SMART health/power-on-hours, from the SAME "
                "smartctl dump the temps come from — no extra SSH round trip) "
                "and disk_usage (df -P per real filesystem). A container (LXC, "
                "docker) shares its host's kernel, BMC and disks, so its sensor "
                "readings are the HOST's: they are reported under host_temps / "
                "host_drives / host_health with temps_from='host' and virt set, "
                "and its own temps/drives/max_c stay empty - percpu and "
                "disk_usage remain its own. Output: {hosts:["
                "{host_id,label,pve,temps:{sensor:celsius},percpu:{cpuN:pct},"
                "health:{fan:{name:rpm},voltage:{name:volts},power:{name:w}},"
                "drives:{dev:{temp_c,health,power_on_hours}},"
                "disk_usage:[{mount,total_gb,used_gb,used_pct}],max_c,"
                "virt,temps_from,host_temps?,host_drives?,host_health?,"
                "updated_at,error}], count}.",
)
async def cap_node_temps(trace_id=None) -> Dict:
    return {"hosts": list(_TEMP_CACHE.values()), "count": len(_TEMP_CACHE)}


# ═════════════════════════════════════════════════════════════════════════════
# UNIFIED PROVISIONING UMBRELLA  — one target model × one payload model
# ═════════════════════════════════════════════════════════════════════════════
# The estate already had every mechanism (nodes.provision, docker.run /
# docker.stack.deploy, provision.worker/deploy, secprov.deploy,
# proxmox.lxc.create + guest.enroll) but each sat behind its own menu, and
# some payloads (ollama, security services) could only be reached from one
# surface. provision.overview + provision.apply expose ONE surface:
#
#   TARGETS   node:<id>                 an enrolled estate node (SSH/docker/pve)
#             docker:<host_id>          a registered Docker engine
#             new-ct:<cluster>:<node>   create + enroll a fresh Proxmox CT first
#
#   PAYLOADS  <component>               any key from nodes.components
#                                       (ollama, vllm, docker, vera-worker, …)
#             stack:<service>           a Vera backing service (stack catalog)
#             image:<ref>               any docker image (options.image {…})
#             security:<service>        secprov service (openbao, step-ca, …)
#             vera-stack                all backing stores in one go
#
# Nothing is re-implemented — every branch delegates to the existing caps.


def _payload_catalog_special() -> List[Dict]:
    return [
        {"key": "vera-stack",
         "label": "Vera stack (backing stores)",
         "desc": "redis + postgres + chromadb + neo4j + garage on the target's "
                 "Docker engine. Add 'vera-worker' as another payload to also "
                 "join the machine to the cluster."},
        {"key": "image:<ref>",
         "label": "Any Docker image",
         "desc": "e.g. 'image:nginx:latest'. Extra settings in options.image: "
                 "{name, ports:'8080:80', env:{}, volumes:'src:dst', command, "
                 "network, restart, pull:true}."},
        {"key": "stack:<service>",
         "label": "One Vera backing service",
         "desc": "A docker.stack.catalog service, e.g. 'stack:redis'."},
        {"key": "security:<service>",
         "label": "Security / identity service",
         "desc": "A secprov.services entry: openbao | step-ca | lldap | opa | "
                 "all — e.g. 'security:openbao'."},
    ]


@capability(
    "provision.overview",
    http_method="GET", http_path="/provision/overview", http_tags=["nodes", "provision"],
    memory="off", silent=True,
    description="One-call catalog for UNIFORM provisioning: every target "
                "(estate nodes, Docker engines, Proxmox clusters for new CTs) "
                "and every payload (components incl. ollama/vllm/vera-worker, "
                "backing-store stacks, security services, arbitrary docker "
                "images, the full Vera stack) with the exact keys "
                "provision.apply expects. Output: {targets, payloads, usage}.",
)
async def cap_provision_overview(trace_id=None) -> Dict:
    nodes = await _build_nodes()
    dhosts = await _docker_hosts()
    clusters: List[Dict] = []
    cl = _rawcap("proxmox.cluster.list")
    if cl:
        try:
            for c in ((await cl()) or {}).get("clusters", []) or []:
                clusters.append({"cluster_id": c.get("id"),
                                 "label": c.get("label") or c.get("id"),
                                 "target": f"new-ct:{c.get('id')}:<pve_node>"})
        except Exception:
            pass
    stacks: List[Dict] = []
    sc = _rawcap("docker.stack.catalog")
    if sc:
        try:
            stacks = [{"key": f"stack:{s['id']}", "label": s.get("label", s["id"]),
                       "image": s.get("image", ""), "desc": s.get("note", "")}
                      for s in ((await sc()) or {}).get("services", []) or []]
        except Exception:
            pass
    sec: List[Dict] = []
    ss = _rawcap("secprov.services")
    if ss:
        try:
            sec = [{"key": f"security:{s['key']}", "label": s.get("label", s["key"]),
                    "system": s.get("system", ""), "desc": s.get("desc", "")}
                   for s in ((await ss()) or {}).get("services", []) or []]
        except Exception:
            pass
    return {
        "ok": True,
        "targets": {
            "nodes": [{"target": f"node:{n['id']}", "id": n["id"],
                       "label": n["label"], "addr": n["addr"],
                       "backends": n.get("backends", []),
                       "gpu": bool((n.get("hw") or {}).get("vram_gb")
                                   or (n.get("facts") or {}).get("gpu_name"))}
                      for n in nodes],
            "docker_engines": [{"target": f"docker:{h.get('id')}",
                                "id": h.get("id"),
                                "label": h.get("label") or h.get("id"),
                                "kind": h.get("kind", "")} for h in dhosts],
            "proxmox_clusters": clusters,
        },
        "payloads": {
            "components": [{"key": k, "group": c["group"], "label": c["label"],
                            "backends": c["backends"], "gpu": c.get("gpu", ""),
                            "needs_model": bool(c.get("needs_model")),
                            "desc": c["desc"]}
                           for k, c in _COMPONENTS.items()],
            "stacks": stacks,
            "security": sec,
            "special": _payload_catalog_special(),
        },
        "usage": "provision.apply(target='node:<id>'|'docker:<host_id>'|"
                 "'new-ct:<cluster_id>:<pve_node>', payloads=[…], options={…})",
    }


@capability(
    "provision.node.new",
    http_method="POST", http_path="/provision/node/new", http_tags=["nodes", "provision"],
    memory="off",
    description="Create a NEW Proxmox LXC container and enroll it as an estate "
                "node (SSH host) in one step — the 'new machine' half of "
                "uniform provisioning. Inputs: cluster_id (str!), node (str! — "
                "PVE node), ostemplate (str! — vztmpl volid), hostname (str), "
                "storage (str='local-lvm'), cores (int=2), memory_mb (int=2048), "
                "disk_gb (int=16), password (str — root password, used for the "
                "SSH enroll too), ssh_public_keys (str), features "
                "(str='nesting=1,keyctl=1' — keeps Docker-in-CT possible), "
                "unprivileged (bool=True), enroll (bool=True), user "
                "(str='root'), key_path (str), wait_secs (int=90 — boot/DHCP "
                "wait for the enroll IP autodetect). Output: {ok, vmid, ip, "
                "ssh_host_id, node_id} — node_id is usable as provision.apply "
                "target 'node:<node_id>'.",
)
async def cap_provision_node_new(cluster_id: str = "", node: str = "",
                                 ostemplate: str = "", hostname: str = "",
                                 storage: str = "local-lvm", cores: int = 2,
                                 memory_mb: int = 2048, disk_gb: int = 16,
                                 password: str = "", ssh_public_keys: str = "",
                                 features: str = "nesting=1,keyctl=1",
                                 unprivileged: bool = True, enroll: bool = True,
                                 user: str = "root", key_path: str = "",
                                 wait_secs: int = 90, trace_id=None) -> Dict:
    create = _rawcap("proxmox.lxc.create")
    if not create:
        return {"error": "proxmox.lxc.create unavailable"}
    if not ostemplate:
        return {"error": "ostemplate required (a vztmpl volid — list with "
                         "proxmox.storage.content content='vztmpl')"}
    res = await create(cluster_id=cluster_id, node=node, ostemplate=ostemplate,
                       hostname=hostname, storage=storage, cores=int(cores),
                       memory=int(memory_mb), disk=int(disk_gb),
                       password=password, ssh_public_keys=ssh_public_keys,
                       features=features, unprivileged=bool(unprivileged),
                       start=True)
    if not res.get("ok"):
        return {"error": str(res.get("error", "create failed"))[:400],
                "create": res}
    vmid = int(res.get("vmid") or 0)
    out: Dict[str, Any] = {"ok": True, "vmid": vmid}
    if not enroll:
        return out
    # One enrolment pipeline: auto-enrol saves the login (register_only runs
    # proxmox.guest.enroll as its login step).
    pipeline = _rawcap("autoenroll.enrol")
    enr = pipeline or _rawcap("proxmox.guest.enroll")
    if not enr:
        out["enroll_error"] = "autoenroll.enrol / proxmox.guest.enroll unavailable"
        return out
    # The CT needs to boot and pull a DHCP lease before its IP is detectable —
    # retry the enroll until the deadline instead of failing on the first probe.
    deadline = time.time() + max(15, int(wait_secs))
    last: Dict = {}
    while time.time() < deadline:
        await asyncio.sleep(6)
        try:
            if pipeline:
                last = await enr(cluster_id=cluster_id, node=node, guest_type="lxc",
                                 vmid=vmid, ssh_user=user or "root", ssh_password=password,
                                 ssh_key_path=key_path, label=hostname or f"ct-{vmid}",
                                 steps="enroll_guest", register_only=True) or {}
            else:
                last = await enr(cluster_id=cluster_id, node=node, guest_type="lxc",
                                 vmid=vmid, user=user or "root", password=password,
                                 key_path=key_path,
                                 label=hostname or f"ct-{vmid}") or {}
        except Exception as e:
            last = {"error": str(e)}
        if last.get("ok"):
            break
    if last.get("ok"):
        out.update({"ssh_host_id": last.get("ssh_host_id"),
                    "node_id": last.get("ssh_host_id"),
                    "ip": last.get("ip"), "enrolled": True})
    else:
        out.update({"ok": False, "enrolled": False,
                    "error": "CT created but enroll failed: "
                             + str(last.get("error", "no IP detected"))[:300]})
    return out


@capability(
    "provision.apply",
    http_method="POST", http_path="/provision/apply", http_tags=["nodes", "provision"],
    memory="off",
    description=(
        "UNIFORM provisioning entrypoint — deploy any payload onto any target "
        "with one call. target: 'node:<id>' (enrolled estate node — see "
        "provision.overview), 'docker:<host_id>' (registered Docker engine), or "
        "'new-ct:<cluster_id>:<pve_node>' (create + enroll a fresh Proxmox CT "
        "first; CT settings in options.ct {ostemplate!, hostname, storage, "
        "cores, memory_mb, disk_gb, password}). payloads (list of str): "
        "component keys from nodes.components (ollama, vllm, docker, "
        "vera-worker, redis, …), 'stack:<service>' (docker.stack.catalog), "
        "'image:<ref>' (any docker image; extras in options.image {name, "
        "ports, env, volumes, command, pull}), 'security:<svc>' (openbao|"
        "step-ca|lldap|opa|all), 'vera-stack' (all backing stores). options "
        "also: backend ('auto'|docker|proxmox|ssh), gpus ('all'), model (HF id "
        "— required for vllm), ports{}, hf_home. Emits "
        "provision.apply.progress events per step. Output: {ok, target, "
        "results:[…]}."
    ),
)
async def cap_provision_apply(target: str = "",
                              payloads: Optional[List[str]] = None,
                              options: Optional[Dict] = None,
                              trace_id=None) -> Dict:
    opt = dict(options or {})
    items = [str(p).strip() for p in (payloads or []) if str(p).strip()]
    if not target:
        return {"error": "target required — node:<id> | docker:<host_id> | "
                         "new-ct:<cluster_id>:<pve_node> (see provision.overview)"}
    if not items:
        return {"error": "payloads required (see provision.overview)"}

    results: List[Dict] = []

    async def _prog(stage: str, **kw):
        try:
            await emit_event({"type": "provision.apply.progress",
                              "target": target, "stage": stage, **kw})
        except Exception:
            pass

    # ── new-CT target: create + enroll first, then continue as a node ──────
    if target.startswith("new-ct:"):
        parts = target.split(":", 2)
        if len(parts) < 3 or not parts[1] or not parts[2]:
            return {"error": "new-ct target must be 'new-ct:<cluster_id>:<pve_node>'"}
        ct = dict(opt.get("ct") or {})
        if not ct.get("ostemplate"):
            return {"error": "options.ct.ostemplate required for a new CT (a "
                             "vztmpl volid, e.g. 'local:vztmpl/debian-12-…tar.zst')"}
        await _prog("ct.create", cluster_id=parts[1], node=parts[2])
        created = await cap_provision_node_new(
            cluster_id=parts[1], node=parts[2],
            ostemplate=str(ct.get("ostemplate", "")),
            hostname=str(ct.get("hostname", "")),
            storage=str(ct.get("storage", "local-lvm")),
            cores=int(ct.get("cores", 2)),
            memory_mb=int(ct.get("memory_mb", 2048)),
            disk_gb=int(ct.get("disk_gb", 16)),
            password=str(ct.get("password", "")),
            ssh_public_keys=str(ct.get("ssh_public_keys", "")),
            features=str(ct.get("features", "nesting=1,keyctl=1")),
            user=str(ct.get("user", "root")),
            key_path=str(ct.get("key_path", "")))
        results.append({"payload": "new-ct",
                        **{k: created.get(k) for k in
                           ("ok", "vmid", "ip", "ssh_host_id", "error")
                           if k in created}})
        if not created.get("ok"):
            await _prog("ct.failed", error=str(created.get("error", ""))[:300])
            return {"ok": False, "target": target, "results": results}
        target = f"node:{created.get('node_id') or created.get('ssh_host_id')}"
        await _prog("ct.ready", node_id=created.get("node_id"))

    # ── resolve target kind ────────────────────────────────────────────────
    node: Optional[Dict] = None
    docker_host = ""
    if target.startswith("node:"):
        node = await _node_by_id(target[5:])
        if not node:
            return {"error": f"node not found: {target[5:]}", "results": results}
    elif target.startswith("docker:"):
        docker_host = target[7:]
    else:
        node = await _node_by_id(target)
        if not node:
            if any(h.get("id") == target for h in await _docker_hosts()):
                docker_host = target
            else:
                return {"error": f"unknown target: {target} (use node:<id> | "
                                 "docker:<host_id> | new-ct:…)",
                        "results": results}

    # ── expand + classify payloads ─────────────────────────────────────────
    comps: List[str] = []
    stacks: List[str] = []
    images: List[Dict] = []
    security: List[str] = []
    for p in items:
        if p == "vera-stack":
            for s in ("redis", "postgres", "chromadb", "neo4j", "garage"):
                if s not in stacks:
                    stacks.append(s)
        elif p in _COMPONENTS:
            comps.append(p)
        elif p.startswith("stack:"):
            stacks.append(p[6:])
        elif p.startswith("image:"):
            images.append({"ref": p[6:], **dict(opt.get("image") or {})})
        elif p.startswith("security:"):
            security.append(p[9:])
        else:
            results.append({"payload": p, "ok": False,
                            "error": "unknown payload (see provision.overview)"})

    overall = all(r.get("ok", True) for r in results)

    async def _engine() -> str:
        """A usable Docker engine on the target (installing Docker over SSH
        first when a node has none)."""
        if docker_host:
            return docker_host
        ok = await _ensure_docker_host(node)
        return ok.get("docker_host_id", "") if ok.get("ok") else ""

    # ── components ─────────────────────────────────────────────────────────
    if comps:
        if node:
            await _prog("components", components=comps)
            res = await cap_nodes_provision(node_id=node["id"], components=comps,
                                            backend=str(opt.get("backend", "auto")),
                                            options=opt)
            overall = overall and bool(res.get("ok"))
            results.append({"payload": "components", "components": comps,
                            "ok": bool(res.get("ok")),
                            "results": res.get("results", []),
                            "warnings": res.get("warnings", [])})
        else:
            # A bare Docker engine can host docker-native components; the rest
            # need an enrolled node (SSH) to install onto.
            for key in comps:
                if key == "vera-worker":
                    spawn = _rawcap("docker.worker.spawn")
                    await _prog("vera-worker", host_id=docker_host)
                    r = await spawn(host_id=docker_host,
                                    gpus=str(opt.get("gpus", ""))) \
                        if spawn else {"ok": False,
                                       "error": "docker.worker.spawn unavailable"}
                    overall = overall and bool(r.get("ok"))
                    results.append({"payload": key, "ok": bool(r.get("ok")),
                                    **({"container_id": r.get("container_id")}
                                       if r.get("ok") else
                                       {"error": str(r.get("error", ""))[:300]})})
                elif key == "ollama" or _COMPONENTS.get(key, {}).get("group") == "stores":
                    if key not in stacks:
                        stacks.append(key)
                else:
                    overall = False
                    results.append({"payload": key, "ok": False,
                                    "error": f"component '{key}' needs an enrolled "
                                             "node target (node:<id>) — a bare "
                                             "docker engine has no SSH plane"})

    # ── backing-service stacks (incl. ollama-as-container) ─────────────────
    if stacks:
        eng = await _engine()
        dep = _rawcap("docker.stack.deploy")
        if not eng or not dep:
            overall = False
            results.append({"payload": "stacks", "ok": False, "services": stacks,
                            "error": "no docker engine available on target"
                                     if not eng else "docker.stack.deploy unavailable"})
        else:
            for svc in stacks:
                await _prog("stack", service=svc, host_id=eng)
                try:
                    r = await dep(host_id=eng, service=svc,
                                  gpus=str(opt.get("gpus", ""))) or {}
                except Exception as e:
                    r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                rok = bool(r.get("ok"))
                overall = overall and rok
                entry: Dict[str, Any] = {"payload": f"stack:{svc}", "ok": rok}
                if not rok:
                    entry["error"] = str(r.get("error", ""))[:300]
                results.append(entry)
                if svc == "ollama" and rok and node:
                    try:
                        results.append({"payload": "ollama.register",
                                        **(await _register_ollama(
                                            node,
                                            int((opt.get("ports") or {}).get("ollama")
                                                or 11434),
                                            bool(opt.get("gpus")),
                                            num_thread=opt.get("num_thread")))})
                    except Exception:
                        pass

    # ── arbitrary images ───────────────────────────────────────────────────
    if images:
        eng = await _engine()
        run = _rawcap("docker.run")
        for im in images:
            ref = str(im.get("ref", "")).strip()
            if not (eng and run and ref):
                overall = False
                results.append({"payload": f"image:{ref or '?'}", "ok": False,
                                "error": "no docker engine on target"
                                         if not eng else
                                         ("docker.run unavailable" if not run
                                          else "image ref required")})
                continue
            await _prog("image", ref=ref, host_id=eng)
            try:
                r = await run(host_id=eng, image=ref,
                              name=str(im.get("name", "")),
                              ports=str(im.get("ports", "")),
                              env=im.get("env") or {},
                              volumes=str(im.get("volumes", "")),
                              network=str(im.get("network", "")),
                              restart=str(im.get("restart", "unless-stopped")),
                              command=str(im.get("command", "")),
                              pull=bool(im.get("pull", True))) or {}
            except Exception as e:
                r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            rok = bool(r.get("ok"))
            overall = overall and rok
            results.append({"payload": f"image:{ref}", "ok": rok,
                            **({"container_id": r.get("container_id"),
                                "name": r.get("name")} if rok else
                               {"error": str(r.get("error", ""))[:300]})})

    # ── security / identity services ───────────────────────────────────────
    if security:
        eng = await _engine()
        dep = _rawcap("secprov.deploy")
        for svc in security:
            if not (eng and dep):
                overall = False
                results.append({"payload": f"security:{svc}", "ok": False,
                                "error": "no docker engine on target"
                                         if not eng else "secprov.deploy unavailable"})
                continue
            await _prog("security", service=svc, host_id=eng)
            try:
                r = await dep(host_id=eng, service=svc) or {}
            except Exception as e:
                r = {"error": f"{type(e).__name__}: {e}"}
            rok = not r.get("error")
            overall = overall and rok
            results.append({"payload": f"security:{svc}", "ok": rok,
                            **({"services": r.get(svc) or
                                {k: v for k, v in r.items()
                                 if k not in ("error",)}} if rok else
                               {"error": str(r.get("error", ""))[:300]})})

    await _prog("done", ok=overall)
    await emit_event({"type": "provision.apply.done", "target": target,
                      "ok": overall,
                      "payloads": items, "steps": len(results)})
    return {"ok": overall, "target": target, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CONCURRENCY + THE GPU NODE'S CPU SIBLING
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "nodes.ollama.tune",
    http_method="POST", http_path="/nodes/ollama/tune", http_tags=["nodes", "ollama"],
    memory="off",
    description="Bring every Ollama node to the concurrency layout (user, 2026-09-28): a CPU "
                "node's Ollama gets 2 slots and room for the embedder beside a large model "
                "(OLLAMA_NUM_PARALLEL=2, MAX_LOADED_MODELS=3, a systemd drop-in on its unit - "
                "applying it RESTARTS that unit, so loaded models reload); a GPU node keeps one "
                "GPU slot and gains a CPU-only sibling Ollama on :11436 (GPU hidden, same "
                "read-only model store), registered as '<id>-cpu' with num_thread 6 - the "
                "primary embedding node. A node already in shape is left alone; a node with "
                "a generation in flight from this process is skipped unless force. Dry run by "
                "default; refused from a dev sandbox. Inputs: dry_run (bool=true), force "
                "(bool), instance_ids (list - default every registered GPU-capable or CPU "
                "Ollama node, siblings excluded). Output: {ok, nodes:[{instance, host, plan, "
                "result}]}.",
)
async def cap_nodes_ollama_tune(dry_run: bool = True, force: bool = False,
                                instance_ids: Optional[List[str]] = None,
                                trace_id=None) -> Dict:
    if not dry_run and _orch.is_dev_sandbox():
        return {"ok": False, "error": "this is a dev sandbox: the Ollama nodes are prod's"}
    run = _rawcap("exec.ssh.run")
    lst = _rawcap("exec.ssh.hosts.list")
    if not run or not lst:
        return {"ok": False, "error": "exec.ssh unavailable"}
    by_addr = {h.get("host"): h.get("id") for h in ((await lst()) or {}).get("hosts", [])
               if h.get("host") and h.get("id")}
    insts = dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {})
    sibling_ids = {_ollama_core.cpu_sibling_id(i) for i in insts}
    targets = [i for i in (instance_ids or list(insts)) if i in insts and i not in sibling_ids]
    out = []
    for iid in targets:
        inst = insts[iid]
        addr = urlparse(str(inst.get("url") or "")).hostname or ""
        row: Dict[str, Any] = {"instance": iid, "host": addr, "has_gpu": bool(inst.get("has_gpu"))}
        hid = by_addr.get(addr)
        if not hid:
            row["plan"] = {"action": "skip", "why": "no stored SSH credential for this address"}
            out.append(row)
            continue
        probe = await run(command=_ollama_core.tune_probe_cmd(), host_id=hid, timeout=30) or {}
        plan = _ollama_core.tune_plan(bool(inst.get("has_gpu")),
                                      _ollama_core.parse_tune_probe(probe.get("stdout") or ""))
        row["plan"] = plan
        if plan["action"] in ("none", "skip") or dry_run:
            # the thread default is its own step: a node already in shape
            # (or a dry run) still gets it checked / planned
            row["threads"] = await _threads_step(iid, inst, hid, dry_run, force)
            out.append(row)
            continue
        if int(inst.get("in_use") or 0) > 0 and not force:
            row["result"] = {"ok": False, "skipped": "a generation is in flight on this node"}
            out.append(row)
            continue
        if plan["action"] == "set_concurrency":
            unit = plan["unit"]
            d = f"/etc/systemd/system/{unit}.service.d"
            body = _ollama_core.concurrency_dropin()
            port = urlparse(str(inst.get("url"))).port or 11435
            cmd = (f"mkdir -p {d} && printf '%s' {shlex.quote(body)} > "
                   f"{d}/{_ollama_core.CONCURRENCY_DROPIN_NAME} && systemctl daemon-reload && "
                   f"systemctl restart {unit} && for i in $(seq 1 30); do "
                   f"curl -fsS -m 3 http://127.0.0.1:{port}/api/tags >/dev/null && "
                   f"echo VERA_TUNED && break; sleep 2; done")
            res = await run(command=cmd, host_id=hid, timeout=120) or {}
            row["result"] = {"ok": "VERA_TUNED" in (res.get("stdout") or ""),
                             "error": "" if "VERA_TUNED" in (res.get("stdout") or "")
                             else str(res.get("stderr") or res.get("error") or "no answer")[:300]}
        elif plan["action"] == "add_sibling":
            unit_txt = _ollama_core.cpu_sibling_unit(plan["models"])
            p = _ollama_core.CPU_SIBLING_PORT
            cmd = (f"printf '%s' {shlex.quote(unit_txt)} > /etc/systemd/system/"
                   f"{_ollama_core.CPU_SIBLING_UNIT} && systemctl daemon-reload && "
                   f"systemctl enable --now {_ollama_core.CPU_SIBLING_UNIT} && "
                   f"for i in $(seq 1 30); do curl -fsS -m 3 http://127.0.0.1:{p}/api/tags "
                   f">/dev/null && echo VERA_TUNED && break; sleep 2; done")
            res = await run(command=cmd, host_id=hid, timeout=120) or {}
            ok = "VERA_TUNED" in (res.get("stdout") or "")
            row["result"] = {"ok": ok, "error": "" if ok else
                             str(res.get("stderr") or res.get("error") or "no answer")[:300]}
            if ok:
                sid = _ollama_core.cpu_sibling_id(iid)
                reg = _ollama_core.registration_plan(_orch.OLLAMA_INSTANCES, addr, p,
                                                     has_gpu=False, preferred_id=sid)
                if reg["action"] == "create":
                    add = _rawcap("ollama.add_instance")
                    await add(id=sid, url=reg["url"], has_gpu=False,
                              label=f"{inst.get('label') or iid} (CPU)",
                              num_thread=_ollama_core.registration_threads(False))
                row["result"]["registered"] = reg["instance_id"]
        await emit_event({"type": "nodes.ollama.tune", "instance": iid,
                          "action": plan["action"], "ok": bool((row.get("result") or {}).get("ok"))})
        row["threads"] = await _threads_step(iid, inst, hid, dry_run, force)
        out.append(row)
    return {"ok": all((r.get("result") or {}).get("ok", True)
                      and (r.get("threads") or {}).get("ok", True) for r in out),
            "dry_run": bool(dry_run), "nodes": out}


#: The node's own Ollama unit, resolved ON the node (ollama-vera on the fleet,
#: stock `ollama` on a fresh install).
_UNIT_EXPR = "$(systemctl is-active --quiet ollama-vera && echo ollama-vera || echo ollama)"


async def _threads_step(iid: str, inst: Dict, hid: str, dry_run: bool, force: bool) -> Dict[str, Any]:
    """The runner thread default on the node's CPU Ollama: the node's own unit
    on a CPU node, its CPU sibling on a GPU node (none registered: nothing)."""
    if inst.get("has_gpu"):
        sid = _ollama_core.cpu_sibling_id(iid)
        t_inst = (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).get(sid)
        if not t_inst:
            return {"plan": {"action": "none", "why": "no CPU sibling registered"}}
        return await _apply_threads(hid, sid, t_inst,
                                    _ollama_core.CPU_SIBLING_UNIT.replace(".service", ""), dry_run, force)
    return await _apply_threads(hid, iid, inst, _UNIT_EXPR, dry_run, force)


def _node_threads(inst: Dict) -> int:
    return _ollama_core.registration_threads(False, (inst or {}).get("num_thread"))


def _restart_unit_script(unit_expr: str, dropin_name: str, dropin_text: str) -> str:
    """Write one drop-in on the unit, restart it, and print VERA_TUNED once the
    Ollama answers on the address the unit itself binds."""
    return (f"U={unit_expr}; D=/etc/systemd/system/$U.service.d; mkdir -p $D && "
            f"printf '%s' {shlex.quote(dropin_text)} > $D/{dropin_name} && "
            "systemctl daemon-reload && systemctl restart $U && "
            "H=$(systemctl show $U -p Environment --value | tr ' ' '\\n' | sed -n 's/^OLLAMA_HOST=//p'); "
            "for i in $(seq 1 45); do curl -fsS -m 3 http://${H:-127.0.0.1:11434}/api/tags >/dev/null "
            "&& echo VERA_TUNED && break; sleep 2; done")


async def _apply_threads(hid: str, iid: str, inst: Dict, unit_expr: str,
                         dry_run: bool, force: bool) -> Dict[str, Any]:
    run = _rawcap("exec.ssh.run")
    want = _node_threads(inst)
    probe = await run(command=_ollama_core.threads_probe_cmd(unit_expr), host_id=hid, timeout=30) or {}
    cur = ""
    for line in (probe.get("stdout") or "").splitlines():
        if line.startswith("THREADS="):
            cur = line[len("THREADS="):].strip()
    plan = _ollama_core.threads_plan(cur, want)
    res: Dict[str, Any] = {"instance": iid, "plan": plan}
    if plan["action"] == "none" or dry_run:
        return res
    if int((inst or {}).get("in_use") or 0) > 0 and not force:
        res.update(ok=False, skipped="a generation is in flight on this node")
        return res
    r = await run(command=_restart_unit_script(unit_expr, _ollama_core.THREADS_DROPIN_NAME,
                                               _ollama_core.threads_dropin(want)),
                  host_id=hid, timeout=150) or {}
    ok = "VERA_TUNED" in (r.get("stdout") or "")
    res.update(ok=ok, error="" if ok else str(r.get("stderr") or r.get("stdout") or "no answer")[-300:])
    await emit_event({"type": "nodes.ollama.threads", "instance": iid, "threads": want, "ok": ok})
    return res


# ─────────────────────────────────────────────────────────────────────────────
# THE ACTIVITY TAP (edge/ollama_tap.py) IN FRONT OF EACH NODE'S OLLAMA
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Vera.vera.provisioning import ollama_tap_core as _tap_core
except Exception:                                    # worktree / app-free import
    from vera.provisioning import ollama_tap_core as _tap_core


async def _tap_redis_env() -> str:
    """The tap's Redis URL: the host's, re-pointed at a LAN address, with the
    node user's credential (never the host's)."""
    import os as _os
    from Vera.vera.provisioning.components_core import rewrite_host
    comp = sys.modules.get("components_capabilities")
    lan = (_os.getenv("VERA_ADVERTISE_HOST", "")
           or (comp._primary_lan_ip() if comp is not None and hasattr(comp, "_primary_lan_ip") else ""))
    # prod's REDIS_URL names localhost; a node must be given a LAN address
    url = rewrite_host(getattr(_orch, "REDIS_URL", "") or "", lan)
    ra = sys.modules.get("redis_auth_capabilities")
    if ra is not None:
        url = await ra.node_redis_url(url)
    return f"VERA_TAP_REDIS_URL={url}\n"


@capability(
    "nodes.ollama.tap",
    http_method="POST", http_path="/nodes/ollama/tap", http_tags=["nodes", "ollama"],
    memory="off", redact_result=True,
    description="Put the activity tap (edge/ollama_tap.py) in front of every Ollama server "
                "on the nodes, so the Estate activity pane sees every request - Vera's, "
                "sandboxes', external callers' - with prompt, response and stats (user, "
                "2026-09-28). The tap takes the PUBLIC port; that Ollama moves to "
                "127.0.0.1:port+10 (a drop-in on its unit, which restarts it). Callers keep "
                "the same address. A cutover that fails rolls itself back. A tap running an "
                "older source than the host's (its /vera-tap/health `source`) is 'stale' and is "
                "refreshed: new source, the tap restarted, Ollama untouched - skipped while "
                "calls are in flight unless force. Requests pass "
                "through byte for byte. Dry run by default; refused from a dev sandbox. "
                "Inputs: dry_run (bool=true), instance_ids (list - default all), force "
                "(bool - cut over even with a generation in flight). Output: {ok, nodes:[{"
                "instance, host, port, state, result}]}.",
)
async def cap_nodes_ollama_tap(dry_run: bool = True, instance_ids: Optional[List[str]] = None,
                               force: bool = False, trace_id=None) -> Dict:
    import base64 as _b64
    from pathlib import Path as _Path
    if not dry_run and _orch.is_dev_sandbox():
        return {"ok": False, "error": "this is a dev sandbox: the Ollama nodes are prod's"}
    run = _rawcap("exec.ssh.run")
    lst = _rawcap("exec.ssh.hosts.list")
    if not run or not lst:
        return {"ok": False, "error": "exec.ssh unavailable"}
    # The install carries the tap source and the Redis credential on STDIN,
    # through exec's internal runner - never as the arguments of exec.ssh.run,
    # which are recorded (the same rule as the native worker install).
    stdin_run = getattr(sys.modules.get(getattr(run, "__module__", "") or ""),
                        "ssh_run_stored", None)
    if stdin_run is None:
        return {"ok": False, "error": "exec module has no ssh_run_stored (stdin transport)"}
    by_addr = {h.get("host"): h.get("id") for h in ((await lst()) or {}).get("hosts", [])
               if h.get("host") and h.get("id")}
    src = (_Path(__file__).resolve().parents[2] / "edge" / "ollama_tap.py").read_bytes()
    src_b64 = _b64.b64encode(src).decode()
    host_version = _tap_core.source_version(src)
    insts = dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {})
    installed_on: set = set()
    out = []
    for iid in [i for i in (instance_ids or list(insts)) if i in insts]:
        inst = insts[iid]
        u = urlparse(str(inst.get("url") or ""))
        addr, port = u.hostname or "", u.port or 11435
        row: Dict[str, Any] = {"instance": iid, "host": addr, "port": port}
        hid = by_addr.get(addr)
        if not hid:
            row.update(state="skip", why="no stored SSH credential for this address")
            out.append(row)
            continue
        st = _tap_core.parse_status((await run(command=_tap_core.status_cmd(port),
                                                 host_id=hid, timeout=30) or {}).get("stdout") or "")
        state = _tap_core.tap_state(st, host_version)
        if state == "tapped":
            row.update(state="tapped", health=st["health"])
            out.append(row)
            continue
        if state == "stale":
            # running an older tap: refresh the source and restart the tap only
            row.update(state="stale", running=st["health"].get("source") or "(unversioned)",
                       version=host_version)
            if dry_run:
                out.append(row)
                continue
            if int(st["health"].get("inflight") or 0) > 0 and not force:
                row["result"] = {"ok": False, "skipped": "calls in flight through the tap"}
                out.append(row)
                continue
            if addr not in installed_on:
                ires = await stdin_run(hid, _tap_core.install_cmd(), timeout=300, input=src_b64)
                if "VERA_TAP_INSTALLED" not in (ires.get("stdout") or ""):
                    row["result"] = {"ok": False, "error": "install failed: " + str(
                        ires.get("stderr") or ires.get("error") or "")[-300:]}
                    out.append(row)
                    continue
                installed_on.add(addr)
            res = await run(command=_tap_core.refresh_cmd(port), host_id=hid, timeout=90) or {}
            ok = _tap_core.DONE in (res.get("stdout") or "")
            row["result"] = {"ok": ok, "refreshed": True,
                             "error": "" if ok else str(res.get("stdout") or res.get("stderr") or "")[-300:]}
            await emit_event({"type": "nodes.ollama.tap", "instance": iid, "ok": ok, "refreshed": True})
            out.append(row)
            continue
        # The unit that owns this port: the CPU sibling, else the node's own
        # Ollama (ollama-vera on the fleet, stock `ollama` on a fresh install) -
        # resolved on the node, where the script runs.
        if port == _ollama_core.CPU_SIBLING_PORT:
            unit_expr, row["unit"] = "ollama-vera-cpu", "ollama-vera-cpu"
        else:
            unit_expr = "$(systemctl is-active --quiet ollama-vera && echo ollama-vera || echo ollama)"
            row["unit"] = "ollama-vera (or ollama)"
        row["state"] = "untapped"
        if dry_run:
            out.append(row)
            continue
        if int(inst.get("in_use") or 0) > 0 and not force:
            row["result"] = {"ok": False, "skipped": "a generation is in flight on this node"}
            out.append(row)
            continue
        if addr not in installed_on:
            sres = await stdin_run(hid, _tap_core.secret_cmd(), timeout=30,
                                   input=await _tap_redis_env())
            ires = await stdin_run(hid, _tap_core.install_cmd(), timeout=300, input=src_b64)
            if ("VERA_TAP_SECRET" not in (sres.get("stdout") or "")
                    or "VERA_TAP_INSTALLED" not in (ires.get("stdout") or "")):
                row["result"] = {"ok": False, "error": "install failed: " + str(
                    ires.get("stderr") or sres.get("stderr") or ires.get("error") or "")[-300:]}
                out.append(row)
                continue
            installed_on.add(addr)
        script = _tap_core.cutover_script("__UNIT__", iid, port)
        script = f"UNIT={unit_expr}; " + script.replace("__UNIT__", "$UNIT")
        res = await run(command=script, host_id=hid, timeout=180) or {}
        so = res.get("stdout") or ""
        ok = _tap_core.DONE in so and _tap_core.ROLLED_BACK not in so
        row["result"] = {"ok": ok, "rolled_back": _tap_core.ROLLED_BACK in so,
                         "error": "" if ok else so[-400:]}
        await emit_event({"type": "nodes.ollama.tap", "instance": iid, "ok": ok})
        out.append(row)
    return {"ok": all((r.get("result") or {}).get("ok", True) for r in out),
            "dry_run": bool(dry_run), "nodes": out}


def _settings_target(iid: str, insts: Dict[str, Dict]) -> Tuple[str, str]:
    """(address, unit expression) for an instance: the sibling unit for a
    '<gpu>-cpu' sibling, the node's own unit otherwise."""
    inst = insts.get(iid) or {}
    addr = urlparse(str(inst.get("url") or "")).hostname or ""
    parent = iid[:-len("-cpu")] if iid.endswith("-cpu") else ""
    if parent and (insts.get(parent) or {}).get("has_gpu"):
        return addr, _ollama_core.CPU_SIBLING_UNIT.replace(".service", "")
    return addr, _UNIT_EXPR


async def _blob_names(url: str) -> Dict[str, str]:
    """sha256-<digest> -> model tag(s), from the node's /api/tags."""
    out: Dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{url}/api/tags")
            for m in (r.json() or {}).get("models") or []:
                d = "sha256-" + str(m.get("digest") or "")
                out[d] = (out[d] + ", " if d in out else "") + str(m.get("name") or "")
    except Exception:
        pass
    return out


@capability(
    "nodes.ollama.settings",
    http_method="GET", http_path="/nodes/ollama/settings", http_tags=["nodes", "ollama"],
    memory="off", silent=True,
    description="Everything tuned on each Ollama node, read live: Vera's registry values (enabled, "
                "priority, num_thread - sent with every routed call - GPU or CPU), the unit's "
                "Ollama / llama.cpp / GPU environment flags and every systemd drop-in with its "
                "contents, the custom flags Vera manages, and the runners loaded right now (model, "
                "context, parallel slots, the -t they were started with and their OS thread count - "
                "a runner with no -t on a CPU node runs llama.cpp's default, 24 on 12 CPUs). "
                "Read-only. Input: instance_ids (list - default all). Output: {nodes:[...]}.",
)
async def cap_nodes_ollama_settings(instance_ids: Optional[List[str]] = None, trace_id=None) -> Dict:
    run = _rawcap("exec.ssh.run")
    lst = _rawcap("exec.ssh.hosts.list")
    if not run or not lst:
        return {"ok": False, "error": "exec.ssh unavailable"}
    by_addr = {h.get("host"): h.get("id") for h in ((await lst()) or {}).get("hosts", [])
               if h.get("host") and h.get("id")}
    insts = dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {})
    out = []
    for iid in [i for i in (instance_ids or sorted(insts)) if i in insts]:
        inst = insts[iid]
        addr, unit = _settings_target(iid, insts)
        row: Dict[str, Any] = {
            "instance": iid, "label": inst.get("label", iid), "url": inst.get("url"),
            "has_gpu": bool(inst.get("has_gpu")), "enabled": inst.get("enabled", True),
            "priority": inst.get("priority"), "status": inst.get("status"),
            "num_thread": None if inst.get("has_gpu") else _node_threads(inst),
            "num_thread_set": inst.get("num_thread")}
        hid = by_addr.get(addr)
        if not hid:
            row["error"] = "no stored SSH login for this address"
            out.append(row)
            continue
        r = await run(command=_ollama_core.settings_probe_cmd(unit), host_id=hid, timeout=40) or {}
        st = _ollama_core.parse_settings(r.get("stdout") or "")
        names = await _blob_names(str(inst.get("url") or ""))
        for rn in st["runners"]:
            rn["model"] = (rn.get("model") or names.get(rn.get("blob") or "")
                           or rn.get("blob", "")[:19])
            rn["default_threads"] = (not rn.get("threads")) and not inst.get("has_gpu")
        custom = next((d for d in st["dropins"] if d["name"] == _ollama_core.CUSTOM_DROPIN_NAME), None)
        row.update(unit=st["unit"], env=st["env"], dropins=st["dropins"], runners=st["runners"],
                   custom=_ollama_core.custom_flags_from_dropin(custom["text"]) if custom else {},
                   threads_default=st["env"].get("LLAMA_ARG_THREADS", ""))
        out.append(row)
    return {"ok": True, "nodes": out}


@capability(
    "nodes.ollama.settings.set",
    http_method="POST", http_path="/nodes/ollama/settings/set", http_tags=["nodes", "ollama"],
    memory="off",
    description="Change what is tuned on one Ollama node. num_thread (int, CPU nodes and the GPU "
                "node's CPU sibling): stored in Vera's registry (sent with every routed call) AND "
                "written as the unit's runner default (LLAMA_ARG_THREADS), so callers that send "
                "none get it too. flags ({KEY: value | null}): custom OLLAMA_* / LLAMA_ARG_* "
                "environment in a drop-in Vera owns (40-vera-custom.conf); null removes a key; "
                "keys Vera manages (OLLAMA_HOST, OLLAMA_MODELS, LLAMA_ARG_THREADS) are refused. "
                "Applying restarts the unit (its loaded models reload) and rolls back if Ollama "
                "does not answer. Dry run by default; a node with a generation in flight is "
                "skipped unless force; refused from a dev sandbox. Inputs: instance_id (str!), "
                "num_thread, flags, dry_run (bool=true), force. Output: {ok, plan, result}.",
)
async def cap_nodes_ollama_settings_set(instance_id: str = "", num_thread: Optional[int] = None,
                                        flags: Optional[Dict[str, Any]] = None,
                                        dry_run: bool = True, force: bool = False,
                                        trace_id=None) -> Dict:
    insts = dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {})
    inst = insts.get(instance_id)
    if not inst:
        return {"ok": False, "error": f"unknown instance {instance_id!r}", "known": sorted(insts)}
    if not dry_run and _orch.is_dev_sandbox():
        return {"ok": False, "error": "this is a dev sandbox: the Ollama nodes are prod's"}
    plan: Dict[str, Any] = {}
    errors: List[str] = []
    if num_thread is not None:
        if inst.get("has_gpu"):
            errors.append("num_thread applies to CPU Ollama (a CPU node or a GPU node's -cpu sibling)")
        elif not 1 <= int(num_thread) <= 64:
            errors.append("num_thread must be 1-64")
        else:
            plan["num_thread"] = int(num_thread)
    clean, ferr = _ollama_core.validate_flags(flags or {})
    errors += ferr
    if errors:
        return {"ok": False, "errors": errors}
    cur = (await cap_nodes_ollama_settings(instance_ids=[instance_id]))["nodes"][0]
    if cur.get("error"):
        return {"ok": False, "error": cur["error"]}
    new_flags = dict(cur.get("custom") or {})
    for k, v in clean.items():
        if v is None:
            new_flags.pop(k, None)
        else:
            new_flags[k] = v
    if clean:
        plan["flags"] = new_flags
        plan["flags_before"] = cur.get("custom") or {}
    if not plan:
        return {"ok": True, "dry_run": bool(dry_run), "plan": {}, "note": "nothing to change"}
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan, "current": cur,
                "note": "applying restarts " + str(cur.get("unit") or "the unit") + " (models reload)"}
    if int(inst.get("in_use") or 0) > 0 and not force:
        return {"ok": False, "skipped": "a generation is in flight on this node", "plan": plan}
    addr, unit = _settings_target(instance_id, insts)
    hid = {h.get("host"): h.get("id") for h in ((await _rawcap("exec.ssh.hosts.list")()) or {})
           .get("hosts", []) if h.get("host")}.get(addr)
    run = _rawcap("exec.ssh.run")
    result: Dict[str, Any] = {}
    if "num_thread" in plan:
        cfg = _orch.CAPABILITY_REGISTRY.get("ollama.node.config")
        await (cfg.get("raw") or cfg.get("func"))(id=instance_id, num_thread=plan["num_thread"])
        text = _ollama_core.threads_dropin(plan["num_thread"])
        r = await run(command=_restart_unit_script(unit, _ollama_core.THREADS_DROPIN_NAME, text),
                      host_id=hid, timeout=150) or {}
        result["num_thread"] = {"ok": "VERA_TUNED" in (r.get("stdout") or "")}
    if "flags" in plan:
        text = _ollama_core.custom_dropin(plan["flags"])
        r = await run(command=_restart_unit_script(unit, _ollama_core.CUSTOM_DROPIN_NAME, text),
                      host_id=hid, timeout=150) or {}
        ok = "VERA_TUNED" in (r.get("stdout") or "")
        if not ok:
            # roll back to the flags that were there, so a bad flag never
            # leaves the node without its Ollama
            back = _ollama_core.custom_dropin(plan["flags_before"])
            await run(command=_restart_unit_script(unit, _ollama_core.CUSTOM_DROPIN_NAME, back),
                      host_id=hid, timeout=150)
        result["flags"] = {"ok": ok, "rolled_back": not ok}
    await emit_event({"type": "nodes.ollama.settings", "instance": instance_id,
                      "changed": sorted(k for k in plan if k != "flags_before")})
    return {"ok": all(v.get("ok") for v in result.values()), "dry_run": False,
            "plan": plan, "result": result}


log.info("nodes: unified node estate capabilities loaded "
         "(%d components)", len(_COMPONENTS))
