"""Live operations - the whole estate as planes of nodes with the requests in
flight running the pipes between them.

Pure functions over the readers' answers (tests/test_ops_core.py runs them
without Vera). `build(src)` takes {reader name: answer | {error}} and returns
the page's snapshot:

  planes   the seven planes, top to bottom: clients, work in flight, Vera core,
           services, runtimes, hosts, devices & mesh - each with its count
  nodes    {id, label, plane, domain, status, detail, load, inflight, errors,
            temp, ref, kind, ...} - the domain is the x lattice (compute, data,
            storage, edge, dev), the plane the y
  links    {a, b, kind} - kind in req · data · runs · repo · mesh
  inflight what is running right now, by kind, with the pipe it runs on
  errors   the findings, each pinned to a node when one is named
  events   the last events, trimmed, for a node's log
  series   per node, the last hour of a reading (tps, cpu) for the inspector
  sources  {reader: 'ok' | 'error text'} - a reader that failed is named, its
           nodes are simply missing, and the page says so

Nothing here reads anything: ops_capabilities gathers the readers (each with
its own timeout) and hands the answers in.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

PLANES: List[Tuple[str, str, str]] = [
    ("client", "Clients", "who is connected to Vera now: machines holding connections to it, the agents and people calling its capabilities, editors, door devices"),
    ("work", "Work in flight", "loops, programmes, pipelines, dreams, the census, background jobs"),
    ("core", "Vera core", "router, scheduler, capability bus, fabric, workers, dream, Loop Lab"),
    ("service", "Services", "ollama instances, redis, postgres, chroma, neo4j, the stack's services"),
    ("runtime", "Runtimes", "running guests, the docker hosts, session sandboxes"),
    ("host", "Hosts", "Proxmox nodes, storage boxes, SSH hosts"),
    ("edge", "Devices & mesh", "ESP32 mesh devices"),
]
DOMAINS = ["compute", "data", "storage", "edge", "dev"]
COMPUTE, DATA, STORAGE, EDGE, DEV = 0, 1, 2, 3, 4

PIPELINES_SHOWN = 6          # the newest pipelines still in flight stand on the work plane; the rest are a count
LINK_KINDS = [("req", "requests"), ("data", "reads + writes"), ("runs", "runs on"), ("repo", "repo + build"), ("mesh", "mesh radio")]
INFLIGHT_KINDS = ["llm.generate", "embed", "cap call", "fabric write", "git op", "background"]

# a guest's domain, from its name
_DOMAIN_HINTS: List[Tuple[str, int]] = [
    (r"ollama|gpu|cpu-2\d\d|llm|vllm|model", COMPUTE),
    (r"neo4j|chroma|redis|postgres|pg|db|dc\b|ldap|ipa|gitea|vs-|vector", DATA),
    (r"vfs|pbs|store|storage|backup|nas|garage|cloud|s3", STORAGE),
    (r"nwm|mesh|netctl|dns|wg|vpn|gateway|tc-|router|print", EDGE),
]


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or v == "":
            return default
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return default


def _ok(src: Mapping[str, Any], name: str) -> Optional[Any]:
    v = src.get(name)
    if v is None or (isinstance(v, Mapping) and v.get("error")):
        return None
    return v


def _err(src: Mapping[str, Any], name: str) -> str:
    v = src.get(name)
    if v is None:
        return "not read"
    if isinstance(v, Mapping) and v.get("error"):
        return str(v["error"])[:160]
    return "ok"


def _domain_of(label: str, default: int = DEV) -> int:
    s = str(label or "").lower()
    for pat, d in _DOMAIN_HINTS:
        if re.search(pat, s):
            return d
    return default


def _norm(label: str) -> str:
    """'Ollama-C (cpu-247)' -> 'ollama-c'; 'ollama126.vera.int' -> 'ollama126'."""
    s = str(label or "").lower()
    s = re.sub(r"\s*\(.*?\)\s*", "", s)
    s = s.split(".")[0]
    return s.strip()


def _age_s(ts: Any, now: Optional[datetime]) -> Optional[float]:
    if not ts or now is None:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (now - t).total_seconds())
    except ValueError:
        return None


def _node(id: str, label: str, plane: str, domain: int, **kw: Any) -> Dict[str, Any]:
    n = {"id": id, "label": label, "plane": plane, "domain": int(domain), "status": "ok", "detail": "",
         "load": None, "inflight": 0, "errors": 0, "temp": None, "ref": "", "kind": plane, "vmid": None,
         "caps": [], "req": {}}
    n.update(kw)
    return n


# ── the builders, one per source ─────────────────────────────────────────────
def _machines(src: Mapping[str, Any], own_ips: Iterable[str], nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Hosts and running guests; the Vera process's own machine is found by IP."""
    out: Dict[str, Any] = {"pve_nodes": {}, "by_ip": {}, "by_ssh": {}, "self": ""}
    m = _ok(src, "estate.machines")
    if not m:
        return out
    own = {str(i) for i in own_ips if i}
    rows = [r for r in (m.get("machines") or []) if isinstance(r, Mapping)]
    localhost_row = None
    for r in rows:
        kind = str(r.get("kind") or "")
        label = str(r.get("label") or r.get("id") or "?")
        ips = [str(i) for i in (r.get("ips") or []) if i] + ([str(r["addr"])] if r.get("addr") else [])
        if kind == "guest":
            if r.get("template") or str(r.get("status") or "") != "running":
                continue
            nid = "guest:" + str(r.get("vmid") if r.get("vmid") is not None else label)
            hw = [str(h) for h in (r.get("hardware") or [])]
            n = _node(nid, label, "runtime", _domain_of(label, DEV), kind="guest", status="ok",
                      detail=(str(r.get("type") or "guest") + (" · " + " · ".join(hw[:2]) if hw else "")),
                      ref="guest:" + str(r["vmid"]) if r.get("vmid") is not None else "", vmid=r.get("vmid"),
                      req={"name": "sysmon.status", "arguments": {}},
                      cluster_id=r.get("cluster_id") or "", pve_node=r.get("node") or "", type=r.get("type") or "",
                      ips=ips[:4], ssh_host_id=str(r.get("ssh_host_id") or ""))
            nodes[nid] = n
            if r.get("node"):
                links.append({"a": nid, "b": "pve:" + str(r["node"]), "kind": "runs"})
        elif kind == "proxmox-node":
            nid = "pve:" + str(r.get("node") or label)
            hw = [str(h) for h in (r.get("hardware") or [])]
            n = _node(nid, label, "host", COMPUTE, kind="host", detail=" · ".join(hw[:3]) or "proxmox node",
                      ref=("host:" + str(r["ssh_host_id"])) if r.get("ssh_host_id") else "",
                      req={"name": "proxmox.status", "arguments": {}},
                      ips=ips[:4], ssh_host_id=str(r.get("ssh_host_id") or ""))
            nodes[nid] = n
            out["pve_nodes"][str(r.get("node") or "")] = nid
        elif kind == "docker-host":
            nid = "dockerhost:" + str(r.get("docker_host_id") or label)
            n = _node(nid, label, "host", DEV, kind="docker-host", detail="docker host",
                      ref=("host:" + str(r["id"])) if r.get("id") else "",
                      req={"name": "docker.ping", "arguments": {"host_id": r.get("docker_host_id") or ""}},
                      ips=ips[:4], dhost=str(r.get("docker_host_id") or ""), ssh_host_id=str(r.get("ssh_host_id") or ""))
            nodes[nid] = n
        else:
            if str(r.get("addr") or "") in ("localhost", "127.0.0.1"):
                localhost_row = r
                continue
            nid = "host:" + _norm(label)
            n = _node(nid, label, "host", _domain_of(label, COMPUTE), kind="host", detail="ssh host",
                      ref=("host:" + str(r["ssh_host_id"])) if r.get("ssh_host_id") else "",
                      req={"name": "nodes.list", "arguments": {}},
                      ips=ips[:4], ssh_host_id=str(r.get("ssh_host_id") or ""))
            nodes[nid] = n
        for ip in ips:
            out["by_ip"][ip] = nid
        if r.get("ssh_host_id"):
            out["by_ssh"][str(r["ssh_host_id"])] = nid
    # the machine Vera runs on: the row whose IP is one of ours, else the 'localhost' SSH host
    for ip in own:
        if out["by_ip"].get(ip):
            out["self"] = out["by_ip"][ip]
            break
    if not out["self"] and localhost_row is not None:
        nid = "host:vera"
        nodes[nid] = _node(nid, "Vera host", "host", COMPUTE, kind="host", detail="the machine Vera runs on",
                           ref=("host:" + str(localhost_row["ssh_host_id"])) if localhost_row.get("ssh_host_id") else "",
                           req={"name": "sysmon.status", "arguments": {}},
                           ssh_host_id=str(localhost_row.get("ssh_host_id") or ""))
        out["self"] = nid
        if localhost_row.get("ssh_host_id"):
            out["by_ssh"][str(localhost_row["ssh_host_id"])] = nid
    elif localhost_row is not None and localhost_row.get("ssh_host_id") and out["self"]:
        out["by_ssh"][str(localhost_row["ssh_host_id"])] = out["self"]
        nodes[out["self"]]["detail"] = (nodes[out["self"]]["detail"] + " · Vera runs here").strip(" ·")
    if out["self"] in nodes:
        nodes[out["self"]]["domain"] = COMPUTE
        nodes[out["self"]]["role"] = "runs Vera"
    return out


