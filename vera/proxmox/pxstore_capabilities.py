"""
pxstore_capabilities.py — Proxmox storage fabric (Iteration 1)
==============================================================

Cluster-wide storage operations for the Proxmox dashboard: everything the
plain PVE API can't do on its own (ZFS, Samba, cpusets, bind-mounts) runs
over root SSH to the node, reusing the shared exec.ssh host registry.

What this module provides (group `pxstore.*`)
─────────────────────────────────────────────
  Settings      pxstore.settings.get / .save   — per-cluster config: node→SSH
                mapping, share root, reserved CPU ranges, store dataset.
  Inventory     pxstore.inventory              — guests (by NAME), ZFS pools/
                datasets, non-ZFS mounts, per-guest disk→dataset resolution,
                unallocated space.
  File fabric   The estate's file server is VFS-02 (vfs.* capabilities);
                the Storage panel drives it directly. pxstore.fs.provision /
                .sync / .status remain for the LEGACY hypervisor share
                (\\\\node\\vera-fs), and pxstore.fs.retire takes that share out
                of service in stages: dry run first, refuses while anyone is
                connected, one call to restore.
  Storage mgmt  pxstore.disk.resize            — grow a guest disk (PVE API;
                LXC filesystems grow automatically, VMs get in-guest steps),
                pxstore.zfs.set / .create      — quotas + datasets.
  CPU / NUMA    pxstore.cpu.topology / .map / .pin / .suggest
                lscpu topology, per-guest pinning (LXC cpuset / QEMU
                affinity), NUMA-span + reserved-range conflict detection,
                and a safe-cpuset allocator that never hands out the ollama
                ranges and never spans NUMA nodes.
  Model store   pxstore.store.provision / .status / .attach / .consolidate
                Central ZFS dataset for models/images/artifacts, bind-mounted
                READ-ONLY into consumer CTs (mpN,ro=1) with OLLAMA_NOPRUNE=1 so
                no node can prune another's blobs. The store has ONE writer:
                pxstore.store.writer.provision installs an Ollama on VFS-02
                bound to 127.0.0.1, and pxstore.models.pull (via=store) runs
                detached pulls through it (pxstore.models.pull.status).
                pxstore.store.export / .attach_remote reuse VFS-02's read-only
                NFS export -- nothing is installed on the hypervisor.
  Backups       pxstore.backup.status — jobs, backup storages, snapshot counts,
                the disk-full guard, replication and the latest per-guest
                results, with plain-language warnings (read-only).
                pxstore.backup.target — a PVE backup storage inside the
                fabric's backup dataset, used by nodes.backup (vzdump).
  Disks         pxstore.disks — every physical disk and what it is used for,
                including free, USB-attached and damaged ones (read-only).
  Vera data     pxstore.veradata.provision / .plan
                Dedicated dataset + NFS export for Vera's databases (the
                "Vera VM keeps filling up" fix) plus a generated stop-copy-
                symlink migration script and an optional docker-stack compose.
  VSCode        pxstore.vscode.targets         — per-guest Remote-SSH config
                block + vscode-remote:// folder URIs + SMB paths.

Design notes
────────────
  • Guests are addressed by their real NAME everywhere; vmid is resolved
    internally.  Share tree:  /srv/vera-fs/<guest-name> → subvol mountpoint.
  • Symlinks (not bind mounts) back the share: they survive reboots, track
    dataset renames, and need no unit files.  The share therefore enables
    `wide links` — fine for a trusted-LAN admin share, called out in the UI.
  • Everything SSH-side is idempotent; scripts re-run safely on every sync.
  • Nothing here mounts a VM disk image on the host (qemu-nbd on a live
    guest corrupts filesystems); VM access is sshfs into the running guest.

Redis layout
────────────
  vera:pxstore:cfg   hash  cluster_id -> JSON settings
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.proxmox.pxstore_attach_core import (
    DEFAULT_CT_PATH as _ATTACH_CT_PATH,
    is_token_bindmount_refusal as _is_token_refusal,
    mp_value as _mp_value,
    pct_set_command as _pct_set_command,
)
from Vera.vera.proxmox.pxstore_backup_core import (
    BACKUP_SCRIPT as _BACKUP_SCRIPT,
    DISKS_SCRIPT as _DISKS_SCRIPT,
    backup_warnings as _backup_warnings,
    classify_disks as _classify_disks,
    lines as _bk_lines,
    parse_datasets as _parse_bk_datasets,
    parse_importable as _parse_importable,
    parse_jobs as _parse_jobs,
    parse_journal as _parse_journal,
    parse_pools as _parse_bk_pools,
    parse_pvesm_status as _parse_pvesm_status,
    parse_pvs as _parse_pvs,
    parse_snap_counts as _parse_snap_counts,
    parse_timers as _parse_timers,
    parse_vzdump as _parse_vzdump,
    sections as _bk_sections,
)
from Vera.vera.proxmox.pxstore_fabric_core import (
    CONSOLIDATE_RSYNC_FLAGS as _RSYNC_FLAGS,
    FABRIC_HOST as _FABRIC_HOST,
    FABRIC_SSH_LABEL as _FABRIC_LABEL,
    LEGACY_PROBE_SCRIPT as _LEGACY_PROBE,
    WRITER_PORT as _WRITER_PORT,
    WRITER_UNIT as _WRITER_UNIT,
    add_export_client_script as _add_export_client,
    backup_target_script as _backup_target_script,
    export_for as _export_for,
    fabric_path as _fabric_path,
    fstab_line as _fstab_line,
    is_read_only_error as _is_ro_error,
    parse_exports as _parse_exports,
    parse_legacy_probe as _parse_legacy_probe,
    parse_pull_log as _parse_pull_log,
    parse_store_consumers as _parse_consumers,
    pull_start_script as _pull_start_script,
    pull_status_script as _pull_status_script,
    restore_legacy_script as _restore_legacy_script,
    retire_legacy_script as _retire_legacy_script,
    valid_client as _valid_client,
    valid_model as _valid_model,
    writer_unit as _writer_unit,
)

log = logging.getLogger("vera.pxstore")

_HERE = Path(__file__).parent
KEY_CFG = "vera:pxstore:cfg"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# ═════════════════════════════════════════════════════════════════════════════
#  CROSS-MODULE PLUMBING
# ═════════════════════════════════════════════════════════════════════════════
def _redis():
    return getattr(_orch, "REDIS", None)


def _pmx():
    """proxmox_capabilities module (loaded before us in _module_files)."""
    return sys.modules.get("proxmox_capabilities")


def _rawcap(name: str):
    """Another capability's undecorated function (no double activity records)."""
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


async def _cluster(cluster_id: str) -> Optional[Dict]:
    pm = _pmx()
    if not pm:
        return None
    return await pm._get_cluster(cluster_id, opened=True)


async def _pve(rec: Dict, method: str, path: str,
               data: Optional[Dict] = None) -> Tuple[Optional[Any], str]:
    pm = _pmx()
    if not pm:
        return None, "proxmox module not loaded"
    return await pm._pve(rec, method, path, data)


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════════════════════
_DEFAULT_CFG: Dict[str, Any] = {
    "node_hosts":    {},                 # node name -> exec.ssh host_id (root)
    "share_root":    "/srv/vera-fs",
    "smb_share":     "vera-fs",
    "store_dataset": "",                 # e.g. rpool/data/vera-store (blank until provisioned)
    "store_mount":   "",                 # its host mountpoint
    "veradata_dataset": "",
    "veradata_mount":   "",
    "reserved_cpus": [],                 # [{label:"ollama", node:"pve", cpus:"0-15", note:""}]
    "mount_vms":     False,              # sshfs running VMs into the share on sync
    "store_writer_instance": "",         # legacy: pulls now go through the writer on VFS-02
    "store_writer_host": "",             # exec.ssh host_id of the store writer (blank = VFS-02 by label)
    "backup_dataset_mount": "/tank_sde/vfs/backup",   # the fabric's backup share, host path
    "nwm_host_id":   "",                 # exec.ssh host_id of the NWM-01 monitor container
}


async def _cfg_get(cluster_id: str) -> Dict:
    r = _redis()
    out = dict(_DEFAULT_CFG)
    if r and cluster_id:
        raw = await r.hget(KEY_CFG, cluster_id)
        if raw:
            try:
                out.update(json.loads(raw))
            except Exception:
                pass
    out["cluster_id"] = cluster_id
    return out


async def _cfg_put(cluster_id: str, cfg: Dict) -> None:
    r = _redis()
    if r and cluster_id:
        cfg = {k: v for k, v in cfg.items() if k != "cluster_id"}
        await r.hset(KEY_CFG, cluster_id, json.dumps(cfg))


@capability(
    "pxstore.settings.get",
    http_method="GET", http_path="/pxstore/settings/get", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Read the storage-fabric settings for a Proxmox cluster "
                "(node→SSH-host mapping, share root, reserved CPU ranges, "
                "central store dataset). Input: cluster_id (str!). "
                "Output: {settings}.",
)
async def cap_settings_get(cluster_id: str = "", trace_id=None) -> Dict:
    if not cluster_id:
        return {"error": "cluster_id required"}
    return {"settings": await _cfg_get(cluster_id)}


@capability(
    "pxstore.settings.save",
    http_method="POST", http_path="/pxstore/settings/save", http_tags=["pxstore"],
    memory="off",
    description="Save storage-fabric settings for a cluster. Any field omitted "
                "keeps its current value. Inputs: cluster_id (str!), node_hosts "
                "(dict node→exec.ssh host_id — the root SSH credential for each "
                "PVE node, enrol via the Workers panel), share_root (str), "
                "store_dataset (str), reserved_cpus (list of {label,node,cpus} — "
                "e.g. the ollama containers' ranges), mount_vms (bool), "
                "store_writer_host (str — exec.ssh host_id of the model "
                "store's writer; blank = VFS-02 found by its label). "
                "Output: {ok, settings}.",
)
async def cap_settings_save(cluster_id: str = "", node_hosts: Dict = None,
                            share_root: str = "", smb_share: str = "",
                            store_dataset: str = "", veradata_dataset: str = "",
                            reserved_cpus: List[Dict] = None,
                            mount_vms: Optional[bool] = None,
                            store_writer_instance: Optional[str] = None,
                            store_writer_host: Optional[str] = None,
                            nwm_host_id: Optional[str] = None,
                            trace_id=None) -> Dict:
    if not cluster_id:
        return {"error": "cluster_id required"}
    cfg = await _cfg_get(cluster_id)
    if node_hosts is not None:
        cfg["node_hosts"] = {str(k): str(v) for k, v in (node_hosts or {}).items()}
    if share_root:
        cfg["share_root"] = share_root
    if smb_share:
        cfg["smb_share"] = _SAFE_NAME.sub("-", smb_share)
    if store_dataset:
        cfg["store_dataset"] = store_dataset
    if veradata_dataset:
        cfg["veradata_dataset"] = veradata_dataset
    if reserved_cpus is not None:
        cfg["reserved_cpus"] = [
            {"label": str(x.get("label", "")), "node": str(x.get("node", "")),
             "cpus": str(x.get("cpus", "")), "note": str(x.get("note", ""))}
            for x in (reserved_cpus or []) if x.get("cpus")
        ]
    if mount_vms is not None:
        cfg["mount_vms"] = bool(mount_vms)
    if store_writer_instance is not None:
        cfg["store_writer_instance"] = store_writer_instance
    if store_writer_host is not None:
        cfg["store_writer_host"] = store_writer_host
    if nwm_host_id is not None:
        cfg["nwm_host_id"] = nwm_host_id
    cfg.pop("worker_policy", None)   # idle-worker policy removed
    await _cfg_put(cluster_id, cfg)
    return {"ok": True, "settings": {**cfg, "cluster_id": cluster_id}}


# ═════════════════════════════════════════════════════════════════════════════
#  SSH ONTO A NODE
# ═════════════════════════════════════════════════════════════════════════════
async def _node_ssh(cluster_id: str, node: str, command: str,
                    timeout: int = 60) -> Dict:
    """Run a shell command as root on a PVE node via the mapped SSH host."""
    cfg = await _cfg_get(cluster_id)
    hid = (cfg.get("node_hosts") or {}).get(node, "")
    if not hid:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": f"no SSH host mapped for node '{node}' — set it in "
                         "pxstore.settings.save (node_hosts)"}
    run = _rawcap("exec.ssh.run")
    if not run:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": "exec.ssh.run unavailable (execution module not loaded)"}
    return await run(command=command, host_id=hid, timeout=timeout)


def _sh(script: str) -> str:
    """Wrap a multi-line script for one ssh exec (bash, fail-fast off)."""
    return "bash -c " + shlex.quote(script)


def _split_sections(text: str, first: str = "HEAD") -> Dict[str, List[str]]:
    """Split a '###NAME'-delimited transcript into named, non-blank line lists."""
    out: Dict[str, List[str]] = {first: []}
    cur = first
    for ln in (text or "").splitlines():
        if ln.startswith("###"):
            cur = ln[3:].strip()
            out[cur] = []
        elif ln.strip():
            out[cur].append(ln)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  SSH ONTO THE FILE FABRIC  (VFS-02)
# ═════════════════════════════════════════════════════════════════════════════
async def _fabric_host(cfg: Dict) -> Tuple[str, str]:
    """(host_id, address) of the file fabric: the configured store writer host,
    else VFS-02 by its host-store label, else by address. Going through the
    store means re-addressing VFS-02 is a host-store edit, not a code change."""
    label, addr = _FABRIC_LABEL, _FABRIC_HOST
    vfs = sys.modules.get("vfs_capabilities")
    if vfs is not None and hasattr(vfs, "_cfg"):
        try:
            vc = await vfs._cfg()
            label, addr = vc.get("ssh_label") or label, vc.get("host") or addr
        except Exception as e:
            log.debug("vfs cfg read failed: %s", e)
    hosts: List[Dict] = []
    listc = _rawcap("exec.ssh.hosts.list")
    if listc:
        try:
            res = await listc()
            got = res.get("hosts") if isinstance(res, dict) else res
            hosts = got if isinstance(got, list) else []
        except Exception as e:
            log.debug("ssh host list failed: %s", e)
    want = cfg.get("store_writer_host", "")
    if want:
        # Configured but gone: fail rather than silently pick another box.
        h = next((h for h in hosts if h.get("id") == want), None)
        return (h.get("id", ""), h.get("host") or addr) if h else ("", addr)
    for key, val in (("label", label), ("host", addr)):
        h = next((h for h in hosts if h.get(key) == val), None)
        if h:
            return h.get("id", ""), h.get("host") or addr
    return "", addr


async def _fabric_ssh(cfg: Dict, command: str, timeout: int = 60) -> Dict:
    """Run a command on the file fabric. The result carries `fabric_addr`."""
    hid, addr = await _fabric_host(cfg)
    base = {"ok": False, "rc": -1, "stdout": "", "stderr": "", "fabric_addr": addr}
    if not hid:
        return {**base, "error": f"no SSH credential for the file fabric "
                                 f"({_FABRIC_LABEL}, {addr}) — enrol it in Workers & "
                                 "Ollama → Connections, or set store_writer_host "
                                 "in pxstore.settings.save"}
    run = _rawcap("exec.ssh.run")
    if not run:
        return {**base, "error": "exec.ssh.run unavailable (execution module not loaded)"}
    try:
        r = await run(command=command, host_id=hid, timeout=timeout)
    except Exception as e:
        return {**base, "error": f"SSH to the file fabric failed: {e}"}
    return {**(r or {}), "fabric_addr": addr}


