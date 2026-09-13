"""One backup group: every copy of the estate in one read.

Backups were spread over five places, each with its own screen or none:
  Proxmox vzdump jobs   jobs.cfg, read on each node by pxstore.backup.status
  PBS-01                the guest backups themselves (the PBS storage's content)
  snapshots, replica    sanoid, the disk-full guard and vera-replicate timers on the node
  the Vera host         the file-level backup of the Vera VM (vera-host-backup.timer),
                        which no Vera screen showed
  nodes.backup          Vera's own interval scheduler (vzdump plus Docker volume tars),
                        off on prod, and overlapping the Proxmox job when it is on

These rules turn those readings into one answer: every guest with its latest
backup and the job that covers it, every schedule with its owner, and warnings
in plain language. Reading only; each schedule stays where it is.

Pure rules, no app imports (tests/test_backup_group_core.py).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import estate_health_core as health

STALE_H = health.BACKUP_STALE_H

HOST_TIMER = "vera-host-backup.timer"
HOST_SERVICE = "vera-host-backup.service"
HOST_SHOW_ARGS = (
    ("systemctl", "show", HOST_TIMER, "--timestamp=unix",
     "-p", "LoadState,ActiveState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime"),
    ("systemctl", "show", HOST_SERVICE, "--timestamp=unix",
     "-p", "ActiveState,SubState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp"),
)

TIMER_WHAT = {"sanoid.timer": "ZFS snapshots",
              "vera-snap-guard.timer": "snapshot disk-full guard",
              "vera-replicate.timer": "file fabric replica on the backup disk"}

STATES = ("failed", "never", "stale", "not covered", "ok", "excluded")


# ── readings ─────────────────────────────────────────────────────────────────

def _stamp(value: str) -> Any:
    """systemctl show timestamps: '@<epoch>' with --timestamp=unix, else as printed."""
    if not value or value in ("n/a", "0"):
        return None
    if value.startswith("@") and value[1:].isdigit():
        return int(value[1:])
    return value


def parse_show(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        out[key] = _stamp(value) if key.endswith(("USec", "Timestamp", "USecRealtime")) else value
    return out


def host_backup(timer: Mapping[str, Any], service: Mapping[str, Any]) -> Dict[str, Any]:
    """The Vera host's file-level backup, from `systemctl show` of its timer and service."""
    if timer.get("LoadState") == "not-found":
        return {"error": f"{HOST_TIMER} is not installed on this host"}
    return {"enabled": timer.get("UnitFileState") == "enabled",
            "scheduled": timer.get("ActiveState") == "active",
            "running": service.get("ActiveState") in ("active", "activating"),
            "result": service.get("Result") or "",
            "exit_status": service.get("ExecMainStatus"),
            "last_start": service.get("ExecMainStartTimestamp"),
            "last_end": service.get("ExecMainExitTimestamp"),
            "next": timer.get("NextElapseUSecRealtime")}


