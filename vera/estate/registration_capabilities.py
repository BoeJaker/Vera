"""estate.registration - who is registered where, and the gaps made actionable.

Four registries - SSH logins, the directory (FreeIPA), the mesh (netctl's
door), certificates - plus backups, each partly filled and never side by side.
This puts every machine against every plane through the joins the entity
drawer already uses, names the registry entries no machine answers to, and
removes them on request (dry run first). When a guest is destroyed through
proxmox.guest.destroy, its own entries go with it.

Capabilities
------------
  estate.registration          the table: machines x planes, counts, stale entries
  estate.registration.prune    remove stale entries (dry run unless confirm)
  estate.registration.forget   remove one destroyed guest's entries
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso
from Vera.vera.estate import registration_core as core

log = logging.getLogger("vera.estate")
_PANEL = Path(__file__).parent / "registration_panel.html"


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


async def _sources(refresh: bool = False):
    ent = _module_of("estate.entity.resolve")
    if not ent or "_sources" not in ent:
        raise RuntimeError("estate.entity.resolve is not loaded, so the readers cannot be joined")
    return await ent["_sources"](refresh)


def _flag(v: Any) -> bool:
    return bool(v) and str(v).strip().lower() not in ("0", "false", "no", "off")


async def _directory_addresses(src) -> Dict[str, List[str]]:
    """The directory's own A records, fqdn -> addresses, so a host is judged by
    where it points rather than what it is called. Empty when the directory
    cannot be asked - then no host is called stale on that evidence."""
    idm = _module_of("identity.status") or {}
    if not all(k in idm for k in ("_state_opened", "_ipa_call")) or not src.identity:
        return {}
    try:
        st = await idm["_state_opened"]()
        if not st.get("ipa_url"):
            return {}
        zones = sorted({".".join(str(h.get("fqdn", "")).split(".")[1:]) for h in src.identity if "." in str(h.get("fqdn", ""))})
        out: Dict[str, List[str]] = {}
        for zone in zones:
            res, err = await idm["_ipa_call"](st, "dnsrecord_find", [zone], {"sizelimit": 2000})
            rows = (res or {}).get("result") if isinstance(res, dict) else None
            if err or rows is None:
                log.debug("registration: dnsrecord_find %s: %s", zone, err)
                continue
            for r in rows:
                name = r.get("idnsname")
                name = name[0] if isinstance(name, list) else name
                name = str(name or "").rstrip(".")
                if not name or name == "@":
                    continue
                fqdn = name if name.endswith(zone) else f"{name}.{zone}"
                out[fqdn.lower()] = [str(a) for a in (r.get("arecord") or [])]
            # every host of a zone we could read is now "known", even with no A record
            for h in src.identity:
                f = str(h.get("fqdn", "")).lower()
                if f.endswith("." + zone):
                    out.setdefault(f, [])
        return out
    except Exception as e:
        log.debug("registration: directory addresses failed: %s", e)
        return {}


async def _run_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    done = []
    for a in actions:
        act = a.get("action") or {}
        res = await _call(act.get("cap", ""), **(act.get("args") or {}))
        ok = bool(res.get("ok")) or (not res.get("error") and res.get("ok") is not False)
        done.append({"kind": a.get("kind"), "id": a.get("id"), "label": a.get("label"), "cap": act.get("cap"),
                     "ok": ok, "error": res.get("error") or ("" if ok else "the capability did not say ok")})
    return done


@capability(
    "estate.registration",
    http_method="GET", http_path="/estate/registration", http_tags=["estate", "netsec"],
    memory="off", silent=True,
    description="Who is registered where: every machine against SSH login, directory (FreeIPA), "
                "mesh (netctl door), certificate and backup - yes / no / n/a / unknown with the "
                "reason - counts per plane, how many machines are complete, and the registry "
                "entries no machine answers to (stale directory hosts, logins, mesh members) with "
                "the action that would remove each. Read-only; joins the same readers as "
                "estate.entity.resolve. Input: refresh (bool). Output: {rows:[{ref,label,kind,"
                "status,addr,vmid,node,type,cluster_id,planes,complete}], counts, machines, "
                "complete, stale:[{kind,id,label,why,action}], errors, checked_at}.",
)
async def cap_registration(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    try:
        src = await _sources(_flag(refresh))
    except RuntimeError as e:
        return {"error": str(e)}
    out = core.coverage(src)
    out["stale"] = core.stale(src, await _directory_addresses(src))
    out["errors"] = dict(src.errors)
    out["checked_at"] = now_iso()
    return out


@capability(
    "estate.registration.prune",
    http_method="POST", http_path="/estate/registration/prune", http_tags=["estate", "netsec"],
    memory="on",
    description="Remove registry entries no machine answers to: directory hosts (identity.host."
                "delete, with DNS), SSH logins (exec.ssh.hosts.delete), mesh members (netsec.mesh."
                "leave). Dry run unless confirm=true: the plan is returned either way. Inputs: "
                "confirm (bool=false), kinds (list - limit to identity|host|mesh), ids (list - "
                "limit to these entry ids). Output: {dry_run, plan:[...], done:[{kind,id,label,cap,"
                "ok,error}]}.",
)
async def cap_registration_prune(confirm: bool = False, kinds: Optional[List[str]] = None,
                                 ids: Optional[List[str]] = None, trace_id=None) -> Dict[str, Any]:
    try:
        src = await _sources(True)
    except RuntimeError as e:
        return {"error": str(e)}
    plan = [p for p in core.stale(src, await _directory_addresses(src)) if p.get("action")]
    if kinds:
        plan = [p for p in plan if p.get("kind") in set(kinds)]
    if ids:
        plan = [p for p in plan if str(p.get("id")) in {str(i) for i in ids}]
    if not _flag(confirm):
        return {"dry_run": True, "plan": plan, "done": []}
    done = await _run_actions(plan)
    await emit_event({"type": "estate.registration.pruned", "removed": sum(1 for d in done if d["ok"]),
                      "failed": sum(1 for d in done if not d["ok"])})
    ent = _module_of("estate.entity.resolve") or {}
    if "_CACHE" in ent:
        ent["_CACHE"]["sources"] = None
    return {"dry_run": False, "plan": plan, "done": done}


@capability(
    "estate.registration.forget",
    http_method="POST", http_path="/estate/registration/forget", http_tags=["estate", "netsec"],
    memory="on",
    description="Remove one destroyed guest's registry entries: its SSH logins (by guest tag, "
                "pve:<vmid>@ label, name or address), its directory host (by name), its mesh "
                "membership. proxmox.guest.destroy calls this after the guest is gone. Inputs: "
                "vmid (int), name (str), addrs (list), confirm (bool=true). Output: {plan, done}.",
)
async def cap_registration_forget(vmid: int = 0, name: str = "", addrs: Optional[List[str]] = None,
                                  confirm: bool = True, trace_id=None) -> Dict[str, Any]:
    if not vmid and not name and not addrs:
        return {"error": "name the guest: vmid, name or addrs"}
    try:
        src = await _sources(True)
    except RuntimeError as e:
        return {"error": str(e)}
    plan = core.forget_plan(src, vmid=vmid or None, name=name, addrs=addrs)
    if not _flag(confirm):
        return {"dry_run": True, "plan": plan, "done": []}
    done = await _run_actions(plan)
    ent = _module_of("estate.entity.resolve") or {}
    if "_CACHE" in ent:
        ent["_CACHE"]["sources"] = None
    return {"dry_run": False, "plan": plan, "done": done}


@APP.get("/estate/registration/panel", include_in_schema=False)
async def _registration_panel():
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists()
                        else "<p style='color:red'>registration_panel.html not found</p>")


log.info("registration_capabilities ready - estate.registration, .prune, .forget")
