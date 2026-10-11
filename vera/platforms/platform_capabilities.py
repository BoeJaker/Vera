"""
platform_capabilities.py — one control surface for platform configuration
=========================================================================

Set a fact once, push it everywhere. Home and work coordinates, a timezone, an
API key — each lives in exactly one record, and every platform that needs it
holds a *reference* rather than a copy, so changing it in one place changes it
everywhere.

  platform.values.*   shared non-secret facts (home_coords, work_coords, …)
  platform.secrets.*  reusable credentials, kept in Vera's secrets service
                      (OpenBao, named secret platform/<key>), redacted on output
  platform.*          the targets themselves (Home Assistant, n8n, …)
  platform.apply      push resolved config INTO the target platform

`platform.apply` defaults to a dry run. It reports exactly what it would send
and requires `dry_run=false` to actually change anything — configuration pushes
are the one place in this module that reach outside Vera.

Wire format details live in `platform_core.py` (pure, unit-tested).

Redis layout
────────────
  vera:platform:values    hash  key -> JSON {key,label,value,updated}
  vera:platform:secrets   hash  key -> JSON {key,label,value(sealed),updated}
  vera:platform:targets   hash  id  -> JSON platform record
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.platforms import connections_core as cc
from Vera.vera.platforms import platform_core as pc
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.platforms")

_HERE = Path(__file__).parent
_PANEL = _HERE / "platform_panel.html"

KEY_VALUES = "vera:platform:values"
KEY_SECRETS = "vera:platform:secrets"
KEY_TARGETS = "vera:platform:targets"


def _redis():
    return getattr(_orch, "REDIS", None)


async def _hgetall(key: str) -> Dict[str, Dict[str, Any]]:
    r = _redis()
    if not r:
        return {}
    raw = await r.hgetall(key)
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        try:
            out[k] = json.loads(v)
        except Exception:
            continue
    return out


async def _hset(key: str, field: str, rec: Dict[str, Any]) -> None:
    r = _redis()
    if r:
        await r.hset(key, field, json.dumps(rec))


def _no_store() -> Dict[str, Any]:
    """Every write goes through this check.

    Without it `_hset` silently does nothing when Redis is absent and the cap
    still answers {"ok": true} — a configuration controller that cheerfully
    reports saving settings it has thrown away is worse than one that refuses.
    """
    return {"error": "configuration store unavailable (Redis is not reachable) "
                     "— nothing was saved"}


async def _values_plain() -> Dict[str, Any]:
    return {k: v.get("value", "") for k, v in (await _hgetall(KEY_VALUES)).items()}


def _secrets_service() -> Optional[Dict[str, Any]]:
    """The secrets service (named secrets in OpenBao), when it is loaded."""
    import inspect
    fn = (_orch.CAPABILITY_REGISTRY.get("secrets.status") or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


def _secret_path(key: str) -> str:
    return f"platform/{key}"


async def _secrets_plain() -> Dict[str, Any]:
    """Every credential's value, for resolving references. A record with a
    `path` lives in the secrets service; an older record carries its own
    sealed value."""
    out = {}
    svc = None
    for k, rec in (await _hgetall(KEY_SECRETS)).items():
        if rec.get("path"):
            svc = svc or _secrets_service()
            got = (await svc["get_named"](rec["path"])) if svc and svc.get("get_named") else None
            out[k] = (got or {}).get("value", "") or ""
            if not out[k]:
                log.warning("platform: credential %s could not be read from the secrets service", k)
            continue
        raw = rec.get("value", "")
        try:
            out[k] = vsecrets.open_secret(raw) if raw else ""
        except Exception:
            log.warning("platform: could not unseal secret %s", k)
            out[k] = ""
    return out


# In-process consumers of a credential that are not platform targets (Brave search, GitHub source pulls, intel's
# keyed sources...): key -> [{who, label, why}]. Filled by declare_secret_use; shown under "used by", and a declared
# key with no record yet is listed as wanted - so the one Platforms page shows every key Vera could use.
_CONSUMERS: Dict[str, List[Dict[str, str]]] = {}


def declare_secret_use(key: str, who: str, *, label: str = "", why: str = "") -> None:
    """A module says it reads credential `key` in-process (idempotent)."""
    k = str(key or "").strip()
    if not k or not who:
        return
    rows = _CONSUMERS.setdefault(k, [])
    if not any(r.get("who") == who for r in rows):
        rows.append({"who": str(who), "label": str(label or ""), "why": str(why or "")})


# In-process copy of the credentials, for modules whose readers are sync (open_value_sync): filled at startup and
# on every read and save here, so it never holds less than the store did when last asked.
_PLAIN: Dict[str, str] = {}


def secret_value_sync(key: str) -> str:
    k = str(key or "").strip()
    if k.startswith(pc.SECRET_REF):
        k = k[len(pc.SECRET_REF):].strip()
    return _PLAIN.get(k, "")


async def warm_secrets() -> int:
    try:
        vals = await _secrets_plain()
    except Exception as e:  # noqa: BLE001
        log.warning("platform: could not warm the credential copy: %s", e)
        return 0
    _PLAIN.update({k: v for k, v in vals.items() if v})
    return len(_PLAIN)


@APP.on_event("startup")
async def _warm_secrets_at_startup():
    async def later():
        for delay in (2, 10, 30, 120):              # the secrets service may come up after us
            await asyncio.sleep(delay)
            if await warm_secrets():
                return
    asyncio.create_task(later())


async def secret_value(key: str) -> str:
    """One credential's current value for in-process use, "" when unset or unreadable. Never exposed through a
    capability - /mcp/call is unauthenticated (secrets service rule: no cap returns a value)."""
    k = str(key or "").strip()
    if k.startswith(pc.SECRET_REF):
        k = k[len(pc.SECRET_REF):].strip()
    if not k:
        return ""
    rec = (await _hgetall(KEY_SECRETS)).get(k)
    if not rec:
        return ""
    if rec.get("path"):
        svc = _secrets_service()
        got = (await svc["get_named"](rec["path"])) if svc and svc.get("get_named") else None
        val = str((got or {}).get("value", "") or "")
    else:
        raw = rec.get("value", "")
        try:
            val = vsecrets.open_secret(raw) if raw else ""
        except Exception:
            log.warning("platform: could not unseal secret %s", k)
            val = ""
    if val:
        _PLAIN[k] = val
    return val


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED VALUES
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "platform.values.list", http_method="GET", http_path="/platform/values",
    http_tags=["platform"], memory="off",
    description="List the shared non-secret values (home_coords, work_coords, "
                "timezone, ...) that platforms reference with @value:<key>. "
                "Output: {values:[{key,label,value,used_by[]}], count}.",
)
async def cap_values_list(trace_id=None):
    vals = await _hgetall(KEY_VALUES)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    out = []
    for k, rec in sorted(vals.items()):
        out.append({**rec, "key": k,
                    "used_by": pc.referencing_platforms(targets, "value", k),
                    "ref": pc.VALUE_REF + k})
    return {"values": out, "count": len(out),
            "store_ok": bool(_redis())}


@capability(
    "platform.values.set", http_method="POST", http_path="/platform/values/set",
    http_tags=["platform"], memory="on",
    description="Create or update a shared value. Coordinate keys (anything "
                "ending _coords) are validated as 'lat,lon' and range-checked. "
                "Input: key (str!), value (str!), label (str). "
                "Output: {ok, key, value, used_by}.",
)
async def cap_values_set(key: str = "", value: str = "", label: str = "",
                         trace_id=None):
    if not _redis():
        return _no_store()
    k = pc.normalise_key(key)
    if not pc.valid_key(k):
        return {"error": "key must be lowercase letters, digits or underscores"}
    if value is None or value == "":
        return {"error": "value is required"}
    if k.endswith("_coords"):
        try:
            lat, lon = pc.parse_coords(value)
            value = pc.format_coords(lat, lon)
        except ValueError as e:
            return {"error": str(e)}
    rec = {"key": k, "label": label or k.replace("_", " ").title(),
           "value": value, "updated": now_iso()}
    await _hset(KEY_VALUES, k, rec)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    used = pc.referencing_platforms(targets, "value", k)
    await emit_event({"type": "platform.value.set", "key": k, "used_by": used})
    return {"ok": True, "key": k, "value": value, "used_by": used,
            "ref": pc.VALUE_REF + k}


@capability(
    "platform.values.delete", http_method="POST",
    http_path="/platform/values/delete", http_tags=["platform"], memory="on",
    description="Delete a shared value. REFUSES if any platform still "
                "references it, unless force=true — deleting would silently "
                "blank those fields. Input: key (str!), force (bool). "
                "Output: {ok, deleted} or {error, used_by}.",
)
async def cap_values_delete(key: str = "", force: bool = False, trace_id=None):
    if not _redis():
        return _no_store()
    k = pc.normalise_key(key)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    used = pc.referencing_platforms(targets, "value", k)
    if used and not force:
        return {"error": f"still referenced by: {', '.join(used)}. "
                         "Repoint those fields first, or pass force=true.",
                "used_by": used}
    r = _redis()
    if r:
        await r.hdel(KEY_VALUES, k)
    return {"ok": True, "deleted": k, "orphaned": used if force else []}


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED SECRETS
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "platform.secrets.list", http_method="GET", http_path="/platform/secrets",
    http_tags=["platform"], memory="off",
    description="List reusable credential records. Values are NEVER returned — "
                "only whether one is set. Output: {secrets:[{key,label,set,"
                "used_by[]}], count}.",
)
async def cap_secrets_list(trace_id=None):
    secs = await _hgetall(KEY_SECRETS)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    out = []
    for k, rec in sorted(secs.items()):
        out.append({"key": k, "label": rec.get("label", k),
                    "set": bool(rec.get("path") or rec.get("value")),
                    "store": rec.get("store") or ("secrets service" if rec.get("path") else "sealed inline"),
                    "path": rec.get("path", ""),
                    "updated": rec.get("updated", ""),
                    "used_by": pc.referencing_platforms(targets, "secret", k)
                               + [c["who"] for c in _CONSUMERS.get(k, [])],
                    "ref": pc.SECRET_REF + k})
    # keys a module reads but nobody has set yet: listed so the Platforms page shows what Vera could use
    for k, rows in sorted(_CONSUMERS.items()):
        if k in secs:
            continue
        first = rows[0] if rows else {}
        out.append({"key": k, "label": first.get("label") or k, "set": False, "store": "", "path": "",
                    "updated": "", "used_by": [c["who"] for c in rows], "ref": pc.SECRET_REF + k,
                    "wanted": True, "why": first.get("why", "")})
    # ...and keys a platform (an API connection, say) references that are not set
    listed = {r["key"] for r in out}
    for t in targets:
        for f, v in (t.get("fields") or {}).items():
            k = v[len(pc.SECRET_REF):].strip() if isinstance(v, str) and v.startswith(pc.SECRET_REF) else ""
            if k and k not in listed:
                listed.add(k)
                out.append({"key": k, "label": k.replace("_", " ").title(), "set": False, "store": "", "path": "",
                            "updated": "", "used_by": pc.referencing_platforms(targets, "secret", k),
                            "ref": pc.SECRET_REF + k, "wanted": True,
                            "why": f"{t.get('label') or t.get('id')} uses it ({f})"})
    return {"secrets": out, "count": len(out),
            "store_ok": bool(_redis())}


@capability(
    "platform.secrets.set", http_method="POST",
    http_path="/platform/secrets/set", http_tags=["platform"], memory="on",
    description="Create or update a reusable credential. Kept in Vera's secrets service (OpenBao, "
                "named secret platform/<key>; sealed inline only when that service is not "
                "available) and "
                "never echoed back. One record can be referenced by several "
                "platforms with @secret:<key>, or each can hold its own. "
                "Input: key (str!), value (str!), label (str). Output: {ok, key}.",
)
async def cap_secrets_set(key: str = "", value: str = "", label: str = "",
                          trace_id=None):
    if not _redis():
        return _no_store()
    k = pc.normalise_key(key)
    if not pc.valid_key(k):
        return {"error": "key must be lowercase letters, digits or underscores"}
    if not value:
        return {"error": "value is required"}
    rec, note = await _store_secret(k, value, label)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    return {"ok": True, "key": k, "ref": pc.SECRET_REF + k, "store": rec["store"], "note": note,
            "used_by": pc.referencing_platforms(targets, "secret", k)}


async def _store_secret(k: str, value: str, label: str = "") -> tuple:
    """Keep credential `k` in the secrets service (sealed inline when there is none) -> (record, note)."""
    rec = {"key": k, "label": label or k.replace("_", " ").title(), "updated": now_iso()}
    svc = _secrets_service()
    stored = {}
    if svc and svc.get("put_named"):
        stored = await svc["put_named"](_secret_path(k), {"value": value, "notes": f"Platforms credential: {rec['label']}"})
    if stored.get("ok"):
        rec.update(path=stored["path"], store="secrets service")
    else:
        # No secrets service here (a sandbox, or OpenBao down): seal it inline so
        # saving still works, and say where it went.
        rec.update(value=vsecrets.seal(value), store="sealed inline")
    await _hset(KEY_SECRETS, k, rec)
    _PLAIN[k] = value
    return rec, (stored.get("error", "") if not stored.get("ok") else "")


async def _vault_literals(rec: Dict[str, Any], dry: bool = False) -> List[str]:
    """Move a platform's literal secret fields into the store as <id>_<field> and leave @secret: references in their
    place (owner 2026-10-11: every credential lives in the store). Returns the keys moved (or that would be)."""
    fields = rec.get("fields") or {}
    moved = []
    for f in pc.secret_field_keys(rec.get("kind", "")):
        v = fields.get(f)
        if not isinstance(v, str) or not v.strip() or v.startswith((pc.SECRET_REF, pc.VALUE_REF)):
            continue
        key = pc.normalise_key(f"{rec.get('id', '')}_{f}")[:64]
        if not dry:
            await _store_secret(key, v.strip(), f"{rec.get('label') or rec.get('id')} {f.replace('_', ' ')}")
            fields[f] = pc.SECRET_REF + key
        moved.append(key)
    rec["fields"] = fields
    return moved


@capability(
    "platform.secrets.delete", http_method="POST",
    http_path="/platform/secrets/delete", http_tags=["platform"], memory="on",
    description="Delete a credential record. REFUSES if still referenced "
                "unless force=true. Input: key (str!), force (bool).",
)
async def cap_secrets_delete(key: str = "", force: bool = False, trace_id=None):
    if not _redis():
        return _no_store()
    k = pc.normalise_key(key)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    used = pc.referencing_platforms(targets, "secret", k)
    if used and not force:
        return {"error": f"still referenced by: {', '.join(used)}. "
                         "Repoint those fields first, or pass force=true.",
                "used_by": used}
    rec = (await _hgetall(KEY_SECRETS)).get(k) or {}
    removed_from_service = False
    if rec.get("path"):
        svc = _secrets_service()
        if svc and svc.get("delete_named"):
            res = await svc["delete_named"](rec["path"])
            if res.get("error"):
                return {"error": f"the secrets service did not delete {rec['path']}: {res['error']}"}
            removed_from_service = True
    r = _redis()
    if r:
        await r.hdel(KEY_SECRETS, k)
    _PLAIN.pop(k, None)
    return {"ok": True, "deleted": k, "orphaned": used if force else [],
            "removed_from_secrets_service": removed_from_service}


# ═════════════════════════════════════════════════════════════════════════════
#  PLATFORMS
# ═════════════════════════════════════════════════════════════════════════════

async def _resolved(pid: str) -> Optional[Dict[str, Any]]:
    rec = (await _hgetall(KEY_TARGETS)).get(pid)
    if not rec:
        return None
    res = pc.resolve_fields(rec.get("fields") or {},
                            await _values_plain(), await _secrets_plain())
    return {"record": rec, **res}


@capability(
    "platform.kinds", http_method="GET", http_path="/platform/kinds",
    http_tags=["platform"], memory="off",
    description="The platform types this controller knows how to configure, "
                "with their field schemas and available apply actions. "
                "Output: {kinds:[{kind,label,icon,fields[],actions[]}]}.",
)
async def cap_kinds(trace_id=None):
    return {"kinds": [{"kind": k, "label": s["label"], "icon": s.get("icon", ""),
                       "docs": s.get("docs", ""), "fields": s["fields"],
                       "actions": s.get("actions", []), "connection": bool(s.get("connection"))}
                      for k, s in sorted(pc.PLATFORM_SPECS.items())]}


@capability(
    "platform.list", http_method="GET", http_path="/platform/list",
    http_tags=["platform"], memory="off",
    description="List configured platforms with their resolved status. Secret "
                "fields are redacted. Output: {platforms:[{id,kind,label,"
                "configured,missing_required,unresolved_refs}], count}.",
)
async def cap_list(trace_id=None):
    targets = await _hgetall(KEY_TARGETS)
    vals, secs = await _values_plain(), await _secrets_plain()
    out = []
    for pid, rec in sorted(targets.items()):
        res = pc.resolve_fields(rec.get("fields") or {}, vals, secs)
        comp = pc.config_completeness(rec.get("kind", ""), res["resolved"])
        out.append({"id": pid, "kind": rec.get("kind", ""),
                    "label": rec.get("label", pid),
                    "enabled": rec.get("enabled", True),
                    "fields": pc.redact_fields(rec.get("fields") or {},
                                               res["secret_fields"]),
                    "secret_refs": {f: (rec.get("fields") or {}).get(f, "") for f in res["secret_fields"]},
                    "unresolved_refs": res["missing"], **comp})
    return {"platforms": out, "count": len(out),
            "store_ok": bool(_redis())}


@capability(
    "platform.upsert", http_method="POST", http_path="/platform/upsert",
    http_tags=["platform"], memory="on",
    description="Create or update a platform. On create, fields are pre-wired "
                "to the shared values. Field values may be literals or "
                "references (@value:<key> / @secret:<key>). "
                "Input: kind (str! on create), id (str), label (str), "
                "fields (dict), enabled (bool). Output: {ok, platform}.",
)
async def cap_upsert(kind: str = "", id: str = "", label: str = "",
                     fields: Optional[Dict[str, Any]] = None,
                     enabled: Optional[bool] = None, trace_id=None):
    if not _redis():
        return _no_store()
    targets = await _hgetall(KEY_TARGETS)
    pid = pc.normalise_key(id or kind)
    rec = targets.get(pid)
    if not rec:
        if not kind:
            return {"error": "kind is required to create a platform "
                             "(see platform.kinds)"}
        try:
            rec = pc.new_platform(kind, pid, label)
        except ValueError as e:
            return {"error": str(e)}
    if label:
        rec["label"] = label
    if enabled is not None:
        rec["enabled"] = bool(enabled)
    if fields:
        merged = dict(rec.get("fields") or {})
        # An empty string means "leave as is" so a partial save from the panel
        # cannot wipe a field the form did not re-send.
        for k, v in fields.items():
            if v != "":
                merged[k] = v
        rec["fields"] = merged
    vaulted = await _vault_literals(rec)
    rec["updated"] = now_iso()
    await _hset(KEY_TARGETS, pid, rec)

    res = pc.resolve_fields(rec.get("fields") or {},
                            await _values_plain(), await _secrets_plain())
    comp = pc.config_completeness(rec.get("kind", ""), res["resolved"])
    return {"ok": True, "vaulted": vaulted, "platform": {
        **rec, "fields": pc.redact_fields(rec["fields"], res["secret_fields"]),
        "secret_refs": {f: rec["fields"].get(f, "") for f in res["secret_fields"]},
        "unresolved_refs": res["missing"], **comp}}


@capability(
    "platform.delete", http_method="POST", http_path="/platform/delete",
    http_tags=["platform"], memory="on",
    description="Delete a platform record. Shared values and secrets are left "
                "alone. Input: id (str!). Output: {ok, deleted}.",
)
async def cap_delete(id: str = "", trace_id=None):
    if not _redis():
        return _no_store()
    r = _redis()
    if r:
        await r.hdel(KEY_TARGETS, pc.normalise_key(id))
    return {"ok": True, "deleted": pc.normalise_key(id)}


def _platform_access_mod():
    try:
        from Vera.vera.platforms import platform_access as _pa
    except ImportError:
        from vera.platforms import platform_access as _pa
    return _pa


@capability(
    "platform.secrets.adopt", http_method="POST", http_path="/platform/secrets/adopt",
    http_tags=["platform"], memory="on",
    description="Move the credentials other modules still seal in their own records (web APIs, MCP servers, "
                "integrations, Home Assistant, n8n, LLM providers, accounts, Telegram, Proxmox, Portainer, "
                "Prometheus, SSH hosts...) into this credential store, leaving @secret: references - every "
                "credential in one store. DRY RUN BY DEFAULT: lists what would move. Input: store (str, one "
                "module's name; empty = all), dry_run (bool=True). Output: {ok, dry_run, stores:{name: {moved:"
                "[{record, field, key}], error?}}, available[]}.",
)
async def cap_secrets_adopt(store: str = "", dry_run: bool = True, trace_id=None):
    pa = _platform_access_mod()
    adopters = dict(getattr(pa, "_ADOPTERS", {}) or {})
    names = [store] if store else sorted(adopters)
    out: Dict[str, Any] = {}
    for name in names:
        fn = adopters.get(name)
        if fn is None:
            out[name] = {"error": "no such store (see available)"}
            continue
        try:
            out[name] = {"moved": await fn(bool(dry_run))}
        except Exception as e:  # noqa: BLE001
            log.warning("platform: adopting %s: %s", name, e)
            out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return {"ok": True, "dry_run": bool(dry_run), "stores": out, "available": sorted(adopters)}


@capability(
    "platform.secrets.vault_literals", http_method="POST", http_path="/platform/secrets/vault_literals",
    http_tags=["platform"], memory="on",
    description="Move every platform's literal secret field (a token typed straight into a record) into the "
                "credential store as <platform>_<field>, leaving an @secret: reference in its place. DRY RUN BY "
                "DEFAULT. Input: dry_run (bool=True). Output: {ok, dry_run, moved:[{platform, keys[]}]}.",
)
async def cap_secrets_vault_literals(dry_run: bool = True, trace_id=None):
    if not _redis():
        return _no_store()
    moved = []
    for pid, rec in sorted((await _hgetall(KEY_TARGETS)).items()):
        rec.setdefault("id", pid)
        keys = await _vault_literals(rec, dry=dry_run)
        if keys:
            moved.append({"platform": pid, "keys": keys})
            if not dry_run:
                rec["updated"] = now_iso()
                await _hset(KEY_TARGETS, pid, rec)
    return {"ok": True, "dry_run": dry_run, "moved": moved}


# ═════════════════════════════════════════════════════════════════════════════
#  API CONNECTIONS  —  platforms of kind "api" (data-connections plan, 2026-10-11)
# ═════════════════════════════════════════════════════════════════════════════
# One record per API: base URL, auth, and the credential as an @secret: reference. Fabric sources, intel and the
# rest reach an API through connection_get, so a key lives in one place and goes only to its own host.

_RATE: Dict[str, List[float]] = {}


def _connections(targets: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {pid: t for pid, t in targets.items()
            if (pc.platform_spec(t.get("kind", "")) or {}).get("connection")}


async def _rate_wait(pid: str, per_min: Any) -> None:
    try:
        n = int(float(per_min or 0))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return
    now = time.monotonic()
    win = [t for t in _RATE.get(pid, []) if now - t < 60]
    if len(win) >= n:
        await asyncio.sleep(min(60.0, 60 - (now - win[0])))
        now = time.monotonic()
        win = [t for t in win if now - t < 60]
    win.append(now)
    _RATE[pid] = win


async def connection_request(pid: str, path: str = "", params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The request for `path` through connection `pid`, its credential resolved - in-process only, never returned
    by a capability. {url, headers, auth, shown_url, fields} or {error}."""
    got = await _resolved(pc.normalise_key(pid))
    if not got:
        return {"error": f"no connection {pid!r} (Platforms > Connections)"}
    if not (pc.platform_spec(got["record"].get("kind", "")) or {}).get("connection"):
        return {"error": f"platform {pid!r} is not an API connection"}
    if got["record"].get("enabled") is False:
        return {"error": f"connection {pid!r} is disabled"}
    fields = got["resolved"]
    req = cc.build_request(fields, cc.expand_path(path), params)
    if req.get("error"):
        missing = [m["key"] for m in got["missing"] if m.get("kind") == "secret"]
        if missing:
            req["error"] += f" (credential {', '.join(missing)} is not set - Platforms > Shared credentials)"
        return req
    return {**req, "shown_url": cc.redact_url(req["url"], fields), "fields": fields}