def _safe(name: str, vmid) -> str:
    n = _SAFE_NAME.sub("-", (name or "").strip()) or f"guest-{vmid}"
    return n


# ═════════════════════════════════════════════════════════════════════════════
#  CPUSET STRING HELPERS  ("0-3,8,10-11" <-> set of ints)
# ═════════════════════════════════════════════════════════════════════════════
def _cpuset_parse(s: str) -> List[int]:
    out: set = set()
    for part in (s or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            except Exception:
                continue
        else:
            try:
                out.add(int(part))
            except Exception:
                continue
    return sorted(out)


def _cpuset_fmt(cpus: List[int]) -> str:
    cpus = sorted(set(cpus))
    if not cpus:
        return ""
    runs, start, prev = [], cpus[0], cpus[0]
    for c in cpus[1:]:
        if c == prev + 1:
            prev = c
            continue
        runs.append((start, prev))
        start = prev = c
    runs.append((start, prev))
    return ",".join(f"{a}-{b}" if b > a else f"{a}" for a, b in runs)


# ═════════════════════════════════════════════════════════════════════════════
#  INVENTORY
# ═════════════════════════════════════════════════════════════════════════════
_INV_SCRIPT = r"""
echo '###ZPOOL'
zpool list -Hp -o name,size,alloc,free,health 2>/dev/null
echo '###ZFS'
zfs list -Hp -t filesystem,volume -o name,used,avail,refer,quota,mountpoint,type 2>/dev/null
echo '###DF'
df -B1 --output=target,source,fstype,size,used,avail -x tmpfs -x devtmpfs -x overlay -x efivarfs 2>/dev/null | tail -n +2
echo '###EXPORTS'
grep -hv '^\s*#' /etc/exports /etc/exports.d/*.exports 2>/dev/null | grep -v '^\s*$'
echo '###END'
"""


def _parse_inventory(stdout: str) -> Dict:
    pools, datasets, mounts, exports = [], [], [], []
    section = ""
    for line in stdout.splitlines():
        line = line.rstrip()
        if line.startswith("###"):
            section = line[3:]
            continue
        if not line.strip():
            continue
        f = line.split()
        try:
            if section == "ZPOOL" and len(f) >= 5:
                pools.append({"name": f[0], "size": int(f[1]), "alloc": int(f[2]),
                              "free": int(f[3]), "health": f[4]})
            elif section == "ZFS" and len(f) >= 7:
                datasets.append({"name": f[0], "used": int(f[1]), "avail": int(f[2]),
                                 "refer": int(f[3]),
                                 "quota": 0 if f[4] in ("-", "0") else int(f[4]),
                                 "mountpoint": f[5], "type": f[6]})
            elif section == "DF" and len(f) >= 6:
                mounts.append({"target": f[0], "source": f[1], "fstype": f[2],
                               "size": int(f[3]), "used": int(f[4]), "avail": int(f[5])})
            elif section == "EXPORTS" and len(f) >= 2:
                exports.append({"path": f[0], "clients": " ".join(f[1:])})
        except Exception:
            continue
    return {"pools": pools, "datasets": datasets, "mounts": mounts,
            "exports": exports}


def _guest_datasets(vmid: int, datasets: List[Dict]) -> List[Dict]:
    """Datasets/zvols belonging to a guest: .../subvol-<vmid>-* or .../vm-<vmid>-*."""
    pats = (f"subvol-{vmid}-", f"vm-{vmid}-", f"base-{vmid}-")
    return [d for d in datasets
            if any(d["name"].rsplit("/", 1)[-1].startswith(p) for p in pats)]


@capability(
    "pxstore.inventory",
    http_method="POST", http_path="/pxstore/inventory", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Full storage inventory of a Proxmox node: guests keyed by real "
                "NAME with their backing ZFS datasets/zvols resolved, ZFS pools "
                "(incl. unallocated space), all datasets, non-ZFS mounts, "
                "EXISTING PVE-defined storages (storage.cfg — NFS/CIFS/dir/"
                "zfspool/lvm) and current NFS exports — so already-attached "
                "storage is detected, not re-provisioned. Inputs: cluster_id "
                "(str!), node (str — blank = first node). Output: {node, pools, "
                "datasets, mounts, storages, exports, guests:[{vmid,name,type,"
                "status,datasets:[{name,used,avail,quota,mountpoint}],disks}], "
                "unallocated_bytes}.",
)
async def cap_inventory(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    rec = await _cluster(cluster_id)
    if not rec:
        return {"error": "cluster not found"}
    res, err = await _pve(rec, "GET", "/cluster/resources")
    if res is None:
        return {"error": err}
    guests = [g for g in res if g.get("type") in ("qemu", "lxc")]
    nodes = sorted({g.get("node", "") for g in guests} |
                   {n.get("node", "") for n in res if n.get("type") == "node"})
    node = node or (nodes[0] if nodes else "")
    if not node:
        return {"error": "no node found"}

    sshr = await _node_ssh(cluster_id, node, _sh(_INV_SCRIPT), timeout=45)
    inv = _parse_inventory(sshr.get("stdout", "")) if sshr.get("rc") == 0 \
        else {"pools": [], "datasets": [], "mounts": [], "exports": []}

    # Existing PVE-defined storages (storage.cfg): NFS/CIFS/dir/zfspool/lvm…
    # so anything already attached to the cluster shows up — no re-provisioning.
    storages: List[Dict] = []
    sdefs, _e = await _pve(rec, "GET", "/storage")
    for s in (sdefs or []):
        storages.append({
            "storage": s.get("storage", ""), "type": s.get("type", ""),
            "content": s.get("content", ""),
            "path": s.get("path", ""), "pool": s.get("pool", ""),
            "server": s.get("server", ""), "export": s.get("export", ""),
            "share": s.get("share", ""),
            "nodes": s.get("nodes", ""), "disabled": bool(s.get("disable", 0)),
        })

    out_guests = []
    for g in guests:
        if g.get("node") != node:
            continue
        vmid = int(g.get("vmid", 0))
        gds = _guest_datasets(vmid, inv["datasets"])
        cfgd, _e = await _pve(rec, "GET",
                              f"/nodes/{node}/{g['type']}/{vmid}/config")
        disks = {}
        for k, v in (cfgd or {}).items():
            if re.match(r"^(rootfs|mp\d+|scsi\d+|virtio\d+|sata\d+|ide\d+|efidisk\d+)$", k) \
                    and isinstance(v, str):
                disks[k] = v
        out_guests.append({
            "vmid": vmid, "name": g.get("name", ""), "type": g.get("type"),
            "status": g.get("status", ""),
            "maxdisk": g.get("maxdisk", 0), "disk": g.get("disk", 0),
            "datasets": gds, "disks": disks,
        })
    out_guests.sort(key=lambda x: x["name"].lower())

    unalloc = sum(p["free"] for p in inv["pools"])
    return {"node": node, "nodes": nodes, "guests": out_guests,
            "pools": inv["pools"], "datasets": inv["datasets"],
            "mounts": inv["mounts"], "storages": storages,
            "exports": inv.get("exports", []),
            "unallocated_bytes": unalloc,
            "ssh_ok": sshr.get("rc") == 0,
            "ssh_error": (sshr.get("error") or sshr.get("stderr", ""))[:400]
                         if sshr.get("rc") != 0 else ""}


# ═════════════════════════════════════════════════════════════════════════════
#  LEGACY FILE SERVER  (Samba on the node) — superseded by VFS-02, retired in stages
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.fs.provision",
    http_method="POST", http_path="/pxstore/fs/provision", http_tags=["pxstore"],
    description="LEGACY: the estate's file server is VFS-02 (vfs.*) — do not "
                "provision this on a new node. "
                "Provision (idempotently) a Samba file server on a Proxmox node "
                "and wire in the vera share config. Installs samba + sshfs, "
                "creates the share root, adds the include to smb.conf, creates "
                "the SMB user, enables smbd. Inputs: cluster_id (str!), node "
                "(str!), smb_user (str='vera'), smb_pass (str — required on "
                "first run), share_root (str — default from settings). "
                "Output: {ok, log} or {error}.",
)
async def cap_fs_provision(cluster_id: str = "", node: str = "",
                           smb_user: str = "vera", smb_pass: str = "",
                           share_root: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    cfg = await _cfg_get(cluster_id)
    root = share_root or cfg["share_root"]
    if share_root and share_root != cfg["share_root"]:
        cfg["share_root"] = share_root
        await _cfg_put(cluster_id, cfg)
    await emit_event({"type": "pxstore.progress", "stage": "fs.provision",
                      "message": f"provisioning samba on {node}"})
    user_block = ""
    if smb_user:
        user_block = f"""
id -u {shlex.quote(smb_user)} >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin {shlex.quote(smb_user)}
"""
        if smb_pass:
            user_block += (
                f"printf '%s\\n%s\\n' {shlex.quote(smb_pass)} {shlex.quote(smb_pass)}"
                f" | smbpasswd -a -s {shlex.quote(smb_user)}\n")
    script = f"""
set -e
export DEBIAN_FRONTEND=noninteractive
command -v smbd >/dev/null 2>&1 || (apt-get -qq update && apt-get -qq -y install samba)
command -v sshfs >/dev/null 2>&1 || apt-get -qq -y install sshfs || true
grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || echo 'user_allow_other' >> /etc/fuse.conf
mkdir -p {shlex.quote(root)}
touch /etc/samba/vera-shares.conf
grep -q 'include *= */etc/samba/vera-shares.conf' /etc/samba/smb.conf || \
  echo 'include = /etc/samba/vera-shares.conf' >> /etc/samba/smb.conf
{user_block}
systemctl enable --now smbd >/dev/null 2>&1 || systemctl enable --now smb
echo PROVISION_OK
"""
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=180)
    if r.get("rc") != 0 or "PROVISION_OK" not in r.get("stdout", ""):
        return {"error": r.get("error") or r.get("stderr", "")[:600] or "provision failed",
                "log": r.get("stdout", "")[-1500:]}
    return {"ok": True, "log": r.get("stdout", "")[-1500:],
            "share_root": root, "smb_user": smb_user}


@capability(
    "pxstore.fs.sync",
    http_method="POST", http_path="/pxstore/fs/sync", http_tags=["pxstore"],
    description="LEGACY hypervisor share — VFS-02 keeps its own estate tree "
                "current (vfs.estate.sync). "
                "(Re)build the name-keyed share tree and Samba share on a node: "
                "one symlink per LXC guest pointing at its ZFS subvol (real "
                "names, not vmids), _host/ links for pools and non-ZFS drives, "
                "optional sshfs mounts for running VMs with enrolled SSH creds, "
                "then rewrite /etc/samba/vera-shares.conf and reload. Safe to "
                "re-run anytime. Inputs: cluster_id (str!), node (str!), "
                "smb_user (str='vera' — 'valid users' for the share), mount_vms "
                "(bool — default from settings). Output: {ok, linked:[...], "
                "vm_mounted:[...], skipped:[{name,reason}], share}.",
)
async def cap_fs_sync(cluster_id: str = "", node: str = "",
                      smb_user: str = "vera", mount_vms: Optional[bool] = None,
                      trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    cfg = await _cfg_get(cluster_id)
    root = cfg["share_root"]
    share = cfg["smb_share"]
    do_vms = cfg["mount_vms"] if mount_vms is None else bool(mount_vms)

    inv = await cap_inventory(cluster_id=cluster_id, node=node)
    if inv.get("error"):
        return {"error": inv["error"]}
    if not inv.get("ssh_ok"):
        return {"error": f"node SSH failed: {inv.get('ssh_error', 'unknown')}"}

    links: List[Tuple[str, str]] = []          # (share name, host path)
    skipped: List[Dict] = []
    vm_targets: List[Dict] = []

    for g in inv["guests"]:
        name = _safe(g["name"], g["vmid"])
        if g["type"] == "lxc":
            root_ds = next((d for d in g["datasets"]
                            if d["type"] == "filesystem"
                            and d["mountpoint"] not in ("-", "none", "legacy")), None)
            if root_ds:
                links.append((name, root_ds["mountpoint"]))
            else:
                skipped.append({"name": name,
                                "reason": "no mounted ZFS subvol found (non-ZFS "
                                          "rootfs — reachable under _host/)"})
        else:
            if do_vms and g["status"] == "running":
                vm_targets.append(g)
            else:
                skipped.append({"name": name,
                                "reason": "VM disks can't be mounted live — "
                                          "enable mount_vms + enrol SSH creds "
                                          "for an sshfs live view"
                                          if g["status"] == "running"
                                          else "VM not running"})

    # Host-level extras: pool roots + non-ZFS data mounts.
    host_links: List[Tuple[str, str]] = []
    for p in inv["pools"]:
        host_links.append((p["name"], f"/{p['name']}"))
    for m in inv["mounts"]:
        if m["fstype"] == "zfs" or m["target"] in ("/", "/boot", "/boot/efi"):
            continue
        host_links.append((_SAFE_NAME.sub("-", m["target"].strip("/") or "root"),
                           m["target"]))

    # VM sshfs mounts — resolve enrolled SSH creds (label 'pve:<vmid>@…').
    vm_mount_lines, vm_mounted = [], []
    if vm_targets:
        ex = sys.modules.get("exec_capabilities")
        hosts = {}
        if ex:
            try:
                hosts = await ex._load_hosts()
            except Exception:
                hosts = {}
        for g in vm_targets:
            name = _safe(g["name"], g["vmid"])
            hrec = next((h for h in hosts.values()
                         if re.match(rf"^pve:{g['vmid']}@", h.get("label", ""))), None)
            if not hrec:
                skipped.append({"name": name,
                                "reason": "no enrolled SSH credential (use "
                                          "proxmox.guest.enroll)"})
                continue
            ip = hrec.get("host", "")
            usr = hrec.get("user", "root")
            pw = ""
            try:
                if hrec.get("auth", "password") == "password":
                    pw = ex._deobfuscate(hrec.get("password_obf", ""))
            except Exception:
                pw = ""
            mnt = f"{root}/{name}"
            if pw:
                vm_mount_lines.append(
                    f"mountpoint -q {shlex.quote(mnt)} || "
                    f"(mkdir -p {shlex.quote(mnt)} && printf '%s\\n' {shlex.quote(pw)} | "
                    f"timeout 20 sshfs -o password_stdin,allow_other,reconnect,"
                    f"ServerAliveInterval=15,StrictHostKeyChecking=no "
                    f"{shlex.quote(usr + '@' + ip)}:/ {shlex.quote(mnt)} "
                    f"&& echo VMOK:{name} || echo VMFAIL:{name})")
            else:
                vm_mount_lines.append(
                    f"mountpoint -q {shlex.quote(mnt)} || "
                    f"(mkdir -p {shlex.quote(mnt)} && "
                    f"timeout 20 sshfs -o allow_other,reconnect,ServerAliveInterval=15,"
                    f"StrictHostKeyChecking=no "
                    f"{shlex.quote(usr + '@' + ip)}:/ {shlex.quote(mnt)} "
                    f"&& echo VMOK:{name} || echo VMFAIL:{name})")
            vm_mounted.append(name)

    ln_lines = [f"ln -sfn {shlex.quote(path)} {shlex.quote(root + '/' + name)}"
                for name, path in links]
    hln_lines = [f"ln -sfn {shlex.quote(path)} {shlex.quote(root + '/_host/' + name)}"
                 for name, path in host_links]

    smb_conf = f"""[global]
   unix extensions = no
   allow insecure wide links = yes

[{share}]
   comment = Vera cluster file fabric ({node})
   path = {root}
   browseable = yes
   read only = no
   follow symlinks = yes
   wide links = yes
   valid users = {smb_user}
   force user = root
   create mask = 0664
   directory mask = 0775
"""
    script = f"""
mkdir -p {shlex.quote(root)} {shlex.quote(root + '/_host')}
# prune dangling symlinks from previous syncs
find {shlex.quote(root)} -maxdepth 2 -xtype l -delete 2>/dev/null
{chr(10).join(ln_lines)}
{chr(10).join(hln_lines)}
{chr(10).join(vm_mount_lines)}
cat > /etc/samba/vera-shares.conf <<'VERASMB'
{smb_conf}
VERASMB
smbcontrol all reload-config >/dev/null 2>&1 || systemctl reload smbd 2>/dev/null || systemctl restart smbd
echo SYNC_OK
"""
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=180)
    if r.get("rc") != 0 or "SYNC_OK" not in r.get("stdout", ""):
        return {"error": r.get("error") or r.get("stderr", "")[:600] or "sync failed",
                "log": r.get("stdout", "")[-1500:]}
    vm_ok = re.findall(r"VMOK:(\S+)", r.get("stdout", ""))
    for nm in re.findall(r"VMFAIL:(\S+)", r.get("stdout", "")):
        skipped.append({"name": nm, "reason": "sshfs mount failed"})
    await emit_event({"type": "pxstore.progress", "stage": "fs.sync",
                      "message": f"share tree rebuilt on {node}: "
                                 f"{len(links)} CT links, {len(vm_ok)} VM mounts"})
    return {"ok": True,
            "linked": [n for n, _ in links],
            "host_linked": [n for n, _ in host_links],
            "vm_mounted": vm_ok,
            "skipped": skipped,
            "share": {"unc": f"\\\\{node}\\{share}", "root": root,
                      "smb_user": smb_user}}


@capability(
    "pxstore.fs.status",
    http_method="POST", http_path="/pxstore/fs/status", http_tags=["pxstore"],
    memory="off", silent=True,
    description="File-server status on a node: smbd state, share tree entries, "
                "active sshfs mounts. Inputs: cluster_id (str!), node (str!). "
                "Output: {running, entries:[{name,target,kind}], share}.",
)
async def cap_fs_status(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    cfg = await _cfg_get(cluster_id)
    root = cfg["share_root"]
    script = f"""
systemctl is-active smbd 2>/dev/null || systemctl is-active smb 2>/dev/null || echo inactive
echo '###LS'
for f in {shlex.quote(root)}/* {shlex.quote(root)}/_host/*; do
  [ -e "$f" ] || [ -L "$f" ] || continue
  t=$(readlink -f "$f" 2>/dev/null || echo '?')
  k=link; mountpoint -q "$f" 2>/dev/null && k=mount
  echo "$f|$k|$t"
done
echo '###PROBE'
{_LEGACY_PROBE}
"""
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=30)
    if r.get("rc") != 0 and not r.get("stdout"):
        return {"error": r.get("error") or r.get("stderr", "")[:400]}
    lines = r.get("stdout", "").splitlines()
    running = bool(lines and lines[0].strip() == "active")
    entries = []
    seen_ls = False
    for ln in lines:
        if ln.startswith("###LS"):
            seen_ls = True
            continue
        if not seen_ls or "|" not in ln:
            continue
        p, k, t = (ln.split("|", 2) + ["", ""])[:3]
        entries.append({"name": p[len(root):].strip("/"), "kind": k, "target": t})
    stdout = r.get("stdout", "")
    probe = _parse_legacy_probe(stdout.split("###PROBE", 1)[1]) \
        if "###PROBE" in stdout else {}
    return {"running": running, "entries": entries, "legacy": True, "probe": probe,
            "share": {"unc": f"\\\\{node}\\{cfg['smb_share']}", "root": root}}


@capability(
    "pxstore.fs.retire",
    http_method="POST", http_path="/pxstore/fs/retire", http_tags=["pxstore"],
    description="Take the LEGACY hypervisor Samba share (\\\\node\\vera-fs) out of "
                "service now that VFS-02 is the file server. Staged and "
                "reversible: the default dry run reports whether smbd runs and "
                "how many clients are connected; confirm=true stops and disables "
                "smbd+nmbd but keeps the config and share tree, and refuses "
                "while anyone is connected unless force=true; restore=true "
                "brings it straight back. Inputs: cluster_id (str!), node "
                "(str!), confirm (bool=false), force (bool=false), restore "
                "(bool=false). Output: {ok, dry_run, action, probe:{active, "
                "enabled, sessions, listening}, blocked} or {error}.",
)
async def cap_fs_retire(cluster_id: str = "", node: str = "",
                        confirm: bool = False, force: bool = False,
                        restore: bool = False, trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    pr = await _node_ssh(cluster_id, node, _sh(_LEGACY_PROBE), timeout=30)
    if pr.get("error"):
        return {"error": pr["error"]}
    probe = _parse_legacy_probe(pr.get("stdout", ""))
    action = "restore" if restore else "retire"
    if not confirm:
        return {"ok": True, "dry_run": True, "action": action, "probe": probe,
                "would": ("enable and start smbd+nmbd" if restore else
                          "stop and disable smbd+nmbd, keeping config and share tree"),
                "blocked": bool(not restore and not force
                                and (probe.get("sessions") or 0) > 0)}
    script = _restore_legacy_script() if restore else _retire_legacy_script(force=force)
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=60)
    out = r.get("stdout", "")
    if "IN_USE" in out:
        return {"error": f"refused: {out.split('IN_USE', 1)[1].split()[0]} client(s) "
                         "connected to the legacy share — move them to VFS-02 "
                         "first, or pass force=true", "probe": probe}
    if ("RESTORED" if restore else "RETIRED") not in out:
        return {"error": r.get("error") or (r.get("stderr") or out)[:400] or f"{action} failed",
                "probe": probe}
    after = await _node_ssh(cluster_id, node, _sh(_LEGACY_PROBE), timeout=30)
    await emit_event({"type": "pxstore.progress", "stage": "fs.retire",
                      "message": f"legacy hypervisor share on {node}: {action}d"})
    return {"ok": True, "dry_run": False, "action": action, "before": probe,
            "probe": _parse_legacy_probe(after.get("stdout", ""))}


# ═════════════════════════════════════════════════════════════════════════════
#  STORAGE MANAGEMENT  (resize / quotas / datasets)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.disk.resize",
    http_method="POST", http_path="/pxstore/disk/resize", http_tags=["pxstore"],
    description="Grow a guest disk (shrink is not supported by PVE). LXC: the "
                "filesystem/quota grows immediately. VM: the block device grows; "
                "the partition+fs inside the guest still need growing — if the "
                "guest has enrolled SSH creds and grow_in_guest=true this runs "
                "growpart+resize2fs/xfs_growfs automatically. Inputs: cluster_id "
                "(str!), node (str!), guest_type ('qemu'|'lxc'), vmid (int!), "
                "disk (str='rootfs' for LXC, e.g. 'scsi0' for VM), size (str! — "
                "'+10G' relative or absolute '50G'), grow_in_guest (bool=false). "
                "Output: {ok, upid, guest_steps} or {error}.",
)
async def cap_disk_resize(cluster_id: str = "", node: str = "",
                          guest_type: str = "", vmid: int = 0,
                          disk: str = "", size: str = "",
                          grow_in_guest: bool = False, trace_id=None) -> Dict:
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    if not (cluster_id and node and vmid and size):
        return {"error": "cluster_id, node, vmid, size required"}
    disk = disk or ("rootfs" if guest_type == "lxc" else "scsi0")
    if not re.match(r"^\+?\d+(\.\d+)?[MGT]$", size):
        return {"error": "size must look like '+10G' or '50G'"}
    rec = await _cluster(cluster_id)
    if not rec:
        return {"error": "cluster not found"}
    upid, err = await _pve(rec, "PUT",
                           f"/nodes/{node}/{guest_type}/{vmid}/resize",
                           {"disk": disk, "size": size})
    if err:
        return {"error": err}
    await emit_event({"type": "pxstore.progress", "stage": "disk.resize",
                      "message": f"resized {guest_type} {vmid} {disk} by {size}"})
    out: Dict[str, Any] = {"ok": True, "upid": upid}
    if guest_type == "qemu":
        steps = ("Inside the guest: 1) lsblk to find the grown device, "
                 "2) growpart /dev/sdX <partnum>, 3) resize2fs /dev/sdXN "
                 "(ext4) or xfs_growfs / (xfs). LVM: pvresize + lvextend -r.")
        out["guest_steps"] = steps
        if grow_in_guest:
            ex = sys.modules.get("exec_capabilities")
            hosts = await ex._load_hosts() if ex else {}
            hrec = next((h for h in hosts.values()
                         if re.match(rf"^pve:{vmid}@", h.get("label", ""))), None)
            if not hrec:
                out["guest_grow"] = "skipped: no enrolled SSH credential for this VM"
            else:
                run = _rawcap("exec.ssh.run")
                gs = ("set -e; command -v growpart >/dev/null || "
                      "(apt-get -qq update && apt-get -qq -y install cloud-guest-utils); "
                      "ROOTSRC=$(findmnt -n -o SOURCE /); DEV=$(lsblk -npo PKNAME $ROOTSRC | head -1); "
                      "PART=$(echo $ROOTSRC | grep -o '[0-9]*$'); "
                      "growpart /dev/$DEV $PART || true; "
                      "FST=$(findmnt -n -o FSTYPE /); "
                      "if [ \"$FST\" = xfs ]; then xfs_growfs /; else resize2fs $ROOTSRC; fi; "
                      "df -h /")
                gr = await run(command=_sh(gs), host_id=hrec.get("id", ""), timeout=120)
                out["guest_grow"] = (gr.get("stdout", "")[-500:]
                                     if gr.get("rc") == 0
                                     else f"failed: {(gr.get('error') or gr.get('stderr',''))[:300]}")
    return out


