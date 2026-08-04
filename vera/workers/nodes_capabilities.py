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
import sys
import time
from typing import Any, Dict, List, Optional
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
                "port}, quantization, install_deps:bool). Output: {ok, node, "
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
                      "port": ports.get(key), "warning": ""})

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
_OLLAMA_CT_INSTALL = (
    "command -v ollama >/dev/null 2>&1 || "
    "(command -v curl >/dev/null 2>&1 || (apt-get -qq update && apt-get -qq -y install curl); "
    "curl -fsSL https://ollama.com/install.sh | sh); "
    "systemctl enable --now ollama && sleep 2 && systemctl is-active ollama"
)


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


async def _register_ollama(node: Dict, port: int, has_gpu: bool) -> Dict:
    add = _rawcap("ollama.add_instance")
    if not add:
        return {"error": "ollama.add_instance unavailable"}
    addr = node.get("addr") or "localhost"
    iid = f"node-{re.sub(r'[^a-zA-Z0-9]+', '-', addr)}-{port}"
    url = f"http://{addr}:{port}"
    res = await add(id=iid, url=url, has_gpu=has_gpu,
                    label=f"{node.get('label', addr)} (ollama)")
    return {"ok": True, "instance_id": iid, "url": url, "result": res}


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
                node, int(ports.get("ollama") or 11434), bool(gpus))
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
            r = await pxm._node_ssh(pmx.get("cluster_id", ""), pve_node,
                                    pxm._sh(pxm._pct_exec(int(pmx.get("vmid") or 0),
                                                          _OLLAMA_CT_INSTALL)),
                                    timeout=900)
            active = "active" in (r.get("stdout", "") or "")
            out = {"ok": active, "log": (r.get("stdout", "") or "")[-800:]}
            if active:
                out["register"] = await _register_ollama(node, port, bool(gpus))
            return out
        inst = _rawcap("provision.install")
        if not inst:
            return {"error": "provision.install unavailable"}
        ires = await inst(host_id=hid, target="ollama", sudo=True, timeout=900)
        out = {"ok": bool(ires.get("ok")), "install": ires}
        if ires.get("ok"):
            out["register"] = await _register_ollama(node, port, bool(gpus))
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
                       repo_url=str(opt.get("repo_url", "")))
        return {"ok": bool(res.get("ok")), "worker": res}

    # ── bundled edge components over ssh ────────────────────────────────────
    if key in ("gpu_inference", "onnx_runtime", "mesh_gateway"):
        dep = _rawcap("provision.deploy")
        if not dep:
            return {"error": "provision.deploy unavailable"}
        kwargs: Dict[str, Any] = {
            "host_id": hid, "component": key,
            "install_deps": bool(opt.get("install_deps", True)),
            "launch": True, "systemd": bool(opt.get("systemd", False))}
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
                "Vera's cluster. Inputs: node_id (str!), components (list!), "
                "backend (str='auto'), options (dict — gpus, model (HF id, "
                "required for vllm), hf_home, ports{}, quantization, pve_node, "
                "install_deps, vera_url, start). Emits nodes.provision.progress "
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
    description="Get the estate backup config + recent run log. Output: "
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
                "pass proxmox/docker dicts to override one-off. Output: {ok, "
                "proxmox:[…], docker:[…]}.",
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
}


@capability(
    "nodes.sync.get",
    http_method="GET", http_path="/nodes/sync", http_tags=["nodes"],
    memory="off", silent=True,
    description="Get the share-tree auto-sync schedule (pxstore.fs.sync on a "
                "timer). Output: {config}.",
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
                "keep). Output: {ok, config}.",
)
async def cap_sync_set(enabled: Optional[bool] = None,
                       interval_hours: Optional[int] = None,
                       clusters: Optional[Dict] = None, trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_SYNC, _DEFAULT_SYNC)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if interval_hours:
        cfg["interval_hours"] = max(1, int(interval_hours))
    if isinstance(clusters, dict):
        cfg["clusters"] = {str(k): [str(n) for n in (v or [])]
                           for k, v in clusters.items()}
    await _json_cfg_put(KEY_SYNC, cfg)
    return {"ok": True, "config": cfg}


@capability(
    "nodes.sync.run",
    http_method="POST", http_path="/nodes/sync/run", http_tags=["nodes"],
    memory="off",
    description="Rebuild the share tree now on every configured node "
                "(pxstore.fs.sync per cluster/node; falls back to every "
                "cluster with a node→SSH mapping when nothing is configured). "
                "Output: {ok, results:[{cluster_id,node,ok,error}]}.",
)
async def cap_sync_run(trace_id=None) -> Dict:
    cfg = await _json_cfg(KEY_SYNC, _DEFAULT_SYNC)
    fs_sync = _rawcap("pxstore.fs.sync")
    if not fs_sync:
        return {"error": "pxstore.fs.sync unavailable"}
    px = await _pxstore_cfgs()
    plan: List[tuple] = []
    wanted = cfg.get("clusters") or {}
    for cid, pcfg in px.items():
        if wanted and cid not in wanted:
            continue
        nodes = wanted.get(cid) or list((pcfg.get("node_hosts") or {}).keys())
        for n in nodes:
            plan.append((cid, n))
    results = []
    for cid, n in plan:
        try:
            r = await fs_sync(cluster_id=cid, node=n)
            results.append({"cluster_id": cid, "node": n,
                            "ok": bool(r.get("ok")),
                            "linked": len(r.get("linked") or []),
                            "error": str(r.get("error", ""))[:300]})
        except Exception as e:
            results.append({"cluster_id": cid, "node": n, "ok": False,
                            "error": str(e)[:300]})
    cfg["last_run"] = time.time()
    await _json_cfg_put(KEY_SYNC, cfg)
    ok = bool(results) and all(r["ok"] for r in results)
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

