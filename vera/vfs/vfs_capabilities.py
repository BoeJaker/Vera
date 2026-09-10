"""
vfs_capabilities.py -- the Vera File Fabric (VFS-02) control surface
====================================================================

VFS-02 (CT160, 192.168.0.160 / 10.33.33.11) is the estate's file server. It
replaces the TurnKey appliance on VFS-01 (CT125), which is retired in stages
rather than switched off -- both run side by side until the last client moves.

What makes it different from VFS-01
-----------------------------------
  * VFS-01 bind-mounted ONE LXC mountpoint per vmid into its own container
    config (mp0..mp16 -> /mnt/vol-105 ...). That needs a container restart per
    change, silently collides when two disks claim one mountpoint (mp14/mp15
    both claimed /mnt/vol-121, so a dataset was invisible), covered 16 of 38
    containers and no VMs at all, and went stale whenever a guest was created.
  * VFS-02 recursively binds the POOL ROOTS once, with slave propagation, so
    every guest dataset -- including ones created after it booted -- is already
    inside its namespace. A timer then projects them into a name-keyed tree of
    READ-ONLY bind mounts. Read-only matters: the guest is usually running and
    writing to that same filesystem.
  * Anonymous access is refused outright. VFS-01 served an anonymous
    read-write share.

This module is the Vera-side control surface: it does not reimplement any of
that, it drives the scripts that already live on the box over the shared
exec.ssh host registry, the same way pxstore.* drives the Proxmox node.

Capabilities (group `vfs.*`)
----------------------------
  vfs.status         one call: services, share tree size, exports, peers, disk
  vfs.health         cheap liveness probe for dashboards and the timeline
  vfs.shares         the share catalogue, with the client mount strings
  vfs.estate.sync    rebuild the name-keyed estate tree now
  vfs.estate.list    what the tree currently exposes, and what was skipped
  vfs.peer.add       enrol a device on the WireGuard door (returns its config)
  vfs.peer.list      enrolled devices and last-handshake times
  vfs.peer.remove    revoke a device

Redis layout
------------
  vera:vfs:cfg   hash  key -> JSON settings (host, ssh label, share root)
"""
from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso,
)

log = logging.getLogger("vera.vfs")

_HERE = Path(__file__).parent
KEY_CFG = "vera:vfs:cfg"

DEFAULTS = {
    "host": "192.168.0.160",
    "host_internal": "10.33.33.11",
    "ssh_label": "VFS-02",
    "share_root": "/srv/pools/tank_sde/vfs",
    "estate_root": "/srv/vfs/estate",
    "cloud_url": "http://192.168.0.161:8080",
    "sync_url": "http://192.168.0.160:8384",
}

# name -> (server path, transport hint, whether it is read-only)
SHARES = {
    "home":      ("/srv/pools/tank_sde/vfs/home",   "smb+nfs", False),
    "cloud":     ("/srv/pools/tank_sde/vfs/cloud",  "smb+nfs", False),
    "sync":      ("/srv/pools/tank_sde/vfs/sync",   "smb+nfs", False),
    "backup":    ("/srv/pools/tank_sde/vfs/backup", "smb+nfs", False),
    "media":     ("/srv/pools/BigDat",              "smb+nfs", False),
    "estate":    ("/srv/vfs/estate",                "smb+nfs", True),
    "estate-rw": ("/srv/vfs/estate-rw",             "smb",     False),
}

# Stable drive letters, so the same share is the same letter on every machine.
DRIVE_LETTERS = {
    "home": "V", "cloud": "N", "sync": "S", "media": "M",
    "backup": "B", "estate": "E", "estate-rw": "W",
}


# ═════════════════════════════════════════════════════════════════════════════
#  PLUMBING
# ═════════════════════════════════════════════════════════════════════════════
def _redis():
    return getattr(_orch, "REDIS", None)


def _rawcap(name: str):
    """Another capability's undecorated function (no double activity records)."""
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


