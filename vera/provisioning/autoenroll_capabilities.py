"""
autoenroll_capabilities.py — Auto-enrolment orchestrator
========================================================

One orchestration layer over primitives that already exist, so the whole estate
onboards itself: existing Docker + Proxmox stacks get enrolled, NEW containers /
VMs are picked up automatically, and each asset is wired into the mesh, given
SSH + TLS certs, registered in the directory, and has its exposed container
ports mounted as apps. Missing credentials become a pending-request queue the
operator (or chat) can fill; secrets it collects are sealed through the shared
helper — which routes to OpenBao when the vault is stood up
([[ollama-routing-concurrency]] is unrelated; see vera/security/secrets.py).

Nothing here re-implements enrolment. It composes:
  • enroll.discover / enroll.guest            — Proxmox guest onboarding (SSH push)
  • ssh.host.list / ssh.host.save             — the exec/enrol SSH host store
  • provisioning.asset.online                 — TLS cert (step-ca) + OpenBao + identity
  • netsec.mesh.join                          — pull a host onto the encrypted overlay
  • identity.host.register / identity.app.register — directory (LDAP/IPA) records
  • docker.hosts.list / docker.ps             — container discovery
  • app.detect / app.mount                    — expose container ports as apps

Safety
──────
`dry_run` defaults TRUE (scan/plan only) and the watch loop is OPT-IN
(config.enabled=false) — nothing is enrolled until the operator turns it on.
Every subsystem is independently toggleable and best-effort: a failure in one
(e.g. no directory configured) never aborts the others; it's recorded and the
run continues.

Capabilities
────────────
  autoenroll.config.get / .save
  autoenroll.scan                 — read-only plan across all sources
  autoenroll.run                  — execute the plan (honours dry_run)
  autoenroll.enrol                — enrol ONE asset now; every enrolment entry point
                                    (Foundry, proxmox.lxc.create, nodes.provision) calls it
  autoenroll.watch.start / .stop / .status
  autoenroll.pending              — list credentials still needed
  autoenroll.cred.provide         — supply a missing credential (then re-enrol)

Redis
─────
  vera:autoenroll:config    hash field 'main'  -> JSON config
  vera:autoenroll:pending   hash key '<asset>:<cred>' -> JSON request
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso,
)
from Vera.vera.security import secrets as vsecrets
# Which steps an asset gets, and why the others are skipped, lives in an app-free
# core; enroll.guest and proxmox.guest.enroll are this pipeline's login step.
from Vera.vera.provisioning import enrol_pipeline_core as _pipe
from Vera.vera.provisioning import ssh_store_merge_core as _merge

log = logging.getLogger("vera.autoenroll")

KEY_CONFIG = "vera:autoenroll:config"
KEY_PENDING = "vera:autoenroll:pending"
_FIELD = "main"

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,          # master switch for the background WATCH loop
    "dry_run": True,           # plan-only unless explicitly turned off
    "interval_s": 300,         # watch rescan cadence
    "do_certs": True,          # issue/install TLS cert via provisioning.asset.online
    "do_mesh": True,           # netsec.mesh.join
    "do_ldap": True,           # identity.host.register / app.register
    "do_apps": True,           # mount exposed container ports as apps
    "src_proxmox": True,
    "src_docker": True,
    "src_ssh_hosts": True,
    "base_domain": "",         # for fqdn synthesis (name.<base_domain>)
    "mount_min_port": 1,       # ignore published ports below this
    "skip_ports": "22,2375,2376",   # never mount these as apps (ssh/docker api)
}


def _redis():
    return getattr(_orch, "REDIS", None)


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════
async def _cfg() -> Dict[str, Any]:
    r = _redis()
    cfg = dict(_DEFAULTS)
    if r:
        try:
            raw = await r.hget(KEY_CONFIG, _FIELD)
            if raw:
                cfg.update({k: v for k, v in json.loads(raw).items() if k in _DEFAULTS})
        except Exception as e:
            log.debug("autoenroll cfg load: %s", e)
    return cfg


@capability(
    "autoenroll.config.get", memory="off", silent=True,
    http_method="GET", http_path="/autoenroll/config", http_tags=["provisioning"],
    description="Get the auto-enrolment config (watch enabled?, dry_run, interval, "
                "which subsystems + sources are on, base_domain). "
                "Output: the config dict.",
)
async def cap_ae_config_get(trace_id=None) -> Dict:
    cfg = await _cfg()
    cfg["watch_running"] = _WATCH["task"] is not None and not _WATCH["task"].done()
    cfg["secret_backend"] = vsecrets.backend_status()
    return cfg


@capability(
    "autoenroll.config.save", memory="off",
    http_method="POST", http_path="/autoenroll/config/save", http_tags=["provisioning"],
    description="Update the auto-enrolment config. Any of: enabled (bool — turn the "
                "WATCH loop on/off), dry_run (bool), interval_s (int), do_certs, "
                "do_mesh, do_ldap, do_apps (bool), src_proxmox, src_docker, "
                "src_ssh_hosts (bool), base_domain (str), skip_ports (csv). "
                "Turning enabled on starts the watcher; off stops it. "
                "Output: the saved config.",
)
async def cap_ae_config_save(
    enabled: Optional[bool] = None, dry_run: Optional[bool] = None,
    interval_s: Optional[int] = None,
    do_certs: Optional[bool] = None, do_mesh: Optional[bool] = None,
    do_ldap: Optional[bool] = None, do_apps: Optional[bool] = None,
    src_proxmox: Optional[bool] = None, src_docker: Optional[bool] = None,
    src_ssh_hosts: Optional[bool] = None,
    base_domain: Optional[str] = None, skip_ports: Optional[str] = None,
    mount_min_port: Optional[int] = None, trace_id=None,
) -> Dict:
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    cur = await _cfg()
    patch = {
        "enabled": enabled, "dry_run": dry_run, "interval_s": interval_s,
        "do_certs": do_certs, "do_mesh": do_mesh, "do_ldap": do_ldap,
        "do_apps": do_apps, "src_proxmox": src_proxmox, "src_docker": src_docker,
        "src_ssh_hosts": src_ssh_hosts, "base_domain": base_domain,
        "skip_ports": skip_ports, "mount_min_port": mount_min_port,
    }
    for k, v in patch.items():
        if k in _DEFAULTS and v is not None:
            cur[k] = v
    cur["updated"] = now_iso()
    await r.hset(KEY_CONFIG, _FIELD, json.dumps(cur))
    # Reconcile the watch loop with the new `enabled` state.
    if cur.get("enabled"):
        await _watch_start()
    else:
        await _watch_stop()
    return await cap_ae_config_get()


# ═════════════════════════════════════════════════════════════════════════════
#  PENDING-CREDENTIAL QUEUE
# ═════════════════════════════════════════════════════════════════════════════
async def _pending_add(asset_key: str, cred: str, prompt: str, meta: Dict) -> None:
    r = _redis()
    if not r:
        return
    rec = {"asset": asset_key, "cred": cred, "prompt": prompt,
           "meta": meta, "ts": now_iso()}
    try:
        await r.hset(KEY_PENDING, f"{asset_key}::{cred}", json.dumps(rec))
        await emit_event({"type": "autoenroll.cred_needed", "asset": asset_key,
                          "cred": cred, "prompt": prompt})
    except Exception as e:
        log.debug("pending add: %s", e)


async def _pending_clear(asset_key: str, cred: str = "") -> None:
    r = _redis()
    if not r:
        return
    try:
        if cred:
            await r.hdel(KEY_PENDING, f"{asset_key}::{cred}")
        else:
            allk = await r.hkeys(KEY_PENDING) or []
            for k in allk:
                kk = k.decode() if isinstance(k, bytes) else k
                if kk.startswith(f"{asset_key}::"):
                    await r.hdel(KEY_PENDING, kk)
    except Exception as e:
        log.debug("pending clear: %s", e)


@capability(
    "autoenroll.pending", memory="off", silent=True,
    http_method="GET", http_path="/autoenroll/pending", http_tags=["provisioning"],
    description="List credentials auto-enrolment still needs before it can finish "
                "an asset (e.g. bootstrap SSH password for a new guest). "
                "Output: {pending:[{asset,cred,prompt,meta,ts}], count}.",
)
async def cap_ae_pending(trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"pending": [], "count": 0}
    out: List[Dict] = []
    try:
        allv = await r.hgetall(KEY_PENDING) or {}
        for _k, v in allv.items():
            try:
                out.append(json.loads(v))
            except Exception:
                continue
    except Exception as e:
        return {"pending": [], "count": 0, "error": str(e)}
    return {"pending": out, "count": len(out)}


@capability(
    "autoenroll.cred.provide", memory="off",
    http_method="POST", http_path="/autoenroll/cred/provide", http_tags=["provisioning"],
    description="Supply a credential auto-enrolment asked for. The secret is SEALED "
                "(→ OpenBao when active, else Fernet). Inputs: asset (str! — the key "
                "from autoenroll.pending), cred (str! — e.g. 'ssh_password'), value "
                "(str! — the secret), plus any context (ssh_user, ssh_port, ip, "
                "fqdn). Clears the pending request; re-run autoenroll.run to apply. "
                "Output: {ok, asset, cleared}.",
)
async def cap_ae_cred_provide(asset: str = "", cred: str = "", value: str = "",
                              ssh_user: str = "", ssh_port: int = 0, ip: str = "",
                              fqdn: str = "", trace_id=None) -> Dict:
    if not (asset and cred and value):
        return {"error": "asset, cred and value are all required"}
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    # Stash the provided credential (sealed) keyed to the asset so the next run
    # picks it up. One box per asset: an address and a password asked for
    # separately both stay, rather than the second replacing the first.
    try:
        raw = await r.hget("vera:autoenroll:creds", asset)
        box = json.loads(raw) if raw else {}
    except Exception:
        box = {}
    if cred == "ip":
        box["ip"] = value
    else:
        try:
            box.update(cred=cred, value=vsecrets.seal(value))
        except RuntimeError as e:
            return {"error": str(e)}
    for k, v in (("ssh_user", ssh_user), ("ip", ip), ("fqdn", fqdn)):
        if v:
            box[k] = v
    if ssh_port:
        box["ssh_port"] = int(ssh_port)
    box["ts"] = now_iso()
    await r.hset("vera:autoenroll:creds", asset, json.dumps(box))
    await _pending_clear(asset, cred)
    await emit_event({"type": "autoenroll.cred_provided", "asset": asset, "cred": cred})
    return {"ok": True, "asset": asset, "cleared": cred,
            "note": "run autoenroll.run to apply (or the watch loop will)."}


async def _provided_cred(asset_key: str) -> Dict:
    r = _redis()
    if not r:
        return {}
    try:
        raw = await r.hget("vera:autoenroll:creds", asset_key)
        if not raw:
            return {}
        box = json.loads(raw)
        box["value"] = vsecrets.open_secret(box.get("value", ""))
        return box
    except Exception:
        return {}


def _opts_from_box(box: Dict) -> Dict[str, Any]:
    """A credential supplied through autoenroll.cred.provide, as login-step options."""
    box = box or {}
    out: Dict[str, Any] = {}
    if box.get("cred") == "ip":               # older boxes held the address as the value
        if box.get("value"):
            out["ip"] = box["value"]
        return out
    if box.get("ip"):
        out["ip"] = box["ip"]
    if box.get("fqdn"):
        out["fqdn"] = box["fqdn"]
    if box.get("value"):
        out["ssh_user"] = box.get("ssh_user") or "root"
        out["ssh_port"] = int(box.get("ssh_port") or 22)
        field = "ssh_key_path" if box.get("cred") in ("ssh_key_path", "key_path") else "ssh_password"
        out[field] = box["value"]
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  DISCOVERY  — build the asset inventory across sources
# ═════════════════════════════════════════════════════════════════════════════
async def _mesh_member_ids() -> set:
    fn = _cap("netsec.mesh.members")
    if not fn:
        return set()
    try:
        r = await fn() or {}
        return {m.get("host_id") for m in r.get("members", []) if m.get("host_id")}
    except Exception:
        return set()


async def _discover() -> List[Dict[str, Any]]:
    """Inventory every candidate asset. Each: {key,name,kind,ip,host_id,
    enrolled_ssh, in_mesh, source}."""
    cfg = await _cfg()
    assets: List[Dict[str, Any]] = []
    mesh_ids = await _mesh_member_ids()

    # ── Exec/enrol SSH hosts (already have creds; candidates for cert+mesh+ldap)
    if cfg.get("src_ssh_hosts"):
        lst = _cap("ssh.host.list")
        if lst:
            try:
                for h in (await lst() or {}).get("hosts", []):
                    # the exec-store login: what the mesh and exec resolve
                    hid = h.get("exec_id") or h.get("id", "")
                    assets.append({
                        "key": f"ssh:{hid}", "name": h.get("label") or h.get("host"),
                        "kind": "host", "ip": h.get("host", ""), "host_id": hid,
                        "enrolled_ssh": True, "in_mesh": hid in mesh_ids,
                        "auth": h.get("auth", ""), "source": "ssh_host",
                    })
            except Exception as e:
                log.debug("discover ssh hosts: %s", e)

    # ── Docker hosts (+ their containers become apps)
    if cfg.get("src_docker"):
        dl = _cap("docker.hosts.list")
        if dl:
            try:
                for d in (await dl() or {}).get("hosts", []):
                    assets.append({
                        "key": f"docker:{d.get('id','')}",
                        "name": d.get("label") or d.get("id"),
                        "kind": "docker", "ip": _docker_addr(d),
                        "host_id": d.get("ssh_host_id", ""),
                        "enrolled_ssh": bool(d.get("ssh_host_id")),
                        "in_mesh": d.get("ssh_host_id", "") in mesh_ids,
                        "docker_id": d.get("id", ""), "source": "docker_host",
                    })
            except Exception as e:
                log.debug("discover docker: %s", e)

    # ── Proxmox guests (need bootstrap SSH creds to enrol)
    if cfg.get("src_proxmox"):
        disc = _cap("enroll.discover")
        clist = _cap("proxmox.cluster.list")
        clusters = []
        if clist:
            try:
                clusters = [c.get("id") for c in (await clist() or {}).get("clusters", [])]
            except Exception:
                clusters = []
        for cid in (clusters or [""]):
            if not disc:
                break
            try:
                res = await disc(cluster_id=cid) or {}
            except Exception as e:
                log.debug("discover proxmox %s: %s", cid, e)
                continue
            for g in res.get("guests", []):
                if g.get("status") != "running":
                    continue                      # can't SSH a stopped guest
                # the exec login is what the mesh joins; discovery resolved the
                # address (LXC config / QEMU agent), so a VM that has one can be
                # enrolled through Proxmox without a password
                hid = g.get("exec_id") or g.get("host_id", "")
                assets.append({
                    "key": _pipe.guest_key(res.get("cluster_id", cid), g.get("vmid")),
                    "name": g.get("name") or f"vmid{g.get('vmid')}",
                    "kind": "lxc" if g.get("type") == "lxc" else "vm",
                    "ip": g.get("ip", ""), "host_id": hid,
                    "enrolled_ssh": bool(g.get("enrolled")),
                    "in_mesh": hid in mesh_ids,
                    "cluster_id": res.get("cluster_id", cid), "vmid": g.get("vmid"),
                    "guest_type": "lxc" if g.get("type") == "lxc" else "qemu",
                    "node": g.get("node", ""), "source": "proxmox",
                })
    return assets


def _docker_addr(d: Dict) -> str:
    """Best-effort address for a docker host record (for app mounting)."""
    url = d.get("url", "") or ""
    if "://" in url:
        hostport = url.split("://", 1)[1]
        return hostport.split(":")[0].split("/")[0]
    if d.get("kind") == "local":
        return "127.0.0.1"
    return ""


def _fqdn_for(asset: Dict, base_domain: str) -> str:
    name = (asset.get("name") or "").strip().replace(" ", "-")
    if not name:
        return asset.get("ip", "")
    return f"{name}.{base_domain}" if base_domain and "." not in name else name


# ═════════════════════════════════════════════════════════════════════════════
#  SCAN  (read-only plan)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "autoenroll.scan", memory="off", silent=True,
    http_method="POST", http_path="/autoenroll/scan", http_tags=["provisioning"],
    description="Read-only: discover every asset (Proxmox guests, Docker hosts + "
                "containers, saved SSH hosts) and return the enrolment PLAN — what "
                "each still needs (cert / mesh / ldap / apps) and which credentials "
                "are missing. Never changes anything. "
                "Output: {plan:[{key,name,kind,ip,actions,needs}], summary}.",
)
async def cap_ae_scan(trace_id=None) -> Dict:
    cfg = await _cfg()
    assets = await _discover()
    plan: List[Dict] = []
    for a in assets:
        provided: Dict[str, Any] = {}
        if _pipe.is_proxmox_guest(a) and not a["enrolled_ssh"]:
            provided = _opts_from_box(await _provided_cred(a["key"]))
            if provided.get("ip") and not a.get("ip"):
                a = dict(a, ip=provided["ip"])
        row = _pipe.scan_row(a, cfg, provided)
        plan.append({
            "key": a["key"], "name": a["name"], "kind": a["kind"],
            "ip": a.get("ip", ""), "host_id": a.get("host_id", ""),
            "enrolled_ssh": a["enrolled_ssh"], "in_mesh": a["in_mesh"],
            "actions": row["actions"], "needs": row["needs"], "login": row["login"],
            "source": a["source"],
        })
    return {
        "plan": plan,
        "summary": {
            "assets": len(plan),
            "need_creds": sum(1 for p in plan if p["needs"]),
            "already_meshed": sum(1 for p in plan if p["in_mesh"]),
            "dry_run": cfg.get("dry_run", True),
            "watch_enabled": cfg.get("enabled", False),
        },
        "secret_backend": vsecrets.backend_status().get("backend"),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  ENROL  (per-asset execution)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_logins() -> List[Dict]:
    """The exec store's logins (redacted): what exec, terminals and the mesh use."""
    fn = _cap("exec.ssh.hosts.list")
    if not fn:
        return []
    try:
        return list((await fn() or {}).get("hosts") or [])
    except Exception as e:
        log.debug("exec logins: %s", e)
        return []


