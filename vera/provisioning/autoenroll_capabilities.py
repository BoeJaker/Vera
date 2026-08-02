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
    # picks it up. SSH creds land in the exec host store on first use; anything
    # else is held here for the enrol step to read.
    try:
        sealed = vsecrets.seal(value)
    except RuntimeError as e:
        return {"error": str(e)}
    box = {"cred": cred, "value": sealed, "ssh_user": ssh_user,
           "ssh_port": int(ssh_port or 0), "ip": ip, "fqdn": fqdn, "ts": now_iso()}
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
                    hid = h.get("id", "")
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
                assets.append({
                    "key": f"pve:{res.get('cluster_id', cid)}:{g.get('vmid')}",
                    "name": g.get("name") or f"vmid{g.get('vmid')}",
                    "kind": "lxc" if g.get("type") == "lxc" else "vm",
                    "ip": "", "host_id": g.get("host_id", ""),
                    "enrolled_ssh": bool(g.get("enrolled")),
                    "in_mesh": g.get("host_id", "") in mesh_ids,
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
        actions: List[str] = []
        needs: List[str] = []
        if cfg.get("do_certs") and a["kind"] != "docker":
            actions.append("cert")
        if cfg.get("do_mesh") and not a["in_mesh"] and a.get("host_id"):
            actions.append("mesh")
        elif cfg.get("do_mesh") and not a["in_mesh"] and not a.get("host_id"):
            # not yet a saved SSH host → can't mesh-join until enrolled
            if a["source"] == "proxmox" and not a["enrolled_ssh"]:
                needs.append("ssh_password")
        if cfg.get("do_ldap"):
            actions.append("ldap")
        if cfg.get("do_apps") and a["kind"] == "docker":
            actions.append("apps")
        if a["source"] == "proxmox" and not a["enrolled_ssh"] and "ssh_password" not in needs:
            needs.append("ssh_password")
        plan.append({
            "key": a["key"], "name": a["name"], "kind": a["kind"],
            "ip": a.get("ip", ""), "host_id": a.get("host_id", ""),
            "enrolled_ssh": a["enrolled_ssh"], "in_mesh": a["in_mesh"],
            "actions": actions, "needs": needs, "source": a["source"],
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
async def _enrol_one(a: Dict, cfg: Dict) -> Dict:
    """Enrol ONE asset across the enabled subsystems. Best-effort per step."""
    key = a["key"]
    steps: Dict[str, Any] = {}
    base = cfg.get("base_domain", "")
    fqdn = _fqdn_for(a, base)

    # ── Proxmox guest that isn't a saved SSH host yet → needs bootstrap creds ──
    if a["source"] == "proxmox" and not a["enrolled_ssh"]:
        prov = await _provided_cred(key)
        if not prov.get("value"):
            await _pending_add(key, "ssh_password",
                               f"Bootstrap SSH password for {a['name']} "
                               f"(Proxmox {a.get('guest_type')} {a.get('vmid')}) to enrol it",
                               {"cluster_id": a.get("cluster_id"), "vmid": a.get("vmid"),
                                "node": a.get("node")})
            return {"key": key, "status": "needs_cred", "needs": ["ssh_password"],
                    "steps": steps}
        ip = prov.get("ip") or a.get("ip") or ""
        if not ip:
            await _pending_add(key, "ip", f"IP address for {a['name']} to SSH into", {})
            return {"key": key, "status": "needs_cred", "needs": ["ip"], "steps": steps}
        eg = _cap("enroll.guest")
        if eg:
            try:
                r = await eg(cluster_id=a.get("cluster_id", ""), vmid=int(a.get("vmid") or 0),
                             guest_type=a.get("guest_type", "qemu"), node=a.get("node", ""),
                             fqdn=fqdn or (prov.get("fqdn") or ip), ip=ip,
                             ssh_user=prov.get("ssh_user") or "root",
                             ssh_password=prov.get("value", ""),
                             ssh_port=int(prov.get("ssh_port") or 22))
                steps["enroll_guest"] = {"ok": bool(r.get("ok")), "host_id": r.get("host_id", ""),
                                         "error": r.get("error", "")}
                if r.get("host_id"):
                    a["host_id"] = r["host_id"]
                    a["enrolled_ssh"] = True
            except Exception as e:
                steps["enroll_guest"] = {"ok": False, "error": str(e)}

    # ── TLS cert (+ OpenBao store + identity register) via the asset-online hook
    if cfg.get("do_certs") and a["kind"] != "docker":
        online = _cap("provisioning.asset.online")
        if online:
            try:
                r = await online(fqdn=fqdn, name=a.get("name", ""), ip=a.get("ip", ""),
                                 kind=a.get("kind", "host"), ssh_host_id=a.get("host_id", ""))
                steps["cert"] = {"ok": bool(r.get("ok")), "fqdn": r.get("fqdn", "")}
            except Exception as e:
                steps["cert"] = {"ok": False, "error": str(e)}

    # ── Directory host record (FreeIPA-first via the resolver, graceful fallback) ──
    if cfg.get("do_ldap"):
        reg = _cap("identity.resolve.host") or _cap("identity.host.register")
        if reg and fqdn:
            try:
                r = await reg(fqdn=fqdn, ip=a.get("ip", ""))
                steps["ldap"] = {"ok": bool(r.get("ok")),
                                 "backend": r.get("backend", ""),
                                 "skipped": r.get("skipped", False),
                                 "error": r.get("error", "")}
            except Exception as e:
                steps["ldap"] = {"ok": False, "error": str(e)}
        else:
            steps["ldap"] = {"skipped": "identity module not configured or no fqdn"}

    # ── Mesh join (needs a saved exec SSH host) ──
    if cfg.get("do_mesh") and not a["in_mesh"]:
        if a.get("host_id"):
            join = _cap("netsec.mesh.join")
            if join:
                try:
                    r = await join(host_id=a["host_id"])
                    steps["mesh"] = {"ok": bool(r.get("ok")), "error": r.get("error", "")}
                except Exception as e:
                    steps["mesh"] = {"ok": False, "error": str(e)}
        else:
            steps["mesh"] = {"skipped": "no exec SSH host yet"}

    # ── Docker: mount exposed container ports as apps ──
    if cfg.get("do_apps") and a["kind"] == "docker":
        steps["apps"] = await _mount_docker_apps(a, cfg)

    ok = all((v.get("ok", True) if isinstance(v, dict) else True)
             for v in steps.values())
    return {"key": key, "status": "ok" if ok else "partial", "steps": steps}


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
            results.append(await _enrol_one(a, cfg))
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