_TEMP_SCRIPT = r"""
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
  (sudo -n ipmitool sdr type temperature 2>/dev/null || ipmitool sdr type temperature 2>/dev/null)
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


def _parse_ipmi_temps(txt: str) -> Dict[str, float]:
    """Parse `ipmitool sdr type temperature` rows:
    'Inlet Ambient    | 01h | ok  |  7.1 | 21 degrees C' -> {"Inlet Ambient": 21.0}.
    Skips sensors with no reading ('No Reading', 'Disabled', non-numeric)."""
    temps: Dict[str, float] = {}
    for ln in (txt or "").splitlines():
        if "|" not in ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 5:
            continue
        name, reading = parts[0], parts[-1]
        if not name:
            continue
        m = re.match(r"(-?\d+(?:\.\d+)?)", reading)
        if not m:
            continue
        try:
            temps[name] = round(float(m.group(1)), 1)
        except ValueError:
            continue
    return temps


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


def _parse_smart_drives(txt: str) -> Dict[str, float]:
    """Parse the SMART_BEGIN..SMART_END block (one smartctl -a dump per
    SMART_DEV|<path> .. SMART_DEV_END section) into {"drive sda": 34.0, ...}."""
    temps: Dict[str, float] = {}
    dev = None
    buf: List[str] = []
    for ln in (txt or "").splitlines():
        if ln.startswith("SMART_DEV|"):
            dev = ln.split("|", 1)[1].strip()
            buf = []
        elif ln.strip() == "SMART_DEV_END":
            if dev:
                t = _parse_smartctl_temp("\n".join(buf))
                if t is not None:
                    temps[f"drive {dev.rsplit('/', 1)[-1]}"] = t
            dev = None
        elif dev is not None:
            buf.append(ln)
    return temps


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


async def _probe_host_temp(host: Dict) -> None:
    host_id = host.get("id", "")
    if not host_id:
        return
    label = host.get("label") or host.get("host") or host_id
    try:
        r = await _ssh(host_id, _TEMP_SCRIPT, timeout=25)
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

        if "IPMI_BEGIN" in out:
            body = out.split("IPMI_BEGIN", 1)[1].split("IPMI_END", 1)[0]
            temps.update(_parse_ipmi_temps(body))
        elif "NO_IPMI" in out:
            missing_tools.append("ipmitool")

        if "SMART_BEGIN" in out:
            body = out.split("SMART_BEGIN", 1)[1].split("SMART_END", 1)[0]
            temps.update(_parse_smart_drives(body))
        elif "NO_SMART" in out:
            missing_tools.append("smartmontools")

        if missing_tools:
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
        _TEMP_CACHE[host_id] = {
            "host_id": host_id, "label": label, "pve": bool(facts.get("pve")),
            "temps": temps, "max_c": max(temps.values()) if temps else None,
            "updated_at": now_iso(), "error": error,
        }
    except Exception as e:
        _TEMP_CACHE[host_id] = {
            "host_id": host_id, "label": label, "pve": False,
            "temps": {}, "max_c": None, "updated_at": now_iso(), "error": str(e)[:200],
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
    description="Core temperatures for every SSH-registered node, probed every "
                f"{int(TEMP_PROBE_SEC)}s across three complementary layers: "
                "sensors -A -u (CPU package/core, falling back to "
                "/sys/class/thermal), ipmitool sdr type temperature (the "
                "iLO/BMC's full sensor list — inlet ambient, DIMM zones, PSU, "
                "and on servers with a smart-array backplane, per-bay drive "
                "temps), and smartctl per block device (direct per-drive SMART "
                "temperature). Installs whichever of lm-sensors/ipmitool/"
                "smartmontools is missing, once per host with a long backoff. "
                "Named/tagged into the obs.* umbrella like obs.cluster, though "
                "implemented here since this module owns the SSH probe. Output: "
                "{hosts:[{host_id,label,pve,temps:{sensor:celsius},max_c,"
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
    enr = _rawcap("proxmox.guest.enroll")
    if not enr:
        out["enroll_error"] = "proxmox.guest.enroll unavailable"
        return out
    # The CT needs to boot and pull a DHCP lease before its IP is detectable —
    # retry the enroll until the deadline instead of failing on the first probe.
    deadline = time.time() + max(15, int(wait_secs))
    last: Dict = {}
    while time.time() < deadline:
        await asyncio.sleep(6)
        try:
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
                                            bool(opt.get("gpus"))))})
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


log.info("nodes: unified node estate capabilities loaded "
         "(%d components)", len(_COMPONENTS))