def _flag(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _need_prompt(a: Dict, need: str) -> str:
    what = a.get("name") or a["key"]
    if a.get("vmid"):
        what = f"{what} (Proxmox {a.get('guest_type') or 'guest'} {a['vmid']})"
    if need == "ip":
        return f"IP address for {what} to SSH into"
    return (f"Bootstrap SSH password for {what} to enrol it, or install the QEMU guest "
            f"agent so Vera can enrol it through Proxmox")


async def _login_step(a: Dict, opts: Dict, fqdn: str, method: str, skip_mesh: bool) -> Dict:
    """The pipeline's login step. enroll.guest pushes trust, Vera's key and a TLS
    cert into the guest, saves the login, registers its directory record and joins
    the mesh; register (proxmox.guest.enroll) only saves the login it is given."""
    if method == "register":
        reg = _cap("proxmox.guest.enroll")
        if not reg:
            return {"summary": {"ok": False, "error": "proxmox.guest.enroll unavailable"}, "sub": {}}
        try:
            r = await reg(cluster_id=a.get("cluster_id", ""), node=a.get("node", ""),
                          guest_type=a.get("guest_type", ""), vmid=int(a.get("vmid") or 0),
                          user=opts.get("ssh_user") or "root", password=opts.get("ssh_password", ""),
                          key_path=opts.get("ssh_key_path", ""), ip=a.get("ip", ""),
                          port=int(opts.get("ssh_port") or 22), label=opts.get("label", "")) or {}
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}"}
        sid = r.get("ssh_host_id", "")
        return {"summary": {"ok": bool(r.get("ok")), "via": "register", "host_id": sid,
                            "exec_host_id": sid, "error": r.get("error", "")},
                "sub": {}, "ip": r.get("ip", ""), "fqdn": ""}
    eg = _cap("enroll.guest")
    if not eg:
        return {"summary": {"ok": False, "error": "enroll.guest unavailable"}, "sub": {}}
    kw: Dict[str, Any] = {"cluster_id": a.get("cluster_id", ""), "vmid": int(a.get("vmid") or 0),
                          "guest_type": a.get("guest_type", ""), "node": a.get("node", ""),
                          "fqdn": fqdn, "ip": a.get("ip", ""), "skip_mesh": bool(skip_mesh)}
    if method == "proxmox":
        kw["via_proxmox"] = True
    else:
        kw["ssh_user"] = opts.get("ssh_user") or "root"
        kw["ssh_port"] = int(opts.get("ssh_port") or 22)
        if method == "key":
            kw["ssh_key_path"] = opts.get("ssh_key_path", "")
        else:
            kw["ssh_password"] = opts.get("ssh_password", "")
    try:
        r = await eg(**kw) or {}
    except Exception as e:
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sub = r.get("steps") or {}
    err = r.get("error") or ""
    if not r.get("ok") and not err:
        err = (sub.get("ssh") or {}).get("error") or "the enrolment script did not complete"
    return {"summary": {"ok": bool(r.get("ok")), "via": method, "host_id": r.get("host_id", ""),
                        "exec_host_id": r.get("exec_host_id", ""), "error": err},
            "sub": {k: sub[k] for k in ("identity", "mesh") if k in sub},
            "ip": "", "fqdn": r.get("fqdn", "")}