def latest_backups(rows: Iterable[Mapping[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Backup storage content rows -> {vmid: count and the newest copy}."""
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows or []:
        try:
            vmid = int(r.get("vmid"))
        except (TypeError, ValueError):
            continue
        ctime = int(r.get("ctime") or 0)
        cur = out.setdefault(vmid, {"count": 0, "latest_at": 0, "size": 0, "verified": None,
                                    "storage": "", "volid": "", "notes": ""})
        cur["count"] += 1
        if ctime >= cur["latest_at"]:
            ver = r.get("verification")
            cur.update(latest_at=ctime, size=int(r.get("size") or 0),
                       verified=ver.get("state") if isinstance(ver, Mapping) else None,
                       volid=str(r.get("volid") or ""), storage=str(r.get("volid") or "").split(":", 1)[0],
                       notes=str(r.get("notes") or ""))
    return out


def _ids(csv: Any) -> set:
    return {int(x) for x in re.split(r"[,\s]+", str(csv or "")) if x.isdigit()}


def coverage(vmid: int, jobs: Iterable[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """Which enabled jobs back a guest up, and which leave it out on purpose."""
    covered, excluded = [], []
    for j in jobs:
        if not j.get("enabled"):
            continue
        if j.get("all"):
            (excluded if vmid in _ids(j.get("exclude")) else covered).append(str(j.get("id")))
        elif vmid in _ids(j.get("vmid")):
            covered.append(str(j.get("id")))
    return {"covered_by": covered, "excluded_by": excluded}


# ── the group ────────────────────────────────────────────────────────────────

def guest_rows(guests: Iterable[Mapping[str, Any]], jobs: List[Mapping[str, Any]],
               backups: Mapping[int, Mapping[str, Any]], runs: Iterable[Mapping[str, Any]],
               now: float, stale_h: float = STALE_H) -> List[Dict[str, Any]]:
    """One row per guest. state: ok, stale (older than stale_h), never (covered but no
    copy), failed (the newest attempt failed after the newest copy), excluded (left
    out of an all-guests job on purpose), not covered (in no enabled job)."""
    last_run: Dict[int, Mapping[str, Any]] = {}
    for r in runs or []:
        v = int(r.get("vmid") or 0)
        if v and int(r.get("at") or 0) >= int((last_run.get(v) or {}).get("at") or 0):
            last_run[v] = r
    rows = []
    for g in sorted(guests, key=lambda g: int(g.get("vmid") or 0)):
        vmid = int(g.get("vmid") or 0)
        cov = coverage(vmid, jobs)
        b = backups.get(vmid) or {}
        run = last_run.get(vmid)
        latest = int(b.get("latest_at") or 0)
        age_h = round((now - latest) / 3600, 1) if latest else None
        if cov["covered_by"]:
            state = "never" if not latest else ("stale" if age_h > stale_h else "ok")
            if run and run.get("result") == "error" and int(run.get("at") or 0) > latest:
                state = "failed"
        elif cov["excluded_by"]:
            state = "excluded"
        else:
            state = "not covered"
        rows.append({"vmid": vmid, "name": g.get("name") or "", "type": g.get("type") or "",
                     "node": g.get("node") or "", "cluster_id": g.get("cluster_id") or "",
                     "status": g.get("status") or "", "template": bool(g.get("template")),
                     **cov, "backups": int(b.get("count") or 0), "latest_at": latest or None,
                     "age_h": age_h, "size": int(b.get("size") or 0), "verified": b.get("verified"),
                     "last_run": ({"result": run.get("result"), "at": run.get("at"), "line": run.get("line")}
                                  if run else None),
                     "state": state})
    return rows


def schedules(node_jobs: Mapping[str, List[Mapping[str, Any]]], node_timers: Mapping[str, Mapping],
              vera_cfg: Optional[Mapping[str, Any]], host: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Every schedule that makes a copy, with who owns it."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for jobs in node_jobs.values():
        for j in jobs:
            if j.get("id") in seen:                 # jobs.cfg is cluster-wide: every node shows it
                continue
            seen.add(j.get("id"))
            if j.get("all"):
                what = "every guest" + (f" except {j['exclude']}" if j.get("exclude") else "")
            else:
                what = f"guests {j['vmid']}" if j.get("vmid") else "the guests of a pool"
            out.append({"id": str(j.get("id")), "owner": "proxmox", "what": what,
                        "schedule": j.get("schedule") or "", "enabled": bool(j.get("enabled")),
                        "storage": j.get("storage") or "", "next_run": j.get("next_run") or None,
                        "last_run": None})
    for node, timers in node_timers.items():
        for unit, t in sorted((timers or {}).items()):
            out.append({"id": f"{node}:{unit}", "owner": "node", "what": TIMER_WHAT.get(unit, unit),
                        "schedule": "", "enabled": t.get("next") is not None, "storage": "",
                        "next_run": t.get("next"), "last_run": t.get("last")})
    if vera_cfg is not None:
        px = vera_cfg.get("proxmox") or {}
        docker = (vera_cfg.get("docker") or {}).get("enabled")
        out.append({"id": "nodes.backup", "owner": "vera",
                    "what": "vzdump through the Proxmox API" + (", plus Docker volumes" if docker else ""),
                    "schedule": f"every {vera_cfg.get('interval_hours', 24)} h",
                    "enabled": bool(vera_cfg.get("enabled")), "storage": px.get("storage") or "",
                    "next_run": None, "last_run": int(vera_cfg.get("last_run") or 0) or None})
    if host is not None and not host.get("error"):
        out.append({"id": HOST_TIMER, "owner": "vera-host",
                    "what": "the Vera VM's files, database dumps and Docker volumes",
                    "schedule": "daily", "enabled": bool(host.get("enabled") and host.get("scheduled")),
                    "storage": "PBS, file level", "next_run": host.get("next"),
                    "last_run": host.get("last_start")})
    return out


def _names(rows: List[Mapping[str, Any]], limit: int = 8) -> str:
    shown = [f"{r['name'] or 'guest'} ({r['vmid']})" for r in rows[:limit]]
    more = len(rows) - limit
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def warnings(rows: List[Mapping[str, Any]], scheds: List[Mapping[str, Any]],
             host: Optional[Mapping[str, Any]], have_content: bool, now: float,
             stale_h: float = STALE_H) -> List[Dict[str, Any]]:
    F = health.finding
    out: List[Dict[str, Any]] = []
    enabled_px = [s for s in scheds if s["owner"] == "proxmox" and s["enabled"]]
    vera = next((s for s in scheds if s["owner"] == "vera"), None)
    if vera and vera["enabled"] and any(s["what"].startswith("every guest") for s in enabled_px):
        out.append(F(health.WARN, "backups", "nodes.backup",
                     "Vera's own backup schedule and a Proxmox job both back up every guest.",
                     "turn one off: nodes.backup.set enabled=false, or disable the Proxmox job"))
    disabled = [s["id"] for s in scheds if s["owner"] == "proxmox" and not s["enabled"]]
    if disabled:
        out.append(F(health.INFO, "backups", "proxmox",
                     f"{len(disabled)} disabled Proxmox backup job(s) are still defined.", ", ".join(disabled)))
    by_state = {st: [r for r in rows if r["state"] == st] for st in STATES}
    if by_state["failed"]:
        out.append(F(health.ERROR, "backups", "guests",
                     f"The last backup of {_names(by_state['failed'])} failed.",
                     "; ".join(str((r["last_run"] or {}).get("line") or "")[:160] for r in by_state["failed"][:3])))
    if by_state["never"] and have_content:
        out.append(F(health.WARN, "backups", "guests",
                     f"{len(by_state['never'])} guest(s) in a backup job have no backup yet: "
                     f"{_names(by_state['never'])}."))
    if by_state["stale"]:
        out.append(F(health.WARN, "backups", "guests",
                     f"{len(by_state['stale'])} guest(s) have not been backed up in {int(stale_h)} hours: "
                     f"{_names(by_state['stale'])}."))
    if by_state["not covered"] and enabled_px:
        out.append(F(health.WARN, "backups", "guests",
                     f"{len(by_state['not covered'])} guest(s) are in no enabled backup job: "
                     f"{_names(by_state['not covered'])}."))
    if by_state["excluded"]:
        out.append(F(health.INFO, "backups", "guests",
                     f"{len(by_state['excluded'])} guest(s) are left out of the backup job on purpose: "
                     f"{_names(by_state['excluded'])}."))
    if have_content and not any(r.get("verified") for r in rows if r.get("backups")):
        out.append(F(health.INFO, "backups", "pbs", "No backup has been verified yet.",
                     "PBS's verify job reads every chunk back; until it runs, a restore is untested"))
    if host is None:
        pass
    elif host.get("error"):
        out.append(F(health.INFO, "backups", HOST_TIMER,
                     "The Vera host's own file backup can only be read on the Vera host.",
                     str(host["error"])[:200]))
    else:
        if not (host.get("enabled") and host.get("scheduled")):
            out.append(F(health.WARN, "backups", HOST_TIMER,
                         "The Vera host's file backup timer is off, so the Vera VM's files and "
                         "databases are not backed up."))
        if host.get("last_start") is None and not host.get("running"):
            out.append(F(health.WARN, "backups", HOST_TIMER, "The Vera host's file backup has never run."))
        elif not host.get("running") and host.get("result") not in ("", "success"):
            out.append(F(health.ERROR, "backups", HOST_TIMER, "The Vera host's last file backup failed.",
                         f"result {host.get('result')}, exit status {host.get('exit_status')}"))
        elif (not host.get("running") and isinstance(host.get("last_end"), int)
              and now - host["last_end"] > stale_h * 3600):
            hours = int((now - host["last_end"]) // 3600)
            out.append(F(health.WARN, "backups", HOST_TIMER,
                         f"The Vera host's file backup last finished {hours} hours ago."))
    return out


def run_target(row: Mapping[str, Any], jobs: Iterable[Mapping[str, Any]],
               storages: Iterable[Mapping[str, Any]], storage: str = "", mode: str = "") -> Dict[str, Any]:
    """Where and how a one-off backup of this guest goes: the storage and mode of the
    job that covers it, else the first active PBS storage on its node, unless named."""
    node = row.get("node") or ""
    active = {s["name"]: s for s in storages if s.get("active") and (s.get("node") or node) == node}
    job = next((j for j in jobs if str(j.get("id")) in (row.get("covered_by") or [])), None) or {}
    chosen = storage or job.get("storage") or next((n for n, s in active.items() if s.get("type") == "pbs"), "")
    if not chosen:
        return {"error": "there is no active backup storage to write to; name one with storage="}
    if chosen not in active:
        return {"error": f"'{chosen}' is not an active backup storage on {node or 'this node'}"}
    chosen_mode = mode or job.get("mode") or "snapshot"
    if chosen_mode not in ("snapshot", "suspend", "stop"):
        return {"error": "mode must be snapshot, suspend or stop"}
    return {"storage": chosen, "mode": chosen_mode, "job": str(job.get("id") or "")}


def summarize(node_reports: Iterable[Mapping[str, Any]], content_rows: List[Mapping[str, Any]],
              guests: Iterable[Mapping[str, Any]], vera_cfg: Optional[Mapping[str, Any]],
              host: Optional[Mapping[str, Any]], now: Optional[float] = None,
              stale_h: float = STALE_H) -> Dict[str, Any]:
    """node_reports: [{node, status}] with pxstore.backup.status's output or {error};
    content_rows: every row read from the backup storages; guests: Proxmox VM/CT rows;
    vera_cfg: nodes.backup's config, or None when that module is not loaded;
    host: host_backup(...), {error}, or None when not read."""
    now = time.time() if now is None else now
    findings: List[Dict[str, Any]] = []
    jobs: List[Mapping[str, Any]] = []
    runs: List[Mapping[str, Any]] = []
    node_jobs: Dict[str, List] = {}
    timers: Dict[str, Mapping] = {}
    storages: List[Dict[str, Any]] = []
    snapshots: Dict[str, Any] = {}
    replication: Dict[str, Any] = {}
    seen = set()
    for rep in node_reports:
        node = str(rep.get("node") or "Proxmox")
        st = rep.get("status") or {}
        if st.get("error"):
            findings.append(health.finding(health.WARN, "backups", node,
                                           f"Could not read the backup system on {node}.",
                                           str(st["error"])[:240]))
            continue
        node_jobs[node] = list(st.get("jobs") or [])
        for j in node_jobs[node]:
            if j.get("id") not in seen:
                seen.add(j.get("id"))
                jobs.append(j)
        runs.extend(st.get("runs") or [])
        timers[node] = st.get("timers") or {}
        storages.extend(dict(s, node=node) for s in st.get("storages") or [])
        snapshots[node] = st.get("snapshots") or {}
        replication[node] = list(st.get("replication") or [])[-3:]
        for w in st.get("warnings") or []:
            findings.append(health.finding(health.WARN, "backups", node, health._sentence(w),
                                           f"Proxmox node {node}"))
    rows = guest_rows(list(guests), jobs, latest_backups(content_rows), runs, now, stale_h)
    scheds = schedules(node_jobs, timers, vera_cfg, host)
    findings += warnings(rows, scheds, host, bool(content_rows), now, stale_h)
    order = {health.ERROR: 0, health.WARN: 1, health.INFO: 2}
    findings.sort(key=lambda f: order.get(f.get("severity"), 3))
    counts = {st: sum(1 for r in rows if r["state"] == st) for st in STATES}
    return {"guests": rows, "schedules": scheds, "storages": storages, "snapshots": snapshots,
            "replication": replication, "host": host, "findings": findings, "counts": counts,
            "checked_at": int(now)}
