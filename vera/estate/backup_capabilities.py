"""backup.* - one backup group over every copy of the estate.

backup.status reads the Proxmox backup jobs and runs on each node
(pxstore.backup.status), the guest backups held in each backup storage, the
guests themselves, Vera's own nodes.backup schedule and the Vera host's
file-level backup, and answers with every guest's latest backup, every schedule
with its owner, and warnings (backup_group_core decides them). backup.guest lists
one guest's backups; backup.run backs one guest up now, a dry run unless
confirm=true. nodes.backup.* and pxstore.backup.* keep working underneath.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
import time
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event
from Vera.vera.estate import backup_group_core as core
from Vera.vera.estate import estate_health_core as health

log = logging.getLogger("vera.estate.backup")

CACHE_TTL_S = 120.0
TIMEOUTS_S = {"proxmox": 25.0, "node": 75.0, "content": 30.0, "vera": 10.0, "host": 10.0}
_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    """The globals of the module that registered `cap_name`, or None."""
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return None
    return getattr(inspect.unwrap(fn), "__globals__", None)


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


def _flag(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


async def _timed(coro, seconds: float, what: str) -> Dict[str, Any]:
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        return {"error": f"{what} did not answer within {int(seconds)} s"}


async def _clusters() -> Dict[str, Any]:
    """One reachable record per Proxmox API host, with its online nodes and guests."""
    px = _module_of("proxmox.status")
    if not px or not all(k in px for k in ("_all_raw", "_open", "_pve")):
        return {"error": "the Proxmox capabilities are not loaded"}
    pve = px["_pve"]
    out, errors = [], []
    for host, group in health.group_clusters(await px["_all_raw"]()).items():
        nodes, err, raw, opened = None, "", None, None
        for candidate in group:
            opened = px["_open"](candidate)
            nodes, err = await pve(opened, "GET", "/cluster/resources?type=node")
            if nodes is not None:
                raw = candidate
                break
        if nodes is None:
            errors.append({"host": host, "error": err})
            continue
        guests, gerr = await pve(opened, "GET", "/cluster/resources?type=vm")
        if guests is None:
            errors.append({"host": host, "error": gerr})
            guests = []
        cid = raw.get("id", "")
        out.append({"cluster_id": cid, "record": opened,
                    "nodes": [n["node"] for n in nodes if n.get("status") == "online" and n.get("node")],
                    "guests": [dict(g, cluster_id=cid) for g in guests if g.get("vmid") is not None]})
    return {"clusters": out, "errors": errors, "pve": pve}


async def _content(pve, record: Dict[str, Any], node: str, storage: str) -> Dict[str, Any]:
    rows, err = await pve(record, "GET", f"/nodes/{node}/storage/{storage}/content?content=backup")
    if rows is None:
        return {"error": err or "no answer"}
    return {"rows": list(rows)}


async def _run(args, timeout: float = 10.0) -> Dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return {"rc": proc.returncode, "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace")}


async def _host() -> Dict[str, Any]:
    """The Vera host's file backup, read with systemctl where Vera itself runs."""
    if not shutil.which("systemctl"):
        return {"error": "systemctl is not available here, so Vera is not running on its host"}
    parsed = []
    for args in core.HOST_SHOW_ARGS:
        res = await _run(list(args))
        if res["rc"] != 0:                       # older systemd: no --timestamp=unix
            res = await _run([a for a in args if a != "--timestamp=unix"])
        if res["rc"] != 0:
            return {"error": (res["stderr"] or "systemctl show failed").strip()[:200]}
        parsed.append(core.parse_show(res["stdout"]))
    return core.host_backup(parsed[0], parsed[1])


async def _gather() -> Dict[str, Any]:
    cl = await _timed(_clusters(), TIMEOUTS_S["proxmox"], "the Proxmox API")
    errors: List[Dict[str, str]] = list(cl.get("errors") or [])
    if cl.get("error"):
        errors.append({"host": "proxmox", "error": cl["error"]})
    reports: List[Dict[str, Any]] = []
    content: List[Dict[str, Any]] = []
    guests: List[Dict[str, Any]] = []
    jobs: List[Dict[str, Any]] = []
    read_storages = set()
    for c in cl.get("clusters") or []:
        guests.extend(c["guests"])
        statuses = await asyncio.gather(*[
            _timed(_call("pxstore.backup.status", cluster_id=c["cluster_id"], node=n),
                   TIMEOUTS_S["node"], f"the backup system on {n}") for n in c["nodes"]])
        for node, st in zip(c["nodes"], statuses):
            reports.append({"node": node, "status": st})
            if st.get("error"):
                continue
            jobs.extend(st.get("jobs") or [])
            for s in st.get("storages") or []:
                key = (c["cluster_id"], s.get("name"))
                if not s.get("active") or key in read_storages:
                    continue                      # shared storages are read once
                read_storages.add(key)
                res = await _timed(_content(cl["pve"], c["record"], node, s["name"]),
                                   TIMEOUTS_S["content"], f"backup storage {s['name']}")
                if res.get("error"):
                    errors.append({"host": s["name"], "error": res["error"]})
                else:
                    content.extend(r for r in res["rows"] if r.get("content", "backup") == "backup")
    vera = await _timed(_call("nodes.backup.get"), TIMEOUTS_S["vera"], "nodes.backup.get")
    host = await _timed(_host(), TIMEOUTS_S["host"], "systemctl")
    out = core.summarize(reports, content, guests, None if vera.get("error") else vera.get("config"), host)
    for e in errors:
        out["findings"].append(health.finding(health.WARN, "backups", str(e.get("host") or "?"),
                                              f"Could not read {e.get('host')}.", str(e.get("error") or "")[:240]))
    out["_content"] = content
    out["_jobs"] = jobs
    return out


