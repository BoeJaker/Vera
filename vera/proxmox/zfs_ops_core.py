"""ZFS operations as plans: what would run, what it costs, what it must not do.

Of 34 storage capabilities three touched ZFS; today's shrink of three guest
disks had to be done by hand. Every operation here is built as a plan first -
the exact commands, the warnings, the layout afterwards - and the capability
runs it only when told to. The rules that keep an operator out of trouble
live here so they are the same in the API, the UI and the tests:

  * a dataset (an LXC subvol) shrinks by lowering refquota, never below what
    it holds; a zvol (a VM disk) never shrinks at all - that destroys data;
  * a rollback to anything but the newest snapshot needs -r and says which
    newer snapshots it throws away;
  * attaching a disk to a single-disk vdev makes a mirror - the fix for
    "no redundancy" - and resilvers; adding a vdev stripes it in and can
    never be removed once raidz is involved;
  * only a fixed set of dataset properties may be tuned, with sane values.

Pure (tests/test_zfs_ops_core.py).
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .pool_core import describe_layout

_UNITS = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
TUNABLE = {
    "recordsize": re.compile(r"^(4K|8K|16K|32K|64K|128K|256K|512K|1M)$", re.I),
    "compression": re.compile(r"^(on|off|lz4|zstd(-\d{1,2})?|gzip(-[1-9])?)$", re.I),
    "atime": re.compile(r"^(on|off)$", re.I),
    "relatime": re.compile(r"^(on|off)$", re.I),
    "sync": re.compile(r"^(standard|always|disabled)$", re.I),
    "primarycache": re.compile(r"^(all|none|metadata)$", re.I),
    "logbias": re.compile(r"^(latency|throughput)$", re.I),
    "xattr": re.compile(r"^(on|off|sa)$", re.I),
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")


def parse_size(text: Any) -> Optional[int]:
    """'200G', '1.5T', '+10G' (relative, sign kept by the caller), or an int."""
    if isinstance(text, (int, float)):
        return int(text)
    m = re.fullmatch(r"\s*\+?(\d+(?:\.\d+)?)\s*([BKMGT])?B?\s*", str(text or ""), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[(m.group(2) or "B").upper()])


def fmt_size(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{int(n)}{unit}" if unit in ("B", "K") or n == int(n) else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def _plan(op: str, commands: List[str], warnings: List[str], **extra: Any) -> Dict[str, Any]:
    return {"op": op, "ok": True, "commands": commands, "warnings": warnings, **extra}


def _refuse(op: str, why: str) -> Dict[str, Any]:
    return {"op": op, "ok": False, "error": why, "commands": [], "warnings": []}


def _valid(name: str) -> bool:
    return bool(name) and bool(_NAME.match(name)) and ".." not in name


# ── resize ───────────────────────────────────────────────────────────────────

def resize_plan(dataset: Mapping[str, Any], want: Any, guest: Optional[Mapping[str, Any]] = None,
                disk_key: str = "rootfs") -> Dict[str, Any]:
    """dataset: {name, type (filesystem|volume), used, quota|refquota, volsize}.
    guest: {vmid, type (lxc|qemu), storage} when the dataset backs a guest disk,
    so the Proxmox config line is kept in step. want: the new absolute size."""
    name = str(dataset.get("name") or "")
    if not _valid(name):
        return _refuse("resize", "dataset name is not valid")
    size = parse_size(want)
    if not size or size <= 0:
        return _refuse("resize", "size must look like 200G or 1.5T")
    kind = dataset.get("type") or "filesystem"
    used = int(dataset.get("used") or 0)
    if kind == "volume":
        current = int(dataset.get("volsize") or 0)
        if current and size < current:
            return _refuse("resize", f"{name} is a zvol (a VM disk) of {fmt_size(current)}: a zvol cannot be "
                                     "shrunk without destroying the filesystem inside it. Shrink the "
                                     "filesystem in the guest first, then ask again, or grow instead.")
        cmds = [f"zfs set volsize={fmt_size(size)} {shlex.quote(name)}"]
        warn = ["the block device grows; the partition and filesystem inside the guest still need growing "
                "(growpart, then resize2fs or xfs_growfs)"]
        direction = "grow"
    else:
        current = int(dataset.get("refquota") or dataset.get("quota") or 0)
        if size < used:
            return _refuse("resize", f"{name} holds {fmt_size(used)}; a limit of {fmt_size(size)} would be below "
                                     "what it already holds")
        direction = "shrink" if (current and size < current) else "grow"
        cmds = [f"zfs set refquota={fmt_size(size)} {shlex.quote(name)}"]
        warn = []
        if direction == "shrink" and used and size < used * 1.2:
            warn.append(f"only {fmt_size(size - used)} of headroom would be left")
        if not current:
            warn.append("the dataset had no limit before; this sets one")
    if guest and guest.get("vmid"):
        conf = f"/etc/pve/{'lxc' if guest.get('type') == 'lxc' else 'qemu-server'}/{int(guest['vmid'])}.conf"
        key = disk_key or ("rootfs" if guest.get("type") == "lxc" else "scsi0")
        cmds.append(f"cp -a {conf} /root/ct-conf-backups/{int(guest['vmid'])}.conf.pre-resize-$(date +%Y%m%d-%H%M%S) 2>/dev/null || true")
        cmds.append(f"sed -i -E '/^{re.escape(key)}:/ s/,size=[0-9.]+[KMGT]?/,size={fmt_size(size)}/' {conf}")
        warn.append("Proxmox shows the size from its config line, so that line is edited to match; "
                    "pct/qm resize is not used because it refuses to shrink")
    return _plan("resize", cmds, warn, direction=direction, dataset=name,
                 before=fmt_size(current) if current else "no limit", after=fmt_size(size), used=fmt_size(used))


# ── scrub / trim ─────────────────────────────────────────────────────────────

def scrub_plan(pool: str, action: str = "start") -> Dict[str, Any]:
    if not _valid(pool) or "/" in pool:
        return _refuse("scrub", "pool name is not valid")
    if action == "stop":
        return _plan("scrub", [f"zpool scrub -s {shlex.quote(pool)}"], [], pool=pool, action="stop")
    if action == "pause":
        return _plan("scrub", [f"zpool scrub -p {shlex.quote(pool)}"], [], pool=pool, action="pause")
    return _plan("scrub", [f"zpool scrub {shlex.quote(pool)}"],
                 ["a scrub reads every block; it slows the pool while it runs and can take hours on a full disk"],
                 pool=pool, action="start")


def trim_plan(pool: str) -> Dict[str, Any]:
    if not _valid(pool) or "/" in pool:
        return _refuse("trim", "pool name is not valid")
    return _plan("trim", [f"zpool trim {shlex.quote(pool)}"],
                 ["only worth it on SSDs; a spinning disk ignores it"], pool=pool)


# ── snapshots ────────────────────────────────────────────────────────────────

def snapshot_plan(dataset: str, name: str = "", recursive: bool = False) -> Dict[str, Any]:
    if not _valid(dataset):
        return _refuse("snapshot", "dataset name is not valid")
    snap = name or "vera-$(date +%Y%m%d-%H%M%S)"
    if name and not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
        return _refuse("snapshot", "snapshot name: letters, digits, _ . : -")
    return _plan("snapshot", [f"zfs snapshot {'-r ' if recursive else ''}{shlex.quote(dataset)}@{snap}"], [],
                 dataset=dataset, snapshot=snap)


def rollback_plan(dataset: str, snapshot: str, snapshots: Iterable[str] = ()) -> Dict[str, Any]:
    """snapshots: the dataset's snapshots, oldest first, as zfs list -t snapshot gives them."""
    if not _valid(dataset) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", snapshot or ""):
        return _refuse("rollback", "dataset and snapshot names must be valid")
    names = [s.split("@", 1)[-1] for s in snapshots]
    if names and snapshot not in names:
        return _refuse("rollback", f"{dataset} has no snapshot called {snapshot}")
    newer = names[names.index(snapshot) + 1:] if snapshot in names else []
    warnings = [f"everything written to {dataset} since {snapshot} is lost"]
    flag = ""
    if newer:
        flag = "-r "
        warnings.append(f"{len(newer)} newer snapshot(s) are destroyed too: {', '.join(newer[:6])}")
    return _plan("rollback", [f"zfs rollback {flag}{shlex.quote(dataset)}@{snapshot}"], warnings,
                 dataset=dataset, snapshot=snapshot, destroys=newer)


