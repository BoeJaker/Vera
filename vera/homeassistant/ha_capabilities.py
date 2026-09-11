"""
ha_capabilities.py — Vera → Home Assistant, natively
====================================================

Vera could already *configure* Home Assistant (`platform.apply` writes the core
location and builds Waze entries) and could *reach* it indirectly by calling an
n8n webhook that proxied a service call. Neither is control: the first writes
settings, the second makes every light switch depend on a second container
being up and on a webhook whose contract lives in a Code node.

This module gives Vera its own connection: read the entity list, resolve a
spoken phrase to an entity, call a service, push a notification to a phone.

  ha.states / ha.state / ha.find   read the world
  ha.call                          the general service call
  ha.set / ha.scene                the two that a sentence maps onto
  ha.notify                        push to a mobile_app target
  ha.health / ha.summary           is it up, and what is unreachable

Config (Redis hash `vera:ha:config`, token sealed via vera/security/secrets)
────────────────────────────────────────────────────────────────────────────
  base_url    e.g. http://192.168.0.96:8123
  token       a Home Assistant long-lived access token   (sealed)
  verify_tls  False for an internal-CA cert

Safety
──────
A service call is the only thing here that moves something physical, so two
guards sit in front of it: an ambiguous phrase is refused with its candidates
rather than resolved to the first row (`ha_core.resolve_entity`), and the
(domain, service) pairs in `ha_core.RISKY` — unlocking a door, disarming an
alarm, opening a cover, restarting the host — require `confirm=true`. A
misheard voice turn must not be able to unlock the front door.
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
    APP, capability, enum_schema, now_iso, register_ui,
)
from Vera.vera.homeassistant import ha_core as core
from Vera.vera.homeassistant import ha_estate as estate
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.homeassistant")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "ha_panel.html"

KEY_CONFIG = "vera:ha:config"

_SECRET_FIELDS = ("token",)

DEFAULTS: Dict[str, Any] = {
    "base_url": "",
    "token": "",
    "verify_tls": False,
    "updated": "",
}


def _redis():
    return getattr(_orch, "REDIS", None)


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════

async def _load_config(open_secrets: bool = True) -> Dict[str, Any]:
    r = _redis()
    cfg = dict(DEFAULTS)
    if not r:
        return cfg
    raw = await r.hgetall(KEY_CONFIG)
    for k, v in (raw or {}).items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        if key == "verify_tls":
            cfg[key] = str(val).lower() in ("1", "true", "yes", "on")
        else:
            cfg[key] = val
    if open_secrets:
        for f in _SECRET_FIELDS:
            if cfg.get(f):
                try:
                    cfg[f] = vsecrets.open_secret(cfg[f])
                except Exception:
                    log.warning("ha: could not unseal %s", f)
                    cfg[f] = ""
    return cfg


def _redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    for f in _SECRET_FIELDS:
        out[f] = "••••••••" if cfg.get(f) else ""
    return out


def _no_store() -> Dict[str, Any]:
    return {"error": "redis unavailable"}


def _unconfigured() -> Dict[str, Any]:
    return {"error": "Home Assistant is not configured - set base_url and "
                     "token with ha.config.set"}


@capability(
    "ha.config.get", http_method="GET", http_path="/ha/config",
    http_tags=["homeassistant"], memory="off",
    description="Read the Home Assistant connection config (token redacted). "
                "Output: {base_url, token, verify_tls, configured}.",
)
async def cap_config_get(trace_id=None):
    cfg = await _load_config()
    out = _redact(cfg)
    out["configured"] = bool(cfg.get("base_url") and cfg.get("token"))
    return out


@capability(
    "ha.config.set", http_method="POST", http_path="/ha/config",
    http_tags=["homeassistant"], memory="on",
    description="Set the Home Assistant connection. The token is sealed at "
                "rest and an empty string leaves the stored one unchanged. A "
                "base_url without a scheme is given http:// rather than being "
                "stored in a shape no HTTP client accepts. "
                "Input: base_url (str), token (str), verify_tls (bool). "
                "Output: {ok, config}.",
)
async def cap_config_set(base_url: str = "", token: str = "",
                         verify_tls: Optional[bool] = None, trace_id=None):
    r = _redis()
    if not r:
        return _no_store()
    cur = await _load_config()
    if base_url:
        cur["base_url"] = core.normalise_base(base_url)
    # Empty means "keep what is stored" - otherwise a partial save from the
    # panel would wipe the token it never re-sends.
    if token:
        cur["token"] = token
    if verify_tls is not None:
        cur["verify_tls"] = bool(verify_tls)
    cur["updated"] = now_iso()

    stored = dict(cur)
    for f in _SECRET_FIELDS:
        if stored.get(f):
            stored[f] = vsecrets.seal(stored[f])
    await r.hset(KEY_CONFIG, mapping={
        k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
        for k, v in stored.items()})
    out = _redact(cur)
    out["configured"] = bool(cur.get("base_url") and cur.get("token"))
    return {"ok": True, "config": out}


# ═════════════════════════════════════════════════════════════════════════════
#  HTTP
# ═════════════════════════════════════════════════════════════════════════════

async def _request(method: str, path: str,
                   body: Optional[Dict[str, Any]] = None,
                   timeout: float = 30.0) -> Any:
    """One HTTP call to HA. Raises RuntimeError with HA's own message."""
    cfg = await _load_config()
    if not cfg.get("base_url") or not cfg.get("token"):
        raise RuntimeError("not configured")
    url = core.api_url(cfg["base_url"], path)
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(verify=bool(cfg.get("verify_tls")),
                                 timeout=timeout,
                                 follow_redirects=True) as c:
        r = await c.request(method, url, headers=headers,
                            content=json.dumps(body) if body is not None
                            else None)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:250]}")
    if not r.content:
        return None
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