async def _enrol_one(a: Dict, cfg: Dict, *, provided: Optional[Dict] = None, only: Any = None,
                     register_only: bool = False, skip_mesh: bool = False) -> Dict:
    """Enrol ONE asset through the pipeline; enrol_pipeline_core decides the steps.
    Each step is best-effort, but when the login step fails the steps that need a
    login are skipped rather than run against a host Vera cannot reach."""
    key = a["key"]
    opts = dict(provided or {})
    if opts.get("ip") and not a.get("ip"):
        a["ip"] = opts["ip"]
    # a bare address is no directory name: synthesise one only from a real name
    fqdn = opts.get("fqdn") or (_fqdn_for(a, cfg.get("base_domain", "")) if a.get("name") else "")
    p = _pipe.plan(a, cfg, provided=opts, only=only, register_only=register_only, skip_mesh=skip_mesh)
    steps: Dict[str, Any] = {}
    out: Dict[str, Any] = {"key": key, "plan": p["steps"], "needs": p["needs"], "steps": steps}
    if p["needs"]:
        for need in p["needs"]:
            await _pending_add(key, need, _need_prompt(a, need),
                               {"cluster_id": a.get("cluster_id"), "vmid": a.get("vmid"),
                                "node": a.get("node")})
        return dict(out, status="needs_cred", host_id=a.get("host_id", ""), ip=a.get("ip", ""), fqdn=fqdn)

    login_failed = ""
    for s in p["steps"]:
        name = s["step"]
        if not s["run"]:
            if not s["covered"]:
                steps.setdefault(name, {"skipped": s["reason"]})
            continue
        if login_failed:
            steps[name] = {"skipped": f"login step failed: {login_failed}"}
            continue
        if name == "enroll_guest":
            r = await _login_step(a, opts, fqdn, p["method"], skip_mesh)
            steps["enroll_guest"] = r["summary"]
            steps.update(r["sub"])            # identity + mesh, as enroll.guest ran them
            if r["summary"].get("ok"):
                hid = r["summary"].get("exec_host_id") or r["summary"].get("host_id")
                if hid:
                    a["host_id"] = hid
                a["enrolled_ssh"] = True
                a["ip"] = a.get("ip") or r.get("ip", "")
                fqdn = r.get("fqdn") or fqdn
                await _pending_clear(key)
            else:
                login_failed = r["summary"].get("error") or "no login saved"
        elif name == "cert":
            # TLS cert (+ OpenBao store + identity register) via the asset-online hook
            online = _cap("provisioning.asset.online")
            if not online:
                steps["cert"] = {"skipped": "provisioning module not loaded"}
                continue
            try:
                r = await online(fqdn=fqdn, name=a.get("name", ""), ip=a.get("ip", ""),
                                 kind=a.get("kind", "host"), ssh_host_id=a.get("host_id", ""))
                steps["cert"] = {"ok": bool(r.get("ok")), "fqdn": r.get("fqdn", "")}
            except Exception as e:
                steps["cert"] = {"ok": False, "error": str(e)}
        elif name == "ldap":
            # Directory host record (FreeIPA-first via the resolver, graceful fallback)
            reg = _cap("identity.resolve.host") or _cap("identity.host.register")
            if not (reg and fqdn):
                steps["ldap"] = {"skipped": "identity module not configured or no fqdn"}
                continue
            try:
                r = await reg(fqdn=fqdn, ip=a.get("ip", ""))
                steps["ldap"] = {"ok": bool(r.get("ok")), "backend": r.get("backend", ""),
                                 "skipped": r.get("skipped", False), "error": r.get("error", "")}
            except Exception as e:
                steps["ldap"] = {"ok": False, "error": str(e)}
        elif name == "mesh":
            join = _cap("netsec.mesh.join")
            if not (join and a.get("host_id")):
                steps["mesh"] = {"skipped": "no exec SSH host yet" if join else "mesh module not loaded"}
                continue
            try:
                r = await join(host_id=a["host_id"])
                steps["mesh"] = {"ok": bool(r.get("ok")), "error": r.get("error", "")}
            except Exception as e:
                steps["mesh"] = {"ok": False, "error": str(e)}
        elif name == "apps":
            steps["apps"] = await _mount_docker_apps(a, cfg)
    return dict(out, status=_pipe.status_of(steps), host_id=a.get("host_id", ""),
                ip=a.get("ip", ""), fqdn=fqdn)