def _public(value: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in value.items() if not k.startswith("_")}


async def _ensure(refresh: bool = False) -> Dict[str, Any]:
    if not refresh and _CACHE["value"] is not None and time.time() - _CACHE["at"] < CACHE_TTL_S:
        return dict(_CACHE["value"], cached=True)
    started = time.time()
    value = await _gather()
    value["elapsed_ms"] = int((time.time() - started) * 1000)
    _CACHE.update(at=time.time(), value=value)
    return dict(value, cached=False)


@capability(
    "backup.status",
    http_method="GET", http_path="/backup/status", http_tags=["estate", "backup"],
    memory="off", silent=True,
    description="Every copy of the estate in one read: each guest with its latest backup, "
                "its size, whether PBS verified it, the job that covers it and a state "
                "(ok, stale, never, failed, excluded, not covered); every schedule with "
                "its owner (Proxmox jobs, the node's snapshot and replica timers, Vera's "
                "nodes.backup, the Vera host's file backup); backup storages, snapshot "
                "counts, recent replication lines; and findings in plain language, "
                "errors first. Read-only; cached 120 s. Inputs: refresh (bool). "
                "Output: {guests, schedules, storages, snapshots, replication, host, "
                "findings, counts, checked_at, elapsed_ms, cached}.",
)
async def cap_backup_status(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    return _public(await _ensure(_flag(refresh)))


@capability(
    "backup.guest",
    http_method="POST", http_path="/backup/guest", http_tags=["estate", "backup"],
    memory="off", silent=True,
    description="One guest's backups, newest first, with its row from backup.status "
                "(the covering job, state and last attempt). Inputs: vmid (int!). "
                "Output: {guest, backups:[{volid, at, size, verified, notes, protected}]} "
                "or {error}.",
)
async def cap_backup_guest(vmid: int = 0, trace_id=None) -> Dict[str, Any]:
    try:
        vmid = int(vmid or 0)
    except (TypeError, ValueError):
        return {"error": "vmid must be a number"}
    if not vmid:
        return {"error": "vmid required"}
    st = await _ensure()
    row = next((g for g in st["guests"] if g["vmid"] == vmid), None)
    if row is None:
        return {"error": f"no guest {vmid} on the registered Proxmox clusters"}
    rows = sorted((r for r in st.get("_content") or [] if int(r.get("vmid") or 0) == vmid),
                  key=lambda r: int(r.get("ctime") or 0), reverse=True)
    backups = [{"volid": r.get("volid", ""), "at": int(r.get("ctime") or 0) or None,
                "size": int(r.get("size") or 0),
                "verified": (r.get("verification") or {}).get("state")
                if isinstance(r.get("verification"), dict) else None,
                "notes": r.get("notes") or "", "protected": bool(r.get("protected"))} for r in rows]
    return {"guest": row, "backups": backups}


@capability(
    "backup.run",
    http_method="POST", http_path="/backup/run", http_tags=["estate", "backup"],
    memory="on",
    description="Back ONE guest up now, outside the schedule. A dry run unless "
                "confirm=true. Inputs: vmid (int!), storage (str; default: the storage "
                "of the enabled job that covers the guest, else the node's first active "
                "PBS storage), mode ('snapshot'|'suspend'|'stop'; default the job's mode, "
                "else snapshot), confirm (bool=false). Output: {ok, dry_run, vmid, node, "
                "storage, mode, job, upid} or {error}.",
)
async def cap_backup_run(vmid: int = 0, storage: str = "", mode: str = "",
                         confirm: bool = False, trace_id=None) -> Dict[str, Any]:
    try:
        vmid = int(vmid or 0)
    except (TypeError, ValueError):
        return {"error": "vmid must be a number"}
    if not vmid:
        return {"error": "vmid required"}
    st = await _ensure()
    row = next((g for g in st["guests"] if g["vmid"] == vmid), None)
    if row is None:
        return {"error": f"no guest {vmid} on the registered Proxmox clusters"}
    target = core.run_target(row, st.get("_jobs") or [], st.get("storages") or [], storage, mode)
    if target.get("error"):
        return target
    plan = {"vmid": vmid, "node": row["node"], "cluster_id": row["cluster_id"], **target}
    if not _flag(confirm):
        return {"ok": True, "dry_run": True, **plan,
                "note": "nothing ran; pass confirm=true to back this guest up now"}
    px = _module_of("proxmox.status")
    if not px or "_get_cluster" not in px or "_pve" not in px:
        return {"error": "the Proxmox capabilities are not loaded", **plan}
    record = await px["_get_cluster"](row["cluster_id"], opened=True)
    if not record:
        return {"error": f"cluster {row['cluster_id']} not found", **plan}
    upid, err = await px["_pve"](record, "POST", f"/nodes/{row['node']}/vzdump",
                                 data={"vmid": str(vmid), "storage": target["storage"],
                                       "mode": target["mode"], "notes-template": "{{guestname}}"})
    if err:
        return {"error": str(err), **plan}
    _CACHE["at"] = 0.0                            # the next status read sees the new copy
    await emit_event({"type": "backup.run", "vmid": vmid, "node": row["node"],
                      "storage": target["storage"], "upid": upid})
    return {"ok": True, "dry_run": False, **plan, "upid": upid if isinstance(upid, str) else ""}


log.info("backup_capabilities ready - backup.status / backup.guest / backup.run")
