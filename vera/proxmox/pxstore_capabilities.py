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
  File server   pxstore.fs.provision / .sync / .status
                One Samba share (\\\\node\\vera-fs) with a NAME-keyed symlink
                per guest: LXC subvol datasets are linked directly (they are
                always mounted on the host); host pools + non-ZFS drives are
                linked under _host/; running VMs are attached via sshfs when
                SSH creds are enrolled (VM block devices can't be mounted
                live, so the guest's own view is the only safe live view).
  Storage mgmt  pxstore.disk.resize            — grow a guest disk (PVE API;
                LXC filesystems grow automatically, VMs get in-guest steps),
                pxstore.zfs.set / .create      — quotas + datasets.
  CPU / NUMA    pxstore.cpu.topology / .map / .pin / .suggest
                lscpu topology, per-guest pinning (LXC cpuset / QEMU
                affinity), NUMA-span + reserved-range conflict detection,
                and a safe-cpuset allocator that never hands out the ollama
                ranges and never spans NUMA nodes.
  Model store   pxstore.store.provision / .attach / .consolidate
                Central ZFS dataset for models/images/artifacts, bind-mounted
                READ-ONLY into consumer CTs (mpN,ro=1) so ollama/ollama-a/
                ollama-b stop replicating model blobs; consolidate rsyncs the
                existing per-CT copies into the store first.
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
    "store_writer_instance": "",         # ollama instance id that PULLS into the shared store
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
                "e.g. the ollama containers' ranges), mount_vms (bool). "
                "Output: {ok, settings}.",
)
async def cap_settings_save(cluster_id: str = "", node_hosts: Dict = None,
                            share_root: str = "", smb_share: str = "",
                            store_dataset: str = "", veradata_dataset: str = "",
                            reserved_cpus: List[Dict] = None,
                            mount_vms: Optional[bool] = None,
                            store_writer_instance: Optional[str] = None,
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
#  FILE SERVER  (Samba on the node, one share, name-keyed symlinks)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "pxstore.fs.provision",
    http_method="POST", http_path="/pxstore/fs/provision", http_tags=["pxstore"],
    description="Provision (idempotently) a Samba file server on a Proxmox node "
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
    description="(Re)build the name-keyed share tree and Samba share on a node: "
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
    return {"running": running, "entries": entries,
            "share": {"unc": f"\\\\{node}\\{cfg['smb_share']}", "root": root}}


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
                "(str!), node (str!), dataset (str='rpool/data/vera-store'), "
                "quota (str — optional). Output: {ok, dataset, mountpoint}.",
)
async def cap_store_provision(cluster_id: str = "", node: str = "",
                              dataset: str = "rpool/data/vera-store",
                              quota: str = "", trace_id=None) -> Dict:
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
                "CT must be RESTARTED for the mount to appear. Inputs: "
                "cluster_id (str!), node (str!), vmid (int!), subdir "
                "(str='models/ollama'), ct_path (str='/root/.ollama/models' — "
                "where it appears inside the CT), ro (bool=true — set false for "
                "the ONE writer CT that pulls new models). Output: {ok, mp_key, "
                "host_path, restart_required:true}.",
)
async def cap_store_attach(cluster_id: str = "", node: str = "", vmid: int = 0,
                           subdir: str = "models/ollama",
                           ct_path: str = "/root/.ollama/models",
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
    val = f"{host_path},mp={ct_path}" + (",ro=1" if ro else "")
    _d, err = await _pve(rec, "PUT", f"/nodes/{node}/lxc/{vmid}/config",
                         {f"mp{idx}": val})
    if err:
        return {"error": err}
    await emit_event({"type": "pxstore.progress", "stage": "store.attach",
                      "message": f"attached {host_path} → CT {vmid}:{ct_path} "
                                 f"({'ro' if ro else 'rw'}) — restart CT to apply"})
    return {"ok": True, "mp_key": f"mp{idx}", "host_path": host_path,
            "ct_path": ct_path, "ro": ro, "restart_required": True}


@capability(
    "pxstore.store.consolidate",
    http_method="POST", http_path="/pxstore/store/consolidate", http_tags=["pxstore"],
    description="Rsync model data FROM one or more LXC containers' local dirs "
                "INTO the central store (host-side, via each CT's subvol — no "
                "guest downtime; identical blobs overwrite harmlessly). Run "
                "once per source CT before switching them to the read-only "
                "mount. Inputs: cluster_id (str!), node (str!), vmid (int!), "
                "src_path (str='/root/.ollama/models' — path INSIDE the CT), "
                "subdir (str='models/ollama'), delete_source (bool=false — "
                "after a successful copy, rename the CT-local dir to "
                "<dir>.pre-store to free the space; do this only after the ro "
                "mount is attached+verified). Output: {ok, stats, freed_hint}.",
)
async def cap_store_consolidate(cluster_id: str = "", node: str = "", vmid: int = 0,
                                src_path: str = "/root/.ollama/models",
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
rsync -a --info=stats2 {shlex.quote(src + '/')} {shlex.quote(dst + '/')}
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
    description="Pull an ollama model the storage-aware way. via='store' "
                "(default when a writer is configured): the pull runs on the "
                "designated WRITER instance only — the model lands in the "
                "central store and every read-only consumer sees it instantly. "
                "via='direct': plain per-instance pull (for instances that "
                "can't share the store, e.g. on another machine). Inputs: model "
                "(str!), cluster_id (str — for the writer setting), instance_id "
                "(str — required for direct, ignored for store), via "
                "('auto'|'store'|'direct'). Output: {ok, routed_to, via} or "
                "{error}.",
    schema={"properties": {"via": {"enum": ["auto", "store", "direct"]}}},
)
async def cap_models_pull(model: str = "", cluster_id: str = "",
                          instance_id: str = "", via: str = "auto",
                          trace_id=None) -> Dict:
    if not model:
        return {"error": "model required"}
    writer = ""
    if cluster_id:
        cfg = await _cfg_get(cluster_id)
        writer = cfg.get("store_writer_instance", "")
    if via == "auto":
        via = "store" if writer else "direct"
    if via == "store":
        if not writer:
            return {"error": "no store writer configured — set "
                             "store_writer_instance in settings, or use "
                             "via='direct'"}
        target = writer
    else:
        if not instance_id:
            return {"error": "instance_id required for a direct pull"}
        target = instance_id
    pull = _rawcap("ollama.pull")
    if not pull:
        return {"error": "ollama.pull unavailable"}
    await emit_event({"type": "pxstore.progress", "stage": "models.pull",
                      "message": f"pulling {model} on {target} (via {via})"})
    res = await pull(model=model, instance_id=target)
    if res.get("error"):
        return {"error": res["error"], "routed_to": target, "via": via}
    return {"ok": True, "routed_to": target, "via": via, **{
        k: v for k, v in res.items() if k in ("model", "status")}}


@capability(
    "pxstore.store.export",
    http_method="POST", http_path="/pxstore/store/export", http_tags=["pxstore"],
    description="NFS-export the central model store to hosts that can't share "
                "the drive locally (other machines / OS+docker stacks). "
                "Read-only by default. Inputs: cluster_id (str!), node (str!), "
                "client (str! — IP or CIDR), rw (bool=false). Output: {ok, "
                "export}.",
)
async def cap_store_export(cluster_id: str = "", node: str = "",
                           client: str = "", rw: bool = False,
                           trace_id=None) -> Dict:
    if not client or not re.match(r"^[0-9./]+$", client):
        return {"error": "client must be an IP or CIDR"}
    cfg = await _cfg_get(cluster_id)
    mp = cfg.get("store_mount", "")
    if not mp:
        return {"error": "central store not provisioned — run "
                         "pxstore.store.provision first"}
    opts = "rw,sync,no_subtree_check,no_root_squash" if rw \
        else "ro,sync,no_subtree_check,root_squash"
    export = f"{mp} {client}({opts})"
    script = f"""
set -e
export DEBIAN_FRONTEND=noninteractive
command -v exportfs >/dev/null 2>&1 || (apt-get -qq update && apt-get -qq -y install nfs-kernel-server)
grep -qF {shlex.quote(export)} /etc/exports || echo {shlex.quote(export)} >> /etc/exports
exportfs -ra
systemctl enable --now nfs-server >/dev/null 2>&1 || true
echo EXPORT_OK
"""
    r = await _node_ssh(cluster_id, node, _sh(script), timeout=180)
    if r.get("rc") != 0 or "EXPORT_OK" not in r.get("stdout", ""):
        return {"error": (r.get("error") or r.get("stderr", ""))[:400]
                         or "export failed"}
    return {"ok": True, "export": export,
            "note": "attach on remote hosts with pxstore.store.attach_remote"}


@capability(
    "pxstore.store.attach_remote",
    http_method="POST", http_path="/pxstore/store/attach_remote",
    http_tags=["pxstore"],
    description="Mount the NFS-exported central store on a REMOTE host (an "
                "OS+docker box or a VM — anything with an enrolled SSH cred): "
                "installs nfs-common, adds the fstab entry, mounts. Bind the "
                "local_path into containers afterwards (e.g. ollama -v "
                "/vera-store/models/ollama:/root/.ollama/models:ro). Inputs: "
                "host_id (str! — exec.ssh host), server (str! — the PVE node "
                "IP), remote_path (str — default the store mountpoint from "
                "settings, needs cluster_id), cluster_id (str), local_path "
                "(str='/vera-store'), ro (bool=true). Output: {ok, mounted_at}.",
)
async def cap_store_attach_remote(host_id: str = "", server: str = "",
                                  remote_path: str = "", cluster_id: str = "",
                                  local_path: str = "/vera-store",
                                  ro: bool = True, trace_id=None) -> Dict:
    if not (host_id and server):
        return {"error": "host_id and server required"}
    if not remote_path:
        if not cluster_id:
            return {"error": "remote_path or cluster_id required"}
        cfg = await _cfg_get(cluster_id)
        remote_path = cfg.get("store_mount", "")
        if not remote_path:
            return {"error": "central store not provisioned"}
    run = _rawcap("exec.ssh.run")
    if not run:
        return {"error": "exec.ssh.run unavailable"}
    opts = "ro,hard,vers=4" if ro else "rw,hard,vers=4"
    fstab = f"{server}:{remote_path} {local_path} nfs {opts} 0 0"
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
    return {"ok": True, "mounted_at": local_path, "fstab": fstab}


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