async def _states() -> List[Dict[str, Any]]:
    out = await _request("GET", "/api/states")
    return out if isinstance(out, list) else []


@capability(
    "ha.health", http_method="GET", http_path="/ha/health",
    http_tags=["homeassistant"], memory="off",
    description="Check the Home Assistant connection. "
                "Output: {ok, message, base_url, entities}.",
)
async def cap_health(trace_id=None):
    cfg = await _load_config()
    if not cfg.get("base_url") or not cfg.get("token"):
        return {"ok": False, **_unconfigured()}
    try:
        info = await _request("GET", "/api/")
        states = await _states()
        return {"ok": True, "base_url": cfg["base_url"],
                "message": (info or {}).get("message", ""),
                "entities": len(states)}
    except Exception as e:
        return {"ok": False, "base_url": cfg["base_url"], "error": str(e)[:300]}


# ═════════════════════════════════════════════════════════════════════════════
#  READ
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "ha.states", http_method="GET", http_path="/ha/states",
    http_tags=["homeassistant"], memory="off",
    description="List Home Assistant entities, newest state included. "
                "Input: domain (str - e.g. light, switch, scene, sensor), "
                "available_only (bool), limit (int=200). "
                "Output: {entities, count, total}.",
)
async def cap_states(domain: str = "", available_only: bool = False,
                     limit: int = 200, trace_id=None):
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    rows = core.find_entities(states, "", domain=domain,
                              limit=limit, available_only=available_only)
    return {"entities": rows, "count": len(rows), "total": len(states)}


@capability(
    "ha.state", http_method="GET", http_path="/ha/state",
    http_tags=["homeassistant"], memory="off",
    description="Full state and attributes of one entity. Accepts an "
                "entity_id or a human phrase ('bedside lamp'). "
                "Input: entity (str!). Output: the entity, or {error}.",
)
async def cap_state(entity: str = "", trace_id=None):
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    found = core.resolve_entity(states, entity)
    if found.get("error"):
        return found
    for e in states:
        if e.get("entity_id") == found["entity_id"]:
            return {"entity_id": e.get("entity_id"),
                    "name": core.friendly_name(e),
                    "state": e.get("state"),
                    "available": core.is_available(e),
                    "attributes": e.get("attributes") or {},
                    "last_changed": e.get("last_changed")}
    return {"error": "entity vanished between lookup and read"}


@capability(
    "ha.find", http_method="GET", http_path="/ha/find",
    http_tags=["homeassistant"], memory="off",
    description="Rank entities against a human phrase - what 'the bedside "
                "lamp' actually refers to. "
                "Input: query (str!), domain (str), limit (int=10). "
                "Output: {matches, count}.",
)
async def cap_find(query: str = "", domain: str = "", limit: int = 10,
                   trace_id=None):
    if not query.strip():
        return {"error": "query is required"}
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    rows = core.find_entities(states, query, domain=domain, limit=limit)
    return {"matches": rows, "count": len(rows)}


@capability(
    "ha.summary", http_method="GET", http_path="/ha/summary",
    http_tags=["homeassistant"], memory="off",
    description="Entity counts per domain and what is currently unreachable - "
                "the health view of the house. "
                "Output: {total, domains, unavailable_count, unavailable}.",
)
async def cap_summary(trace_id=None):
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    return core.summarise(states)