@capability(
    "pxstore.zfs.set",
    http_method="POST", http_path="/pxstore/zfs/set", http_tags=["pxstore"],
    description="Set ZFS properties on a dataset (quota management). Inputs: "
                "cluster_id (str!), node (str!), dataset (str!), quota (str — "
                "'none' or e.g. '50G'), refquota (str), compression (str), "
                "reservation (str). Only supplied fields are set. Output: {ok}.",
)
async def cap_zfs_set(cluster_id: str = "", node: str = "", dataset: str = "",
                      quota: str = "", refquota: str = "", compression: str = "",
                      reservation: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node and dataset):
        return {"error": "cluster_id, node, dataset required"}
    if not re.match(r"^[A-Za-z0-9._/-]+$", dataset):
        return {"error": "invalid dataset name"}
    sets = []
    for prop, val in (("quota", quota), ("refquota", refquota),
                      ("compression", compression), ("reservation", reservation)):
        if val:
            if not re.match(r"^[A-Za-z0-9.]+$", val):
                return {"error": f"invalid value for {prop}"}
            sets.append(f"zfs set {prop}={val} {shlex.quote(dataset)}")
    if not sets:
        return {"error": "no properties supplied"}
    r = await _node_ssh(cluster_id, node, _sh("set -e\n" + "\n".join(sets) + "\necho OK"))
    if r.get("rc") != 0:
        return {"error": (r.get("error") or r.get("stderr", ""))[:400]}
    return {"ok": True, "applied": sets}


@capability(
    "pxstore.zfs.create",
    http_method="POST", http_path="/pxstore/zfs/create", http_tags=["pxstore"],
    description="Create a ZFS dataset (idempotent, -p creates parents). Inputs: "
                "cluster_id (str!), node (str!), dataset (str! — e.g. "
                "'rpool/data/vera-store'), mountpoint (str), compression "
                "(str='zstd'), quota (str). Output: {ok, mountpoint}.",
)
async def cap_zfs_create(cluster_id: str = "", node: str = "", dataset: str = "",
                         mountpoint: str = "", compression: str = "zstd",
                         quota: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node and dataset):
        return {"error": "cluster_id, node, dataset required"}
    if not re.match(r"^[A-Za-z0-9._/-]+$", dataset):
        return {"error": "invalid dataset name"}
    opts = f"-o compression={compression}" if compression else ""
    if mountpoint:
        opts += f" -o mountpoint={shlex.quote(mountpoint)}"
    if quota:
        opts += f" -o quota={quota}"
    script = f"""
set -e
zfs list {shlex.quote(dataset)} >/dev/null 2>&1 || zfs create -p {opts} {shlex.quote(dataset)}
zfs get -H -o value mountpoint {shlex.quote(dataset)}
"""
    r = await _node_ssh(cluster_id, node, _sh(script))
    if r.get("rc") != 0:
        return {"error": (r.get("error") or r.get("stderr", ""))[:400]}
    return {"ok": True, "dataset": dataset,
            "mountpoint": r.get("stdout", "").strip().splitlines()[-1]}


# ═════════════════════════════════════════════════════════════════════════════
#  CPU / NUMA
# ═════════════════════════════════════════════════════════════════════════════
_CPU_SCRIPT = r"""
echo '###TOPO'
lscpu -p=CPU,NODE,SOCKET,CORE 2>/dev/null | grep -v '^#'
echo '###LXC'
for f in /etc/pve/lxc/*.conf; do
  [ -e "$f" ] || continue
  vmid=$(basename "$f" .conf)
  pin=$(grep -E '^lxc\.cgroup2?\.cpuset\.cpus' "$f" | tail -1 | sed 's/.*= *//;s/.*: *//')
  cores=$(grep -E '^cores:' "$f" | tail -1 | awk '{print $2}')
  live=$(cat /sys/fs/cgroup/lxc/$vmid/cpuset.cpus.effective 2>/dev/null)
  echo "$vmid|$pin|$cores|$live"
done
echo '###QEMU'
for f in /etc/pve/qemu-server/*.conf; do
  [ -e "$f" ] || continue
  vmid=$(basename "$f" .conf)
  aff=$(grep -E '^affinity:' "$f" | tail -1 | awk '{print $2}')
  cores=$(grep -E '^cores:' "$f" | tail -1 | awk '{print $2}')
  pid=$(cat /var/run/qemu-server/$vmid.pid 2>/dev/null)
  live=''
  [ -n "$pid" ] && live=$(taskset -pc $pid 2>/dev/null | awk '{print $NF}')
  echo "$vmid|$aff|$cores|$live"
done
echo '###END'
"""


