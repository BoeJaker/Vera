"""pxstore_backup_core.py -- what the Storage panel needs to show a node's
physical disks and its backup system, decided without any I/O.

Two blind spots this closes, both found on 2026-09-12:

  * the panel listed ZFS pools but never DISKS, so a whole unused 1.1 TB disk
    was invisible -- and so were a damaged pool on the internal SD card and an
    LVM volume group on a USB-attached drive;
  * nothing showed whether backups ran. Both vzdump jobs had been disabled for
    over a year, every pool is a single disk, and no screen said so.

The capability module runs DISKS_SCRIPT / BACKUP_SCRIPT on the node over SSH
and hands the transcript to these parsers (tests/test_pxstore_backup_core.py).
Both scripts are read-only: `zpool import` with no pool name only LISTS what
could be imported.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

DISKS_SCRIPT = "\n".join([
    "echo '###LSBLK'",
    "lsblk -J -b -o NAME,SIZE,TYPE,ROTA,MODEL,SERIAL,TRAN,FSTYPE,LABEL,MOUNTPOINT 2>/dev/null",
    "echo '###IMPORTED'",
    "zpool list -H -o name 2>/dev/null",
    "echo '###IMPORTABLE'",
    "zpool import 2>&1 | grep -E '^[[:space:]]+(pool|state):'",
    "echo '###PVS'",
    "pvs --noheadings -o pv_name,vg_name 2>/dev/null",
])

TIMER_UNITS = ("sanoid.timer", "vera-snap-guard.timer", "vera-replicate.timer")

BACKUP_SCRIPT = "\n".join([
    "echo '###JOBS'",
    "pvesh get /cluster/backup --output-format json 2>/dev/null",
    "echo '###STORAGE'",
    "pvesm status --content backup 2>/dev/null",
    "echo '###POOLS'",
    "zpool list -Hp -o name,size,alloc,free,capacity 2>/dev/null",
    "echo '###DATASETS'",
    "zfs list -Hp -o name,used,avail,quota -r backup 2>/dev/null",
    "echo '###SNAPS'",
    "zfs list -H -t snapshot -o name 2>/dev/null | grep '@autosnap_' | cut -d@ -f1 "
    "| cut -d/ -f1 | sort | uniq -c",
    "echo '###TIMERS'",
    "systemctl list-timers --all --output=json " + " ".join(TIMER_UNITS) + " 2>/dev/null",
    "echo '###GUARD'",
    "journalctl -t vera-snap-guard -n 10 --no-pager -o short-iso 2>/dev/null",
    "echo '###REPL'",
    "journalctl -t vera-replicate -n 10 --no-pager -o short-iso 2>/dev/null",
    "echo '###VZDUMP'",
    "for f in $(ls -t /var/log/vzdump/*.log 2>/dev/null | head -20); do "
    "printf '%s|%s|%s\\n' \"$(basename \"$f\" .log)\" \"$(stat -c %Y \"$f\")\" "
    "\"$(grep -hE 'Finished Backup|ERROR|Starting Backup' \"$f\" | tail -1)\"; done",
    "echo '###RUNNING'",
    "pgrep -a vzdump 2>/dev/null | head -3",
])


# ═════════════════════════════════════════════════════════════════════════════
#  TRANSCRIPT
# ═════════════════════════════════════════════════════════════════════════════
def sections(text: str) -> Dict[str, str]:
    """'###NAME'-delimited transcript -> {NAME: raw text}. Raw, because some
    sections are multi-line JSON."""
    out: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in (text or "").splitlines():
        if line.startswith("###"):
            if current is not None:
                out[current] = "\n".join(buf)
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def lines(text: str) -> List[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


# ═════════════════════════════════════════════════════════════════════════════
#  DISKS
# ═════════════════════════════════════════════════════════════════════════════
def parse_importable(text: str) -> Dict[str, str]:
    """`zpool import` listing -> {pool: state}."""
    out: Dict[str, str] = {}
    pool = None
    for ln in lines(text):
        m = re.match(r"^pool:\s*(\S+)", ln)
        if m:
            pool = m.group(1)
            continue
        m = re.match(r"^state:\s*(\S+)", ln)
        if m and pool:
            out[pool] = m.group(1)
            pool = None
    return out


def parse_pvs(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ln in lines(text):
        f = ln.split()
        if len(f) >= 2:
            out[f[0]] = f[1]
    return out


def _flatten(dev: Dict) -> List[Dict]:
    out = [dev]
    for child in dev.get("children") or []:
        out.extend(_flatten(child))
    return out


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True")


_DAMAGED = ("UNAVAIL", "FAULTED", "DEGRADED", "SUSPENDED")


def classify_disks(lsblk_json: str, imported: List[str], importable: Dict[str, str],
                   pvs: Dict[str, str]) -> List[Dict]:
    """One record per physical disk: what it is used for, and whether it is free."""
    try:
        devs = json.loads(lsblk_json or "{}").get("blockdevices") or []
    except (ValueError, AttributeError):
        return []
    out: List[Dict] = []
    for d in devs:
        name = str(d.get("name") or "")
        if d.get("type") != "disk" or name.startswith(("zd", "loop", "nbd", "ram", "dm-")):
            continue
        nodes = _flatten(d)
        # A partition label beats a stale whole-disk label (an SD card can carry both).
        part_pools = [n.get("label") for n in nodes[1:]
                      if n.get("fstype") == "zfs_member" and n.get("label")]
        disk_pool = d.get("label") if d.get("fstype") == "zfs_member" else None
        pool = (part_pools[0] if part_pools else disk_pool) or ""
        lvm = [n for n in nodes if n.get("fstype") == "LVM2_member"]
        mounted = [n for n in nodes if n.get("mountpoint")
                   and n.get("fstype") not in ("zfs_member", "LVM2_member", "swap")]
        rec = {"name": name, "size": int(d.get("size") or 0),
               "model": (d.get("model") or "").strip(), "transport": d.get("tran") or "",
               "usb": d.get("tran") == "usb", "rotational": _truthy(d.get("rota")),
               "pool": pool}
        if pool:
            if pool in imported:
                role, state, detail = "zfs pool", "in use", pool
            elif pool in importable:
                pst = importable[pool]
                role = "zfs pool, not imported"
                state = "damaged" if pst.upper() in _DAMAGED else "importable"
                detail = f"{pool} ({pst})"
            else:
                # Not imported and not listed as importable (the listing can come
                # back empty on a busy node). Never call a labelled disk free:
                # that is how a pool someone still needs gets wiped.
                role, state = "zfs label, pool not imported", "labelled"
                detail = f"carries pool {pool}; check before reusing"
        elif lvm:
            vg = pvs.get("/dev/" + str(lvm[0].get("name")), "?")
            role, state, detail = "lvm", "in use", f"volume group {vg}"
        elif mounted:
            m = mounted[0]
            role, state = "filesystem", "in use"
            detail = f"{m.get('fstype') or '?'} at {m.get('mountpoint')}"
        elif any(n.get("fstype") for n in nodes):
            f = next(n for n in nodes if n.get("fstype"))
            role, state, detail = "filesystem", "not mounted", str(f.get("fstype"))
        else:
            role, state, detail = "empty", "free", "no partitions or filesystems"
        rec.update(role=role, state=state, detail=detail)
        out.append(rec)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  BACKUP SYSTEM
# ═════════════════════════════════════════════════════════════════════════════
def parse_jobs(text: str) -> List[Dict]:
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict):
            continue
        out.append({
            "id": j.get("id", ""), "schedule": j.get("schedule", ""),
            "storage": j.get("storage", ""), "mode": j.get("mode", ""),
            # PVE omits `enabled` when it is on.
            "enabled": str(j.get("enabled", 1)) not in ("0", "False", "false"),
            "all": str(j.get("all", 0)) in ("1", "True", "true"),
            "vmid": str(j.get("vmid", "") or ""), "exclude": str(j.get("exclude", "") or ""),
            "comment": j.get("comment", ""), "next_run": int(j.get("next-run") or 0),
        })
    return out


def parse_pvesm_status(text: str) -> List[Dict]:
    """`pvesm status` (sizes in KiB) -> records in bytes."""
    out = []
    for ln in lines(text):
        f = ln.split()
        if len(f) < 7 or f[0] == "Name":
            continue
        try:
            out.append({"name": f[0], "type": f[1], "active": f[2] == "active",
                        "total": int(f[3]) * 1024, "used": int(f[4]) * 1024,
                        "avail": int(f[5]) * 1024})
        except ValueError:
            continue
    return out


def parse_pools(text: str) -> List[Dict]:
    out = []
    for ln in lines(text):
        f = ln.split()
        if len(f) < 5:
            continue
        try:
            out.append({"name": f[0], "size": int(f[1]), "alloc": int(f[2]),
                        "free": int(f[3]), "capacity": int(f[4].rstrip("%"))})
        except ValueError:
            continue
    return out


def parse_datasets(text: str) -> List[Dict]:
    out = []
    for ln in lines(text):
        f = ln.split()
        if len(f) < 4:
            continue
        try:
            out.append({"name": f[0], "used": int(f[1]), "avail": int(f[2]),
                        "quota": int(f[3]) if f[3] not in ("-", "none") else 0})
        except ValueError:
            continue
    return out


def parse_snap_counts(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for ln in lines(text):
        f = ln.split()
        if len(f) == 2 and f[0].isdigit():
            out[f[1]] = int(f[0])
    return out


def parse_timers(text: str) -> Dict[str, Dict]:
    """systemctl list-timers --output=json -> {unit: {next, last}} in epoch
    seconds (None when never / not scheduled)."""
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return {}
    out: Dict[str, Dict] = {}
    for t in data if isinstance(data, list) else []:
        unit = t.get("unit")
        if not unit:
            continue
        nxt, last = t.get("next") or 0, t.get("last") or 0
        out[unit] = {"next": int(nxt) // 1_000_000 if nxt else None,
                     "last": int(last) // 1_000_000 if last else None}
    return out


def parse_journal(text: str) -> List[Dict]:
    out = []
    for ln in lines(text):
        if ln.startswith("--"):
            continue
        m = re.match(r"^(\S+)\s+\S+\s+[^:]+:\s*(.*)$", ln)
        if m:
            out.append({"at": m.group(1), "msg": m.group(2)})
    return out


def parse_vzdump(text: str) -> List[Dict]:
    """`name|mtime|last status line` per guest log -> latest result per guest."""
    out = []
    for ln in lines(text):
        parts = ln.split("|", 2)
        if len(parts) < 3:
            continue
        guest, mtime, msg = parts
        m = re.match(r"^(lxc|qemu)-(\d+)$", guest)
        try:
            at = int(mtime)
        except ValueError:
            at = 0
        if "ERROR" in msg:
            result = "error"
        elif "Finished Backup" in msg:
            result = "ok"
        elif "Starting Backup" in msg:
            result = "running"
        else:
            result = "unknown"
        out.append({"guest": guest, "kind": m.group(1) if m else "",
                    "vmid": int(m.group(2)) if m else 0, "at": at,
                    "result": result, "line": msg.strip()})
    return out


def backup_warnings(jobs: List[Dict], storages: List[Dict], pools: List[Dict],
                    timers: Dict[str, Dict], high: int = 80,
                    root_dir_storages=("local", "bpool")) -> List[str]:
    """Plain-language problems, most serious first."""
    w: List[str] = []
    by_name = {s["name"]: s for s in storages}
    enabled = [j for j in jobs if j["enabled"]]
    if not enabled:
        w.append("no backup job is enabled, so nothing backs the estate up")
    for j in enabled:
        s = by_name.get(j["storage"])
        if s is None:
            w.append(f"job {j['id']} targets '{j['storage']}', which is not a backup "
                     "storage on this node")
        elif not s["active"]:
            w.append(f"job {j['id']} targets '{j['storage']}', which is offline")
        elif j["storage"] in root_dir_storages:
            w.append(f"job {j['id']} writes to '{j['storage']}', a folder on the "
                     "hypervisor's root disk")
    for s in storages:
        if s["total"] and s["used"] * 100 // s["total"] >= high:
            w.append(f"backup storage '{s['name']}' is {s['used'] * 100 // s['total']}% full")
    for p in pools:
        if p["capacity"] >= high:
            w.append(f"pool {p['name']} is {p['capacity']}% full; the snapshot guard "
                     "starts pruning at 85%")
    for unit in TIMER_UNITS:
        t = timers.get(unit)
        if t is None:
            w.append(f"{unit} is not installed on this node")
        elif t.get("next") is None:
            w.append(f"{unit} is installed but not scheduled")
    return w