# ── tuning ───────────────────────────────────────────────────────────────────

def tune_plan(dataset: str, props: Mapping[str, Any]) -> Dict[str, Any]:
    if not _valid(dataset):
        return _refuse("tune", "dataset name is not valid")
    cmds, warnings, applied = [], [], {}
    for k, v in (props or {}).items():
        k = str(k).lower()
        v = str(v)
        if k not in TUNABLE:
            return _refuse("tune", f"{k} is not a property this can set (allowed: {', '.join(TUNABLE)})")
        if not TUNABLE[k].match(v):
            return _refuse("tune", f"{v!r} is not a value {k} accepts")
        applied[k] = v.lower()
        cmds.append(f"zfs set {k}={v.lower()} {shlex.quote(dataset)}")
    if not cmds:
        return _refuse("tune", "nothing to set")
    if applied.get("sync") == "disabled":
        warnings.append("sync=disabled loses the last seconds of writes on a power cut; only for scratch data")
    if "recordsize" in applied:
        warnings.append("recordsize applies to blocks written from now on; existing data keeps its size")
    if applied.get("compression") == "off":
        warnings.append("new writes stay uncompressed; existing blocks keep their compression")
    return _plan("tune", cmds, warnings, dataset=dataset, props=applied)


# ── layout ───────────────────────────────────────────────────────────────────

