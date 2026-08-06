"""connectors.py — unified "connectables" across Vera's infrastructure.

The operator can drive any web UI; this module lets it reach anything ALREADY
registered elsewhere in Vera without duplicating a single registry — it just
calls each subsystem's existing list cap and normalises the result into one
shape:

    {source, ref, label, url, console_url, type, driveable, group, detail}

Sources:
  • integration — the Integrations Hub (apps with base_url; access-gated). WEB.
  • ollama      — the Ollama cluster instances (HTTP APIs).               API.
  • node        — the unified nodes/hosts registry (nodes.list).      WEB|SSH.
  • docker      — containers with published ports (docker.hosts + ps).    WEB.
  • proxmox     — VM/LXC guests → their in-Vera noVNC console.            VNC.

Per the design: web-served things (apps, docker web ports, Proxmox noVNC,
code-server) are *driveable* by the operator; API/SSH-only things are listed and
their endpoint surfaced (drive them through their own caps). Everything is
best-effort — a missing/renamed subsystem cap yields no connectables, never an
error. ``call_cap`` (in-process dispatch) is injected, so this is unit-testable
with mocks and imports nothing heavy.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("vera.operator.connectors")

WEB, API, SSH, VNC = "web", "api", "ssh", "vnc"
CONNECTOR_SOURCES = ("integration", "ollama", "node", "docker", "proxmox")

CallCap = Callable[..., Awaitable[Any]]


def _mk(source: str, ref: str, label: str, *, url: str = "", console_url: str = "",
        ctype: str = WEB, driveable: bool = True, group: str = "", detail: str = "") -> Dict[str, Any]:
    return {"source": source, "ref": str(ref), "label": label or str(ref),
            "url": url, "console_url": console_url, "type": ctype,
            "driveable": bool(driveable), "group": group or source.title(),
            "detail": detail}


async def _try(call_cap: CallCap, name: str, **kw) -> Any:
    try:
        return await call_cap(name, **kw)
    except Exception as e:  # pragma: no cover - dispatch dependent
        log.debug("connectors: %s failed: %s", name, e)
        return {"error": str(e)}


def _host_of(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\w+://", "", s)          # strip scheme
    s = s.split("/")[0]                     # drop path
    return s


# ─────────────────────────────────────────────────────────────────────────────
#  INTEGRATIONS HUB
# ─────────────────────────────────────────────────────────────────────────────
async def from_integrations(call_cap: CallCap) -> List[Dict[str, Any]]:
    res = await _try(call_cap, "integration.list")
    items = (res or {}).get("integrations", []) if isinstance(res, dict) else []
    out = []
    for it in items:
        base = it.get("base_url") or ""
        access = it.get("access") or {}
        interact = bool(access.get("interact")) if isinstance(access, dict) else False
        sensitive = bool(it.get("sensitive"))
        out.append(_mk("integration", it.get("id", ""), it.get("label") or it.get("id", ""),
                       url=base, ctype=WEB, group="Integrations",
                       driveable=bool(base) and interact and not sensitive,
                       detail=(it.get("kind") or "") + ("" if base else " (no url)")
                               + ("" if interact else " · interact-locked")
                               + (" · sensitive" if sensitive else "")))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  OLLAMA CLUSTER
# ─────────────────────────────────────────────────────────────────────────────
async def from_ollama(call_cap: CallCap) -> List[Dict[str, Any]]:
    res = await _try(call_cap, "ollama.instances")
    inst = res
    if isinstance(res, dict) and "instances" in res:
        inst = res["instances"]
    out = []
    if isinstance(inst, dict):
        pairs = inst.items()
    elif isinstance(inst, list):
        pairs = [(i.get("id", ""), i) for i in inst if isinstance(i, dict)]
    else:
        pairs = []
    for iid, i in pairs:
        if not isinstance(i, dict):
            continue
        url = i.get("url", "")
        gpu = " · GPU" if i.get("has_gpu") else ""
        out.append(_mk("ollama", iid, i.get("label") or iid, url=url, ctype=API,
                       driveable=False, group="Ollama",
                       detail=(i.get("status", "") or "") + gpu))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  NODES / WORKERS  (unified machine registry)
# ─────────────────────────────────────────────────────────────────────────────
async def from_nodes(call_cap: CallCap) -> List[Dict[str, Any]]:
    res = await _try(call_cap, "nodes.list")
    nodes = (res or {}).get("nodes", []) if isinstance(res, dict) else []
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        url = n.get("url", "")
        host = n.get("host", "")
        is_web = bool(url) and url.startswith(("http://", "https://"))
        out.append(_mk("node", n.get("id", ""), n.get("label") or n.get("id", ""),
                       url=url if is_web else (f"http://{host}" if host else ""),
                       ctype=WEB if is_web else SSH, driveable=is_web, group="Nodes",
                       detail=host or n.get("kind", "")))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  DOCKER  (containers with published web ports)
# ─────────────────────────────────────────────────────────────────────────────
_PORT_STR = re.compile(r"(?:(\d+\.\d+\.\d+\.\d+|\[[0-9a-f:]+\]):)?(\d+)->(\d+)/tcp")


def _published_ports(container: Dict[str, Any]) -> List[int]:
    """Extract published host TCP ports from either the dict-list or string form."""
    ports = container.get("Ports") or container.get("ports") or []
    out: List[int] = []
    if isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict):
                pub = p.get("PublicPort") or p.get("public") or p.get("host")
                typ = (p.get("Type") or p.get("type") or "tcp").lower()
                if pub and typ == "tcp":
                    out.append(int(pub))
            elif isinstance(p, str):
                for m in _PORT_STR.finditer(p):
                    out.append(int(m.group(2)))
    elif isinstance(ports, str):
        for m in _PORT_STR.finditer(ports):
            out.append(int(m.group(2)))
    # de-dup, keep order
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


async def from_docker(call_cap: CallCap, max_hosts: int = 6) -> List[Dict[str, Any]]:
    hres = await _try(call_cap, "docker.hosts.list")
    hosts = (hres or {}).get("hosts", []) if isinstance(hres, dict) else []
    out = []
    for h in hosts[:max_hosts]:
        if not isinstance(h, dict):
            continue
        hid = h.get("id", "")
        addr = _host_of(h.get("addr") or h.get("host") or "") or "localhost"
        if addr in ("", "unix", "/var/run/docker.sock"):
            addr = "localhost"
        ps = await _try(call_cap, "docker.ps", host_id=hid, all=False)
        conts = (ps or {}).get("containers") if isinstance(ps, dict) else None
        if conts is None and isinstance(ps, list):
            conts = ps
        for c in (conts or []):
            if not isinstance(c, dict):
                continue
            name = (c.get("Names") or c.get("name") or c.get("Name") or "")
            if isinstance(name, list):
                name = name[0] if name else ""
            name = str(name).lstrip("/")
            for port in _published_ports(c):
                out.append(_mk("docker", f"{hid}|{addr}|{port}",
                               f"{name} :{port}", url=f"http://{addr}:{port}",
                               ctype=WEB, driveable=True, group="Docker",
                               detail=f"{h.get('label') or hid} · {c.get('Image') or c.get('image') or ''}"))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  PROXMOX  (guest consoles)
# ─────────────────────────────────────────────────────────────────────────────
async def from_proxmox(call_cap: CallCap) -> List[Dict[str, Any]]:
    cres = await _try(call_cap, "proxmox.cluster.list")
    clusters = (cres or {}).get("clusters", []) if isinstance(cres, dict) else []
    out = []
    for cl in clusters:
        if not isinstance(cl, dict):
            continue
        cid = cl.get("id", "")
        st = await _try(call_cap, "proxmox.status", cluster_id=cid)
        guests = (st or {}).get("guests", []) if isinstance(st, dict) else []
        for g in guests:
            if not isinstance(g, dict):
                continue
            vmid = g.get("vmid") or g.get("id")
            node = g.get("node", "")
            gtype = g.get("type") or ("lxc" if g.get("type") == "lxc" else "qemu")
            running = (g.get("status") == "running")
            out.append(_mk("proxmox", f"{cid}|{node}|{gtype}|{vmid}",
                           g.get("name") or f"vm {vmid}", ctype=VNC,
                           driveable=running, group="Proxmox",
                           detail=f"{node} · {gtype} · {g.get('status','')}"
                                  + ("" if running else " (start it to console)")))
    return out


_SOURCE_FNS = {
    "integration": from_integrations,
    "ollama": from_ollama,
    "node": from_nodes,
    "docker": from_docker,
    "proxmox": from_proxmox,
}


async def list_connectables(call_cap: CallCap,
                            sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """Aggregate connectables from the requested sources (default all). Each
    source is best-effort; one failing never breaks the rest."""
    want = [s for s in (sources or CONNECTOR_SOURCES) if s in _SOURCE_FNS]
    items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for s in want:
        try:
            items.extend(await _SOURCE_FNS[s](call_cap))
        except Exception as e:  # pragma: no cover
            errors[s] = str(e)
    groups: Dict[str, int] = {}
    for it in items:
        groups[it["group"]] = groups.get(it["group"], 0) + 1
    return {"connectables": items, "count": len(items), "groups": groups,
            "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
#  RESOLVE a single connectable → a target for a browser session
# ─────────────────────────────────────────────────────────────────────────────
async def resolve(source: str, ref: str, call_cap: CallCap) -> Dict[str, Any]:
    """Resolve (source, ref) to {url, base_url, canvas, type, driveable, error}.
    Called at connect time (e.g. mints a Proxmox console ticket then)."""
    source = (source or "").lower()
    ref = str(ref or "")
    if source == "integration":
        rec = await _try(call_cap, "integration.get", id=ref)
        base = (rec or {}).get("base_url") or ""
        if isinstance(rec, dict) and rec.get("error"):
            return {"error": rec["error"]}
        if not base:
            return {"error": f"integration '{ref}' has no resolvable URL"}
        return {"url": base, "base_url": base, "canvas": False, "type": WEB}

    if source == "ollama":
        res = await _try(call_cap, "ollama.instances")
        inst = res.get("instances", res) if isinstance(res, dict) else res
        url = ""
        if isinstance(inst, dict):
            url = (inst.get(ref) or {}).get("url", "")
        if not url and isinstance(inst, list):
            for i in inst:
                if isinstance(i, dict) and i.get("id") == ref:
                    url = i.get("url", "")
        if not url:
            return {"error": f"ollama instance '{ref}' not found"}
        return {"url": url, "base_url": url, "canvas": False, "type": API,
                "driveable": False}

    if source == "node":
        res = await _try(call_cap, "nodes.list")
        for n in (res or {}).get("nodes", []) if isinstance(res, dict) else []:
            if isinstance(n, dict) and n.get("id") == ref:
                url = n.get("url") or (f"http://{n.get('host')}" if n.get("host") else "")
                if not url:
                    return {"error": f"node '{ref}' has no URL", "type": SSH}
                web = url.startswith(("http://", "https://"))
                return {"url": url, "base_url": url, "canvas": False,
                        "type": WEB if web else SSH, "driveable": web}
        return {"error": f"node '{ref}' not found"}

    if source == "docker":
        parts = ref.split("|")
        if len(parts) >= 3:
            addr, port = parts[1], parts[2]
            url = f"http://{addr}:{port}"
            return {"url": url, "base_url": url, "canvas": False, "type": WEB}
        return {"error": f"bad docker ref '{ref}' (want host|addr|port)"}

    if source == "proxmox":
        parts = ref.split("|")
        if len(parts) < 4:
            return {"error": f"bad proxmox ref '{ref}' (want cluster|node|type|vmid)"}
        cid, node, gtype, vmid = parts[0], parts[1], parts[2], parts[3]
        kind = "vnc" if gtype in ("qemu", "kvm") else "term"
        tk = await _try(call_cap, "proxmox.console.ticket", cluster_id=cid, node=node,
                        guest_type=gtype, vmid=vmid, kind=kind)
        if isinstance(tk, dict):
            url = tk.get("console_url") or tk.get("deeplink_url") or ""
            if url:
                return {"url": url, "base_url": _origin(url), "canvas": True,
                        "type": VNC, "ticket": tk}
        return {"error": f"could not open a console for proxmox guest {vmid}"}

    return {"error": f"unknown connector source '{source}'"}


def _origin(url: str) -> str:
    m = re.match(r"^(\w+://[^/]+)", url or "")
    return m.group(1) if m else url