@capability(
    "ha.services", http_method="GET", http_path="/ha/services",
    http_tags=["homeassistant"], memory="off",
    description="Services Home Assistant exposes, optionally for one domain. "
                "Input: domain (str). Output: {services, count}.",
)
async def cap_services(domain: str = "", trace_id=None):
    try:
        out = await _request("GET", "/api/services")
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    rows = out if isinstance(out, list) else []
    dom = (domain or "").strip().lower()
    if dom:
        rows = [r for r in rows if str(r.get("domain", "")).lower() == dom]
    return {"services": [{"domain": r.get("domain"),
                          "services": sorted((r.get("services") or {}).keys())}
                         for r in rows],
            "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  CONTROL  -  the only part that changes the physical world
# ═════════════════════════════════════════════════════════════════════════════

async def _do_call(domain: str, service: str,
                   payload: Dict[str, Any]) -> Dict[str, Any]:
    result = await _request("POST", f"/api/services/{domain}/{service}",
                            payload)
    return {"ok": True, "service": f"{domain}.{service}",
            "changed": core.changed_entity_ids(result),
            "payload": payload}


@capability(
    "ha.call", http_method="POST", http_path="/ha/call",
    http_tags=["homeassistant"], memory="on",
    description="Call any Home Assistant service against one entity. The "
                "entity may be an entity_id or a phrase; an ambiguous phrase "
                "is refused with its candidates rather than guessed. Service "
                "may be 'turn_on' or 'light.turn_on'. Service calls listed in "
                "ha_core.RISKY (unlock, disarm, open cover, restart host) "
                "need confirm=true. "
                "Input: entity (str!), service (str!), data (dict), "
                "confirm (bool). Output: {ok, service, changed}.",
)
async def cap_call(entity: str = "", service: str = "",
                   data: Optional[Dict[str, Any]] = None,
                   confirm: bool = False, trace_id=None):
    if not entity.strip() or not service.strip():
        return {"error": "entity and service are required"}
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}

    found = core.resolve_entity(states, entity)
    if found.get("error"):
        return found
    try:
        planned = core.build_service_call(found["entity_id"],
                                          service=service, data=data)
    except ValueError as e:
        return {"error": str(e)}
    if planned["risky"] and not confirm:
        return {"error": f"{planned['domain']}.{planned['service']} is a "
                         "guarded action - re-call with confirm=true",
                "entity_id": found["entity_id"], "requires_confirm": True}
    try:
        return await _do_call(planned["domain"], planned["service"],
                              planned["payload"])
    except Exception as e:
        return {"error": str(e)[:300]}


@capability(
    "ha.set", http_method="POST", http_path="/ha/set",
    http_tags=["homeassistant"], memory="on",
    schema=enum_schema(action=["on", "off", "toggle"]),
    description="Turn something on, off, or toggle it, naming it the way a "
                "person would ('bedside lamp', 'kitchen light'). Extra "
                "options (brightness_pct, color_name, temperature) pass "
                "through in data. "
                "Input: entity (str!), action (on|off|toggle), data (dict), "
                "confirm (bool). Output: {ok, service, changed}.",
)
async def cap_set(entity: str = "", action: str = "on",
                  data: Optional[Dict[str, Any]] = None,
                  confirm: bool = False, trace_id=None):
    if not entity.strip():
        return {"error": "entity is required"}
    if action not in core.ACTIONS:
        return {"error": f"action must be one of {core.ACTIONS}"}
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}

    found = core.resolve_entity(states, entity)
    if found.get("error"):
        return found
    try:
        planned = core.build_service_call(found["entity_id"], action=action,
                                          data=data)
    except ValueError as e:
        return {"error": str(e)}
    if planned["risky"] and not confirm:
        return {"error": f"{planned['domain']}.{planned['service']} is a "
                         "guarded action - re-call with confirm=true",
                "entity_id": found["entity_id"], "requires_confirm": True}
    try:
        out = await _do_call(planned["domain"], planned["service"],
                             planned["payload"])
        out["resolved"] = {"entity_id": found["entity_id"],
                           "name": found.get("name"),
                           "exact": found.get("exact")}
        # A device HA cannot reach accepts the call and does nothing, so say so
        # rather than reporting a success the user will not see happen.
        if not found.get("available", True):
            out["warning"] = (f"{found['entity_id']} was unavailable when the "
                              "call was made - the device may not have "
                              "responded")
        return out
    except Exception as e:
        return {"error": str(e)[:300]}