async def _mount_docker_apps(a: Dict, cfg: Dict) -> Dict:
    """Discover published container ports on a docker host and mount each as an app."""
    ps = _cap("docker.ps")
    mount = _cap("app.mount")
    if not (ps and mount):
        return {"skipped": "docker.ps / app.mount unavailable"}
    addr = a.get("ip", "") or ""
    if not addr:
        return {"skipped": "no reachable address for this docker host"}
    skip = {int(x) for x in str(cfg.get("skip_ports", "")).replace(" ", "").split(",")
            if x.strip().isdigit()}
    minp = int(cfg.get("mount_min_port", 1) or 1)
    mounted: List[Dict] = []
    try:
        rows = (await ps(host_id=a.get("docker_id", ""), all=False) or {}).get("containers", [])
    except Exception as e:
        return {"error": str(e)}
    seen: set = set()
    for c in rows:
        cname = (c.get("Names") or ["?"])
        cname = (cname[0] if isinstance(cname, list) and cname else str(cname)).lstrip("/")
        for p in (c.get("Ports") or []):
            pub = p.get("PublicPort")
            if not pub or int(pub) in skip or int(pub) < minp:
                continue
            if pub in seen:
                continue
            seen.add(pub)
            scheme = "https" if int(pub) in (443, 8443) else "http"
            label = f"{cname}:{pub}"
            try:
                r = await mount(label=label, host=addr, port=int(pub), scheme=scheme)
                if r.get("ok"):
                    mounted.append({"label": label, "port": pub})
            except Exception as e:
                log.debug("app.mount %s:%s failed: %s", addr, pub, e)
    return {"ok": True, "mounted": mounted, "count": len(mounted)}


