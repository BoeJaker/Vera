"""pxstore.zfs.* controls - resize, scrub, trim, snapshot, rollback, tune,
attach, add - each a plan first and an action only when confirmed.

Three storage capabilities touched ZFS; today's shrink of three guest disks
had to be done by hand. Every operation here answers with the exact commands
and their warnings (dry run, the default) and runs them on the node over its
mapped SSH login only with confirm=true. The two that change a pool's layout,
attach and add, also want the pool's name typed back (confirm_pool), because
a vdev added to a pool with raidz in it can never be removed. The rules are
zfs_ops_core; this module reads the facts a plan needs from the node and runs
the plan.

Capabilities
------------
  pxstore.zfs.resize     grow a guest disk, or shrink a dataset by refquota (never a zvol)
  pxstore.zfs.scrub      start, pause or stop a scrub
  pxstore.zfs.trim       trim a pool
  pxstore.zfs.snapshot   snapshot a dataset
  pxstore.zfs.snapshots  list a dataset's snapshots
  pxstore.zfs.rollback   roll a dataset back, saying which newer snapshots go
  pxstore.zfs.tune       set a fixed set of dataset properties
  pxstore.zfs.attach     turn a single disk into a mirror
  pxstore.zfs.add        stripe a new vdev into a pool
"""
from __future__ import annotations

import inspect
import logging
import shlex
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event
from Vera.vera.proxmox import pool_core as _pool
from Vera.vera.proxmox import zfs_ops_core as core

log = logging.getLogger("vera.pxstore")


def _px() -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get("pxstore.inventory") or {}).get("func")
    g = getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None
    if not g or "_node_ssh" not in g or "_sh" not in g:
        raise RuntimeError("the storage capabilities are not loaded")
    return g


def _flag(v: Any) -> bool:
    return bool(v) and str(v).strip().lower() not in ("0", "false", "no", "off")


async def _node(cluster_id: str, node: str, script: str, timeout: int = 60) -> Dict[str, Any]:
    g = _px()
    return await g["_node_ssh"](cluster_id, node, g["_sh"](script), timeout=timeout) or {}


async def _dataset_facts(cluster_id: str, node: str, dataset: str) -> Dict[str, Any]:
    r = await _node(cluster_id, node, f"zfs get -Hp -o property,value type,used,refquota,quota,volsize {shlex.quote(dataset)}")
    if r.get("rc") not in (0, None) and not (r.get("stdout") or "").strip():
        raise RuntimeError((r.get("stderr") or r.get("error") or f"{dataset} was not found on {node}")[:300])
    facts: Dict[str, Any] = {"name": dataset}
    for line in (r.get("stdout") or "").splitlines():
        f = line.split("\t") if "\t" in line else line.split()
        if len(f) >= 2:
            k, v = f[0], f[1]
            if k == "type":
                facts["type"] = v
            elif v not in ("-", "none"):
                try:
                    facts[k] = int(v)
                except ValueError:
                    pass
    return facts


async def _vdevs(cluster_id: str, node: str, pool: str) -> List[Dict[str, Any]]:
    r = await _node(cluster_id, node, f"zpool status {shlex.quote(pool)}")
    st = _pool.parse_zpool_status(r.get("stdout") or "")
    if pool not in st:
        raise RuntimeError(f"pool {pool} was not found on {node}")
    return st[pool]["vdevs"]


async def _finish(plan: Dict[str, Any], cluster_id: str, node: str, confirm: bool,
                  stage: str, timeout: int = 120) -> Dict[str, Any]:
    """Return the plan, or run it and report."""
    if not plan.get("ok"):
        return {"ok": False, "dry_run": True, "error": plan.get("error"), "plan": plan}
    if not confirm:
        return {"ok": True, "dry_run": True, "plan": plan}
    script = "set -e\nmkdir -p /root/ct-conf-backups\n" + "\n".join(plan["commands"])
    r = await _node(cluster_id, node, script, timeout=timeout)
    ok = r.get("rc") == 0 or (r.get("ok") and not r.get("error"))
    out = {"ok": bool(ok), "dry_run": False, "plan": plan,
           "stdout": (r.get("stdout") or "")[-800:], "stderr": (r.get("stderr") or "")[-800:]}
    if not ok:
        out["error"] = (r.get("error") or r.get("stderr") or "the commands did not all succeed")[:400]
    await emit_event({"type": "pxstore.progress", "stage": stage, "ok": bool(ok),
                      "message": f"{plan.get('op')} on {node}: " + "; ".join(plan["commands"])[:200]})
    return out