async def _host_id(cfg: Dict[str, Any]) -> str:
    """Resolve the SSH host-store id for VFS-02, by LABEL then by address.

    Going through the store rather than a hardcoded address means
    re-addressing the server is a host-store edit, not a code change.
    """
    listc = _rawcap("exec.ssh.hosts.list")
    if listc is None:
        return ""
    try:
        res = await listc()
    except Exception as e:
        log.debug("ssh host list failed: %s", e)
        return ""
    hosts = res.get("hosts") if isinstance(res, dict) else res
    if not isinstance(hosts, list):
        return ""
    label, addr = cfg["ssh_label"], cfg["host"]
    for h in hosts:
        if h.get("label") == label:
            return h.get("id", "")
    for h in hosts:
        if h.get("host") == addr:
            return h.get("id", "")
    return ""


async def _cfg() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    r = _redis()
    if r is not None:
        try:
            raw = await r.hget(KEY_CFG, "settings")
            if raw:
                cfg.update(json.loads(raw))
        except Exception as e:
            log.debug("vfs cfg read failed, using defaults: %s", e)
    return cfg


def _sh(script: str) -> str:
    """Wrap a multi-line script so it survives one ssh exec intact."""
    return "bash -c " + shlex.quote(script)


async def _ssh(script: str, timeout: int = 120) -> Dict[str, Any]:
    """Run a shell script on VFS-02 via the shared exec.ssh host registry."""
    cfg = await _cfg()
    run = _rawcap("exec.ssh.run")
    if run is None:
        return {"error": "exec.ssh.run unavailable (execution module not loaded)"}
    hid = await _host_id(cfg)
    if not hid:
        return {"error": f"no enrolled SSH credential for {cfg['ssh_label']!r} "
                         f"({cfg['host']}) -- add one with exec.ssh.hosts.save and "
                         f"label it {cfg['ssh_label']!r}"}
    try:
        return await run(command=_sh(script), host_id=hid, timeout=timeout)
    except Exception as e:
        return {"error": f"SSH to {cfg['ssh_label']} failed: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
#  STATUS / HEALTH
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "vfs.health",
    http_method="POST", http_path="/vfs/health", http_tags=["vfs"],
    memory="off", silent=True,
    description="Cheap liveness probe for the Vera File Fabric (VFS-02): are "
                "smbd, nfsd, wireguard and syncthing up, and is the estate tree "
                "mounted. No inputs. Output: {ok, services:{name:bool}, "
                "estate_mounts:int} or {error}.",
)
async def cap_health(trace_id=None) -> Dict:
    script = (
        "for s in smbd nfs-server wg-quick@wg0 syncthing@syncthing nginx; do "
        "printf '%s=%s\\n' \"$s\" \"$(systemctl is-active $s 2>/dev/null)\"; done; "
        "echo mounts=$(findmnt -rno TARGET 2>/dev/null | grep -c '^/srv/vfs/estate/')"
    )
    r = await _ssh(script, timeout=30)
    if r.get("error"):
        return {"ok": False, "error": r["error"]}
    services, mounts = {}, 0
    for line in (r.get("stdout") or "").splitlines():
        if line.startswith("mounts="):
            try:
                mounts = int(line.split("=", 1)[1])
            except ValueError:
                mounts = 0
        elif "=" in line:
            k, v = line.split("=", 1)
            services[k] = v.strip() == "active"
    return {"ok": all(services.values()) and mounts > 0,
            "services": services, "estate_mounts": mounts}