async def connection_info(pid: str, path: str = "") -> Dict[str, Any]:
    """A connection without its credential - for a source to show where it reads from."""
    pid = pc.normalise_key(pid)
    rec = (await _hgetall(KEY_TARGETS)).get(pid)
    if not rec or not (pc.platform_spec(rec.get("kind", "")) or {}).get("connection"):
        return {"error": f"no API connection {pid!r} (Platforms > Connections)"}
    fields = pc.resolve_fields(rec.get("fields") or {}, await _values_plain(), {})["resolved"]
    shown = cc.build_request({**fields, "auth": "none", "credential": ""}, cc.expand_path(path))
    return {"id": pid, "label": rec.get("label", pid), "base_url": fields.get("base_url", ""),
            "auth": cc.describe(fields), "url": shown.get("url", ""), "error_url": shown.get("error", ""),
            "enabled": rec.get("enabled", True) is not False}


async def connection_get(pid: str, path: str = "", params: Optional[Dict[str, Any]] = None, *,
                         timeout: float = 30, accept: str = "") -> Dict[str, Any]:
    """GET through a connection: {response, shown_url, ms} or {error}. Redirects are followed only on the
    connection's own host, so the credential never follows one elsewhere; its rate limit is kept."""
    req = await connection_request(pid, path, params)
    if req.get("error"):
        return req
    await _rate_wait(pc.normalise_key(pid), req["fields"].get("rate_per_min"))
    headers = dict(req["headers"])
    if accept and "Accept" not in headers:
        headers["Accept"] = accept
    headers.setdefault("User-Agent", "Vera/1.0 (+data fabric)")
    host = (httpx.URL(req["url"]).host or "").lower()
    url, t0 = req["url"], time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            for _ in range(4):
                r = await c.get(url, headers=headers, auth=req["auth"])
                if r.status_code not in (301, 302, 303, 307, 308) or "location" not in r.headers:
                    break
                nxt = r.url.join(r.headers["location"])
                if (nxt.host or "").lower() != host:
                    return {"error": f"the API redirected to another host ({nxt.host}); not followed with the credential",
                            "shown_url": req["shown_url"]}
                url = str(nxt)
    except httpx.HTTPError as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "shown_url": req["shown_url"]}
    return {"response": r, "shown_url": req["shown_url"], "ms": int((time.monotonic() - t0) * 1000)}