@capability(
    "ha.scene", http_method="POST", http_path="/ha/scene",
    http_tags=["homeassistant"], memory="on",
    description="Activate a scene by name ('movie night', 'main lights off'). "
                "Input: scene (str!). Output: {ok, service, changed}.",
)
async def cap_scene(scene: str = "", trace_id=None):
    if not scene.strip():
        return {"error": "scene is required"}
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}

    found = core.resolve_entity(states, scene, domain="scene")
    if found.get("error"):
        return found
    try:
        out = await _do_call("scene", "turn_on",
                             {"entity_id": found["entity_id"]})
        out["resolved"] = {"entity_id": found["entity_id"],
                           "name": found.get("name")}
        return out
    except Exception as e:
        return {"error": str(e)[:300]}


@capability(
    "ha.notify", http_method="POST", http_path="/ha/notify",
    http_tags=["homeassistant"], memory="on",
    description="Push a notification through a Home Assistant notify target - "
                "a paired phone, say. Target accepts 'notify.smasnug', "
                "'mobile_app_x' or the bare slug. "
                "Input: message (str!), title (str), target (str), "
                "data (dict). Output: {ok, service}.",
)
async def cap_notify(message: str = "", title: str = "",
                     target: str = "", data: Optional[Dict[str, Any]] = None,
                     trace_id=None):
    if not message.strip():
        return {"error": "message is required"}
    if not target.strip():
        return {"error": "target is required - list them with "
                         "ha.services(domain='notify')"}
    domain, service = core.notify_service(target)
    payload = core.notify_payload(message, title, data)
    try:
        return await _do_call(domain, service, payload)
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    except Exception as e:
        return {"error": str(e)[:300]}


# ═════════════════════════════════════════════════════════════════════════════
#  ESTATE  -  project Vera's service registry into Home Assistant
# ═════════════════════════════════════════════════════════════════════════════

async def _registry() -> List[Dict[str, Any]]:
    """Vera's own view of what the estate is running.

    Read through the capability rather than the integrations store directly,
    so the access policy applied there is applied here too.
    """
    cap = _orch.CAPABILITY_REGISTRY.get("integration.list")
    if not cap:
        return []
    try:
        out = await cap["func"]()
    except Exception:
        log.debug("ha: integration.list failed", exc_info=True)
        return []
    if isinstance(out, dict):
        return out.get("integrations") or out.get("items") or []
    return out if isinstance(out, list) else []


async def _probe(url: str, timeout: float = 4.0) -> bool:
    """Is the service answering? Any HTTP response counts as alive.

    A 401 or a 404 still proves something is listening and serving, which is
    the question being asked. Only a connection failure or a timeout is down.
    """
    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout,
                                     follow_redirects=False) as c:
            await c.get(url)
        return True
    except Exception:
        return False


@capability(
    "ha.estate.plan", http_method="GET", http_path="/ha/estate/plan",
    http_tags=["homeassistant"], memory="off",
    description="What an estate sync would push into Home Assistant, without "
                "pushing it. One entity per registered service. Ephemeral "
                "Loop Lab sandboxes are skipped unless include_ephemeral. "
                "Input: include_ephemeral (bool), prune (bool=True). "
                "Output: {create, update, remove, skipped, counts}.",
)
async def cap_estate_plan(include_ephemeral: bool = False,
                          prune: bool = True, trace_id=None):
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    ints = await _registry()
    if not ints:
        return {"error": "Vera's integration registry is empty - run "
                         "integration.discover first"}
    plan = estate.plan_sync(ints, states, include_ephemeral=include_ephemeral,
                            prune=prune)
    plan["summary"] = estate.summarise_plan(plan)
    plan["registered"] = len(ints)
    return plan