@capability(
    "autoenroll.run", memory="on",
    http_method="POST", http_path="/autoenroll/run", http_tags=["provisioning"],
    description="Enrol assets across the enabled subsystems (mesh / certs / ldap / "
                "apps). Honours the config's dry_run (pass dry_run=false to actually "
                "apply, or set it in config). Inputs: dry_run (bool — overrides "
                "config for this run), only (csv of asset keys — default all "
                "actionable). Assets missing a credential are added to "
                "autoenroll.pending instead of failing. "
                "Output: {ran, dry_run, results:[...], pending, summary}.",
)
async def cap_ae_run(dry_run: Optional[bool] = None, only: str = "", trace_id=None) -> Dict:
    cfg = await _cfg()
    eff_dry = cfg.get("dry_run", True) if dry_run is None else bool(dry_run)
    assets = await _discover()
    only_set = {x.strip() for x in (only or "").split(",") if x.strip()}
    if only_set:
        assets = [a for a in assets if a["key"] in only_set]

    if eff_dry:
        scan = await cap_ae_scan()
        return {"ran": False, "dry_run": True,
                "plan": scan["plan"], "summary": scan["summary"],
                "note": "dry run — nothing changed. Re-run with dry_run=false to apply."}

    results: List[Dict] = []
    for a in assets:
        try:
            provided = (_opts_from_box(await _provided_cred(a["key"]))
                        if _pipe.is_proxmox_guest(a) and not a["enrolled_ssh"] else {})
            results.append(await _enrol_one(a, cfg, provided=provided))
        except Exception as e:
            results.append({"key": a["key"], "status": "error", "error": str(e)})
    pend = await cap_ae_pending()
    await emit_event({"type": "autoenroll.run_done",
                      "assets": len(results),
                      "ok": sum(1 for r in results if r.get("status") == "ok"),
                      "pending": pend.get("count", 0)})
    return {"ran": True, "dry_run": False, "results": results,
            "pending": pend.get("count", 0),
            "summary": {"assets": len(results),
                        "ok": sum(1 for r in results if r.get("status") == "ok"),
                        "partial": sum(1 for r in results if r.get("status") == "partial"),
                        "needs_cred": sum(1 for r in results if r.get("status") == "needs_cred")}}