def _shape(data: Any) -> Dict[str, Any]:
    """What came back, in outline: top-level keys and the largest list (where the items are, usually)."""
    if isinstance(data, list):
        return {"type": "list", "items": len(data)}
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    lists = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    for k, v in data.items():                                # one level down: {"response": {"results": [...]}}
        if isinstance(v, dict):
            lists.update({f"{k}.{k2}": len(v2) for k2, v2 in v.items() if isinstance(v2, list)})
    best = max(lists, key=lists.get) if lists else ""
    return {"type": "object", "keys": list(data)[:20], "item_path": best, "items": lists.get(best, 0)}


@capability(
    "platform.connection.templates", http_method="GET", http_path="/platform/connection/templates",
    http_tags=["platform"], memory="off",
    description="Known APIs that can be added as a connection in one step (Brave, GitHub, Guardian, Companies House, "
                "EPC, Hacker News, Polymarket, Manifold, Wikidata, Nominatim), with whether each is already added and "
                "whether its credential is set. Output: {templates:[{id,label,base_url,auth,key,key_set,added,"
                "docs,signup,tags}]}.",
)
async def cap_connection_templates(trace_id=None):
    targets = await _hgetall(KEY_TARGETS)
    secs = await _hgetall(KEY_SECRETS)
    conns = _connections(targets)
    by_base = {str((t.get("fields") or {}).get("base_url", "")).rstrip("/"): pid for pid, t in conns.items()}
    out = []
    for t in cc.TEMPLATES:
        added = t["id"] if t["id"] in conns else by_base.get(t["base_url"].rstrip("/"), "")
        srec = secs.get(t["key"]) or {} if t["key"] else {}
        out.append({**{k: t.get(k, "") for k in ("id", "label", "base_url", "auth", "key", "docs", "signup",
                                                  "sample_path", "username_hint")},
                    "tags": t.get("tags", []), "auth_desc": cc.describe(t),
                    "key_set": bool(srec.get("path") or srec.get("value")),
                    "added": added})
    return {"templates": out, "count": len(out)}