def _parse_cpu(stdout: str) -> Tuple[List[Dict], Dict[int, Dict], Dict[int, Dict]]:
    topo, lxc, qemu = [], {}, {}
    section = ""
    for ln in stdout.splitlines():
        ln = ln.strip()
        if ln.startswith("###"):
            section = ln[3:]
            continue
        if not ln:
            continue
        if section == "TOPO":
            f = ln.split(",")
            if len(f) >= 4:
                try:
                    topo.append({"cpu": int(f[0]), "node": int(f[1]),
                                 "socket": int(f[2]), "core": int(f[3])})
                except Exception:
                    pass
        elif section in ("LXC", "QEMU"):
            f = (ln.split("|") + ["", "", "", ""])[:4]
            try:
                vmid = int(f[0])
            except Exception:
                continue
            rec = {"pinned": f[1], "cores": f[2], "live": f[3]}
            (lxc if section == "LXC" else qemu)[vmid] = rec
    return topo, lxc, qemu


def _numa_of(cpus: List[int], topo: List[Dict]) -> List[int]:
    bynode = {t["cpu"]: t["node"] for t in topo}
    return sorted({bynode.get(c, -1) for c in cpus})


@capability(
    "pxstore.cpu.topology",
    http_method="POST", http_path="/pxstore/cpu/topology", http_tags=["pxstore"],
    memory="off", silent=True,
    description="CPU/NUMA topology of a node from lscpu: every CPU with its "
                "NUMA node, socket and core. Inputs: cluster_id (str!), node "
                "(str!). Output: {cpus:[{cpu,node,socket,core}], numa_nodes:"
                "{node:[cpus]}}.",
)
async def cap_cpu_topology(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    r = await _node_ssh(cluster_id, node, _sh(_CPU_SCRIPT), timeout=30)
    if r.get("rc") != 0:
        return {"error": (r.get("error") or r.get("stderr", ""))[:400]}
    topo, _l, _q = _parse_cpu(r.get("stdout", ""))
    nodes: Dict[int, List[int]] = {}
    for t in topo:
        nodes.setdefault(t["node"], []).append(t["cpu"])
    return {"cpus": topo,
            "numa_nodes": {str(k): sorted(v) for k, v in sorted(nodes.items())}}


@capability(
    "pxstore.cpu.map",
    http_method="POST", http_path="/pxstore/cpu/map", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Per-guest CPU pinning map with conflict analysis: each guest's "
                "configured pin (LXC cpuset / QEMU affinity), live cpuset, NUMA "
                "nodes it touches, plus flags — spans_numa (bad), overlaps a "
                "reserved range (e.g. the ollama containers'), or unpinned "
                "(floats over ALL cpus incl. reserved ones). Inputs: cluster_id "
                "(str!), node (str!). Output: {guests:[{vmid,name,type,status,"
                "pinned,live,cores,numa,flags}], topology, reserved}.",
)
async def cap_cpu_map(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    rec = await _cluster(cluster_id)
    if not rec:
        return {"error": "cluster not found"}
    cfg = await _cfg_get(cluster_id)
    r = await _node_ssh(cluster_id, node, _sh(_CPU_SCRIPT), timeout=30)
    if r.get("rc") != 0:
        return {"error": (r.get("error") or r.get("stderr", ""))[:400]}
    topo, lxc, qemu = _parse_cpu(r.get("stdout", ""))
    res, _e = await _pve(rec, "GET", "/cluster/resources")
    names = {int(g.get("vmid", 0)): (g.get("name", ""), g.get("status", ""))
             for g in (res or []) if g.get("type") in ("qemu", "lxc")
             and g.get("node") == node}

    reserved_all: List[int] = []
    for rr in cfg.get("reserved_cpus", []):
        if not rr.get("node") or rr["node"] == node:
            reserved_all += _cpuset_parse(rr.get("cpus", ""))
    reserved_set = set(reserved_all)
    all_cpus = [t["cpu"] for t in topo]

    guests = []
    for vmid, data, gtype in ([(v, d, "lxc") for v, d in lxc.items()]
                              + [(v, d, "qemu") for v, d in qemu.items()]):
        pin = data.get("pinned", "")
        live = data.get("live", "")
        eff = _cpuset_parse(pin or live)
        nm, st = names.get(vmid, ("", "unknown"))
        flags = []
        if not pin:
            flags.append("unpinned")
            if reserved_set:
                flags.append("floats-over-reserved")
        if eff:
            numa = _numa_of(eff, topo)
            if len([n for n in numa if n >= 0]) > 1:
                flags.append("spans-numa")
            if reserved_set & set(eff):
                # a guest that IS the reservation owner overlaps by design —
                # exact match with a reserved range is treated as the owner
                owned = any(set(_cpuset_parse(rr.get("cpus", ""))) == set(eff)
                            for rr in cfg.get("reserved_cpus", []))
                if not owned:
                    flags.append("overlaps-reserved")
        else:
            numa = []
        guests.append({"vmid": vmid, "name": nm or f"guest-{vmid}",
                       "type": gtype, "status": st,
                       "pinned": pin, "live": live,
                       "cores": data.get("cores", ""),
                       "cpus": eff, "numa": numa, "flags": flags})
    guests.sort(key=lambda g: g["name"].lower())
    nodes: Dict[int, List[int]] = {}
    for t in topo:
        nodes.setdefault(t["node"], []).append(t["cpu"])
    return {"guests": guests,
            "topology": {"cpus": all_cpus,
                         "numa_nodes": {str(k): sorted(v)
                                        for k, v in sorted(nodes.items())}},
            "reserved": cfg.get("reserved_cpus", [])}


@capability(
    "pxstore.cpu.pin",
    http_method="POST", http_path="/pxstore/cpu/pin", http_tags=["pxstore"],
    description="Pin a guest to a cpuset. LXC: writes lxc.cgroup2.cpuset.cpus "
                "into the CT config and applies live via cgroup when running. "
                "QEMU: qm set --affinity (live threads are re-tasksetted too, "
                "but a restart makes it fully durable). Refuses sets that "
                "overlap a reserved range or span NUMA nodes unless force=true. "
                "Inputs: cluster_id (str!), node (str!), guest_type "
                "('qemu'|'lxc'), vmid (int!), cpus (str! — e.g. '16-23'), force "
                "(bool=false). Output: {ok, applied_live} or {error}.",
)
async def cap_cpu_pin(cluster_id: str = "", node: str = "", guest_type: str = "",
                      vmid: int = 0, cpus: str = "", force: bool = False,
                      trace_id=None) -> Dict:
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    if not (cluster_id and node and vmid and cpus):
        return {"error": "cluster_id, node, vmid, cpus required"}
    want = _cpuset_parse(cpus)
    if not want:
        return {"error": "cpus could not be parsed (expected e.g. '16-23' or '4,6,8')"}
    cpus = _cpuset_fmt(want)

    # Guard rails: reserved overlap + NUMA span.
    cfg = await _cfg_get(cluster_id)
    tr = await _node_ssh(cluster_id, node, _sh(_CPU_SCRIPT), timeout=30)
    topo, _l, _q = _parse_cpu(tr.get("stdout", "")) if tr.get("rc") == 0 else ([], {}, {})
    if not force:
        for rr in cfg.get("reserved_cpus", []):
            if rr.get("node") and rr["node"] != node:
                continue
            rset = set(_cpuset_parse(rr.get("cpus", "")))
            if rset and (rset & set(want)) and set(want) != rset:
                return {"error": f"cpuset overlaps reserved range "
                                 f"'{rr.get('label') or rr.get('cpus')}' "
                                 f"({rr.get('cpus')}) — pass force=true to override"}
        if topo:
            numa = [n for n in _numa_of(want, topo) if n >= 0]
            if len(numa) > 1:
                return {"error": f"cpuset spans NUMA nodes {numa} — pass "
                                 "force=true to override"}

    if guest_type == "lxc":
        conf = f"/etc/pve/lxc/{int(vmid)}.conf"
        script = f"""
set -e
test -f {conf}
sed -i '/^lxc\\.cgroup2\\?\\.cpuset\\.cpus/d' {conf}
echo 'lxc.cgroup2.cpuset.cpus: {cpus}' >> {conf}
LIVE=no
if [ -w /sys/fs/cgroup/lxc/{int(vmid)}/cpuset.cpus ]; then
  echo '{cpus}' > /sys/fs/cgroup/lxc/{int(vmid)}/cpuset.cpus && LIVE=yes
fi
echo PIN_OK live=$LIVE
"""
    else:
        script = f"""
set -e
qm set {int(vmid)} --affinity '{cpus}' >/dev/null
LIVE=no
PID=$(cat /var/run/qemu-server/{int(vmid)}.pid 2>/dev/null || true)
if [ -n "$PID" ]; then
  for t in /proc/$PID/task/*; do taskset -pc '{cpus}' $(basename $t) >/dev/null 2>&1 || true; done
  LIVE=yes
fi
echo PIN_OK live=$LIVE
"""
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=45)
    if r.get("rc") != 0 or "PIN_OK" not in r.get("stdout", ""):
        return {"error": (r.get("error") or r.get("stderr", ""))[:400] or "pin failed"}
    live = "live=yes" in r.get("stdout", "")
    await emit_event({"type": "pxstore.progress", "stage": "cpu.pin",
                      "message": f"pinned {guest_type} {vmid} to {cpus}"
                                 + ("" if live else " (takes effect on next start)")})
    return {"ok": True, "cpus": cpus, "applied_live": live}


@capability(
    "pxstore.cpu.suggest",
    http_method="POST", http_path="/pxstore/cpu/suggest", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Suggest a safe cpuset for a new/re-pinned guest: N cpus on ONE "
                "NUMA node, excluding all reserved ranges (ollama) and cpus "
                "already pinned to other guests (least-loaded node preferred). "
                "Inputs: cluster_id (str!), node (str!), count (int!), "
                "exclude_vmid (int — ignore this guest's own pin). Output: "
                "{cpus, numa_node, free_per_node} or {error}.",
)
async def cap_cpu_suggest(cluster_id: str = "", node: str = "", count: int = 1,
                          exclude_vmid: int = 0, trace_id=None) -> Dict:
    m = await cap_cpu_map(cluster_id=cluster_id, node=node)
    if m.get("error"):
        return {"error": m["error"]}
    reserved = set()
    for rr in m.get("reserved", []):
        if not rr.get("node") or rr["node"] == node:
            reserved |= set(_cpuset_parse(rr.get("cpus", "")))
    taken = set(reserved)
    for g in m["guests"]:
        if g["vmid"] == exclude_vmid:
            continue
        taken |= set(g.get("cpus") or [])
    free_per_node: Dict[str, List[int]] = {}
    for nn, cpus in m["topology"]["numa_nodes"].items():
        free_per_node[nn] = [c for c in cpus if c not in taken]
    # pick the node with most free cpus that can fit `count`
    best = max((nn for nn in free_per_node), default=None,
               key=lambda nn: len(free_per_node[nn]))
    if best is None or len(free_per_node[best]) < max(1, int(count)):
        return {"error": f"no NUMA node has {count} free cpus "
                         f"(free: { {k: len(v) for k, v in free_per_node.items()} })",
                "free_per_node": {k: _cpuset_fmt(v) for k, v in free_per_node.items()}}
    pick = free_per_node[best][: max(1, int(count))]
    return {"cpus": _cpuset_fmt(pick), "numa_node": int(best),
            "free_per_node": {k: _cpuset_fmt(v) for k, v in free_per_node.items()}}


# ═════════════════════════════════════════════════════════════════════════════
#  CENTRAL MODEL / ARTIFACT STORE
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.store.provision",
    http_method="POST", http_path="/pxstore/store/provision", http_tags=["pxstore"],
    description="Create the central store dataset on a node (idempotent): a ZFS "
                "dataset with zstd compression and models/ images/ artifacts/ "
                "subdirs, remembered in settings. Bind-mount it read-only into "
                "consumer CTs with pxstore.store.attach. Inputs: cluster_id "
                "(str!), node (str!), dataset (str — default the configured store, "
                "else 'tank_sdh/vera-store'), "
                "quota (str — optional). Output: {ok, dataset, mountpoint}.",
)
async def cap_store_provision(cluster_id: str = "", node: str = "",
                              dataset: str = "",
                              quota: str = "", trace_id=None) -> Dict:
    if not dataset:
        dataset = (await _cfg_get(cluster_id)).get("store_dataset") or "tank_sdh/vera-store"
    r = await cap_zfs_create(cluster_id=cluster_id, node=node, dataset=dataset,
                             compression="zstd", quota=quota)
    if r.get("error"):
        return r
    mp = r["mountpoint"]
    mk = await _node_ssh(cluster_id, node, _sh(
        f"mkdir -p {shlex.quote(mp)}/models/ollama {shlex.quote(mp)}/images "
        f"{shlex.quote(mp)}/artifacts && echo OK"))
    if mk.get("rc") != 0:
        return {"error": (mk.get("error") or mk.get("stderr", ""))[:400]}
    cfg = await _cfg_get(cluster_id)
    cfg["store_dataset"], cfg["store_mount"] = dataset, mp
    await _cfg_put(cluster_id, cfg)
    await emit_event({"type": "pxstore.progress", "stage": "store.provision",
                      "message": f"central store ready at {mp} ({dataset})"})
    return {"ok": True, "dataset": dataset, "mountpoint": mp}


def _next_mp_index(disks: Dict[str, str]) -> int:
    used = {int(m.group(1)) for k in disks
            if (m := re.match(r"^mp(\d+)$", k))}
    i = 0
    while i in used:
        i += 1
    return i


