"""
platform_capabilities.py — one control surface for platform configuration
=========================================================================

Set a fact once, push it everywhere. Home and work coordinates, a timezone, an
API key — each lives in exactly one record, and every platform that needs it
holds a *reference* rather than a copy, so changing it in one place changes it
everywhere.

  platform.values.*   shared non-secret facts (home_coords, work_coords, …)
  platform.secrets.*  reusable credentials, sealed at rest, redacted on output
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

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
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


async def _secrets_plain() -> Dict[str, Any]:
    out = {}
    for k, rec in (await _hgetall(KEY_SECRETS)).items():
        raw = rec.get("value", "")
        try:
            out[k] = vsecrets.open_secret(raw) if raw else ""
        except Exception:
            log.warning("platform: could not unseal secret %s", k)
            out[k] = ""
    return out


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
                    "set": bool(rec.get("value")),
                    "updated": rec.get("updated", ""),
                    "used_by": pc.referencing_platforms(targets, "secret", k),
                    "ref": pc.SECRET_REF + k})
    return {"secrets": out, "count": len(out),
            "store_ok": bool(_redis())}


@capability(
    "platform.secrets.set", http_method="POST",
    http_path="/platform/secrets/set", http_tags=["platform"], memory="on",
    description="Create or update a reusable credential. Sealed at rest and "
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
    rec = {"key": k, "label": label or k.replace("_", " ").title(),
           "value": vsecrets.seal(value), "updated": now_iso()}
    await _hset(KEY_SECRETS, k, rec)
    targets = list((await _hgetall(KEY_TARGETS)).values())
    return {"ok": True, "key": k, "ref": pc.SECRET_REF + k,
            "used_by": pc.referencing_platforms(targets, "secret", k)}


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
    r = _redis()
    if r:
        await r.hdel(KEY_SECRETS, k)
    return {"ok": True, "deleted": k, "orphaned": used if force else []}


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
                       "actions": s.get("actions", [])}
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
    rec["updated"] = now_iso()
    await _hset(KEY_TARGETS, pid, rec)

    res = pc.resolve_fields(rec.get("fields") or {},
                            await _values_plain(), await _secrets_plain())
    comp = pc.config_completeness(rec.get("kind", ""), res["resolved"])
    return {"ok": True, "platform": {
        **rec, "fields": pc.redact_fields(rec["fields"], res["secret_fields"]),
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
    for kind in pc.PLATFORM_SPECS:
        if kind in targets:
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
    "platform-config", "Platforms", "🎛",
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
             "platform.secrets.set", "platform.secrets.delete"],
    mode="tab",
    tab_order=70,
)

log.info("platform_capabilities: ready — %d platform kinds",
         len(pc.PLATFORM_SPECS))