@capability(
    "platform.connection.add", http_method="POST", http_path="/platform/connection/add",
    http_tags=["platform"], memory="on",
    description="Add an API connection, from a template (template=guardian) or by hand (base_url + auth). The "
                "credential is never stored in the connection: a template points at its named credential, and a "
                "value given here goes into the credential store as <id>_credential. "
                "Input: template (str), id (str), label (str), base_url (str), auth (str: none | bearer | "
                "header:<Name> | query:<param> | basic), credential (str: a value or @secret:<key>), username (str), "
                "headers (str JSON), sample_path (str), rate_per_min (int). Output: {ok, platform, vaulted}.",
)
async def cap_connection_add(template: str = "", id: str = "", label: str = "", base_url: str = "",
                             auth: str = "", credential: str = "", username: str = "", headers: str = "",
                             sample_path: str = "", rate_per_min: Any = "", trace_id=None):
    if not _redis():
        return _no_store()
    t = cc.template(template) if template else None
    if template and not t:
        return {"error": f"unknown template {template!r} (platform.connection.templates)"}
    fields = cc.fields_from_template(t) if t else {"auth": "none"}
    given = {"base_url": base_url, "auth": auth, "credential": credential, "username": username,
             "headers": headers, "sample_path": sample_path, "rate_per_min": str(rate_per_min or "")}
    fields.update({k: str(v).strip() for k, v in given.items() if str(v or "").strip()})
    if not fields.get("base_url"):
        return {"error": "base_url is required (or pick a template)"}
    if fields.get("headers"):
        try:
            if not isinstance(json.loads(fields["headers"]), dict):
                raise ValueError
        except ValueError:
            return {"error": "headers must be a JSON object, e.g. {\"Accept\": \"application/json\"}"}
    probe = cc.build_request({**fields, "credential": "x"}, "")
    if probe.get("error"):
        return {"error": probe["error"]}
    labels = (httpx.URL(fields["base_url"]).host or "").split(".")
    pid = pc.normalise_key(id or (t["id"] if t else "") or (labels[-2] if len(labels) > 1 else labels[0]))
    if not pc.valid_key(pid):
        return {"error": "id must be lowercase letters, digits or underscores"}
    if pid in await _hgetall(KEY_TARGETS):
        return {"error": f"a platform {pid!r} already exists - edit it, or give another id"}
    return await cap_upsert(kind="api", id=pid, label=label or (t["label"] if t else pid), fields=fields)