@capability(
    "pxstore.store.attach",
    http_method="POST", http_path="/pxstore/store/attach", http_tags=["pxstore"],
    description="Bind-mount a subdir of the central store into an LXC container "
                "(read-only by default) so guests share one copy of models "
                "instead of replicating them. Uses the next free mpN slot; the "
                "CT must be RESTARTED for the mount to appear. Proxmox refuses a "
                "bind-mount mpN from an API token (root@pam only), so on that "
                "HTTP 403 this falls back to `pct set` over the node's mapped SSH "
                "host. Inputs: cluster_id (str!), node (str!), vmid (int!), subdir "
                "(str='models/ollama'), ct_path (str='/.ollama/models' — where it "
                "appears inside the CT; not /root, which unprivileged CTs cannot "
                "traverse), ro (bool=true — set false for the ONE writer CT that "
                "pulls new models). Output: {ok, mp_key, host_path, via "
                "('api'|'node_shell'), restart_required:true}.",
)
async def cap_store_attach(cluster_id: str = "", node: str = "", vmid: int = 0,
                           subdir: str = "models/ollama",
                           ct_path: str = _ATTACH_CT_PATH,
                           ro: bool = True, trace_id=None) -> Dict:
    if not (cluster_id and node and vmid):
        return {"error": "cluster_id, node, vmid required"}
    rec = await _cluster(cluster_id)
    if not rec:
        return {"error": "cluster not found"}
    cfg = await _cfg_get(cluster_id)
    mp_root = cfg.get("store_mount", "")
    if not mp_root:
        return {"error": "central store not provisioned — run pxstore.store.provision first"}
    host_path = f"{mp_root}/{subdir}".rstrip("/")
    cfgd, err = await _pve(rec, "GET", f"/nodes/{node}/lxc/{vmid}/config")
    if cfgd is None:
        return {"error": err}
    # already attached?
    for k, v in cfgd.items():
        if re.match(r"^mp\d+$", k) and isinstance(v, str) and v.startswith(host_path + ","):
            return {"ok": True, "mp_key": k, "host_path": host_path,
                    "already_attached": True, "restart_required": False}
    idx = _next_mp_index({k: v for k, v in cfgd.items() if isinstance(v, str)})
    mp_key = f"mp{idx}"
    try:
        val = _mp_value(host_path, ct_path, ro)
    except ValueError as e:
        return {"error": str(e)}
    _d, err = await _pve(rec, "PUT", f"/nodes/{node}/lxc/{vmid}/config", {mp_key: val})
    via = "api"
    if err:
        # A 403 here is Proxmox refusing a bind-mount mpN from an API token
        # (root@pam only) -- it will never succeed on retry. Anything else is a
        # real failure, and falling back would only hide it.
        if not _is_token_refusal(err):
            return {"error": err}
        try:
            cmd = _pct_set_command(vmid, mp_key, val)
        except ValueError as e:
            return {"error": str(e)}
        r = await _node_ssh(cluster_id, node, cmd, timeout=60)
        if r.get("error") or r.get("rc", 0) != 0:
            return {"error": "the API refused the bind mount (" + str(err) + ") and "
                             "the node-shell fallback failed: "
                             + (r.get("error") or (r.get("stderr") or "")[:300]
                                or f"rc={r.get('rc')}"),
                    "hint": "map this node to an SSH host with "
                            "pxstore.settings.save node_hosts"}
        via = "node_shell"
    await emit_event({"type": "pxstore.progress", "stage": "store.attach",
                      "message": f"attached {host_path} → CT {vmid}:{ct_path} "
                                 f"({'ro' if ro else 'rw'}, via {via}) — restart CT to apply"})
    return {"ok": True, "mp_key": mp_key, "host_path": host_path,
            "ct_path": ct_path, "ro": ro, "via": via, "restart_required": True}


@capability(
    "pxstore.store.consolidate",
    http_method="POST", http_path="/pxstore/store/consolidate", http_tags=["pxstore"],
    description="Rsync model data FROM one or more LXC containers' local dirs "
                "INTO the central store (host-side, via each CT's subvol — no "
                "guest downtime; existing blobs are never rewritten and in-flight "
                "partial pulls are skipped). Run "
                "once per source CT before switching them to the read-only "
                "mount. Inputs: cluster_id (str!), node (str!), vmid (int!), "
                "src_path (str='/.ollama/models' — path INSIDE the CT; read "
                "OLLAMA_MODELS from its ollama-vera unit, it differs per node), "
                "subdir (str='models/ollama'), delete_source (bool=false — "
                "after a successful copy, rename the CT-local dir to "
                "<dir>.pre-store to free the space; do this only after the ro "
                "mount is attached+verified). Output: {ok, stats, freed_hint}.",
)
async def cap_store_consolidate(cluster_id: str = "", node: str = "", vmid: int = 0,
                                src_path: str = "/.ollama/models",
                                subdir: str = "models/ollama",
                                delete_source: bool = False, trace_id=None) -> Dict:
    if not (cluster_id and node and vmid):
        return {"error": "cluster_id, node, vmid required"}
    cfg = await _cfg_get(cluster_id)
    mp_root = cfg.get("store_mount", "")
    if not mp_root:
        return {"error": "central store not provisioned — run pxstore.store.provision first"}
    inv = await cap_inventory(cluster_id=cluster_id, node=node)
    if inv.get("error"):
        return {"error": inv["error"]}
    g = next((x for x in inv["guests"] if x["vmid"] == int(vmid)), None)
    if not g:
        return {"error": f"vmid {vmid} not found on {node}"}
    if g["type"] != "lxc":
        return {"error": "consolidate works on LXC containers (VM disks aren't "
                         "host-mountable live) — for VMs rsync over SSH instead"}
    root_ds = next((d for d in g["datasets"]
                    if d["type"] == "filesystem"
                    and d["mountpoint"] not in ("-", "none", "legacy")), None)
    if not root_ds:
        return {"error": "could not resolve the CT's ZFS subvol mountpoint"}
    src = f"{root_ds['mountpoint']}{src_path}".rstrip("/")
    dst = f"{mp_root}/{subdir}".rstrip("/")
    await emit_event({"type": "pxstore.progress", "stage": "store.consolidate",
                      "message": f"rsyncing CT {vmid} ({g['name']}) {src_path} → store"})
    script = f"""
set -e
test -d {shlex.quote(src)} || {{ echo NO_SOURCE; exit 3; }}
mkdir -p {shlex.quote(dst)}
rsync {_RSYNC_FLAGS} {shlex.quote(src + '/')} {shlex.quote(dst + '/')}
"""
    if delete_source:
        script += f"mv {shlex.quote(src)} {shlex.quote(src + '.pre-store')}\n"
    script += "echo CONSOLIDATE_OK"
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=3600)
    so = r.get("stdout", "")
    if "NO_SOURCE" in so:
        return {"error": f"source dir not found inside CT: {src_path} "
                         f"(host path {src})"}
    if r.get("rc") != 0 or "CONSOLIDATE_OK" not in so:
        return {"error": (r.get("error") or r.get("stderr", ""))[:600] or "rsync failed",
                "log": so[-1500:]}
    stats = "\n".join(ln for ln in so.splitlines()
                      if re.match(r"^(Number of|Total|Literal|sent|total size)", ln))
    return {"ok": True, "stats": stats, "src_host_path": src, "dst": dst,
            "freed_hint": (f"{src} renamed to {src}.pre-store — rm -rf it once "
                           "the ro mount is verified" if delete_source else
                           "source left in place — re-run with delete_source="
                           "true after attaching + verifying the ro mount")}


@capability(
    "pxstore.store.status",
    http_method="POST", http_path="/pxstore/store/status", http_tags=["pxstore"],
    memory="off", silent=True,
    description="The shared model store at a glance: dataset usage and "
                "compression, how many Ollama models it holds, which containers "
                "mount it and whether read-only, the store's writer on the file "
                "fabric (VFS-02), and how other machines reach it. Inputs: "
                "cluster_id (str!), node (str!). Output: {dataset, mount, used, "
                "avail, ratio, models, consumers:[{vmid,name,status,ct_path,ro}], "
                "writer:{host,active,version,port,error}, share:{server,path,nfs,"
                "unc,smb,clients}} or {error}.",
)
async def cap_store_status(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    cfg = await _cfg_get(cluster_id)
    ds, mp = cfg.get("store_dataset", ""), cfg.get("store_mount", "")
    if not (ds and mp):
        return {"error": "central store not provisioned — run pxstore.store.provision first",
                "provisioned": False}
    try:
        fab = _fabric_path(mp)
    except ValueError:
        fab = ""
    node_script = "\n".join([
        f"zfs list -Hp -o used,avail,compressratio {shlex.quote(ds)} 2>/dev/null",
        "echo '###MODELS'",
        f"find {shlex.quote(mp + '/models/ollama/manifests')} -type f 2>/dev/null | wc -l",
        "echo '###CONSUMERS'",
        f"grep -H {shlex.quote(mp)} /etc/pve/lxc/*.conf 2>/dev/null",
        "echo '###STATUS'",
        "pct list 2>/dev/null | tail -n +2",
    ])
    fabric_script = "\n".join([
        f"echo \"active=$(systemctl is-active {_WRITER_UNIT} 2>/dev/null)\"",
        f"echo \"version=$(curl -s -m 3 http://127.0.0.1:{_WRITER_PORT}/api/version 2>/dev/null)\"",
        "echo '###EXPORTS'",
        "cat /etc/exports 2>/dev/null",
        "echo '###SMB'",
        "testparm -s 2>/dev/null | awk '/^\\[/{s=$0} /^[[:space:]]*path = /{print s, $3}'",
    ])
    node_r, fab_r = await asyncio.gather(
        _node_ssh(cluster_id, node, _sh(node_script), timeout=45),
        _fabric_ssh(cfg, _sh(fabric_script), timeout=30))
    if node_r.get("error"):
        return {"error": node_r["error"]}

    s = _split_sections(node_r.get("stdout", ""))
    used = avail = 0
    ratio = ""
    if s["HEAD"]:
        f = s["HEAD"][0].split()
        if len(f) >= 3:
            used = int(f[0]) if f[0].isdigit() else 0
            avail = int(f[1]) if f[1].isdigit() else 0
            ratio = f[2]
    mc = (s.get("MODELS") or ["0"])[0].strip()
    status = {}
    for ln in s.get("STATUS", []):
        f = ln.split()
        if len(f) >= 2 and f[0].isdigit():
            status[int(f[0])] = {"status": f[1], "name": f[-1]}
    consumers = _parse_consumers("\n".join(s.get("CONSUMERS", [])), mp)
    for c in consumers:
        c.update(status.get(c["vmid"], {"status": "unknown", "name": str(c["vmid"])}))

    addr = fab_r.get("fabric_addr") or _FABRIC_HOST
    writer = {"host": addr, "active": False, "version": "", "port": _WRITER_PORT,
              "error": fab_r.get("error", "")}
    share = {"server": addr, "path": fab, "nfs": "", "unc": "", "smb": "", "clients": []}
    if not fab_r.get("error"):
        fs = _split_sections(fab_r.get("stdout", ""))
        kv = {k: v for k, _, v in (ln.partition("=") for ln in fs["HEAD"])}
        writer["active"] = kv.get("active", "").strip() == "active"
        try:
            writer["version"] = json.loads(kv.get("version") or "{}").get("version", "")
        except ValueError:
            writer["version"] = ""
        if fab:
            ex = _parse_exports("\n".join(fs.get("EXPORTS", [])))
            share["clients"] = [{"client": c, "read_only": "ro" in o.split(",")}
                                for c, o in ex.get(fab, [])]
            if share["clients"]:
                share["nfs"] = f"{addr}:{fab}"
            for ln in fs.get("SMB", []):
                parts = ln.split()
                if len(parts) == 2 and parts[1].rstrip("/") == fab:
                    share["smb"] = parts[0].strip("[]")
                    share["unc"] = f"\\\\{addr}\\{share['smb']}"
    return {"dataset": ds, "mount": mp, "used": used, "avail": avail, "ratio": ratio,
            "models": int(mc) if mc.isdigit() else 0, "consumers": consumers,
            "writer": writer, "share": share, "checked_at": now_iso()}


@capability(
    "pxstore.store.writer.provision",
    http_method="POST", http_path="/pxstore/store/writer/provision",
    http_tags=["pxstore"],
    description="Install or update the model store's ONE writer on the file "
                "fabric (VFS-02): the exact ollama binary the inference nodes "
                "run, copied from a source container so manifests stay "
                "compatible, as ollama-store-writer.service bound to "
                "127.0.0.1:11436 with OLLAMA_NOPRUNE=1 and a guard that refuses "
                "to start unless the store is mounted. Idempotent; refuses to "
                "restart the writer while a pull is running. Nothing is exposed "
                "on the network and no serving node needs a writable mount. "
                "Inputs: cluster_id (str!), node (str!), source_vmid (int! — a "
                "running CT with the fleet's ollama), fabric_vmid (int=160). "
                "Output: {ok, version, models, changed, host} or {error}.",
)
async def cap_store_writer_provision(cluster_id: str = "", node: str = "",
                                     source_vmid: int = 0, fabric_vmid: int = 160,
                                     trace_id=None) -> Dict:
    if not (cluster_id and node and source_vmid):
        return {"error": "cluster_id, node and source_vmid required"}
    cfg = await _cfg_get(cluster_id)
    mp = cfg.get("store_mount", "")
    if not mp:
        return {"error": "central store not provisioned — run pxstore.store.provision first"}
    try:
        fab = _fabric_path(mp)
        src, dst = int(source_vmid), int(fabric_vmid)
    except (ValueError, TypeError) as e:
        return {"error": str(e)}
    tmp = f"/root/.vera-ollama-{src}.bin"
    copy = "\n".join([
        "set -e",
        f"pct exec {src} -- test -x /usr/local/bin/ollama",
        f"pct pull {src} /usr/local/bin/ollama {tmp}",
        f"pct push {dst} {tmp} /usr/local/bin/.ollama.vera-new --perms 755",
        f"rm -f {tmp}",
        "echo COPY_OK",
    ])
    await emit_event({"type": "pxstore.progress", "stage": "store.writer",
                      "message": f"copying ollama from CT {src} to the file fabric"})
    r = await _node_ssh(cluster_id, node, _sh(copy), timeout=300)
    if r.get("error") or "COPY_OK" not in r.get("stdout", ""):
        return {"error": "copying the ollama binary failed: "
                         + (r.get("error") or (r.get("stderr") or r.get("stdout") or "")[:400])}
    U, P = _WRITER_UNIT, _WRITER_PORT
    unit = _writer_unit(store_mount=fab, models_dir=fab + "/models/ollama")
    install = "\n".join([
        "set -e",
        "NEW=/usr/local/bin/.ollama.vera-new; changed=0",
        "if [ -f $NEW ]; then",
        "  if cmp -s $NEW /usr/local/bin/ollama; then rm -f $NEW; else changed=1; fi",
        "fi",
        "cat > /tmp/vera-writer.unit <<'VERAUNIT'",
        unit.rstrip("\n"),
        "VERAUNIT",
        f"cmp -s /tmp/vera-writer.unit /etc/systemd/system/{U} || changed=1",
        "if [ \"$changed\" = 1 ] && systemctl list-units --state=active --no-legend "
        "'vera-store-pull-*' | grep -q .; then",
        "  rm -f /tmp/vera-writer.unit $NEW; echo PULL_ACTIVE; exit 7",
        "fi",
        "if [ -f $NEW ]; then mv -f $NEW /usr/local/bin/ollama; fi",
        f"mv -f /tmp/vera-writer.unit /etc/systemd/system/{U}",
        "systemctl daemon-reload",
        f"systemctl enable {U} >/dev/null 2>&1",
        f"if [ \"$changed\" = 1 ]; then systemctl restart {U}; else systemctl start {U}; fi",
        "ok=0",
        f"for i in $(seq 1 30); do curl -sf -m 2 http://127.0.0.1:{P}/api/version "
        ">/dev/null && ok=1 && break; sleep 1; done",
        f"if [ \"$ok\" != 1 ]; then echo WRITER_NOT_UP; journalctl -u {U} -n 5 --no-pager; exit 8; fi",
        f"echo \"version=$(curl -s -m 3 http://127.0.0.1:{P}/api/version)\"",
        f"echo \"models=$(curl -s -m 10 http://127.0.0.1:{P}/api/tags | grep -o '\"name\":' | wc -l)\"",
        "echo \"changed=$changed\"",
        "echo INSTALL_OK",
    ])
    r2 = await _fabric_ssh(cfg, _sh(install), timeout=120)
    out = r2.get("stdout", "")
    if "PULL_ACTIVE" in out:
        return {"error": "a store pull is running on the writer — updating it now "
                         "would abort that download; try again when it finishes"}
    if "INSTALL_OK" not in out:
        return {"error": r2.get("error") or (r2.get("stderr") or out)[-600:]
                         or "writer install failed"}
    kv = {k: v for k, _, v in (ln.partition("=") for ln in out.splitlines())
          if k in ("version", "models", "changed")}
    try:
        version = json.loads(kv.get("version") or "{}").get("version", "")
    except ValueError:
        version = ""
    models = (kv.get("models") or "").strip()
    await emit_event({"type": "pxstore.progress", "stage": "store.writer",
                      "message": f"store writer ready on {r2.get('fabric_addr')} "
                                 f"(ollama {version})"})
    return {"ok": True, "version": version, "models": int(models) if models.isdigit() else 0,
            "changed": kv.get("changed", "").strip() == "1",
            "host": r2.get("fabric_addr", "")}


# ═════════════════════════════════════════════════════════════════════════════
#  VERA DATA OFFLOAD  (dataset + NFS export + migration plan)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.veradata.provision",
    http_method="POST", http_path="/pxstore/veradata/provision", http_tags=["pxstore"],
    description="Create a dedicated ZFS dataset for Vera's databases on the "
                "node and export it over NFS to the Vera VM, so Vera's data "
                "lives on the big pool instead of filling the VM's disk. "
                "Inputs: cluster_id (str!), node (str!), dataset "
                "(str='rpool/data/vera-data'), client (str! — Vera VM IP or "
                "CIDR allowed to mount), quota (str). Output: {ok, mountpoint, "
                "mount_cmd — run this inside the Vera VM}.",
)
async def cap_veradata_provision(cluster_id: str = "", node: str = "",
                                 dataset: str = "rpool/data/vera-data",
                                 client: str = "", quota: str = "",
                                 trace_id=None) -> Dict:
    if not client:
        return {"error": "client (Vera VM IP or CIDR) required"}
    if not re.match(r"^[0-9./]+$", client):
        return {"error": "client must be an IP or CIDR"}
    r = await cap_zfs_create(cluster_id=cluster_id, node=node, dataset=dataset,
                             compression="zstd", quota=quota)
    if r.get("error"):
        return r
    mp = r["mountpoint"]
    export = f"{mp} {client}(rw,sync,no_subtree_check,no_root_squash)"
    script = f"""
set -e
export DEBIAN_FRONTEND=noninteractive
command -v exportfs >/dev/null 2>&1 || (apt-get -qq update && apt-get -qq -y install nfs-kernel-server)
grep -qF {shlex.quote(export)} /etc/exports || echo {shlex.quote(export)} >> /etc/exports
exportfs -ra
systemctl enable --now nfs-server >/dev/null 2>&1 || true
echo NFS_OK
"""
    rr = await _node_ssh(cluster_id, node, _sh(script), timeout=180)
    if rr.get("rc") != 0 or "NFS_OK" not in rr.get("stdout", ""):
        return {"error": (rr.get("error") or rr.get("stderr", ""))[:600] or "NFS setup failed"}
    cfg = await _cfg_get(cluster_id)
    cfg["veradata_dataset"], cfg["veradata_mount"] = dataset, mp
    await _cfg_put(cluster_id, cfg)
    host_hint = "<node-ip>"
    hid = (cfg.get("node_hosts") or {}).get(node, "")
    listc = _rawcap("exec.ssh.hosts.list")
    if hid and listc:
        try:
            res = await listc()
            hosts = res.get("hosts") if isinstance(res, dict) else res
            host_hint = next((h.get("host") for h in (hosts or [])
                              if h.get("id") == hid and h.get("host")), host_hint)
        except Exception as e:
            log.debug("node address lookup failed: %s", e)
    mount_cmd = (f"mkdir -p /mnt/vera-data && "
                 f"echo '{host_hint}:{mp} /mnt/vera-data nfs "
                 f"rw,hard,intr,vers=4 0 0' >> /etc/fstab && mount -a")
    await emit_event({"type": "pxstore.progress", "stage": "veradata.provision",
                      "message": f"vera-data dataset exported: {mp} → {client}"})
    return {"ok": True, "dataset": dataset, "mountpoint": mp,
            "export": export, "mount_cmd": mount_cmd}