@capability(
    "autoenroll.enrol", memory="on",
    http_method="POST", http_path="/autoenroll/enrol", http_tags=["provisioning"],
    description="Enrol ONE asset now through the auto-enrol pipeline, which every "
                "enrolment entry point uses: login (enroll.guest; proxmox.guest.enroll "
                "with register_only) -> cert -> directory (ldap) -> mesh -> apps. "
                "Target: cluster_id + vmid + guest_type ('lxc'|'qemu') (+ node, name) for "
                "a Proxmox guest, or host_id for a saved exec SSH login. Login options: "
                "fqdn, ip, ssh_user, ssh_password, ssh_key_path, ssh_port, label, "
                "via_proxmox (bool); with no credentials a guest is enrolled through "
                "Proxmox (containers always, VMs whose guest agent answers). steps (csv "
                "of enroll_guest,cert,ldap,mesh,apps; default: the auto-enrol config's "
                "switches, and an asset that already has a login skips the login step; "
                "naming enroll_guest always runs it). skip_mesh (bool: the mesh is "
                "joined elsewhere), register_only (bool: save the given login without "
                "changing the guest). Acts immediately: naming one asset is the "
                "confirmation, unlike autoenroll.run. A missing credential is queued in "
                "autoenroll.pending. Output: {ok, status, key, steps, plan, needs, "
                "host_id, exec_host_id, ssh_host_id, ip, fqdn, error}.",
)
async def cap_ae_enrol(cluster_id: str = "", vmid: int = 0, node: str = "", guest_type: str = "",
                       name: str = "", host_id: str = "", fqdn: str = "", ip: str = "",
                       ssh_user: str = "", ssh_password: str = "", ssh_key_path: str = "",
                       ssh_port: int = 22, label: str = "", steps: str = "",
                       skip_mesh: bool = False, register_only: bool = False,
                       via_proxmox: bool = False, trace_id=None) -> Dict:
    bad = _pipe.unknown_steps(steps)
    if bad:
        return {"ok": False, "error": f"unknown steps: {', '.join(bad)} "
                                      f"(known: {', '.join(_pipe.STEPS)})"}
    try:
        vmid = int(vmid or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "vmid must be a number"}
    forced_login = "enroll_guest" in _pipe.parse_steps(steps)
    opts: Dict[str, Any] = {k: v for k, v in (
        ("ssh_user", ssh_user), ("ssh_password", ssh_password), ("ssh_key_path", ssh_key_path),
        ("ip", ip), ("fqdn", fqdn), ("label", label)) if v}
    if ssh_port and int(ssh_port) != 22:
        opts["ssh_port"] = int(ssh_port)
    if _flag(via_proxmox):
        opts["via_proxmox"] = True
    logins = await _exec_logins()
    mesh_ids = await _mesh_member_ids()
    if cluster_id and vmid:
        if guest_type not in ("lxc", "qemu"):
            return {"ok": False, "error": "guest_type must be 'lxc' or 'qemu'"}
        login = _merge.login_for_guest(logins, cluster_id, vmid) or {}
        hid = login.get("id", "")
        a: Dict[str, Any] = {
            "key": _pipe.guest_key(cluster_id, vmid), "name": name, "source": "proxmox",
            "kind": "lxc" if guest_type == "lxc" else "vm", "guest_type": guest_type,
            "cluster_id": cluster_id, "vmid": vmid, "node": node, "ip": ip,
            "host_id": hid, "enrolled_ssh": bool(hid), "in_mesh": bool(hid) and hid in mesh_ids,
        }
        if not ip and not forced_login:
            # an existing login's address serves the later steps; a guest being
            # (re)enrolled finds its own, because a reused VMID can carry an old one
            a["ip"] = login.get("host", "")
    elif host_id:
        login = next((h for h in logins if h.get("id") == host_id), None)
        if not login:
            return {"ok": False, "error": f"no exec SSH login with id {host_id}"}
        a = {"key": f"ssh:{host_id}", "name": name or login.get("label") or login.get("host", ""),
             "kind": "host", "source": "ssh_host", "ip": ip or login.get("host", ""),
             "host_id": host_id, "enrolled_ssh": True, "in_mesh": host_id in mesh_ids}
    else:
        return {"ok": False, "error": "name the asset: cluster_id + vmid (with node and "
                                      "guest_type) or host_id"}
    res = await _enrol_one(a, await _cfg(), provided=opts, only=steps or None,
                           register_only=_flag(register_only), skip_mesh=_flag(skip_mesh))
    login_step = res["steps"].get("enroll_guest") or {}
    ran_login = any(s["step"] == "enroll_guest" and s["run"] for s in res["plan"])
    if res["status"] == "needs_cred":
        ok, err = False, "missing " + ", ".join(res["needs"])
    elif ran_login:
        ok, err = bool(login_step.get("ok")), login_step.get("error", "")
    else:
        ok, err = res["status"] == "ok", ""
    exec_id = login_step.get("exec_host_id") or res.get("host_id", "")
    await emit_event({"type": "autoenroll.enrol", "key": res["key"],
                      "status": res["status"], "ok": ok})
    return dict(res, ok=ok, error=err, exec_host_id=exec_id, ssh_host_id=exec_id)


