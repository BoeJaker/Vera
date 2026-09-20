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
  vfs.peer.add       give a device file access over netctl's WireGuard door (files profile)
  vfs.peer.list      enrolled devices and last-handshake times
  vfs.peer.remove    revoke a device

Redis layout
------------
  vera:vfs:cfg   hash  key -> JSON settings (host, ssh label, share root)
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import shlex
import string
from typing import Any, Dict, List, Optional

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso,
)

log = logging.getLogger("vera.vfs")

KEY_CFG = "vera:vfs:cfg"

DEFAULTS = {
    "host": "192.168.0.160",
    "host_internal": "10.33.33.11",
    "ssh_label": "VFS-02",
    "share_root": "/srv/pools/tank_sde/vfs",
    "estate_root": "/srv/vfs/estate",
    "cloud_url": "http://192.168.0.161:8080",
    "sync_url": "http://192.168.0.160:8384",
    # Devices reach the file server through netctl's WireGuard door (files
    # profile). Vera acts there with a token that can only add, list and revoke
    # files-only devices, held in the secrets service at door_secret.
    "door_url": "http://192.168.0.221:8088",
    "door_secret": "netctl/door-files",
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
                "smbd, nfsd, syncthing and nginx up, and is the estate tree "
                "mounted. No inputs. Output: {ok, services:{name:bool}, "
                "estate_mounts:int} or {error}.",
)
async def cap_health(trace_id=None) -> Dict:
    script = (
        "for s in smbd nfs-server syncthing@syncthing nginx; do "
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
                "estate tree size, file devices on netctl's WireGuard door, and the last estate sync "
                "report. No inputs. Output: {host, services, shares, exports, "
                "estate:{mounted,skipped}, peers, disk} or {error}.",
)
async def cap_status(trace_id=None) -> Dict:
    cfg = await _cfg()
    script = r"""
echo '###SERVICES'
for s in smbd nfs-server syncthing@syncthing nginx vfs-estate-sync.timer; do
  printf '%s=%s\n' "$s" "$(systemctl is-active $s 2>/dev/null)"
done
echo '###DISK'
df -PB1 --output=target,size,used,avail /srv/pools/tank_sde/vfs /srv/pools/BigDat 2>/dev/null | tail -n +2
echo '###EXPORTS'
exportfs -s 2>/dev/null | head -20
echo '###ESTATE'
findmnt -rno TARGET 2>/dev/null | grep '^/srv/vfs/estate/' | wc -l
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

    # File devices connect through netctl's WireGuard door (files profile).
    door = await _door("GET", "/api/door/files")
    peers = [{"name": d.get("name", ""), "address": d.get("address", ""),
              "last_handshake_s": d.get("last_handshake_s"), "connected": bool(d.get("connected"))}
             for d in door.get("peers") or []]

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
        "door_error": door.get("error", ""),
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
_PEER_NAME_OK = set(string.ascii_letters + string.digits + "-_")


def _valid_peer(name: str) -> bool:
    # netctl's own rule for device names
    return bool(name) and len(name) <= 32 and set(name) <= _PEER_NAME_OK


def _secrets_service() -> Optional[Dict[str, Any]]:
    fn = (_orch.CAPABILITY_REGISTRY.get("secrets.status") or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


async def _door(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """netctl's files-only door API, authorised by the door token held in the
    secrets service. netctl lets that token add, list and revoke files-profile
    devices and reach nothing else."""
    cfg = await _cfg()
    svc = _secrets_service()
    if not svc or "get_named" not in svc:
        return {"error": "the secrets service is not loaded, so the netctl door token cannot be read"}
    where = cfg.get("door_secret") or DEFAULTS["door_secret"]
    token = ((await svc["get_named"](where)) or {}).get("value", "")
    if not token:
        return {"error": f"the secrets service holds no netctl door token at {where!r}"}
    base = (cfg.get("door_url") or DEFAULTS["door_url"]).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(method, base + path, headers={"X-Netctl-Door": token}, json=body)
        data = r.json() if r.content else {}
    except Exception as e:
        return {"error": f"netctl's door at {base} did not answer: {type(e).__name__}"}
    if r.status_code == 401:
        return {"error": "netctl refused the door token"}
    if not isinstance(data, dict):
        return {"error": "netctl gave an unreadable answer"}
    if r.status_code >= 400:
        return {"error": data.get("error") or data.get("message") or f"netctl answered HTTP {r.status_code}"}
    return data


@capability(
    "vfs.peer.add",
    http_method="POST", http_path="/vfs/peer/add", http_tags=["vfs"],
    # The result carries the device's PRIVATE KEY. Never let it into the
    # memory/fabric stores, and keep it out of the activity transcript.
    memory="off", silent=True,
    description="Give a device file-server access over netctl's WireGuard door (on NWM-02) "
                "and return its client config. The device gets netctl's 'files' profile: "
                "the tunnel carries only the file server (VFS-02), normal internet traffic "
                "is untouched, and nothing else on the estate answers. Each device has its "
                "own key pair, so a lost phone is one revocation. Vera acts with a netctl "
                "token that can only add, list and revoke files-only devices. Inputs: name "
                "(str! -- letters, digits, - and _, up to 32). Output: {ok, name, address, "
                "config, qr_svg} or {error}. The config contains a private key: treat it "
                "as a secret.",
)
async def cap_peer_add(name: str = "", trace_id=None) -> Dict:
    if not _valid_peer(name):
        return {"error": "name required: letters, digits, '-' and '_', up to 32 characters"}
    r = await _door("POST", "/api/door/files/peer", {"name": name, "dns": False})
    if r.get("error") or not r.get("ok"):
        return {"error": r.get("error") or r.get("message") or "the device was not enrolled"}
    await emit_event({"type": "vfs.progress", "stage": "peer.add",
                      "message": f"WireGuard files device enrolled: {name} ({r.get('address', '')})"})
    return {"ok": True, "name": name, "address": r.get("address", ""), "config": r.get("config", ""),
            "qr_svg": r.get("qr_svg", ""), "endpoint_note": r.get("endpoint_note", ""),
            "note": "contains a private key -- deliver it over a trusted channel"}


@capability(
    "vfs.peer.list",
    http_method="POST", http_path="/vfs/peer/list", http_tags=["vfs"],
    memory="off", silent=True,
    description="Devices with file access over netctl's WireGuard door, with their tunnel "
                "address and when each last completed a handshake, plus whether the door is "
                "open. No inputs. Output: {peers:[{name,address,last_handshake,connected}], "
                "count, door:{enabled,up,endpoint,port,router}}.",
)
async def cap_peer_list(trace_id=None) -> Dict:
    r = await _door("GET", "/api/door/files")
    if r.get("error"):
        return {"error": r["error"]}
    peers = []
    for d in r.get("peers") or []:
        seconds = d.get("last_handshake_s")
        peers.append({"name": d.get("name", ""), "address": d.get("address", ""),
                      "last_handshake": "never" if seconds is None else f"{int(seconds)}s ago",
                      "connected": bool(d.get("connected"))})
    return {"peers": peers, "count": len(peers),
            "door": {"enabled": r.get("enabled"), "up": r.get("up"), "endpoint": r.get("endpoint", ""),
                     "port": r.get("port"), "router": r.get("router", "")}}


@capability(
    "vfs.peer.remove",
    http_method="POST", http_path="/vfs/peer/remove", http_tags=["vfs"],
    description="Revoke a device's file access over netctl's WireGuard door. Takes effect "
                "immediately. Only files-only devices can be revoked this way. Inputs: name "
                "(str!). Output: {ok, name} or {error}.",
)
async def cap_peer_remove(name: str = "", trace_id=None) -> Dict:
    if not _valid_peer(name):
        return {"error": "name required: letters, digits, '-' and '_', up to 32 characters"}
    r = await _door("POST", f"/api/door/files/peer/delete/{name}")
    if r.get("error") or not r.get("ok"):
        return {"error": r.get("error") or r.get("message") or "the device was not revoked"}
    await emit_event({"type": "vfs.progress", "stage": "peer.remove",
                      "message": f"WireGuard files device revoked: {name}"})
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
                "(str), ssh_label (str), cloud_url (str), sync_url (str), door_url (str -- netctl's address), door_secret (str -- where the secrets service holds the door token). "
                "Output: {ok, settings}.",
)
async def cap_settings_save(host: str = "", host_internal: str = "",
                            ssh_label: str = "", cloud_url: str = "",
                            sync_url: str = "", door_url: str = "",
                            door_secret: str = "", trace_id=None) -> Dict:
    cfg = await _cfg()
    for field, value in (("host", host), ("host_internal", host_internal),
                         ("ssh_label", ssh_label), ("cloud_url", cloud_url),
                         ("sync_url", sync_url), ("door_url", door_url),
                         ("door_secret", door_secret)):
        if value:
            cfg[field] = value
    r = _redis()
    if r is None:
        return {"error": "Redis unavailable -- settings not persisted"}
    await r.hset(KEY_CFG, "settings", json.dumps(cfg))
    return {"ok": True, "settings": cfg}


# ═════════════════════════════════════════════════════════════════════════════
#  WRITABLE ESTATE - which guests estate-rw exposes, and who may reach it
# ═════════════════════════════════════════════════════════════════════════════
from Vera.vera.vfs import vfs_rw_core as _rw          # noqa: E402  (plans, app-free)


async def _rw_state() -> Dict[str, Any]:
    """The list, what is actually mounted writable, and the share's reach."""
    r = await _ssh(f"cat {_rw.RW_LIST} 2>/dev/null; echo '###RW'; "
                   "python3 -c \"import json;d=json.load(open('/var/lib/vfs/estate-state.json'));"
                   "print(json.dumps({'rw':d.get('rw',[]),'mounted':[m.get('name') for m in d.get('mounted',[]) "
                   "if m.get('state') in ('mounted','already')]}))\" 2>/dev/null; echo '###SMB'; "
                   f"sed -n '/^\[{_rw.SHARE}\]/,/^\[/p' {_rw.SMB_CONF} 2>/dev/null", timeout=45)
    if r.get("error"):
        return {"error": r["error"]}
    out = r.get("stdout") or ""
    lst, _, rest = out.partition("###RW")
    rep, _, smb = rest.partition("###SMB")
    try:
        report = json.loads(rep.strip() or "{}")
    except json.JSONDecodeError:
        report = {}
    reach = _rw.share_reach(smb, _rw.SHARE)          # the sed slice already starts at the header
    return {"names": _rw.parse_list(lst), "rw": report.get("rw", []), "known": report.get("mounted", []),
            "share": reach}