@capability(
    "vfs.status",
    http_method="POST", http_path="/vfs/status", http_tags=["vfs"],
    memory="off",
    description="Full status of the Vera File Fabric (VFS-02) in one call: "
                "service states, share catalogue with free space, NFS exports, "
                "estate tree size, WireGuard peers, and the last estate sync "
                "report. No inputs. Output: {host, services, shares, exports, "
                "estate:{mounted,skipped}, peers, disk} or {error}.",
)
async def cap_status(trace_id=None) -> Dict:
    cfg = await _cfg()
    script = r"""
echo '###SERVICES'
for s in smbd nfs-server wg-quick@wg0 syncthing@syncthing nginx vfs-estate-sync.timer; do
  printf '%s=%s\n' "$s" "$(systemctl is-active $s 2>/dev/null)"
done
echo '###DISK'
df -PB1 --output=target,size,used,avail /srv/pools/tank_sde/vfs /srv/pools/BigDat 2>/dev/null | tail -n +2
echo '###EXPORTS'
exportfs -s 2>/dev/null | head -20
echo '###ESTATE'
findmnt -rno TARGET 2>/dev/null | grep '^/srv/vfs/estate/' | wc -l
echo '###PEERS'
wg show wg0 dump 2>/dev/null | tail -n +2
echo '###SYNCREPORT'
cat /var/lib/vfs/estate-state.json 2>/dev/null | head -c 4000
"""
    r = await _ssh(script, timeout=60)
    if r.get("error"):
        return {"error": r["error"]}

    sections = _split_sections(r.get("stdout") or "")

    services = {}
    for line in sections.get("SERVICES", []):
        if "=" in line:
            k, v = line.split("=", 1)
            services[k] = v.strip()

    disk = []
    for line in sections.get("DISK", []):
        parts = line.split()
        if len(parts) == 4:
            try:
                disk.append({"target": parts[0], "size": int(parts[1]),
                             "used": int(parts[2]), "avail": int(parts[3])})
            except ValueError:
                continue

    peers = []
    for line in sections.get("PEERS", []):
        f = line.split("\t")
        if len(f) >= 5:
            try:
                handshake = int(f[4])
            except ValueError:
                handshake = 0
            peers.append({"pubkey": f[0][:16] + "...", "allowed_ips": f[3],
                          "last_handshake": handshake})

    estate_mounts = 0
    if sections.get("ESTATE"):
        try:
            estate_mounts = int(sections["ESTATE"][0])
        except (ValueError, IndexError):
            estate_mounts = 0

    sync_report = {}
    raw = "\n".join(sections.get("SYNCREPORT", []))
    if raw.strip():
        try:
            sync_report = json.loads(raw)
        except json.JSONDecodeError:
            sync_report = {}

    return {
        "host": cfg["host"],
        "services": services,
        "shares": _share_catalogue(cfg),
        "exports": sections.get("EXPORTS", []),
        "estate": {"mounted": estate_mounts,
                   "skipped": sync_report.get("skipped", []),
                   "vm": sync_report.get("vm", [])},
        "peers": peers,
        "disk": disk,
        "cloud_url": cfg["cloud_url"],
        "sync_url": cfg["sync_url"],
        "checked_at": now_iso(),
    }


def _split_sections(stdout: str) -> Dict[str, List[str]]:
    """Split a '###NAME'-delimited script transcript into named line lists."""
    out: Dict[str, List[str]] = {}
    current = None
    for line in stdout.splitlines():
        if line.startswith("###"):
            current = line[3:].strip()
            out[current] = []
        elif current is not None and line.strip():
            out[current].append(line)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  SHARES