_DATA_CANDIDATES = ("data", "db", "storage", "chroma", "neo4j", "redis",
                    "sqlite", "fabric", "models", "artifacts", "uploads")


@capability(
    "pxstore.veradata.plan",
    http_method="POST", http_path="/pxstore/veradata/plan", http_tags=["pxstore"],
    memory="off",
    description="Migration plan for moving Vera's databases off the VM disk: "
                "measures Vera's local data dirs + free space, and generates a "
                "stop-copy-symlink migration script targeting the NFS mount "
                "(from pxstore.veradata.provision) plus an optional docker-"
                "compose for a dedicated postgres/redis data stack. Inputs: "
                "base_dir (str — default: Vera's working dir), mount_point "
                "(str='/mnt/vera-data'). Output: {dirs:[{path,bytes}], disk, "
                "migrate_script, compose}.",
)
async def cap_veradata_plan(base_dir: str = "", mount_point: str = "/mnt/vera-data",
                            trace_id=None) -> Dict:
    import os
    import shutil
    base = Path(base_dir) if base_dir else Path.cwd()
    dirs = []
    try:
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if child.name.lower() not in _DATA_CANDIDATES and \
                    not any(k in child.name.lower() for k in ("data", "db", "store")):
                continue
            total = 0
            for root, _ds, files in os.walk(child, followlinks=False):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass
            dirs.append({"path": str(child), "bytes": total})
    except Exception as e:
        return {"error": f"could not scan {base}: {e}"}
    dirs.sort(key=lambda d: -d["bytes"])
    try:
        du = shutil.disk_usage(str(base))
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except Exception:
        disk = {}
    move_lines = "\n".join(
        f'move_dir "{d["path"]}"' for d in dirs if d["bytes"] > 0)
    script = f"""#!/usr/bin/env bash
# Vera data migration — stop Vera first, then run as root inside the Vera VM.
# Copies each data dir to the NFS mount and replaces it with a symlink.
set -euo pipefail
MNT={mount_point}
mountpoint -q "$MNT" || {{ echo "NFS not mounted at $MNT — run the mount_cmd from pxstore.veradata.provision first"; exit 1; }}
move_dir() {{
  local src="$1"; local name; name=$(basename "$src")
  [ -L "$src" ] && {{ echo "skip (already a link): $src"; return; }}
  [ -d "$src" ] || {{ echo "skip (missing): $src"; return; }}
  echo "==> $src → $MNT/$name"
  rsync -a "$src/" "$MNT/$name/"
  mv "$src" "$src.migrated"
  ln -s "$MNT/$name" "$src"
}}
{move_lines}
echo "Done. Start Vera, verify, then rm -rf the *.migrated dirs to free space."
"""
    compose = """# Optional: dedicated Vera data stack (run on a docker-enabled CT/VM
# whose disk lives on the big pool, or bind /vera-data into it).
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    environment: [POSTGRES_PASSWORD=change-me, POSTGRES_DB=vera]
    volumes: ["./pg:/var/lib/postgresql/data"]
    ports: ["5432:5432"]
  redis:
    image: redis:7
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes: ["./redis:/data"]
    ports: ["6379:6379"]
  neo4j:
    image: neo4j:5
    restart: unless-stopped
    environment: [NEO4J_AUTH=neo4j/change-me]
    volumes: ["./neo4j:/data"]
    ports: ["7474:7474", "7687:7687"]
"""
    return {"dirs": dirs, "disk": disk, "mount_point": mount_point,
            "migrate_script": script, "compose": compose,
            "note": "Path A (simplest): NFS mount + this script — data moves to "
                    "the pool, Vera config unchanged. Path B: run the compose "
                    "on a dedicated CT and point Vera's REDIS/PG/NEO4J URLs at "
                    "it — better isolation, needs .env changes."}


# ═════════════════════════════════════════════════════════════════════════════
#  VSCODE INTEGRATION
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.vscode.targets",
    http_method="POST", http_path="/pxstore/vscode/targets", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Everything VSCode needs to open cluster guests: per-guest "
                "Remote-SSH config entries (from enrolled SSH creds), "
                "vscode-remote:// folder URIs, and SMB paths via the vera-fs "
                "share for guests without SSH. Inputs: cluster_id (str!), node "
                "(str). Output: {targets:[{name,vmid,type,via,uri,smb}], "
                "ssh_config — paste into ~/.ssh/config}.",
)
async def cap_vscode_targets(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    rec = await _cluster(cluster_id)
    if not rec:
        return {"error": "cluster not found"}
    cfg = await _cfg_get(cluster_id)
    res, err = await _pve(rec, "GET", "/cluster/resources")
    if res is None:
        return {"error": err}
    ex = sys.modules.get("exec_capabilities")
    hosts = {}
    if ex:
        try:
            hosts = await ex._load_hosts()
        except Exception:
            hosts = {}
    targets, blocks = [], []
    for g in sorted((x for x in res if x.get("type") in ("qemu", "lxc")),
                    key=lambda x: (x.get("name") or "").lower()):
        if node and g.get("node") != node:
            continue
        vmid = int(g.get("vmid", 0))
        name = _safe(g.get("name", ""), vmid)
        gnode = g.get("node", "")
        hrec = next((h for h in hosts.values()
                     if re.match(rf"^pve:{vmid}@", h.get("label", ""))), None)
        smb = f"\\\\{gnode}\\{cfg['smb_share']}\\{name}"
        if hrec:
            alias = f"vera-{name}"
            blocks.append(
                f"Host {alias}\n  HostName {hrec.get('host','')}\n"
                f"  User {hrec.get('user','root')}\n"
                f"  Port {hrec.get('port',22)}\n")
            targets.append({
                "name": name, "vmid": vmid, "type": g.get("type"),
                "node": gnode, "via": "ssh",
                "host": hrec.get("host", ""),
                "uri": f"vscode://vscode-remote/ssh-remote+{alias}/root",
                "smb": smb})
        else:
            targets.append({"name": name, "vmid": vmid, "type": g.get("type"),
                            "node": gnode, "via": "smb", "uri": "", "smb": smb})
    return {"targets": targets, "ssh_config": "\n".join(blocks),
            "note": "SSH targets: paste ssh_config into ~/.ssh/config, then the "
                    "vscode-remote URIs open the guest directly (full terminal "
                    "+ extensions). SMB targets: open the UNC path once the "
                    "file server is provisioned+synced (browse/edit only)."}


# ═════════════════════════════════════════════════════════════════════════════
#  LLM BACKEND SWITCHING  (ollama ↔ vLLM on the same CT)
# ═════════════════════════════════════════════════════════════════════════════
def _pct_exec(vmid: int, inner: str) -> str:
    """Wrap a script to run INSIDE an LXC container via the node's pct."""
    return f"pct exec {int(vmid)} -- bash -c {shlex.quote(inner)}"


@capability(
    "pxstore.backend.status",
    http_method="POST", http_path="/pxstore/backend/status", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Which LLM backend each container is running: checks the ollama "
                "and vera-vllm systemd units inside each LXC (via pct exec on "
                "the node) and cross-references Vera's ollama/vllm instance "
                "registries. Inputs: cluster_id (str!), node (str!), vmids "
                "(list — blank = LXC guests whose name contains 'ollama'). "
                "Output: {guests:[{vmid,name,ollama,vllm,vllm_provisioned,"
                "registry}]}.",
)
async def cap_backend_status(cluster_id: str = "", node: str = "",
                             vmids: List[int] = None, trace_id=None) -> Dict:
    inv = await cap_inventory(cluster_id=cluster_id, node=node)
    if inv.get("error"):
        return {"error": inv["error"]}
    targets = [g for g in inv["guests"] if g["type"] == "lxc"
               and (int(g["vmid"]) in [int(v) for v in vmids] if vmids
                    else "ollama" in g["name"].lower())]
    if not targets:
        return {"guests": [], "note": "no matching LXC guests"}
    inner = ("o=$(systemctl is-active ollama 2>/dev/null||echo none); "
             "v=$(systemctl is-active vera-vllm 2>/dev/null||echo none); "
             "p=no; [ -f /etc/systemd/system/vera-vllm.service ] && p=yes; "
             "echo \"BK|$o|$v|$p\"")
    out = []
    inst_by_url: Dict[str, str] = {}
    for iid, i in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).items():
        inst_by_url[iid] = i.get("url", "")
    for g in targets:
        r = await _node_ssh(cluster_id, node, _sh(_pct_exec(g["vmid"], inner)),
                            timeout=30)
        m = re.search(r"BK\|(\S+)\|(\S+)\|(\S+)", r.get("stdout", ""))
        reg = [iid for iid, u in inst_by_url.items() if g["name"] in u or
               (getattr(_orch, "OLLAMA_INSTANCES", {}).get(iid, {})
                .get("label", "") or "").lower() == g["name"].lower()]
        out.append({"vmid": g["vmid"], "name": g["name"],
                    "status": g["status"],
                    "ollama": m.group(1) if m else "?",
                    "vllm": m.group(2) if m else "?",
                    "vllm_provisioned": (m.group(3) == "yes") if m else False,
                    "registry": reg,
                    "error": "" if m else (r.get("error") or
                                           r.get("stderr", ""))[:200]})
    return {"guests": out}