# ═════════════════════════════════════════════════════════════════════════════
#  WATCH LOOP  (opt-in — auto-enrol NEW containers/VMs as they appear)
# ═════════════════════════════════════════════════════════════════════════════
_WATCH: Dict[str, Any] = {"task": None}


async def _watch_loop():
    log.info("autoenroll watch loop started")
    try:
        while True:
            cfg = await _cfg()
            if not cfg.get("enabled"):
                break
            try:
                # The watch loop only ACTS on assets that need nothing manual — it
                # never blocks on a missing credential (those queue for the operator).
                if not cfg.get("dry_run"):
                    await cap_ae_run(dry_run=False)
                else:
                    await cap_ae_scan()   # keep the plan/pending list warm
            except Exception as e:
                log.debug("autoenroll watch tick: %s", e)
            await asyncio.sleep(max(60, int(cfg.get("interval_s", 300))))
    except asyncio.CancelledError:
        pass
    finally:
        log.info("autoenroll watch loop stopped")


async def _watch_start():
    t = _WATCH.get("task")
    if t and not t.done():
        return
    _WATCH["task"] = asyncio.create_task(_watch_loop())


async def _watch_stop():
    t = _WATCH.get("task")
    if t and not t.done():
        t.cancel()
    _WATCH["task"] = None


