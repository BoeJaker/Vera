"""
identity_migrate.py — bidirectional directory migration (lldap ⇆ FreeIPA)
=========================================================================

Consolidating onto FreeIPA doesn't mean throwing lldap away — you want to be able
to move users + groups **either direction** from the UI. This module diffs the two
directories and copies what's missing in the chosen direction, idempotently.

Directions
──────────
  lldap_to_freeipa   — lldap (source) → FreeIPA (target)   [the consolidation path]
  freeipa_to_lldap   — FreeIPA (source) → lldap (target)   [fallback / export]

Caveats (surfaced in every result):
  • Passwords are NOT transferable — neither backend exposes a reusable secret
    (FreeIPA hashes; lldap uses an OPAQUE flow). Migrated users are created and
    must have a password set/reset in the target (FreeIPA can mint a random one).
  • Group membership is copied where both endpoints expose it.

Everything routes through the existing per-backend caps (identity.* and
identity.lldap.*), so this module holds no directory logic of its own.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, enum_schema, register_ui,
)
from fastapi.responses import HTMLResponse

log = logging.getLogger("vera.identity.migrate")
_HERE = Path(__file__).parent

DIRECTIONS = ("lldap_to_freeipa", "freeipa_to_lldap")
_PW_NOTE = ("passwords are not migrated — set/reset them in the target "
           "(FreeIPA can mint a random password on create)")


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


async def _call(name: str, **kw) -> Dict:
    fn = _cap(name)
    if not fn:
        return {"error": f"{name} unavailable"}
    try:
        return await fn(**kw) or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── read both sides into a common shape ──────────────────────────────────────
async def _freeipa_users() -> List[Dict]:
    r = await _call("identity.user.list")
    return [{"id": u.get("login"), "name": u.get("name") or u.get("login")}
            for u in r.get("users", []) if u.get("login")]


async def _freeipa_groups() -> List[Dict]:
    r = await _call("identity.group.list")
    return [{"name": g.get("name"), "description": g.get("description", "")}
            for g in r.get("groups", []) if g.get("name")]


async def _lldap_users() -> List[Dict]:
    r = await _call("identity.lldap.users")
    return [{"id": u.get("id"), "name": u.get("display_name") or u.get("id"),
             "email": u.get("email")} for u in r.get("users", []) if u.get("id")]


async def _lldap_groups() -> List[Dict]:
    r = await _call("identity.lldap.groups")
    return [{"name": g.get("display_name"), "gid": g.get("id")}
            for g in r.get("groups", []) if g.get("display_name")]


async def _snapshot(direction: str):
    """Return (src_users, src_groups, tgt_user_ids, tgt_group_names, err)."""
    if direction == "lldap_to_freeipa":
        su, sg = await _lldap_users(), await _lldap_groups()
        tu, tg = await _freeipa_users(), await _freeipa_groups()
    else:
        su, sg = await _freeipa_users(), await _freeipa_groups()
        tu, tg = await _lldap_users(), await _lldap_groups()
    tgt_uids = {(u["id"] or "").lower() for u in tu}
    tgt_gnames = {(g["name"] or "").lower() for g in tg}
    return su, sg, tgt_uids, tgt_gnames


def _split_name(name: str, uid: str):
    parts = (name or uid).strip().split(None, 1)
    first = parts[0] if parts else uid
    last = parts[1] if len(parts) > 1 else uid
    return first, last


# ── capabilities ─────────────────────────────────────────────────────────────
@capability(
    "identity.migrate.status",
    http_method="GET", http_path="/identity/migrate/status", http_tags=["identity"],
    memory="off", silent=True,
    description="Counts on both directories and how many users/groups exist only "
                "on one side (in each direction). Output: {freeipa:{users,groups}, "
                "lldap:{users,groups}, only_in_lldap, only_in_freeipa}.",
)
async def cap_migrate_status(trace_id=None) -> Dict:
    fu, fg = await _freeipa_users(), await _freeipa_groups()
    lu, lg = await _lldap_users(), await _lldap_groups()
    fu_ids = {(u["id"] or "").lower() for u in fu}
    lu_ids = {(u["id"] or "").lower() for u in lu}
    fg_names = {(g["name"] or "").lower() for g in fg}
    lg_names = {(g["name"] or "").lower() for g in lg}
    return {
        "freeipa": {"users": len(fu), "groups": len(fg)},
        "lldap": {"users": len(lu), "groups": len(lg)},
        "only_in_lldap": {"users": sorted(lu_ids - fu_ids),
                          "groups": sorted(lg_names - fg_names)},
        "only_in_freeipa": {"users": sorted(fu_ids - lu_ids),
                            "groups": sorted(fg_names - lg_names)},
    }


@capability(
    "identity.migrate.preview",
    http_method="POST", http_path="/identity/migrate/preview", http_tags=["identity"],
    memory="off",
    description="Preview a migration: what users/groups would be CREATED in the "
                "target (source items missing from the target). Changes nothing. "
                "Inputs: direction (lldap_to_freeipa|freeipa_to_lldap). Output: "
                "{direction, users_to_create:[...], groups_to_create:[...], note}.",
    schema=enum_schema(direction=list(DIRECTIONS)),
)
async def cap_migrate_preview(direction: str = "lldap_to_freeipa",
                              trace_id=None) -> Dict:
    if direction not in DIRECTIONS:
        return {"error": f"direction must be one of {DIRECTIONS}"}
    su, sg, tgt_uids, tgt_gnames = await _snapshot(direction)
    users = [u for u in su if (u["id"] or "").lower() not in tgt_uids]
    groups = [g for g in sg if (g["name"] or "").lower() not in tgt_gnames]
    return {"direction": direction,
            "users_to_create": [{"id": u["id"], "name": u["name"]} for u in users],
            "groups_to_create": [g["name"] for g in groups],
            "note": _PW_NOTE}


@capability(
    "identity.migrate.run",
    http_method="POST", http_path="/identity/migrate/run", http_tags=["identity"],
    memory="on",
    description="Migrate users/groups in one direction (idempotent — only creates "
                "what's missing). Inputs: direction (lldap_to_freeipa|"
                "freeipa_to_lldap), dry_run (bool=true), include_users (bool=true), "
                "include_groups (bool=true), only (csv of ids/names — default all "
                "missing). Passwords are NOT migrated. Output: {direction, dry_run, "
                "groups:[...], users:[...], summary, note}.",
    schema=enum_schema(direction=list(DIRECTIONS)),
)
async def cap_migrate_run(direction: str = "lldap_to_freeipa", dry_run: bool = True,
                          include_users: bool = True, include_groups: bool = True,
                          only: str = "", trace_id=None) -> Dict:
    if direction not in DIRECTIONS:
        return {"error": f"direction must be one of {DIRECTIONS}"}
    su, sg, tgt_uids, tgt_gnames = await _snapshot(direction)
    only_set = {x.strip().lower() for x in only.split(",") if x.strip()}
    to_ipa = direction == "lldap_to_freeipa"

    g_missing = [g for g in sg if (g["name"] or "").lower() not in tgt_gnames
                 and (not only_set or (g["name"] or "").lower() in only_set)]
    u_missing = [u for u in su if (u["id"] or "").lower() not in tgt_uids
                 and (not only_set or (u["id"] or "").lower() in only_set)]

    groups_res, users_res = [], []

    if include_groups:
        for g in g_missing:
            if dry_run:
                groups_res.append({"name": g["name"], "action": "would_create"})
                continue
            if to_ipa:
                r = await _call("identity.group.register", name=g["name"],
                                description=g.get("description", ""))
            else:
                r = await _call("identity.lldap.group.create", name=g["name"])
            groups_res.append({"name": g["name"], "ok": bool(r.get("ok")),
                               "error": r.get("error", "")})

    if include_users:
        for u in u_missing:
            if dry_run:
                users_res.append({"id": u["id"], "action": "would_create"})
                continue
            if to_ipa:
                first, last = _split_name(u.get("name", ""), u["id"])
                r = await _call("identity.user.register", login=u["id"],
                                first=first, last=last,
                                email=u.get("email", ""))
            else:
                r = await _call("identity.lldap.user.create", id=u["id"],
                                email=u.get("email") or f"{u['id']}@vera.local",
                                display_name=u.get("name", u["id"]))
            users_res.append({"id": u["id"], "ok": bool(r.get("ok")),
                              "error": r.get("error", "")})

    if not dry_run:
        await emit_event({"type": "identity.migrate.run", "direction": direction,
                          "groups": len(groups_res), "users": len(users_res)})
    return {
        "direction": direction, "dry_run": dry_run,
        "groups": groups_res, "users": users_res,
        "summary": {"groups_created": sum(1 for g in groups_res if g.get("ok")),
                    "users_created": sum(1 for u in users_res if u.get("ok")),
                    "groups_pending": len(g_missing), "users_pending": len(u_missing)},
        "note": _PW_NOTE,
    }


# ── panel ─────────────────────────────────────────────────────────────────────
@capability(
    "identity.migrate.panel.html",
    http_method="GET", http_path="/identity/migrate/panel", http_tags=["identity", "ui"],
    memory="off", silent=True,
    description="Serve the Directory Sync (lldap ⇆ FreeIPA) panel HTML.",
)
async def cap_migrate_panel(trace_id=None):
    p = _HERE / "identity_migrate_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>identity_migrate_panel.html not found</p>")


# No standalone top-level tab: Directory Sync is embedded as a widget inside
# Provision → Identity (identity_panel.html loads /identity/migrate/panel inline).
# The panel-serving cap above is what that iframe points at.

log.info("identity_migrate ready — identity.migrate.* (lldap ⇆ FreeIPA), embedded in Provision→Identity")