@capability(
    "pxstore.backend.provision_vllm",
    http_method="POST", http_path="/pxstore/backend/provision_vllm",
    http_tags=["pxstore"],
    description="Install vLLM inside an LXC container (venv at /opt/vera-vllm + "
                "a 'vera-vllm' systemd unit, NOT started) so the CT can be "
                "switched between ollama and vLLM. NOTE: the stock 'vllm' wheel "
                "needs CUDA — for the CPU nodes pass pip_spec pointing at a CPU "
                "build. Point hf_home at the central store mount to share "
                "weights. This downloads torch — expect several GB / minutes. "
                "Inputs: cluster_id (str!), node (str!), vmid (int!), model "
                "(str! — HF id, e.g. 'Qwen/Qwen2.5-7B-Instruct'), port "
                "(int=8000), hf_home (str='/models/hf'), pip_spec (str='vllm'), "
                "extra_args (str — appended to `vllm serve`). Output: {ok, log}.",
)
async def cap_backend_provision_vllm(cluster_id: str = "", node: str = "",
                                     vmid: int = 0, model: str = "",
                                     port: int = 8000,
                                     hf_home: str = "/models/hf",
                                     pip_spec: str = "vllm",
                                     extra_args: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node and vmid and model):
        return {"error": "cluster_id, node, vmid, model required"}
    if not re.match(r"^[A-Za-z0-9._/-]+$", model):
        return {"error": "invalid model id"}
    await emit_event({"type": "pxstore.progress", "stage": "backend.provision",
                      "message": f"installing vLLM in CT {vmid} (this can take "
                                 "several minutes — torch download)"})
    unit = f"""[Unit]
Description=vLLM OpenAI server (Vera-managed)
After=network.target

[Service]
Environment=HF_HOME={hf_home}
ExecStart=/opt/vera-vllm/bin/vllm serve {model} --host 0.0.0.0 --port {int(port)} {extra_args}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    inner = f"""set -e
export DEBIAN_FRONTEND=noninteractive
command -v python3 >/dev/null 2>&1 || (apt-get -qq update && apt-get -qq -y install python3)
python3 -m venv -h >/dev/null 2>&1 || apt-get -qq -y install python3-venv
[ -d /opt/vera-vllm ] || python3 -m venv /opt/vera-vllm
/opt/vera-vllm/bin/pip install -q -U {shlex.quote(pip_spec)}
mkdir -p {shlex.quote(hf_home)}
cat > /etc/systemd/system/vera-vllm.service <<'VLLMUNIT'
{unit}
VLLMUNIT
systemctl daemon-reload
echo VLLM_PROVISION_OK"""
    r = await _node_ssh(cluster_id, node, _sh(_pct_exec(vmid, inner)),
                        timeout=3600)
    if r.get("rc") != 0 or "VLLM_PROVISION_OK" not in r.get("stdout", ""):
        return {"error": (r.get("error") or r.get("stderr", ""))[:800]
                         or "provision failed",
                "log": r.get("stdout", "")[-1500:]}
    return {"ok": True, "vmid": int(vmid), "model": model, "port": int(port),
            "log": r.get("stdout", "")[-800:],
            "note": "unit installed but not started — use pxstore.backend.switch"}


@capability(
    "pxstore.backend.switch",
    http_method="POST", http_path="/pxstore/backend/switch", http_tags=["pxstore"],
    description="Switch an LXC node between LLM backends: stops one service, "
                "starts the other (inside the CT via pct exec), then updates "
                "Vera's registries — ollama instance disabled + vLLM instance "
                "registered (or the reverse). Inputs: cluster_id (str!), node "
                "(str!), vmid (int!), backend ('vllm'|'ollama'), "
                "ollama_instance_id (str — the OLLAMA_INSTANCES id for this CT, "
                "e.g. 'cpu-246'; blank = registry untouched), vllm_port "
                "(int=8000), vllm_url (str — override auto ip:port). "
                "Output: {ok, service, registry} or {error}.",
    schema={"properties": {"backend": {"enum": ["vllm", "ollama"]}}},
)
async def cap_backend_switch(cluster_id: str = "", node: str = "", vmid: int = 0,
                             backend: str = "vllm", ollama_instance_id: str = "",
                             vllm_port: int = 8000, vllm_url: str = "",
                             trace_id=None) -> Dict:
    if backend not in ("vllm", "ollama"):
        return {"error": "backend must be 'vllm' or 'ollama'"}
    if not (cluster_id and node and vmid):
        return {"error": "cluster_id, node, vmid required"}
    if backend == "vllm":
        inner = ("systemctl stop ollama 2>/dev/null || true; "
                 "systemctl enable --now vera-vllm && "
                 "sleep 2 && systemctl is-active vera-vllm")
    else:
        inner = ("systemctl stop vera-vllm 2>/dev/null || true; "
                 "systemctl disable vera-vllm 2>/dev/null || true; "
                 "systemctl enable --now ollama && "
                 "sleep 2 && systemctl is-active ollama")
    r = await _node_ssh(cluster_id, node, _sh(_pct_exec(vmid, inner)), timeout=90)
    active = r.get("stdout", "").strip().splitlines()[-1:] or [""]
    if r.get("rc") != 0 or active[0] != "active":
        return {"error": f"service did not come up ({active[0] or 'unknown'}): "
                         + (r.get("error") or r.get("stderr", ""))[:400]}

    registry: Dict[str, Any] = {}
    insts = getattr(_orch, "OLLAMA_INSTANCES", {}) or {}
    vadd = _rawcap("vllm.instances.add")
    vdel = _rawcap("vllm.instances.remove")
    vid = f"vllm-{ollama_instance_id or vmid}"
    if backend == "vllm":
        if ollama_instance_id and ollama_instance_id in insts:
            insts[ollama_instance_id]["enabled"] = False
            registry["ollama_disabled"] = ollama_instance_id
        if not vllm_url:
            rec = await _cluster(cluster_id)
            pm = _pmx()
            ip = await pm._guest_ip(rec, node, "lxc", int(vmid)) if (rec and pm) else ""
            vllm_url = f"http://{ip}:{int(vllm_port)}" if ip else ""
        if vadd and vllm_url:
            vr = await vadd(id=vid, url=vllm_url, label=f"vllm@{vmid}")
            registry["vllm_added"] = {"id": vid, "url": vllm_url,
                                      "ok": bool(vr.get("ok")),
                                      "error": vr.get("error", "")}
        elif not vllm_url:
            registry["vllm_added"] = {"error": "could not resolve guest IP — "
                                               "pass vllm_url explicitly"}
    else:
        if ollama_instance_id and ollama_instance_id in insts:
            insts[ollama_instance_id]["enabled"] = True
            registry["ollama_enabled"] = ollama_instance_id
        if vdel:
            vr = await vdel(instance_id=vid)
            registry["vllm_removed"] = {"id": vid, "ok": bool(vr.get("ok"))}
    await emit_event({"type": "pxstore.backend.switched", "vmid": int(vmid),
                      "backend": backend, "registry": registry})
    return {"ok": True, "backend": backend, "service": "active",
            "registry": registry,
            "note": "OLLAMA_INSTANCES enable/disable is in-process; persisted "
                    "routing profiles may re-enable it on restart"}


# (The idle-worker policy — spawning Vera workers on idle ollama nodes — was
#  removed: pxstore.worker.status/tick and the 60s watcher are gone.)


# ═════════════════════════════════════════════════════════════════════════════
#  MODEL PULL ROUTER  (+ store export for hosts that can't share the drive)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.models.pull",
    http_method="POST", http_path="/pxstore/models/pull", http_tags=["pxstore"],
    description="Pull an Ollama model the storage-aware way. via='store' (the "
                "default once the store is provisioned): the download runs on "
                "the store's one writer on the file fabric (VFS-02) as a "
                "detached job, lands in the shared store, and every read-only "
                "node lists it at once with no restart; poll "
                "pxstore.models.pull.status. via='direct': a plain per-instance "
                "pull, only for an instance that does NOT serve the shared store "
                "(a read-only instance refuses it). Inputs: model (str!), "
                "cluster_id (str — required for store), instance_id (str — "
                "required for direct), via ('auto'|'store'|'direct'). Output: "
                "{ok, via, routed_to, state, already_running} or {error, hint}.",
    schema={"properties": {"via": {"enum": ["auto", "store", "direct"]}}},
)
async def cap_models_pull(model: str = "", cluster_id: str = "",
                          instance_id: str = "", via: str = "auto",
                          trace_id=None) -> Dict:
    if not model:
        return {"error": "model required"}
    if not _valid_model(model):
        return {"error": f"not a valid Ollama model reference: {model!r}"}
    cfg = await _cfg_get(cluster_id) if cluster_id else {}
    if via == "auto":
        via = "store" if cfg.get("store_mount") else "direct"
    if via == "store":
        if not cluster_id:
            return {"error": "cluster_id required for a store pull"}
        if not cfg.get("store_mount"):
            return {"error": "central store not provisioned — run pxstore.store.provision first"}
        r = await _fabric_ssh(cfg, _sh(_pull_start_script(model)), timeout=45)
        out, host = r.get("stdout", ""), r.get("fabric_addr", "")
        if "WRITER_DOWN" in out:
            return {"error": "the store writer is not running on the file fabric",
                    "hint": "install it with pxstore.store.writer.provision",
                    "via": via, "routed_to": host}
        if "STARTED" not in out and "ALREADY_RUNNING" not in out:
            return {"error": r.get("error") or (r.get("stderr") or out)[:400]
                             or "could not start the pull", "via": via, "routed_to": host}
        await emit_event({"type": "pxstore.progress", "stage": "models.pull",
                          "message": f"pulling {model} into the shared store (writer on {host})"})
        return {"ok": True, "via": via, "routed_to": f"store writer on {host}",
                "model": model, "state": "running",
                "already_running": "ALREADY_RUNNING" in out,
                "poll": "pxstore.models.pull.status"}
    if not instance_id:
        return {"error": "instance_id required for a direct pull"}
    pull = _rawcap("ollama.pull")
    if not pull:
        return {"error": "ollama.pull unavailable"}
    await emit_event({"type": "pxstore.progress", "stage": "models.pull",
                      "message": f"pulling {model} on {instance_id} (direct)"})
    res = await pull(model=model, instance_id=instance_id)
    if res.get("error"):
        err = str(res["error"])
        out = {"error": err, "routed_to": instance_id, "via": via}
        if _is_ro_error(err):
            out["hint"] = ("this instance serves the shared read-only store — pull "
                           "with via='store' and it appears here without a restart")
        return out
    return {"ok": True, "routed_to": instance_id, "via": via, "state": "done", **{
        k: v for k, v in res.items() if k in ("model", "status")}}


@capability(
    "pxstore.models.pull.status",
    http_method="POST", http_path="/pxstore/models/pull/status",
    http_tags=["pxstore"],
    memory="off", silent=True,
    description="Progress of a store pull started by pxstore.models.pull "
                "(via=store), read from the writer's own download stream. "
                "Inputs: model (str!), cluster_id (str!). Output: {model, state "
                "('running'|'done'|'failed'|'unknown'), status, completed, "
                "total, percent, error}.",
)
async def cap_models_pull_status(model: str = "", cluster_id: str = "",
                                 trace_id=None) -> Dict:
    if not (model and cluster_id):
        return {"error": "model and cluster_id required"}
    if not _valid_model(model):
        return {"error": f"not a valid Ollama model reference: {model!r}"}
    cfg = await _cfg_get(cluster_id)
    r = await _fabric_ssh(cfg, _sh(_pull_status_script(model)), timeout=30)
    if r.get("error"):
        return {"error": r["error"]}
    head, _, log_txt = r.get("stdout", "").partition("###LOG")
    active = "state=active" in head or "state=activating" in head
    return {"model": model, **_parse_pull_log(log_txt.splitlines(), unit_active=active)}


@capability(
    "pxstore.store.export",
    http_method="POST", http_path="/pxstore/store/export", http_tags=["pxstore"],
    description="Share the central model store with a machine that cannot bind "
                "the drive locally. The file fabric (VFS-02) already exports it "
                "read-only over NFS 4.2, so this confirms the client is covered "
                "and returns the mount line; a client outside the existing "
                "ranges is added to that read-only export. Nothing is installed "
                "on the hypervisor, and the store is never exported writable "
                "(it has one writer). Inputs: cluster_id (str!), client (str! — "
                "IP or CIDR), node (str — unused), rw (bool — refused). Output: "
                "{ok, already_covered, server, path, export, fstab} or {error}.",
)
async def cap_store_export(cluster_id: str = "", node: str = "",
                           client: str = "", rw: bool = False,
                           trace_id=None) -> Dict:
    if not _valid_client(client):
        return {"error": "client must be an IP or CIDR"}
    if rw:
        return {"error": "the shared store is never exported writable — it has one "
                         "writer (pxstore.models.pull via='store'), and a writable "
                         "client could prune every node's models"}
    cfg = await _cfg_get(cluster_id)
    mp = cfg.get("store_mount", "")
    if not mp:
        return {"error": "central store not provisioned — run pxstore.store.provision first"}
    try:
        fab = _fabric_path(mp)
    except ValueError as e:
        return {"error": f"the file fabric cannot see the store: {e}"}
    r = await _fabric_ssh(cfg, "cat /etc/exports", timeout=30)
    if r.get("error"):
        return {"error": r["error"]}
    server = r.get("fabric_addr") or _FABRIC_HOST
    fstab = _fstab_line(server, fab, "/vera-store")
    hit = _export_for(_parse_exports(r.get("stdout", "")), fab, client)
    if hit:
        return {"ok": True, "already_covered": True, "server": server, "path": fab,
                "export": f"{fab} {hit['client']}({hit['opts']})", "fstab": fstab,
                "note": "mount it with pxstore.store.attach_remote"}
    try:
        script = _add_export_client(fab, client)
    except ValueError as e:
        return {"error": str(e)}
    rr = await _fabric_ssh(cfg, _sh(script), timeout=60)
    if "EXPORT_OK" not in rr.get("stdout", ""):
        return {"error": rr.get("error") or (rr.get("stderr") or rr.get("stdout") or "")[:400]
                         or "export failed"}
    await emit_event({"type": "pxstore.progress", "stage": "store.export",
                      "message": f"model store export on {server} now admits {client} (read-only)"})
    return {"ok": True, "already_covered": False, "server": server, "path": fab,
            "export": f"{fab} {client} (read-only)", "fstab": fstab,
            "note": "mount it with pxstore.store.attach_remote"}


@capability(
    "pxstore.store.attach_remote",
    http_method="POST", http_path="/pxstore/store/attach_remote",
    http_tags=["pxstore"],
    description="Mount the central model store READ-ONLY on another machine (an "
                "OS+docker box or a VM — anything with an enrolled SSH cred) "
                "from the file fabric's NFS export: installs the NFS client, adds "
                "a boot-safe fstab entry (_netdev,nofail), mounts. Then bind it "
                "into containers read-only with pruning off, e.g. -v "
                "/vera-store/models/ollama:/root/.ollama/models:ro -e "
                "OLLAMA_NOPRUNE=1. Inputs: host_id (str! — exec.ssh host), "
                "cluster_id (str — for the defaults), server (str — default the "
                "file fabric), remote_path (str — default the store's path on "
                "the fabric), local_path (str='/vera-store'), ro (bool=true). "
                "Output: {ok, mounted_at, fstab, hint} or {error}.",
)
async def cap_store_attach_remote(host_id: str = "", server: str = "",
                                  remote_path: str = "", cluster_id: str = "",
                                  local_path: str = "/vera-store",
                                  ro: bool = True, trace_id=None) -> Dict:
    if not host_id:
        return {"error": "host_id required"}
    if not (remote_path and server) and not cluster_id:
        return {"error": "cluster_id required unless server and remote_path are given"}
    cfg = await _cfg_get(cluster_id) if cluster_id else {}
    if not remote_path:
        mp = cfg.get("store_mount", "")
        if not mp:
            return {"error": "central store not provisioned"}
        try:
            remote_path = _fabric_path(mp)
        except ValueError as e:
            return {"error": f"the file fabric cannot see the store: {e}"}
    if not server:
        _hid, server = await _fabric_host(cfg)
    try:
        fstab = _fstab_line(server, remote_path, local_path, ro=ro)
    except ValueError as e:
        return {"error": str(e)}
    run = _rawcap("exec.ssh.run")
    if not run:
        return {"error": "exec.ssh.run unavailable"}
    script = f"""