@capability(
    "autoenroll.watch.start", memory="off",
    http_method="POST", http_path="/autoenroll/watch/start", http_tags=["provisioning"],
    description="Start the background watcher that periodically re-scans and "
                "auto-enrols NEW containers/VMs/hosts (sets config.enabled=true). "
                "Respects dry_run. Output: {ok, running}.",
)
async def cap_ae_watch_start(trace_id=None) -> Dict:
    await cap_ae_config_save(enabled=True)
    return {"ok": True, "running": True}


@capability(
    "autoenroll.watch.stop", memory="off",
    http_method="POST", http_path="/autoenroll/watch/stop", http_tags=["provisioning"],
    description="Stop the background auto-enrol watcher (config.enabled=false). "
                "Output: {ok, running}.",
)
async def cap_ae_watch_stop(trace_id=None) -> Dict:
    await cap_ae_config_save(enabled=False)
    return {"ok": True, "running": False}


@capability(
    "autoenroll.watch.status", memory="off", silent=True,
    http_method="GET", http_path="/autoenroll/watch/status", http_tags=["provisioning"],
    description="Is the auto-enrol watcher running? Output: {running, config}.",
)
async def cap_ae_watch_status(trace_id=None) -> Dict:
    t = _WATCH.get("task")
    return {"running": bool(t and not t.done()), "config": await cap_ae_config_get()}


# ═════════════════════════════════════════════════════════════════════════════
#  STARTUP — resume the watcher if it was left enabled
# ═════════════════════════════════════════════════════════════════════════════
async def _startup():
    try:
        cfg = await _cfg()
        if cfg.get("enabled"):
            await _watch_start()
            log.info("autoenroll: watch loop resumed (enabled in config)")
    except Exception as e:
        log.debug("autoenroll startup: %s", e)


try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass

log.info("autoenroll_capabilities loaded — scan/run/watch/pending/cred.provide")