# ═════════════════════════════════════════════════════════════════════════════
def _share_catalogue(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    host = cfg["host"]
    out = []
    for name, (path, transport, ro) in SHARES.items():
        entry = {
            "name": name,
            "path": path,
            "read_only": ro,
            "drive_letter": DRIVE_LETTERS.get(name, ""),
            "unc": f"\\\\{host}\\{name}",
            "admin_only": name.startswith("estate"),
        }
        if "nfs" in transport:
            entry["nfs"] = f"{host}:{path}"
        out.append(entry)
    return out


@capability(
    "vfs.shares",
    http_method="POST", http_path="/vfs/shares", http_tags=["vfs"],
    memory="off", silent=True,
    description="The Vera File Fabric share catalogue with ready-to-paste client "
                "mount strings (UNC path, Windows drive letter, NFS export). No "
                "inputs. Output: {shares:[{name,path,unc,nfs,drive_letter,"
                "read_only,admin_only}], setup:{windows,linux}}.",
)
async def cap_shares(trace_id=None) -> Dict:
    cfg = await _cfg()
    host = cfg["host"]
    return {
        "shares": _share_catalogue(cfg),
        "setup": {
            # The bootstrap endpoint on the server itself, so a new machine
            # needs one line and no copy of this repo.
            "windows": f"irm http://{host}/setup.ps1 | iex",
            "linux": f"curl -fsS http://{host}/setup.sh | sudo bash",
        },
        "cloud_url": cfg["cloud_url"],
        "sync_url": cfg["sync_url"],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  ESTATE TREE
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "vfs.estate.sync",
    http_method="POST", http_path="/vfs/estate/sync", http_tags=["vfs"],
    description="Rebuild the name-keyed estate tree on VFS-02 now, instead of "
                "waiting for its 5-minute timer. Every container and VM "
                "filesystem is projected to /srv/vfs/estate/<guest-name> as a "
                "READ-ONLY bind mount (running VMs come in over sshfs). Safe to "
                "re-run at any time; it is idempotent. No inputs. Output: "
                "{ok, mounted, vm, skipped, pruned} or {error}.",
)
async def cap_estate_sync(trace_id=None) -> Dict:
    await emit_event({"type": "vfs.progress", "stage": "estate.sync",
                      "message": "rebuilding the VFS-02 estate tree"})
    r = await _ssh("systemctl start vfs-estate-sync.service && "
                   "cat /var/lib/vfs/estate-state.json", timeout=300)
    if r.get("error"):
        return {"error": r["error"]}
    try:
        report = json.loads(r.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"error": "estate sync ran but its report was unreadable",
                "log": (r.get("stdout") or "")[-800:]}
    mounted = [m for m in report.get("mounted", [])
               if m.get("state") in ("mounted", "already")]
    failed = [m for m in report.get("mounted", []) + report.get("vm", [])
              if m.get("state") not in ("mounted", "already")]
    await emit_event({"type": "vfs.progress", "stage": "estate.sync",
                      "message": f"estate tree rebuilt: {len(mounted)} filesystems"})
    return {"ok": not failed,
            "mounted": len(mounted),
            "vm": report.get("vm", []),
            "failed": failed,
            "skipped": report.get("skipped", []),
            "pruned": report.get("pruned", [])}


@capability(
    "vfs.estate.list",
    http_method="POST", http_path="/vfs/estate/list", http_tags=["vfs"],
    memory="off", silent=True,
    description="What the VFS-02 estate tree currently exposes: one entry per "
                "guest filesystem, by NAME, plus everything that was skipped and "
                "why. Inputs: refresh (bool=false -- re-read the live mount table "
                "rather than the last sync report). Output: {entries:[{name,"
                "source,kind}], skipped:[{name,reason}], count}.",
)
async def cap_estate_list(refresh: bool = False, trace_id=None) -> Dict:
    if refresh:
        r = await _ssh(
            "findmnt -rno TARGET,SOURCE,FSTYPE 2>/dev/null | "
            "grep '^/srv/vfs/estate/' | head -300", timeout=45)
        if r.get("error"):
            return {"error": r["error"]}
        entries = []
        for line in (r.get("stdout") or "").splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            target, source, fstype = parts
            entries.append({"name": target.split("/srv/vfs/estate/", 1)[-1],
                            "source": source,
                            "kind": "sshfs" if "sshfs" in fstype else "bind"})
        return {"entries": entries, "count": len(entries), "skipped": []}

    r = await _ssh("cat /var/lib/vfs/estate-state.json 2>/dev/null", timeout=30)
    if r.get("error"):
        return {"error": r["error"]}
    try:
        report = json.loads(r.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"error": "no readable estate report yet -- run vfs.estate.sync"}
    entries = [{"name": m["name"], "source": m.get("src", ""), "kind": "bind"}
               for m in report.get("mounted", [])
               if m.get("state") in ("mounted", "already")]
    entries += [{"name": m["name"], "source": "sshfs", "kind": "sshfs"}
                for m in report.get("vm", [])
                if m.get("state") in ("mounted", "already")]
    return {"entries": entries, "count": len(entries),
            "skipped": report.get("skipped", [])}


# ═════════════════════════════════════════════════════════════════════════════
#  WIREGUARD DOOR
# ═════════════════════════════════════════════════════════════════════════════
_PEER_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _valid_peer(name: str) -> bool:
    return bool(name) and len(name) <= 40 and set(name) <= _PEER_NAME_OK


@capability(
    "vfs.peer.add",
    http_method="POST", http_path="/vfs/peer/add", http_tags=["vfs"],
    description="Enrol a device on the VFS-02 WireGuard door and return its "
                "client config. Each device gets its own keypair, so losing one "
                "phone is one revocation rather than a rotation across every "
                "device. The tunnel routes ONLY the file server -- normal "
                "internet traffic is untouched, and a peer cannot reach the rest "
                "of the estate. Inputs: name (str! -- letters, digits, - and _). "
                "Output: {ok, name, address, config} or {error}. The config "
                "contains a private key: treat it as a secret.",
)
async def cap_peer_add(name: str = "", trace_id=None) -> Dict:
    if not _valid_peer(name):
        return {"error": "name required: letters, digits, '-' and '_', max 40 chars"}
    r = await _ssh(f"/usr/local/sbin/vfs-peer add {shlex.quote(name)}", timeout=90)
    if r.get("error"):
        return {"error": r["error"]}
    out = r.get("stdout") or ""
    if "already exists" in out or r.get("rc", 0) != 0:
        return {"error": (r.get("stderr") or out).strip()[:300] or "peer add failed"}
    # The tool prints the config between its own markers; take the [Interface]
    # block onward and stop before the QR code.
    conf, addr = [], ""
    started = False
    for line in out.splitlines():
        if line.startswith("[Interface]"):
            started = True
        if line.startswith("--- scan"):
            break
        if started:
            conf.append(line)
            if line.startswith("Address"):
                addr = line.split("=", 1)[-1].strip()
    await emit_event({"type": "vfs.progress", "stage": "peer.add",
                      "message": f"WireGuard device enrolled: {name} ({addr})"})
    return {"ok": True, "name": name, "address": addr,
            "config": "\n".join(conf).strip(),
            "note": "contains a private key -- deliver it over a trusted channel"}


@capability(
    "vfs.peer.list",
    http_method="POST", http_path="/vfs/peer/list", http_tags=["vfs"],
    memory="off", silent=True,
    description="Devices enrolled on the VFS-02 WireGuard door, with their "
                "tunnel address and how long ago each last completed a "
                "handshake. No inputs. Output: {peers:[{name,address,"
                "last_handshake}], count}.",
)
async def cap_peer_list(trace_id=None) -> Dict:
    r = await _ssh("/usr/local/sbin/vfs-peer list", timeout=45)
    if r.get("error"):
        return {"error": r["error"]}
    peers = []
    for line in (r.get("stdout") or "").splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) >= 2:
            peers.append({"name": parts[0], "address": parts[1],
                          "last_handshake": parts[2].strip() if len(parts) > 2 else ""})
    return {"peers": peers, "count": len(peers)}