set -e
export DEBIAN_FRONTEND=noninteractive
command -v mount.nfs >/dev/null 2>&1 || (apt-get -qq update && apt-get -qq -y install nfs-common) || yum -y install nfs-utils
mkdir -p {shlex.quote(local_path)}
grep -qF {shlex.quote(fstab)} /etc/fstab || echo {shlex.quote(fstab)} >> /etc/fstab
mountpoint -q {shlex.quote(local_path)} || mount {shlex.quote(local_path)}
echo ATTACH_OK
"""
    r = await run(command=_sh(script), host_id=host_id, timeout=300)
    if r.get("rc") != 0 or "ATTACH_OK" not in r.get("stdout", ""):
        return {"error": (r.get("error") or r.get("stderr", ""))[:500]
                         or "mount failed"}
    return {"ok": True, "mounted_at": local_path, "fstab": fstab,
            "hint": f"bind it into containers read-only with pruning off: -v "
                    f"{local_path}/models/ollama:/root/.ollama/models:ro "
                    f"-e OLLAMA_NOPRUNE=1"}


# ═════════════════════════════════════════════════════════════════════════════
#  PHYSICAL DISKS + BACKUP SYSTEM STATUS  (read-only)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.disks",
    http_method="POST", http_path="/pxstore/disks", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Every physical disk on a Proxmox node and what it is used for: "
                "the ZFS pool it belongs to (imported, importable or damaged), an "
                "LVM volume group, a mounted filesystem, or nothing at all, so an "
                "unused disk shows up, and so does a USB-attached one. Read-only: "
                "importable pools are listed, never imported. Inputs: cluster_id "
                "(str!), node (str!). Output: {disks:[{name,size,model,transport,"
                "usb,rotational,role,state,detail,pool}], free:[names]} or {error}.",
)
async def cap_disks(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    r = await _node_ssh(cluster_id, node, _sh(_DISKS_SCRIPT), timeout=60)
    if r.get("error"):
        return {"error": r["error"]}
    s = _bk_sections(r.get("stdout", ""))
    disks = _classify_disks(s.get("LSBLK", ""), _bk_lines(s.get("IMPORTED", "")),
                            _parse_importable(s.get("IMPORTABLE", "")),
                            _parse_pvs(s.get("PVS", "")))
    return {"disks": disks, "free": [d["name"] for d in disks if d["state"] == "free"]}


@capability(
    "pxstore.backup.status",
    http_method="POST", http_path="/pxstore/backup/status", http_tags=["pxstore"],
    memory="off", silent=True,
    description="The backup system on a Proxmox node at a glance: backup jobs "
                "(and whether any is enabled), backup storages with usage, pool "
                "capacity, ZFS snapshot counts per pool, the snapshot disk-full "
                "guard and replication timers with their recent log lines, the "
                "latest result per guest, anything running now, and plain-language "
                "warnings (no enabled job, a job writing to the hypervisor's root "
                "disk, storage or a pool over 80%, a timer missing). Read-only. "
                "Inputs: cluster_id (str!), node (str!). Output: {jobs, storages, "
                "pools, datasets, snapshots, timers, guard, replication, runs, "
                "running, warnings} or {error}.",
)
async def cap_backup_status(cluster_id: str = "", node: str = "", trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    r = await _node_ssh(cluster_id, node, _sh(_BACKUP_SCRIPT), timeout=60)
    if r.get("error"):
        return {"error": r["error"]}
    s = _bk_sections(r.get("stdout", ""))
    jobs = _parse_jobs(s.get("JOBS", ""))
    storages = _parse_pvesm_status(s.get("STORAGE", ""))
    pools = _parse_bk_pools(s.get("POOLS", ""))
    timers = _parse_timers(s.get("TIMERS", ""))
    return {"jobs": jobs, "storages": storages, "pools": pools,
            "datasets": _parse_bk_datasets(s.get("DATASETS", "")),
            "snapshots": _parse_snap_counts(s.get("SNAPS", "")),
            "timers": timers,
            "guard": _parse_journal(s.get("GUARD", "")),
            "replication": _parse_journal(s.get("REPL", "")),
            "runs": _parse_vzdump(s.get("VZDUMP", "")),
            "running": _bk_lines(s.get("RUNNING", "")),
            "warnings": _backup_warnings(jobs, storages, pools, timers),
            "checked_at": now_iso()}


# ═════════════════════════════════════════════════════════════════════════════
#  BACKUP TARGET  (vzdump into the file fabric's backup share)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.backup.target",
    http_method="POST", http_path="/pxstore/backup/target", http_tags=["pxstore"],
    description="Give estate backups (nodes.backup, vzdump) a target inside the "
                "file fabric's backup dataset, so dumps land on the backup disk "
                "and show up read-only in VFS-02's backup share, instead of "
                "sharing the hypervisor's root disk like the stock 'local' and "
                "'bpool' storages. Registers a PVE dir storage with "
                "is_mountpoint, so PVE marks it offline rather than filling the "
                "root disk if the dataset is ever unmounted. Dry run by default; "
                "creating it backs nothing up. Inputs: cluster_id (str!), node "
                "(str!), storage_id (str='vfs-backup'), keep_last (int=3), "
                "confirm (bool=false). Output: {ok, dry_run, storage_id, path, "
                "fabric_path, keep_last, command|already_existed} or {error}.",
)
async def cap_backup_target(cluster_id: str = "", node: str = "",
                            storage_id: str = "vfs-backup", keep_last: int = 3,
                            confirm: bool = False, trace_id=None) -> Dict:
    if not (cluster_id and node):
        return {"error": "cluster_id and node required"}
    cfg = await _cfg_get(cluster_id)
    mount = cfg.get("backup_dataset_mount") or "/tank_sde/vfs/backup"
    try:
        script = _backup_target_script(storage_id, mount, "pve", keep_last)
        fab = _fabric_path(mount + "/pve")
    except (ValueError, TypeError) as e:
        return {"error": str(e)}
    info = {"storage_id": storage_id, "path": f"{mount}/pve", "fabric_path": fab,
            "keep_last": max(1, int(keep_last))}
    if not confirm:
        return {"ok": True, "dry_run": True, **info, "command": script}
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=60)
    out = r.get("stdout", "")
    if "NOT_MOUNTED" in out:
        return {"error": f"{mount} is not mounted on {node}", **info}
    if "TARGET_OK" not in out and "ALREADY_EXISTS" not in out:
        return {"error": r.get("error") or (r.get("stderr") or out)[:400] or "failed", **info}
    await emit_event({"type": "pxstore.progress", "stage": "backup.target",
                      "message": f"backup storage {storage_id} → {mount}/pve"})
    return {"ok": True, "dry_run": False, "already_existed": "ALREADY_EXISTS" in out, **info}


# ═════════════════════════════════════════════════════════════════════════════
#  NWM-01 NETWORK MONITOR  (all traffic flows through it — sample from there)
# ═════════════════════════════════════════════════════════════════════════════
async def _nwm_ssh(cluster_id: str, host_id: str, command: str,
                   timeout: int = 60) -> Dict:
    if not host_id and cluster_id:
        cfg = await _cfg_get(cluster_id)
        host_id = cfg.get("nwm_host_id", "")
    if not host_id:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": "no NWM host — set nwm_host_id in settings or pass "
                         "host_id (enrol NWM-01's SSH cred first)"}
    run = _rawcap("exec.ssh.run")
    if not run:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": "exec.ssh.run unavailable"}
    return await run(command=command, host_id=host_id, timeout=timeout)


_CT_RE = re.compile(
    r"^(?P<proto>\w+)\s+\d+\s+\d+\s+(?:(?P<state>[A-Z_]+)\s+)?"
    r"src=(?P<src>\S+)\s+dst=(?P<dst>\S+)\s+sport=(?P<sport>\d+)\s+dport=(?P<dport>\d+)"
    r"(?:.*?bytes=(?P<bytes>\d+))?")


@capability(
    "pxstore.nwm.flows",
    http_method="POST", http_path="/pxstore/nwm/flows", http_tags=["pxstore"],
    memory="off", silent=True,
    description="Live connection table from the NWM-01 monitor container (all "
                "traffic routes through it): conntrack flows aggregated into "
                "top flows + top talkers (falls back to ss when conntrack is "
                "missing — no byte counts then). Inputs: cluster_id (str — for "
                "the saved nwm_host_id), host_id (str — override), limit "
                "(int=40). Output: {tool, flows:[{src,dst,proto,dport,conns,"
                "bytes}], talkers:[{host,conns,bytes}]}.",
)
async def cap_nwm_flows(cluster_id: str = "", host_id: str = "",
                        limit: int = 40, trace_id=None) -> Dict:
    script = ("if command -v conntrack >/dev/null 2>&1; then echo '###CT'; "
              "conntrack -L -o extended 2>/dev/null | head -4000; "
              "else echo '###SS'; ss -tuna 2>/dev/null | tail -n +2 | head -4000; fi")
    r = await _nwm_ssh(cluster_id, host_id, _sh(script), timeout=45)
    if r.get("rc") != 0 and not r.get("stdout"):
        return {"error": r.get("error") or r.get("stderr", "")[:300]}
    lines = r.get("stdout", "").splitlines()
    tool = "conntrack" if lines and lines[0].strip() == "###CT" else "ss"
    flows: Dict[Tuple, Dict] = {}
    talkers: Dict[str, Dict] = {}

    def _bump(src, dst, proto, dport, nbytes):
        k = (src, dst, proto, dport)
        f = flows.setdefault(k, {"src": src, "dst": dst, "proto": proto,
                                 "dport": dport, "conns": 0, "bytes": 0})
        f["conns"] += 1
        f["bytes"] += nbytes
        for h in (src, dst):
            t = talkers.setdefault(h, {"host": h, "conns": 0, "bytes": 0})
            t["conns"] += 1
            t["bytes"] += nbytes

    for ln in lines[1:]:
        ln = ln.strip()
        if not ln:
            continue
        if tool == "conntrack":
            # kernel prefixes proto lines like "ipv4 2 tcp 6 ..." — normalise
            ln2 = re.sub(r"^ipv[46]\s+\d+\s+", "", ln)
            m = _CT_RE.match(ln2)
            if m:
                _bump(m.group("src"), m.group("dst"), m.group("proto"),
                      m.group("dport"), int(m.group("bytes") or 0))
        else:
            f = ln.split()
            if len(f) >= 6:
                proto = f[0]
                laddr, raddr = f[4], f[5]
                rip, _, rport = raddr.rpartition(":")
                lip = laddr.rpartition(":")[0]
                if rip and lip:
                    _bump(lip.strip("[]"), rip.strip("[]"), proto, rport, 0)

    fl = sorted(flows.values(), key=lambda x: (-x["bytes"], -x["conns"]))
    tk = sorted(talkers.values(), key=lambda x: (-x["bytes"], -x["conns"]))
    return {"tool": tool, "flows": fl[: max(1, int(limit))],
            "talkers": tk[:20], "total_flows": len(fl)}


@capability(
    "pxstore.nwm.capture",
    http_method="POST", http_path="/pxstore/nwm/capture", http_tags=["pxstore"],
    description="Timed tcpdump sample on NWM-01: capture N seconds of headers "
                "and aggregate packets/bytes per src→dst conversation — the "
                "'what is actually flowing right now' view. Inputs: cluster_id "
                "(str), host_id (str — override), seconds (int=10, max 60), "
                "iface (str='any'), filter (str — tcpdump BPF, e.g. 'not port "
                "22'). Output: {convs:[{src,dst,pkts,bytes}], pkts_seen}.",
)
async def cap_nwm_capture(cluster_id: str = "", host_id: str = "",
                          seconds: int = 10, iface: str = "any",
                          filter: str = "", trace_id=None) -> Dict:
    seconds = max(2, min(60, int(seconds or 10)))
    if not re.match(r"^[A-Za-z0-9@._-]+$", iface or "any"):
        return {"error": "invalid iface"}
    if filter and not re.match(r"^[A-Za-z0-9 ._:\[\]()!=<>-]+$", filter):
        return {"error": "invalid filter chars"}
    cmd = (f"timeout {seconds} tcpdump -i {shlex.quote(iface)} -nn -q -l "
           + (shlex.quote(filter) + " " if filter else "")
           + "2>/dev/null | head -8000; true")
    r = await _nwm_ssh(cluster_id, host_id, _sh(cmd), timeout=seconds + 30)
    out = r.get("stdout", "")
    if not out:
        return {"error": r.get("error") or "no packets captured (tcpdump "
                                           "installed on NWM-01?)"}
    convs: Dict[Tuple, Dict] = {}
    pkts = 0
    pat = re.compile(r"IP6?\s+(\S+?)\.(\d+|\w+)\s+>\s+(\S+?)\.(\d+|\w+):"
                     r".*?(?:length|len)\s+(\d+)", re.I)
    for ln in out.splitlines():
        m = pat.search(ln)
        if not m:
            continue
        pkts += 1
        src, dst, ln_b = m.group(1), m.group(3), int(m.group(5) or 0)
        k = (src, dst)
        c = convs.setdefault(k, {"src": src, "dst": dst, "pkts": 0, "bytes": 0})
        c["pkts"] += 1
        c["bytes"] += ln_b
    cv = sorted(convs.values(), key=lambda x: -x["bytes"])
    return {"convs": cv[:60], "pkts_seen": pkts, "seconds": seconds}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/pxstore/panel", include_in_schema=False)
async def _pxstore_panel():
    p = _HERE / "pxstore_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>pxstore_panel.html not found</p>")


@APP.get("/pxstore/netops-panel", include_in_schema=False)
async def _pxstore_netops_panel():
    # Network operations surface (Proxmox firewall + NWM-01 traffic). Embedded
    # as the Network → Traffic sub-tab of the workers/Ollama panel. Kept out of
    # the storage panel — networking lives with the network graph, not storage.
    p = _HERE / "netops_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>netops_panel.html not found</p>")


# Standalone top-level tab retired — the storage UI is now embedded as the
# "Storage" pane of the workers/Ollama panel (iframe → /pxstore/panel), the
# same way Proxmox and Docker live there. The /pxstore/panel route is kept.
# (Set VERA_PXSTORE_TAB=1 to restore the standalone tab for debugging.)
import os as _os
_register_ui = register_ui if _os.getenv("VERA_PXSTORE_TAB") else (lambda *a, **k: None)
_register_ui(
    "pxstore-panel",
    "Storage",
    "⛁",
    """<div id="pxstore-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/pxstore/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "pxstore.settings.get", "pxstore.settings.save", "pxstore.inventory",
        "pxstore.fs.provision", "pxstore.fs.sync", "pxstore.fs.status",
        "pxstore.disk.resize", "pxstore.zfs.set", "pxstore.zfs.create",
        "pxstore.cpu.topology", "pxstore.cpu.map", "pxstore.cpu.pin",
        "pxstore.cpu.suggest",
        "pxstore.store.provision", "pxstore.store.attach",
        "pxstore.store.consolidate", "pxstore.store.export",
        "pxstore.store.attach_remote", "pxstore.models.pull",
        "pxstore.backend.status", "pxstore.backend.provision_vllm",
        "pxstore.backend.switch",
        "pxstore.veradata.provision", "pxstore.veradata.plan",
        "pxstore.vscode.targets",
    ],
    mode="tab",
    tab_order=56,
)

log.info("pxstore_capabilities ready — proxmox storage fabric")