def attach_plan(pool: str, vdevs: Iterable[Mapping[str, Any]], existing: str, new: str) -> Dict[str, Any]:
    """Turn a single disk into a mirror by attaching `new` beside `existing`."""
    if not _valid(pool) or "/" in pool:
        return _refuse("attach", "pool name is not valid")
    if not (_valid(existing) and _valid(new)) or existing == new:
        return _refuse("attach", "name the existing disk and a different new disk")
    data = [v for v in vdevs if v.get("role") == "data"]
    target = next((v for v in data if any(d.get("name") == existing for d in v.get("disks") or [])), None)
    if not target:
        return _refuse("attach", f"{existing} is not a data disk of {pool}")
    if target.get("type") not in ("disk", "mirror"):
        return _refuse("attach", f"{existing} sits in a {target.get('type')} vdev; a disk can only be attached to a "
                                 "single disk or a mirror")
    after_vdevs = []
    for v in vdevs:
        if v.get("role") == "data" and any(d.get("name") == existing for d in v.get("disks") or []):
            after_vdevs.append({"type": "mirror", "role": "data", "disks": list(v.get("disks") or []) + [{"name": new}]})
        else:
            after_vdevs.append(v)
    return _plan("attach", [f"zpool attach {shlex.quote(pool)} {shlex.quote(existing)} {shlex.quote(new)}"],
                 [f"{new} must be empty and at least as large as {existing}; everything on it is overwritten",
                  "the pool resilvers onto the new disk - hours for a large disk - and stays usable meanwhile",
                  "this can be undone with zpool detach"],
                 pool=pool, before=describe_layout(vdevs), after=describe_layout(after_vdevs))


def add_plan(pool: str, vdevs: Iterable[Mapping[str, Any]], disks: List[str], kind: str = "") -> Dict[str, Any]:
    """Stripe a new vdev into the pool: `disks` as a plain disk, a mirror, or raidz."""
    if not _valid(pool) or "/" in pool:
        return _refuse("add", "pool name is not valid")
    disks = [d for d in (disks or []) if _valid(d)]
    if not disks:
        return _refuse("add", "name at least one disk")
    kind = (kind or ("mirror" if len(disks) == 2 else "disk")).lower()
    if kind not in ("disk", "mirror", "raidz1", "raidz2", "raidz3"):
        return _refuse("add", "kind must be disk, mirror, raidz1, raidz2 or raidz3")
    need = {"disk": 1, "mirror": 2, "raidz1": 3, "raidz2": 4, "raidz3": 5}[kind]
    if len(disks) < need:
        return _refuse("add", f"a {kind} vdev needs at least {need} disks")
    spec = (" ".join(shlex.quote(d) for d in disks) if kind == "disk"
            else f"{kind} " + " ".join(shlex.quote(d) for d in disks))
    after = list(vdevs) + [{"type": kind, "role": "data", "disks": [{"name": d} for d in disks]}]
    warnings = ["a vdev added to a pool cannot be removed again once any raidz vdev is in the pool; "
                "with only disks and mirrors zpool remove can evacuate it, slowly",
                "data is striped across vdevs from now on: the pool is as safe as its LEAST redundant vdev"]
    if kind == "disk":
        warnings.append(f"a single disk has no redundancy - if it fails the whole of {pool} is lost")
    existing_kinds = {v.get("type") for v in vdevs if v.get("role") == "data"}
    if existing_kinds and existing_kinds != {kind}:
        warnings.append(f"mixing {kind} with the existing {', '.join(sorted(existing_kinds))} vdevs is allowed but unusual")
    return _plan("add", [f"zpool add {shlex.quote(pool)} {spec}"], warnings,
                 pool=pool, before=describe_layout(vdevs), after=describe_layout(after))