@capability(
    "vfs.estate.rw",
    http_method="POST", http_path="/vfs/estate/rw", http_tags=["vfs"],
    memory="off", silent=True,
    description="Which guests the file server exposes WRITABLE: the names in "
                "/etc/vfs/estate-rw.list on VFS-02, what is actually mounted under "
                "/srv/vfs/estate-rw, and who may open that share (valid users, hosts "
                "allow, door_only). No inputs. Output: {names, rw:[{name,src,state}], "
                "known:[guest names in the tree], share:{valid_users,hosts_allow,door_only}}.",
)
async def cap_estate_rw(trace_id=None) -> Dict:
    return await _rw_state()


@capability(
    "vfs.estate.rw.set",
    http_method="POST", http_path="/vfs/estate/rw/set", http_tags=["vfs"],
    description="Make exactly these guests writable through estate-rw (@vfs-admin "
                "only): writes /etc/vfs/estate-rw.list and rebuilds the tree. Dry run "
                "by default - the plan says which guests are added or dropped, which "
                "of them are running (the guest writes the same files), and what the "
                "rebuild does; confirm=true runs it. Inputs: names (list of guest "
                "names as the tree knows them), confirm (bool=false). Output: {plan:{"
                "commands,warnings,adds,drops}, dry_run} or {ok, rw:[...], names}.",
)
async def cap_estate_rw_set(names: Optional[List[str]] = None, confirm: bool = False,
                            trace_id=None) -> Dict:
    st = await _rw_state()
    if st.get("error"):
        return {"error": st["error"]}
    if isinstance(names, str):
        names = [n for n in re.split(r"[\s,]+", names) if n]
    chk = _rw.check_names(names or [], st["known"])
    if chk["bad"]:
        return {"error": f"not a guest name: {', '.join(chk['bad'])}"}
    if chk["unknown"]:
        return {"error": f"not in the estate tree: {', '.join(chk['unknown'])} - only guests the tree already "
                         f"mounts can be made writable (run vfs.estate.sync if one is missing)"}
    running: List[str] = []
    listc = _rawcap("estate.machines")
    if listc is not None:
        try:
            res = await listc()
            running = [m.get("label") for m in (res or {}).get("machines", []) if m.get("status") == "running"]
        except Exception as e:                                      # pragma: no cover - best effort
            log.debug("estate.machines unavailable for the rw plan: %s", e)
    plan = _rw.set_plan(chk["ok"], st["names"], running)
    if not confirm:
        return {"plan": plan, "dry_run": True, "current": st["names"]}
    await emit_event({"type": "vfs.progress", "stage": "estate.rw",
                      "message": f"writable estate set to {len(chk['ok'])} guest(s)"})
    r = await _ssh(_rw.script(plan["commands"]), timeout=300)
    if r.get("error") or r.get("rc") not in (0, None):
        return {"plan": plan, "ok": False, "error": r.get("error") or (r.get("stderr") or "")[-400:]}
    after = await _rw_state()
    return {"plan": plan, "ok": True, "names": after.get("names", []), "rw": after.get("rw", [])}