_COMMON = ("Inputs also: cluster_id (str!), node (str!), confirm (bool=false - dry run unless true). "
           "Output: {ok, dry_run, plan:{op, commands, warnings, ...}, stdout, stderr, error}.")


@capability(
    "pxstore.zfs.resize",
    http_method="POST", http_path="/pxstore/zfs/resize", http_tags=["pxstore"], memory="on",
    description="Resize a guest disk's dataset to an absolute size. A dataset (LXC subvol) shrinks "
                "by lowering refquota, never below what it holds, and the guest's Proxmox config "
                "line is kept in step; a zvol (VM disk) grows only - shrinking one destroys the "
                "filesystem inside. Inputs: dataset (str! e.g. tank_sdh/subvol-126-disk-0), size "
                "(str! '200G'), vmid (int), guest_type ('lxc'|'qemu'), disk (str - rootfs|scsi0). " + _COMMON,
)
async def cap_zfs_resize(cluster_id: str = "", node: str = "", dataset: str = "", size: str = "",
                         vmid: int = 0, guest_type: str = "", disk: str = "", confirm: bool = False,
                         trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and dataset and size):
        return {"error": "cluster_id, node, dataset and size are required"}
    try:
        facts = await _dataset_facts(cluster_id, node, dataset)
    except RuntimeError as e:
        return {"error": str(e)}
    guest = {"vmid": int(vmid), "type": guest_type or "lxc"} if vmid else None
    plan = core.resize_plan(facts, size, guest=guest, disk_key=disk or ("rootfs" if (guest_type or "lxc") == "lxc" else "scsi0"))
    return await _finish(plan, cluster_id, node, _flag(confirm), "zfs.resize")


@capability(
    "pxstore.zfs.scrub",
    http_method="POST", http_path="/pxstore/zfs/scrub", http_tags=["pxstore"], memory="on",
    description="Start, pause or stop a scrub of a pool. Inputs: pool (str!), action "
                "('start'|'pause'|'stop'). " + _COMMON,
)
async def cap_zfs_scrub(cluster_id: str = "", node: str = "", pool: str = "", action: str = "start",
                        confirm: bool = False, trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and pool):
        return {"error": "cluster_id, node and pool are required"}
    return await _finish(core.scrub_plan(pool, action or "start"), cluster_id, node, _flag(confirm), "zfs.scrub")


@capability(
    "pxstore.zfs.trim",
    http_method="POST", http_path="/pxstore/zfs/trim", http_tags=["pxstore"], memory="on",
    description="Trim a pool (SSDs). Inputs: pool (str!). " + _COMMON,
)
async def cap_zfs_trim(cluster_id: str = "", node: str = "", pool: str = "", confirm: bool = False,
                       trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and pool):
        return {"error": "cluster_id, node and pool are required"}
    return await _finish(core.trim_plan(pool), cluster_id, node, _flag(confirm), "zfs.trim")


@capability(
    "pxstore.zfs.snapshot",
    http_method="POST", http_path="/pxstore/zfs/snapshot", http_tags=["pxstore"], memory="on",
    description="Snapshot a dataset. Inputs: dataset (str!), name (str - default vera-<timestamp>), "
                "recursive (bool=false). " + _COMMON,
)
async def cap_zfs_snapshot(cluster_id: str = "", node: str = "", dataset: str = "", name: str = "",
                           recursive: bool = False, confirm: bool = False, trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and dataset):
        return {"error": "cluster_id, node and dataset are required"}
    return await _finish(core.snapshot_plan(dataset, name, _flag(recursive)), cluster_id, node, _flag(confirm), "zfs.snapshot")


@capability(
    "pxstore.zfs.snapshots",
    http_method="POST", http_path="/pxstore/zfs/snapshots", http_tags=["pxstore"], memory="off", silent=True,
    description="A dataset's snapshots, oldest first. Inputs: cluster_id (str!), node (str!), dataset "
                "(str!). Output: {snapshots:[{name, used, created}]}.",
)
async def cap_zfs_snapshots(cluster_id: str = "", node: str = "", dataset: str = "", trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and dataset):
        return {"error": "cluster_id, node and dataset are required"}
    r = await _node(cluster_id, node, f"zfs list -Hp -t snapshot -o name,used,creation -s creation -d 1 {shlex.quote(dataset)}")
    rows = []
    for line in (r.get("stdout") or "").splitlines():
        f = line.split("\t") if "\t" in line else line.split()
        if len(f) >= 3:
            rows.append({"name": f[0], "used": int(f[1]) if f[1].isdigit() else 0,
                         "created": int(f[2]) if f[2].isdigit() else None})
    return {"dataset": dataset, "snapshots": rows}