@capability(
    "platform.connection.test", http_method="POST", http_path="/platform/connection/test",
    http_tags=["platform"], memory="off",
    description="Make one GET through an API connection (its sample path, or `path`) and say what came back: "
                "status, time, content type and the shape of a JSON answer (top-level keys, where the item list "
                "is). The credential is never shown. Input: id (str!), path (str). Output: {ok, status, ms, "
                "content_type, bytes, url, shape, preview}.",
)
async def cap_connection_test(id: str = "", path: str = "", trace_id=None):
    pid = pc.normalise_key(id)
    rec = (await _hgetall(KEY_TARGETS)).get(pid) or {}
    p = path or (rec.get("fields") or {}).get("sample_path", "")
    got = await connection_get(pid, p, timeout=20)
    if got.get("error"):
        return {"ok": False, "id": pid, "error": got["error"], "url": got.get("shown_url", "")}
    r = got["response"]
    ctype = r.headers.get("content-type", "").split(";")[0]
    text = r.text or ""
    req = await connection_request(pid, p)
    cred = str((req.get("fields") or {}).get("credential") or "")
    if cred and len(cred) >= 4:
        text = text.replace(cred, "***")                     # an API that echoes the key back
    shape: Dict[str, Any] = {}
    if "json" in ctype or text[:1] in "[{":
        try:
            shape = _shape(json.loads(text))
        except ValueError:
            pass
    return {"ok": r.status_code < 400, "id": pid, "status": r.status_code, "ms": got["ms"],
            "content_type": ctype, "bytes": len(r.content), "url": got["shown_url"], "shape": shape,
            "preview": text[:600]}