@capability(
    "vfs.estate.rw.door_only",
    http_method="POST", http_path="/vfs/estate/rw/door_only", http_tags=["vfs"],
    description="Limit the writable estate share to devices on the VFS WireGuard "
                "door (10.55.55.0/24) - a device key plus the @vfs-admin password - "
                "or reopen it to every network Samba listens on. Edits only the "
                "[estate-rw] block of smb.conf on VFS-02 with a dated backup, reloads "
                "only if testparm accepts the result, and never drops sessions. Dry "
                "run by default. Inputs: enable (bool=true), confirm (bool=false). "
                "Output: {plan:{commands,warnings}, dry_run} or {ok, share}.",
)
async def cap_estate_rw_door_only(enable: bool = True, confirm: bool = False,
                                  trace_id=None) -> Dict:
    plan = _rw.door_only_plan(bool(enable))
    if not confirm:
        st = await _rw_state()
        return {"plan": plan, "dry_run": True, "share": st.get("share")}
    await emit_event({"type": "vfs.progress", "stage": "estate.rw",
                      "message": "estate-rw reach: " + ("door only" if enable else "any network")})
    r = await _ssh(_rw.script(plan["commands"]), timeout=120)
    if r.get("error") or r.get("rc") not in (0, None):
        return {"plan": plan, "ok": False, "error": r.get("error") or (r.get("stderr") or "")[-400:],
                "log": (r.get("stdout") or "")[-400:]}
    after = await _rw_state()
    return {"plan": plan, "ok": True, "share": after.get("share"), "log": (r.get("stdout") or "")[-200:]}