@capability(
    "pxstore.zfs.rollback",
    http_method="POST", http_path="/pxstore/zfs/rollback", http_tags=["pxstore"], memory="on",
    description="Roll a dataset back to a snapshot. Everything written since is lost; rolling back "
                "past newer snapshots destroys them too and the plan names them. Inputs: dataset "
                "(str!), snapshot (str! - the part after @). " + _COMMON,
)
async def cap_zfs_rollback(cluster_id: str = "", node: str = "", dataset: str = "", snapshot: str = "",
                           confirm: bool = False, trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and dataset and snapshot):
        return {"error": "cluster_id, node, dataset and snapshot are required"}
    listed = await cap_zfs_snapshots(cluster_id=cluster_id, node=node, dataset=dataset)
    names = [s["name"] for s in listed.get("snapshots") or []]
    return await _finish(core.rollback_plan(dataset, snapshot, names), cluster_id, node, _flag(confirm), "zfs.rollback")


@capability(
    "pxstore.zfs.tune",
    http_method="POST", http_path="/pxstore/zfs/tune", http_tags=["pxstore"], memory="on",
    description="Set dataset properties from a fixed set: recordsize, compression, atime, relatime, "
                "sync, primarycache, logbias, xattr. Inputs: dataset (str!), props (dict!). " + _COMMON,
)
async def cap_zfs_tune(cluster_id: str = "", node: str = "", dataset: str = "", props: Optional[Dict[str, Any]] = None,
                       confirm: bool = False, trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and dataset):
        return {"error": "cluster_id, node and dataset are required"}
    return await _finish(core.tune_plan(dataset, props or {}), cluster_id, node, _flag(confirm), "zfs.tune")


@capability(
    "pxstore.zfs.attach",
    http_method="POST", http_path="/pxstore/zfs/attach", http_tags=["pxstore"], memory="on",
    description="Turn a single disk into a mirror by attaching a new, empty disk beside it - the "
                "fix for a pool with no redundancy. The pool resilvers and stays usable; zpool "
                "detach undoes it. Inputs: pool (str!), existing (str! - a data disk of the pool), "
                "new (str! - an empty disk at least as large), confirm_pool (str - the pool's name "
                "typed back, required with confirm). " + _COMMON,
)
async def cap_zfs_attach(cluster_id: str = "", node: str = "", pool: str = "", existing: str = "", new: str = "",
                         confirm: bool = False, confirm_pool: str = "", trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and pool and existing and new):
        return {"error": "cluster_id, node, pool, existing and new are required"}
    try:
        vdevs = await _vdevs(cluster_id, node, pool)
    except RuntimeError as e:
        return {"error": str(e)}
    plan = core.attach_plan(pool, vdevs, existing, new)
    if _flag(confirm) and confirm_pool != pool:
        return {"ok": False, "dry_run": True, "plan": plan, "error": "type the pool's name in confirm_pool to run this"}
    return await _finish(plan, cluster_id, node, _flag(confirm), "zfs.attach", timeout=180)


@capability(
    "pxstore.zfs.add",
    http_method="POST", http_path="/pxstore/zfs/add", http_tags=["pxstore"], memory="on",
    description="Stripe a new vdev into a pool: disks as a single disk, a mirror or raidz. Raises "
                "throughput; the pool is then as safe as its least redundant vdev, and a vdev "
                "cannot be removed once raidz is in the pool. The plan shows the layout after. "
                "Inputs: pool (str!), disks (list!), kind ('disk'|'mirror'|'raidz1'|'raidz2'|"
                "'raidz3'), confirm_pool (str - the pool's name typed back, required with confirm). " + _COMMON,
)
async def cap_zfs_add(cluster_id: str = "", node: str = "", pool: str = "", disks: Optional[List[str]] = None,
                      kind: str = "", confirm: bool = False, confirm_pool: str = "", trace_id=None) -> Dict[str, Any]:
    if not (cluster_id and node and pool and disks):
        return {"error": "cluster_id, node, pool and disks are required"}
    try:
        vdevs = await _vdevs(cluster_id, node, pool)
    except RuntimeError as e:
        return {"error": str(e)}
    plan = core.add_plan(pool, vdevs, list(disks), kind)
    if _flag(confirm) and confirm_pool != pool:
        return {"ok": False, "dry_run": True, "plan": plan, "error": "type the pool's name in confirm_pool to run this"}
    return await _finish(plan, cluster_id, node, _flag(confirm), "zfs.add", timeout=180)


log.info("zfs_ops_capabilities ready - pxstore.zfs.resize/scrub/trim/snapshot/snapshots/rollback/tune/attach/add")