@capability(
    "vfs.peer.remove",
    http_method="POST", http_path="/vfs/peer/remove", http_tags=["vfs"],
    description="Revoke a device's access to the VFS-02 WireGuard door. Takes "
                "effect immediately -- the peer is dropped from the live "
                "interface as well as the config. Inputs: name (str!). Output: "
                "{ok, name} or {error}.",
)
async def cap_peer_remove(name: str = "", trace_id=None) -> Dict:
    if not _valid_peer(name):
        return {"error": "name required: letters, digits, '-' and '_', max 40 chars"}
    r = await _ssh(f"/usr/local/sbin/vfs-peer remove {shlex.quote(name)}", timeout=60)
    if r.get("error"):
        return {"error": r["error"]}
    if r.get("rc", 0) != 0:
        return {"error": (r.get("stderr") or r.get("stdout") or "").strip()[:300]
                         or "peer remove failed"}
    await emit_event({"type": "vfs.progress", "stage": "peer.remove",
                      "message": f"WireGuard device revoked: {name}"})
    return {"ok": True, "name": name}


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "vfs.settings.save",
    http_method="POST", http_path="/vfs/settings/save", http_tags=["vfs"],
    description="Update where the Vera File Fabric lives, for when VFS-02 is "
                "re-addressed or its SSH host-store label changes. Only the "
                "fields you pass are altered. Inputs: host (str), host_internal "
                "(str), ssh_label (str), cloud_url (str), sync_url (str). "
                "Output: {ok, settings}.",
)
async def cap_settings_save(host: str = "", host_internal: str = "",
                            ssh_label: str = "", cloud_url: str = "",
                            sync_url: str = "", trace_id=None) -> Dict:
    cfg = await _cfg()
    for field, value in (("host", host), ("host_internal", host_internal),
                         ("ssh_label", ssh_label), ("cloud_url", cloud_url),
                         ("sync_url", sync_url)):
        if value:
            cfg[field] = value
    r = _redis()
    if r is None:
        return {"error": "Redis unavailable -- settings not persisted"}
    await r.hset(KEY_CFG, "settings", json.dumps(cfg))
    return {"ok": True, "settings": cfg}