@capability(
    "ha.estate.sync", http_method="POST", http_path="/ha/estate/sync",
    http_tags=["homeassistant"], memory="on",
    description="Push one Home Assistant entity per registered Vera service, "
                "so the estate sits alongside the house and can drive "
                "dashboards and automations. Each service is probed first, so "
                "the entity reads Connected or Disconnected. DRY RUN BY "
                "DEFAULT - pass dry_run=false to apply. Only entities stamped "
                "by a previous sync are ever updated or removed, so a real "
                "device cannot be touched. Note these entities are not backed "
                "by a config entry: Home Assistant forgets them on restart, "
                "so this is a mirror that wants re-running on a schedule. "
                "Input: dry_run (bool=True), include_ephemeral (bool), "
                "prune (bool=True), probe (bool=True). "
                "Output: {ok, dry_run, created, updated, removed, failed}.",
)
async def cap_estate_sync(dry_run: bool = True, include_ephemeral: bool = False,
                          prune: bool = True, probe: bool = True,
                          trace_id=None):
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    ints = await _registry()
    if not ints:
        return {"error": "Vera's integration registry is empty - run "
                         "integration.discover first"}

    plan = estate.plan_sync(ints, states, include_ephemeral=include_ephemeral,
                            prune=prune)
    if dry_run:
        return {"ok": True, "dry_run": True, "would": plan,
                "summary": estate.summarise_plan(plan)}

    by_id = {str(i.get("id") or ""): i for i in ints}
    ids = estate.entity_ids_for(
        [i for i in ints if include_ephemeral
         or not estate.is_ephemeral(str(i.get("label") or i.get("id") or ""))])

    reachable: Dict[str, bool] = {}
    if probe:
        for key, url in estate.probe_targets(
                [by_id[k] for k in ids if k in by_id]):
            reachable[key] = await _probe(url)

    checked = now_iso()
    created, updated, failed = [], [], []
    wanted = {r["entity_id"] for r in plan["create"]} | \
             {r["entity_id"] for r in plan["update"]}
    is_new = {r["entity_id"] for r in plan["create"]}

    for key, eid in ids.items():
        if eid not in wanted:
            continue
        body = estate.build_entity(by_id[key], eid,
                                   reachable.get(key) if probe else None,
                                   checked)
        try:
            await _request("POST", f"/api/states/{eid}",
                           {"state": body["state"],
                            "attributes": body["attributes"]})
            (created if eid in is_new else updated).append(eid)
        except Exception as e:
            failed.append({"entity_id": eid, "error": str(e)[:160]})

    removed = []
    for row in plan["remove"]:
        eid = row["entity_id"]
        try:
            await _request("DELETE", f"/api/states/{eid}")
            removed.append(eid)
        except Exception as e:
            failed.append({"entity_id": eid, "error": str(e)[:160]})

    return {"ok": not failed, "dry_run": False, "created": created,
            "updated": updated, "removed": removed, "failed": failed,
            "probed": len(reachable),
            "summary": f"{len(created)} added, {len(updated)} refreshed, "
                       f"{len(removed)} removed, {len(failed)} failed"}


@capability(
    "ha.estate.clear", http_method="POST", http_path="/ha/estate/clear",
    http_tags=["homeassistant"], memory="on",
    description="Remove every Home Assistant entity a previous estate sync "
                "created. Only entities carrying Vera's own source tag are "
                "considered, so nothing of yours is at risk. DRY RUN BY "
                "DEFAULT. Input: dry_run (bool=True). "
                "Output: {ok, dry_run, removed, failed}.",
)
async def cap_estate_clear(dry_run: bool = True, trace_id=None):
    try:
        states = await _states()
    except RuntimeError as e:
        return _unconfigured() if "not configured" in str(e) else {"error": str(e)}
    owned = estate.owned_entity_ids(states)
    if dry_run:
        return {"ok": True, "dry_run": True, "would_remove": owned,
                "count": len(owned)}
    removed, failed = [], []
    for eid in owned:
        try:
            await _request("DELETE", f"/api/states/{eid}")
            removed.append(eid)
        except Exception as e:
            failed.append({"entity_id": eid, "error": str(e)[:160]})
    return {"ok": not failed, "dry_run": False, "removed": removed,
            "failed": failed}


# ═════════════════════════════════════════════════════════════════════════════
#  UI
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "ha.panel.html", http_method="GET", http_path="/ha/panel",
    http_tags=["homeassistant", "ui"], memory="off", silent=True,
    description="Serve the Home Assistant control panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        return HTMLResponse(_PANEL_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef5b5b;"
            "font-family:monospace;padding:40px'><h2>ha_panel.html not found</h2>"
            f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")


@APP.get("/ha/panel", include_in_schema=False)
async def _ha_panel_route():
    p = _HERE / "ha_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>ha_panel.html not found</p>")


register_ui(
    "ha-panel", "Home", "\U0001F3E0",
    """<div id="ha-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/ha/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["ha.config.get", "ha.config.set", "ha.health", "ha.states",
             "ha.state", "ha.find", "ha.summary", "ha.set", "ha.scene",
             "ha.notify", "ha.call", "ha.estate.plan", "ha.estate.sync",
             "ha.estate.clear"],
    mode="tab",
    tab_order=74,
)

log.info("ha_capabilities: ready")