# ═════════════════════════════════════════════════════════════════════════════
#  APPLY  —  the only part that reaches outside Vera
# ═════════════════════════════════════════════════════════════════════════════

async def _ha_post(base: str, token: str, path: str,
                   body: Optional[Dict] = None) -> Dict[str, Any]:
    url = base.rstrip("/") + path
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(verify=False, timeout=45,
                                 follow_redirects=True) as c:
        r = await c.post(url, headers=headers,
                         content=json.dumps(body or {}))
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:250]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:250]}


async def _apply_ha_location(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    lat, lon = pc.parse_coords(cfg.get("home_coords") or "")
    body = pc.ha_core_config_payload(
        lat, lon, timezone=cfg.get("timezone") or "")
    if dry:
        return {"action": "ha.core_location", "would_post":
                "/api/config/core/update", "body": body}
    out = await _ha_post(cfg["base_url"], cfg["token"],
                         "/api/config/core/update", body)
    return {"action": "ha.core_location", "applied": True, "result": out}


async def _apply_ha_waze(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    """Create the two Waze Travel Time entries via HA's config-entry flow.

    Doing this by hand is fiddly: the flow is two steps, and the origin and
    destination fields silently reject shapes other than raw 'lat,lon'.
    """
    pairs = pc.waze_pair(cfg.get("home_coords") or "",
                         cfg.get("work_coords") or "")
    region = (cfg.get("waze_region") or "gb").lower()
    for p in pairs:
        p["region"] = region
    if dry:
        return {"action": "ha.waze_travel_time",
                "would_create": len(pairs), "entries": pairs,
                "flow": "POST /api/config/config_entries/flow "
                        "{handler: waze_travel_time} then POST the step data"}

    created, failed = [], []
    for p in pairs:
        try:
            flow = await _ha_post(cfg["base_url"], cfg["token"],
                                  "/api/config/config_entries/flow",
                                  {"handler": "waze_travel_time",
                                   "show_advanced_options": False})
            fid = flow.get("flow_id")
            if not fid:
                raise RuntimeError(f"no flow_id in response: "
                                   f"{json.dumps(flow)[:200]}")
            res = await _ha_post(cfg["base_url"], cfg["token"],
                                 f"/api/config/config_entries/flow/{fid}", p)
            if res.get("type") == "create_entry" or res.get("result"):
                created.append(p.get("name") or p["origin"])
            else:
                failed.append({"entry": p.get("name"),
                               "detail": json.dumps(res)[:250]})
        except Exception as e:
            failed.append({"entry": p.get("name"), "detail": str(e)[:250]})
    return {"action": "ha.waze_travel_time", "applied": True,
            "created": created, "failed": failed}


async def _apply_n8n_ping(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/api/v1/workflows?limit=1"
    if dry:
        return {"action": "n8n.ping", "would_get": url}
    async with httpx.AsyncClient(verify=False, timeout=30) as c:
        r = await c.get(url, headers={"X-N8N-API-KEY": cfg.get("api_key", "")})
    return {"action": "n8n.ping", "applied": True, "status": r.status_code,
            "ok": r.status_code < 400}


_ACTIONS = {
    "ha.core_location": _apply_ha_location,
    "ha.waze_travel_time": _apply_ha_waze,
    "n8n.ping": _apply_n8n_ping,
}


@capability(
    "platform.apply", http_method="POST", http_path="/platform/apply",
    http_tags=["platform"], memory="on",
    description="Push resolved configuration INTO a platform. DRY RUN BY "
                "DEFAULT: shows exactly what would be sent and changes nothing "
                "until dry_run=false. Actions: ha.core_location (set HA's home "
                "latitude/longitude/timezone), ha.waze_travel_time (create the "
                "home-to-work and work-to-home commute sensors), n8n.ping. "
                "Input: id (str!), action (str!), dry_run (bool=True). "
                "Output: {ok, dry_run, result}.",
)
async def cap_apply(id: str = "", action: str = "", dry_run: bool = True,
                    trace_id=None):
    got = await _resolved(pc.normalise_key(id))
    if not got:
        return {"error": f"unknown platform: {id}"}
    rec, cfg = got["record"], got["resolved"]

    if got["missing"]:
        refs = ", ".join(f"{m['field']} -> @{m['kind']}:{m['key']}"
                         for m in got["missing"])
        return {"error": f"unresolved references: {refs}"}
    comp = pc.config_completeness(rec.get("kind", ""), cfg)
    if not comp["configured"]:
        return {"error": "missing required config: "
                         + ", ".join(comp["missing_required"])}

    fn = _ACTIONS.get(action)
    if not fn:
        allowed = (pc.platform_spec(rec.get("kind", "")) or {}).get("actions", [])
        return {"error": f"unknown action {action!r}. "
                         f"Available for {rec.get('kind')}: {', '.join(allowed)}"}
    try:
        result = await fn(cfg, dry_run)
    except Exception as e:
        return {"error": str(e)[:400], "action": action}
    if not dry_run:
        await emit_event({"type": "platform.applied", "platform": rec["id"],
                          "action": action})
    return {"ok": True, "dry_run": dry_run, "platform": rec["id"],
            "result": result}


@capability(
    "platform.seed", http_method="POST", http_path="/platform/seed",
    http_tags=["platform"], memory="on",
    description="Create blank records for every known platform kind that is "
                "not configured yet, pre-wired to the shared values. Existing "
                "records are left untouched. Output: {ok, created[]}.",
)
async def cap_seed(trace_id=None):
    if not _redis():
        return _no_store()
    targets = await _hgetall(KEY_TARGETS)
    created = []
    for kind, spec in pc.PLATFORM_SPECS.items():
        if kind in targets or spec.get("connection"):      # API connections come from platform.connection.add
            continue
        await _hset(KEY_TARGETS, kind, pc.new_platform(kind))
        created.append(kind)
    return {"ok": True, "created": created,
            "note": "fill in base_url and the credential, then platform.apply"}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "platform.panel.html", http_method="GET", http_path="/platform/panel",
    http_tags=["platform", "ui"], memory="off", silent=True,
    description="Serve the platform configuration panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        return HTMLResponse(_PANEL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<p style='color:red'>platform_panel.html missing</p>")


@APP.get("/platform/panel", include_in_schema=False)
async def _platform_panel_route():
    return HTMLResponse(_PANEL.read_text(encoding="utf-8")
                        if _PANEL.exists()
                        else "<p style='color:red'>platform_panel.html missing</p>")


register_ui(
    "platform-config", "Platforms", "⌸",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/platform/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["platform.kinds", "platform.list", "platform.upsert",
             "platform.delete", "platform.apply", "platform.seed",
             "platform.values.list", "platform.values.set",
             "platform.values.delete", "platform.secrets.list",
             "platform.secrets.set", "platform.secrets.delete", "platform.secrets.vault_literals",
             "platform.secrets.adopt",
             "platform.connection.templates", "platform.connection.add", "platform.connection.test",
             "fabric.api.map", "fabric.sources.add", "fabric.sources.connections", "web.api.list"],
    mode="tab",
    tab_order=70,
)

log.info("platform_capabilities: ready — %d platform kinds",
         len(pc.PLATFORM_SPECS))