def _docker(src: Mapping[str, Any], M: Mapping[str, Any], nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> str:
    """The docker hosts as runtimes, from sysmon; returns the id of the stack's host."""
    sy = _ok(src, "sysmon.status") or {}
    stack_host = ""
    dk = (sy.get("docker") or {}) if isinstance(sy, Mapping) else {}
    for h in dk.get("hosts") or []:
        if not isinstance(h, Mapping):
            continue
        hid = str(h.get("id") or "")
        nid = "docker:" + hid
        reach = bool(h.get("reachable"))
        n = _node(nid, "Docker · " + str(h.get("label") or hid), "runtime", DEV, kind="docker",
                  status="ok" if reach else "down",
                  detail=(f"{h.get('running', 0)} of {h.get('containers', 0)} running" if reach else str(h.get("error") or "unreachable")),
                  load=(100.0 * _num(h.get("running"), 0) / max(1.0, _num(h.get("containers"), 1))) if reach and h.get("containers") else None,
                  req={"name": "docker.ps", "arguments": {"host_id": hid}}, dhost=hid)
        nodes[nid] = n
        if hid == "local":
            stack_host = nid
            if M.get("self"):
                links.append({"a": nid, "b": M["self"], "kind": "runs"})
        elif "dockerhost:" + hid in nodes:
            links.append({"a": nid, "b": "dockerhost:" + hid, "kind": "runs"})
        else:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", hid)
            owner = M.get("by_ip", {}).get(m.group(1)) if m else None
            if owner and owner in nodes:
                links.append({"a": nid, "b": owner, "kind": "runs"})
                if nodes[owner].get("kind") == "guest" and nodes[owner]["domain"] == DEV:
                    nodes[owner]["role"] = "docker host"
    return stack_host


def _services(src: Mapping[str, Any], M: Mapping[str, Any], stack_host: str, nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> None:
    h = _ok(src, "obs.health") or {}
    # the four stores, as obs.health sees them
    for key, label in (("redis", "Redis"), ("postgres", "PostgreSQL"), ("chroma", "ChromaDB"), ("neo4j", "Neo4j")):
        if key in h:
            nid = "svc:" + key
            nodes[nid] = _node(nid, label, "service", DATA, kind="store", status="ok" if h.get(key) else "down",
                               detail="reachable" if h.get(key) else "not reachable", req={"name": "obs.health", "arguments": {}})
            if stack_host:
                links.append({"a": nid, "b": stack_host, "kind": "runs"})
    # the stack's other services (garage, ...)
    st = _ok(src, "docker.stack.status") or {}
    for s in st.get("services") or []:
        if not isinstance(s, Mapping) or not s.get("exists"):
            continue
        sid = str(s.get("id") or "")
        if "svc:" + sid in nodes or sid in ("ollama",):
            continue
        nid = "svc:" + sid
        nodes[nid] = _node(nid, str(s.get("label") or sid), "service", STORAGE if re.search(r"garage|minio|s3|store", sid) else DATA, kind="store",
                           status="ok" if s.get("running") else "down", detail=str(s.get("status") or ("running" if s.get("running") else "stopped")),
                           req={"name": "docker.stack.status", "arguments": {}})
        if stack_host:
            links.append({"a": nid, "b": stack_host, "kind": "runs"})
    # the ollama instances
    inst = _ok(src, "ollama.instances") or {}
    gate = _ok(src, "ollama.gate.status") or {}
    held = {str(g.get("node")): g for g in (gate.get("nodes") or []) if isinstance(g, Mapping)}
    for iid, q in inst.items():
        if not isinstance(q, Mapping):
            continue
        nid = "ollama:" + str(iid)
        g = held.get(str(iid)) or {}
        models = q.get("models") or []
        st_ = str(q.get("status") or "")
        busy = int(_num(q.get("in_use"), 0) or 0) + int(_num(g.get("held"), 0) or 0)
        n = _node(nid, str(q.get("label") or iid), "service", COMPUTE, kind="ollama",
                  status="down" if st_ in ("offline", "error", "down") else ("run" if busy else "ok"),
                  detail=f"{len(models)} models · {int(_num(q.get('latency_ms'), 0) or 0)} ms" + (" · gpu" if q.get("has_gpu") else ""),
                  inflight=busy, load=(100.0 * _num(g.get("held"), 0) / _num(g.get("capacity"), 1)) if g.get("gated") and _num(g.get("capacity"), 0) else None,
                  errors=int(_num(q.get("errors"), 0) or 0), caps=[str(x) for x in models][:12],
                  req={"name": "ollama.instances", "arguments": {}}, url=str(q.get("url") or ""), gate=g or None)
        nodes[nid] = n
        # runs on the guest or host that owns the instance's IP
        m = re.search(r"//([^:/]+)", str(q.get("url") or ""))
        host = M.get("by_ip", {}).get(m.group(1)) if m else None
        if host:
            links.append({"a": nid, "b": host, "kind": "runs"})
            if host in nodes and nodes[host].get("kind") == "guest":
                nodes[host]["domain"] = COMPUTE          # the role decides the lane, not the name
                nodes[host]["role"] = "serves " + str(q.get("label") or iid)


def _core(src: Mapping[str, Any], stack_host: str, nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> None:
    h = _ok(src, "obs.health") or {}
    gate = (h.get("gpu_gate") or {}) if isinstance(h, Mapping) else {}
    nodes["core:router"] = _node("core:router", "router", "core", COMPUTE, kind="core",
                                 status="run" if gate.get("busy") else "ok",
                                 detail=(f"gate {gate.get('held', 0)}/{gate.get('capacity', 1)} held" if gate else "routing"),
                                 inflight=int(_num(gate.get("held"), 0) or 0), req={"name": "ollama.gate.status", "arguments": {}})
    nodes["core:cap-bus"] = _node("core:cap-bus", "cap-bus", "core", DATA, kind="core",
                                  detail=f"{h.get('caps', 0)} registered" if h else "capability bus", req={"name": "obs.health", "arguments": {}})
    links.append({"a": "core:router", "b": "core:cap-bus", "kind": "req"})
    sched = _ok(src, "obs.scheduler")
    if isinstance(sched, list):
        runs = sum(int(_num(j.get("runs"), 0) or 0) for j in sched if isinstance(j, Mapping))
        nodes["core:scheduler"] = _node("core:scheduler", "scheduler", "core", COMPUTE, kind="core",
                                        detail=f"{len(sched)} jobs · {runs} runs", req={"name": "obs.scheduler", "arguments": {}})
        links.append({"a": "core:scheduler", "b": "svc:redis", "kind": "data"})
    fh = _ok(src, "fabric.health")
    if isinstance(fh, Mapping):
        q = int(_num(fh.get("write_queue_size"), 0) or 0)
        nodes["core:fabric"] = _node("core:fabric", "fabric", "core", DATA, kind="core",
                                     status="ok" if fh.get("writer_task_alive", True) else "warn",
                                     detail=f"{int(_num(fh.get('records_count'), 0) or 0):,} records" + (f" · queue {q}" if q else ""),
                                     inflight=q, req={"name": "fabric.health", "arguments": {}})
        links.append({"a": "core:cap-bus", "b": "core:fabric", "kind": "req"})
        links.append({"a": "core:fabric", "b": "svc:chroma", "kind": "data"})
        links.append({"a": "core:fabric", "b": "svc:neo4j", "kind": "data"})
    links.append({"a": "core:cap-bus", "b": "svc:redis", "kind": "data"})
    links.append({"a": "core:cap-bus", "b": "svc:postgres", "kind": "data"})
    w = _ok(src, "obs.workers")
    if isinstance(w, Mapping):
        busy = [k for k, v in w.items() if isinstance(v, Mapping) and str(v.get("status") or "") not in ("idle", "provisioned", "")]
        nodes["core:workers"] = _node("core:workers", "workers", "core", DEV, kind="core", status="run" if busy else "ok",
                                      detail=f"{len(busy)} busy of {len(w)}", inflight=len(busy), req={"name": "obs.workers", "arguments": {}})
        links.append({"a": "core:workers", "b": "core:router", "kind": "req"})
    d = _ok(src, "dream.scheduler.status")
    if isinstance(d, Mapping):
        nodes["core:dream"] = _node("core:dream", "dream", "core", DEV, kind="core",
                                    status="run" if d.get("in_cycle") else ("ok" if d.get("enabled") else "warn"),
                                    detail=("in a cycle" if d.get("in_cycle") else ("enabled" if d.get("enabled") else "off")) + f" · idle {int(_num(d.get('idle_minutes'), 0) or 0)} min",
                                    req={"name": "dream.scheduler.status", "arguments": {}})
        links.append({"a": "core:dream", "b": "core:router", "kind": "req"})
    bg = _ok(src, "background.status")
    if isinstance(bg, Mapping):
        running = str(bg.get("running") or "")
        nodes["core:background"] = _node("core:background", "background", "core", DEV, kind="core", status="run" if running else "ok",
                                         detail=(running if running else f"quiet {int(_num(bg.get('quiet_for_s'), 0) or 0)} s") + (f" · queue {bg.get('queue', {}).get('depth', 0)}" if isinstance(bg.get("queue"), Mapping) else ""),
                                         inflight=1 if running else 0, req={"name": "background.status", "arguments": {}})
        links.append({"a": "core:background", "b": "core:scheduler", "kind": "req"})
        if running:
            nodes["work:bg"] = _node("work:bg", running, "work", DEV, kind="work", status="run", detail="background job", inflight=1,
                                     req={"name": "background.status", "arguments": {}})
            links.append({"a": "work:bg", "b": "core:background", "kind": "req"})
    sb = _ok(src, "evolve.sandbox.list")
    if isinstance(sb, Mapping):
        cap = sb.get("capacity") or {}
        rows = [s for s in (sb.get("sandboxes") or []) if isinstance(s, Mapping)]
        up = [s for s in rows if s.get("running")]
        nodes["core:looplab"] = _node("core:looplab", "Loop Lab", "core", DEV, kind="core",
                                      status="warn" if cap.get("exhausted") else "ok",
                                      detail=f"{len(up)} of {len(rows)} sandboxes up" + (f" · {cap.get('available_slots')} slots free" if cap.get("available_slots") is not None else ""),
                                      req={"name": "evolve.sandbox.list", "arguments": {}})
        links.append({"a": "core:looplab", "b": "core:cap-bus", "kind": "req"})
        for s in up:
            nid = "sbx:" + str(s.get("name") or s.get("branch"))
            nodes[nid] = _node(nid, str(s.get("name") or s.get("branch")), "runtime", DEV, kind="sandbox", status="ok",
                               detail=str(s.get("branch") or "") + (f" · :{s.get('port')}" if s.get("port") else ""),
                               req={"name": "evolve.sandbox.status", "arguments": {"branch": s.get("branch") or ""}}, branch=s.get("branch") or "")
            if stack_host:
                links.append({"a": nid, "b": stack_host, "kind": "runs"})
    for nid in list(nodes):
        if nid.startswith("ollama:"):
            links.append({"a": "core:router", "b": nid, "kind": "req"})


def _work(src: Mapping[str, Any], nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> None:
    h = _ok(src, "obs.health") or {}
    c = (h.get("census") or {}) if isinstance(h, Mapping) else {}
    if c.get("busy"):
        nodes["work:census"] = _node("work:census", "census " + str(c.get("census_run") or ""), "work", COMPUTE, kind="work", status="run",
                                     detail=f"{c.get('template', '')} · {c.get('done', 0)}/{c.get('total', 0)} · {c.get('goal', '')}".strip(" ·"), inflight=1,
                                     req={"name": "census.live", "arguments": {}})
        links.append({"a": "work:census", "b": "core:router", "kind": "req"})
    pl = _ok(src, "loops.program.list") or {}
    for p in pl.get("programs") or []:
        if not isinstance(p, Mapping) or str(p.get("status") or "").lower() not in ("running", "active", "in_progress", "open"):
            continue
        nid = "prog:" + str(p.get("id") or p.get("name"))
        nodes[nid] = _node(nid, str(p.get("name") or p.get("id")), "work", COMPUTE, kind="work", status="run",
                           detail=f"programme · {p.get('status')}", inflight=len(p.get("loops") or []) or 1,
                           req={"name": "loops.program.list", "arguments": {}})
        links.append({"a": nid, "b": "core:router", "kind": "req"})
    pp = _ok(src, "evolve.pipeline.list") or {}
    live = [q for q in (pp.get("pipelines") or []) if isinstance(q, Mapping)
            and str(q.get("status") or "").lower() not in ("adopted", "merged", "closed", "rejected", "promoted", "abandoned", "failed", "done")]
    live.sort(key=lambda q: str(q.get("created_at") or ""), reverse=True)
    if len(live) > PIPELINES_SHOWN and "core:looplab" in nodes:
        nodes["core:looplab"]["detail"] += f" · {len(live) - PIPELINES_SHOWN} more pipelines in flight"
        nodes["core:looplab"]["pipelines_more"] = len(live) - PIPELINES_SHOWN
    for q in live[:PIPELINES_SHOWN]:
        st = str(q.get("status") or "").lower()
        nid = "pipe:" + str(q.get("id"))
        nodes[nid] = _node(nid, "pipeline " + str(q.get("id")), "work", DEV, kind="work",
                           status="warn" if st in ("review", "blocked") else "run",
                           detail=f"{st} · {q.get('branch', '')}".strip(" ·"), inflight=1,
                           req={"name": "evolve.pipeline.get", "arguments": {"id": q.get("id")}})
        links.append({"a": nid, "b": "core:looplab" if "core:looplab" in nodes else "core:cap-bus", "kind": "repo"})


def _mesh(src: Mapping[str, Any], now: Optional[datetime], nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> None:
    m = _ok(src, "mesh.nodes") or {}
    rows = [r for r in (m.get("nodes") or []) if isinstance(r, Mapping)]
    if not rows:
        return
    tr = m.get("transports") or {}
    nodes["core:mesh"] = _node("core:mesh", "mesh hub", "core", EDGE, kind="core",
                               detail=" · ".join(k for k, v in tr.items() if v) or "no transport up",
                               status="ok" if any(tr.values()) else "warn", req={"name": "mesh.nodes", "arguments": {}})
    links.append({"a": "core:mesh", "b": "core:cap-bus", "kind": "req"})
    ids = {str(r.get("node_id")) for r in rows}
    for r in rows:
        nid = "mesh:" + str(r.get("node_id"))
        age = _age_s(r.get("last_seen"), now)
        rssi = _num(r.get("rssi"))
        nodes[nid] = _node(nid, str(r.get("name") or r.get("node_id")), "edge", EDGE, kind="mesh",
                           status="down" if (age is not None and age > 600) else ("warn" if (rssi is not None and rssi < -75) else "ok"),
                           detail=(f"{int(rssi)} dBm" if rssi is not None else "") + (f" · {r.get('board')}" if r.get("board") else "") + (f" · fw {r.get('fw_version')}" if r.get("fw_version") else ""),
                           ref="mesh:" + str(r.get("node_id")), req={"name": "mesh.node", "arguments": {"node_id": r.get("node_id")}})
        parent = str(r.get("parent_id") or "")
        links.append({"a": nid, "b": ("mesh:" + parent) if parent and parent in ids else "core:mesh", "kind": "mesh"})


def _metrics(src: Mapping[str, Any], M: Mapping[str, Any], nodes: Dict[str, Dict[str, Any]]) -> None:
    """Load from sysmon, temperatures from obs.node_temps, onto the nodes they belong to."""
    sy = _ok(src, "sysmon.status") or {}
    px = (sy.get("proxmox") or {}) if isinstance(sy, Mapping) else {}
    for g in px.get("top_guests") or []:
        if not isinstance(g, Mapping):
            continue
        n = nodes.get("guest:" + str(g.get("vmid")))
        if n:
            n["load"] = _num(g.get("cpu_pct"))
            n["mem_pct"] = _num(g.get("mem_pct"))
            if str(g.get("status") or "") == "running" and (n["load"] or 0) >= 50:
                n["status"] = "run"
    res = (sy.get("resources") or {}) if isinstance(sy, Mapping) else {}
    if M.get("self") and M["self"] in nodes and res:
        nodes[M["self"]]["load"] = _num(res.get("cpu"))
        nodes[M["self"]]["mem_pct"] = _num(res.get("mem"))
    t = _ok(src, "obs.node_temps") or {}
    by_norm = {}
    for n in nodes.values():
        by_norm.setdefault(_norm(n["label"]), n)
    for h in t.get("hosts") or []:
        if not isinstance(h, Mapping):
            continue
        n = None
        hid = str(h.get("host_id") or "")
        if hid and M.get("by_ssh", {}).get(hid):
            n = nodes.get(M["by_ssh"][hid])
        if n is None:
            n = by_norm.get(_norm(h.get("label")))
        if n is None:
            continue
        if h.get("max_c") is not None:
            n["temp"] = _num(h.get("max_c"))
        du = h.get("disk_usage") or []
        root = next((d for d in du if isinstance(d, Mapping) and str(d.get("mount")) == "/"), None) or (du[0] if du and isinstance(du[0], Mapping) else None)
        if root and root.get("used_pct") is not None:
            n["disk_pct"] = _num(root.get("used_pct"))
        if h.get("error") and n.get("kind") == "host":
            n["note"] = str(h["error"])[:120]


def _requests(src: Mapping[str, Any], now: Optional[datetime], nodes: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], Dict[str, List[float]]]:
    """The model requests in flight (the last two minutes of the log), the last
    requests through each instance, and each instance's latency spread."""
    rl = _ok(src, "ollama.request_log") or {}
    inflight: List[Dict[str, Any]] = []
    through: Dict[str, List[Dict[str, Any]]] = {}
    lat: Dict[str, List[float]] = {}
    running_by: Dict[str, int] = {}
    for e in rl.get("entries") or []:
        if not isinstance(e, Mapping):
            continue
        inst = "ollama:" + str(e.get("instance") or "")
        ev = e.get("prompt_evidence") or {}
        kind = "embed" if (isinstance(ev, Mapping) and ev.get("kind") == "embed") or "embed" in str(e.get("model") or "") else "llm.generate"
        age = _age_s(e.get("ts"), now)
        ms = _num(e.get("elapsed_s"))
        row = {"n": f"{kind} {e.get('model', '')}".strip(), "kind": kind, "ms": (f"{ms:.1f}s" if ms is not None and ms >= 1 else (f"{int(ms * 1000)}ms" if ms is not None else "")), "status": str(e.get("status") or ""), "age": age}
        through.setdefault(inst, []).append(row)
        if ms is not None:
            lat.setdefault(inst, []).append(ms * 1000.0)
        st = str(e.get("status") or "")
        running = st in ("running", "started", "pending", "queued") or (ms is None and age is not None and age < 120)
        if running:
            inflight.append({"kind": kind, "a": "core:router", "b": inst, "age": age, "label": str(e.get("model") or "")})
            running_by[inst] = running_by.get(inst, 0) + 1
    # the gate's held slots and the log's running requests describe the same work: the larger count stands
    for inst, c in running_by.items():
        if inst in nodes:
            nodes[inst]["inflight"] = max(int(nodes[inst].get("inflight") or 0), c)
            nodes[inst]["status"] = "run"
    for k in through:
        through[k] = through[k][:8]
    return inflight, through, lat


def _events(src: Mapping[str, Any], now: Optional[datetime]) -> Tuple[List[Dict[str, Any]], int]:
    ev = _ok(src, "obs.events")
    if not isinstance(ev, list):
        return [], 0
    out = []
    open_calls: Dict[str, str] = {}
    for e in ev:
        if not isinstance(e, Mapping):
            continue
        t = str(e.get("type") or "")
        tid = str(e.get("trace_id") or "")
        if t == "cap.call" and tid:
            open_calls[tid] = str(e.get("name") or "")
        elif t in ("cap.ok", "cap.error", "cap.done") and tid:
            open_calls.pop(tid, None)
        out.append({"ts": str(e.get("ts") or "")[11:19], "type": t, "name": str(e.get("name") or e.get("node_id") or e.get("model") or ""),
                    "group": str(e.get("group") or ""), "trace": tid[:8], "text": str(e.get("args_preview") or e.get("detail") or e.get("error") or "")[:80]})
    return out[:100], len(open_calls)


def _errors(src: Mapping[str, Any], nodes: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    errs: List[Dict[str, Any]] = []
    by_ref = {n["ref"]: n for n in nodes.values() if n.get("ref")}
    by_norm = {_norm(n["label"]): n for n in nodes.values()}
    h = _ok(src, "estate.health") or {}
    for f in h.get("findings") or []:
        if not isinstance(f, Mapping) or str(f.get("severity") or "") not in ("error", "warn"):
            continue
        subj = str(f.get("subject") or "")
        ref = str(f.get("ref") or "")
        n = by_ref.get(ref)
        if n is None and ref.startswith("container:"):
            n = nodes.get("docker:" + ref.split(":", 1)[1].split("/")[0])
        if n is None:
            n = by_norm.get(_norm(subj))
        if n is not None:
            n["errors"] = int(n.get("errors") or 0) + 1
            if str(f.get("severity")) == "error" and n["status"] != "down":
                n["status"] = "warn"
        errs.append({"n": (subj + " " + str(f.get("message") or "")).strip()[:90], "sev": str(f.get("severity")), "c": 1,
                     "node": n["id"] if n else "", "section": str(f.get("section") or ""), "ref": ref})
    el = _ok(src, "evolve.errors.list") or {}
    new = [i for i in (el.get("items") or []) if isinstance(i, Mapping) and str(i.get("state") or "") == "new"]
    for i in new[:12]:
        errs.append({"n": str(i.get("title") or i.get("sig") or "")[:90], "sev": "warn", "c": int(_num(i.get("count"), 1) or 1),
                     "node": "core:looplab" if "core:looplab" in nodes else "", "section": "loop lab", "ref": ""})
    if new and "core:looplab" in nodes:
        nodes["core:looplab"]["errors"] = int(nodes["core:looplab"].get("errors") or 0) + len(new)
    for n in nodes.values():
        if n.get("errors") and n["status"] == "ok":
            n["status"] = "warn"
    errs.sort(key=lambda e: (0 if e["sev"] == "error" else 1, -e["c"]))
    return errs[:40]


def _series(src: Mapping[str, Any], M: Mapping[str, Any]) -> Dict[str, Dict[str, List[float]]]:
    out: Dict[str, Dict[str, List[float]]] = {}
    b = _ok(src, "bench.node_perf.history") or {}
    for name, pts in (b.get("series") or {}).items():
        if not isinstance(pts, list):
            continue
        tps = [_num(p.get("tps"), 0) or 0 for p in pts if isinstance(p, Mapping)]
        ping = [_num(p.get("ping_ms"), 0) or 0 for p in pts if isinstance(p, Mapping)]
        out["ollama:" + str(name)] = {"tps": tps[-60:], "ping": ping[-60:]}
    sh = _ok(src, "sysmon.history") or {}
    if M.get("self") and isinstance(sh.get("samples"), list):
        out[M["self"]] = {"cpu": [_num(s.get("cpu"), 0) or 0 for s in sh["samples"] if isinstance(s, Mapping)][-90:]}
    return out


CLIENT_RECENT_S = 1800      # an agent or person who called a capability in the last half hour is a client now


def _clients(src: Mapping[str, Any], M: Mapping[str, Any], now: Optional[datetime], nodes: Dict[str, Dict[str, Any]], links: List[Dict[str, Any]]) -> None:
    """Who is connected to Vera now: the machines holding connections to its port (by address; one that is an estate
    machine is joined to it), the agents and people who called capabilities lately, VS Code clients, door devices."""
    router = "core:router" if "core:router" in nodes else ""
    bus = "core:cap-bus" if "core:cap-bus" in nodes else router
    conn = _ok(src, "ops.connections") or {}
    for p in (conn.get("peers") or []) if isinstance(conn, Mapping) else []:
        ip, n = str(p.get("ip") or ""), int(_num(p.get("n"), 0) or 0)
        if not ip or ip.startswith("127.") or ip == "::1":
            continue
        owner = M.get("by_ip", {}).get(ip)
        label = nodes[owner]["label"] if owner in nodes else ("a container on this host" if ip.startswith("172.") else ip)
        nid = "client:ip:" + ip
        nodes[nid] = _node(nid, label, "client", EDGE if not ip.startswith("172.") else DEV, kind="client", ckind="peer",
                           detail=f"{ip} · {n} connection{'s' if n != 1 else ''} to Vera", ips=[ip], conns=n)
        if router:
            links.append({"a": nid, "b": router, "kind": "req"})
        if owner in nodes:
            links.append({"a": nid, "b": owner, "kind": "runs"})
    act = _ok(src, "activity.sessions") or {}
    agg: Dict[str, Dict[str, Any]] = {}
    for s in (act.get("sessions") or []) if isinstance(act, Mapping) else []:
        if not isinstance(s, Mapping):
            continue
        age = _age_s(s.get("last_ts"), now)
        if age is None or age > CLIENT_RECENT_S:
            continue
        actor = str(s.get("actor") or "unknown")
        a = agg.setdefault(actor, {"count": 0, "age": age, "areas": []})
        a["count"] += int(_num(s.get("count"), 0) or 0)
        a["age"] = min(a["age"], age)
        a["areas"] += [str(x) for x in (s.get("areas") or []) if x and str(x) not in a["areas"]]
    for actor, a in agg.items():
        agent = actor.startswith("agent:")
        name = actor.split(":", 1)[-1].replace("-", " ").replace("_", " ") or actor
        nid = "client:actor:" + _norm(actor)
        nodes[nid] = _node(nid, name, "client", DEV if agent else EDGE, kind="client", ckind="agent" if agent else "person",
                           status="run" if a["age"] < 120 else "ok",
                           detail=f"{a['count']} calls · last {int(a['age'])} s ago" + (" · " + ", ".join(a["areas"][:3]) if a["areas"] else ""))
        if bus:
            links.append({"a": nid, "b": bus, "kind": "req"})
    ide = _ok(src, "ide.remote.instances") or {}
    for i in (ide.get("instances") or []) if isinstance(ide, Mapping) else []:
        if not isinstance(i, Mapping) or i.get("kind") != "vscode-client":
            continue
        age = _age_s(i.get("last_seen"), now)
        if age is None or age > 30 * 86400:
            continue
        nid = "client:ide:" + str(i.get("id") or "")
        idle = f"idle {int(age // 86400)} d" if age >= 86400 else f"last seen {int(age // 60)} min ago"
        nodes[nid] = _node(nid, str(i.get("label") or "VS Code"), "client", DEV, kind="client", ckind="editor",
                           detail="VS Code client · " + idle)
        if router:
            links.append({"a": nid, "b": router, "kind": "req"})
    vp = _ok(src, "vfs.peer.list") or {}
    door = next((k for k, v in nodes.items() if str(v.get("label", "")).upper().startswith("NWM-02")), "")
    for p in (vp.get("peers") or []) if isinstance(vp, Mapping) else []:
        if not isinstance(p, Mapping):
            continue
        name = str(p.get("name") or p.get("id") or "device")
        addr = str(p.get("address") or p.get("ip") or p.get("tunnel_ip") or "")
        nid = "client:door:" + _norm(name)
        nodes[nid] = _node(nid, name, "client", STORAGE, kind="client", ckind="door",
                           detail="door device" + (" · " + addr if addr else ""), ips=[addr.split("/")[0]] if addr else [])
        links.append({"a": nid, "b": door or router, "kind": "req"})


def _registration(src: Mapping[str, Any], nodes: Dict[str, Dict[str, Any]]) -> None:
    """Each machine's registration - SSH login, directory, mesh door, certificate, backup - by its estate ref."""
    reg = _ok(src, "estate.registration")
    rows = (reg.get("rows") if isinstance(reg, Mapping) else None) or []
    by_ref = {str(r.get("ref")): r for r in rows if isinstance(r, Mapping) and r.get("ref")}
    for n in nodes.values():
        r = by_ref.get(str(n.get("ref") or ""))
        if not r:
            continue
        planes = {str(k): {"state": str(v.get("state") or ""), "detail": str(v.get("detail") or "")}
                  for k, v in (r.get("planes") or {}).items() if isinstance(v, Mapping)}
        n["reg"] = {"complete": bool(r.get("complete")), "planes": planes}


def build(src: Mapping[str, Any], own_ips: Iterable[str] = (), now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    nodes: Dict[str, Dict[str, Any]] = {}
    links: List[Dict[str, Any]] = []
    M = _machines(src, own_ips, nodes, links)
    stack_host = _docker(src, M, nodes, links)
    _services(src, M, stack_host, nodes, links)
    _core(src, stack_host, nodes, links)
    _work(src, nodes, links)
    _mesh(src, now, nodes, links)
    _metrics(src, M, nodes)
    _registration(src, nodes)
    _clients(src, M, now, nodes, links)
    inflight, through, lat = _requests(src, now, nodes)
    events, open_calls = _events(src, now)
    errors = _errors(src, nodes)
    series = _series(src, M)
    # keep only the links whose ends exist, once each
    seen = set()
    kept = []
    for l in links:
        k = (l["a"], l["b"], l["kind"])
        if l["a"] in nodes and l["b"] in nodes and l["a"] != l["b"] and k not in seen:
            seen.add(k)
            kept.append(l)
    # in-flight counts by kind
    fh = _ok(src, "fabric.health") or {}
    kinds = {k: 0 for k in INFLIGHT_KINDS}
    for f in inflight:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    kinds["cap call"] = open_calls
    kinds["fabric write"] = int(_num(fh.get("write_queue_size"), 0) or 0)
    kinds["git op"] = sum(1 for n in nodes.values() if n["id"].startswith("pipe:"))
    kinds["background"] = 1 if "work:bg" in nodes else 0
    if open_calls:
        inflight.append({"kind": "cap call", "a": "core:router", "b": "core:cap-bus", "age": None, "label": f"{open_calls} open"})
    if kinds["fabric write"]:
        inflight.append({"kind": "fabric write", "a": "core:fabric", "b": "svc:chroma", "age": None, "label": f"queue {kinds['fabric write']}"})
    for n in nodes.values():
        if n["id"].startswith("pipe:"):
            inflight.append({"kind": "git op", "a": n["id"], "b": "core:looplab", "age": None, "label": n["detail"]})
    total = sum(kinds.values())
    planes = [{"id": p, "name": name, "title": title, "count": sum(1 for n in nodes.values() if n["plane"] == p)} for p, name, title in PLANES]
    for nid, rows in through.items():
        if nid in nodes:
            nodes[nid]["through"] = rows
    for nid, ms in lat.items():
        if nid in nodes and ms:
            s = sorted(ms)
            nodes[nid]["p95_ms"] = s[min(len(s) - 1, int(len(s) * 0.95))]
            nodes[nid]["latency"] = s[-40:]
    ordered = sorted(nodes.values(), key=lambda n: ([p[0] for p in PLANES].index(n["plane"]), n["domain"], n["label"].lower()))
    return {
        "planes": planes, "domains": DOMAINS, "link_kinds": [list(k) for k in LINK_KINDS],
        "nodes": ordered, "links": kept, "inflight": inflight, "inflight_kinds": kinds, "inflight_total": total,
        "errors": errors, "events": events, "series": series,
        "counts": {"nodes": len(nodes), "links": len(kept), "errors": len(errors)},
        "sources": {k: _err(src, k) for k in src},
        "ts": now.isoformat(),
    }
