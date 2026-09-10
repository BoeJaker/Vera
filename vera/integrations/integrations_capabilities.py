"""
integrations_capabilities.py — Vera Integrations Hub (group `integration.*`)
============================================================================

A first-class, integration-*centric* layer over the pieces Vera already has
(app.mount reverse-proxy, the operator, the MCP catalog, the SSH/exec host store,
the identity/PKI/mesh provisioning stack). Each external service — n8n, Home
Assistant, Gitea, GitHub, Grafana, WordPress, … local or cloud — becomes one
**integration record** you can:

  • **embed**   — view its web UI through Vera's reverse proxy (phone-friendly)
  • **interact**— let the operator drive its pages (observe→think→act)
  • **api**     — call its HTTP API through an authenticated passthrough
  • **mcp**     — activate/drive a paired MCP server
  • **ssh**     — reach the host shell (for web servers like WordPress)

…each behind a **per-integration access toggle that is ENFORCED** at every entry
point (`policy.require_access`), so a locked-down or `sensitive` integration
genuinely cannot be interacted with / API-called / MCP-driven — not merely hidden
in the UI.

Auto-discovery (`integration.discover`) surfaces local Docker containers, detected
web apps + MCP servers, and directory-registered hosts as candidate integrations,
created **default-locked** (embed on; interact/api/mcp/ssh off) so nothing is
reachable until you deliberately enable it.

Pure logic (kind specs / URL resolution / the access gate) lives in ``policy.py``
so it is unit-testable without Redis or the orchestrator; this module wires it to
the capability + HTTP surface.

Redis
─────
  vera:integrations   hash  id -> JSON   (api auth token sealed via secrets.py)

Reverse proxy
─────────────
  /integrations/{id}/embed            (+ /{path}) — gated by access.embed
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.integrations import policy as _policy
from Vera.vera.integrations.source_intake import (
    inspect_source as _inspect_source,
    lifecycle_contract as _source_lifecycle_contract,
    plan_transition as _plan_source_transition,
)
from Vera.vera.integrations.source_build_plan import (
    build_plan_contract as _source_build_plan_contract,
    plan_source_build as _plan_source_build,
)
from Vera.vera.integrations.external_effects import (
    plan_api_effect_shadow as _plan_api_effect_shadow,
    plan_external_effect as _plan_external_effect,
)
from Vera.vera.integrations.effect_receipts import default_external_effect_receipt_ledger
from Vera.vera.integrations.effect_shadow_evidence import default_external_effect_shadow_evidence
from Vera.vera.integrations.effect_enforcement_decision import (
    DecisionConflict, default_external_effect_enforcement_decisions)
from Vera.vera.integrations.effect_enforcement_activation import (
    ActivationConflict, default_external_effect_enforcement_activations)
from Vera.vera.integrations.effect_retry import plan_effect_retry as _plan_effect_retry
from Vera.vera.integrations.connection_projection import project_connections

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                   # pragma: no cover
    vsecrets = None                                 # type: ignore

log = logging.getLogger("vera.integrations")

_HERE = Path(__file__).parent
KEY_INTEGRATIONS = "vera:integrations"


def _effect_enforcement_runtime_gate() -> bool:
    return os.getenv("VERA_INTEGRATION_API_EFFECT_ENFORCEMENT", "").strip().lower() \
        in {"1", "true", "yes", "on"}

# Aliases onto the pure policy module (single source of truth, shared with tests).
ACCESS_MODES = _policy.ACCESS_MODES
DEFAULT_ACCESS = _policy.DEFAULT_ACCESS
KIND_SPECS = _policy.KIND_SPECS
_guess_kind = _policy.guess_kind
_base_url = _policy.base_url
_require_access = _policy.require_access


def _redis():
    return getattr(_orch, "REDIS", None)


def _cap_raw(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


def _seal(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


def _open(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.open_secret(v)
        except Exception:
            return v
    return v


# ═════════════════════════════════════════════════════════════════════════════
#  STORE
# ═════════════════════════════════════════════════════════════════════════════
async def _all() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_INTEGRATIONS)
    except Exception:
        return []
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda x: (x.get("label") or x.get("id") or "").lower())
    return out


async def _get(iid: str) -> Optional[Dict]:
    r = _redis()
    if not r or not iid:
        return None
    raw = await r.hget(KEY_INTEGRATIONS, iid)
    return json.loads(raw) if raw else None


async def _put(rec: Dict) -> Dict:
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    rec["updated"] = now_iso()
    await r.hset(KEY_INTEGRATIONS, rec["id"], json.dumps(rec))
    return rec


def _redact(rec: Dict) -> Dict:
    """UI-safe copy: strip the sealed API secret, keep a has_auth hint."""
    out = dict(rec)
    api = dict(out.get("api") or {})
    if "auth" in api:
        api["has_auth"] = bool(api.pop("auth"))
    out["api"] = api
    out["base_url"] = _base_url(rec)
    return out


def _apply_api_auth(rec: Dict, headers: Dict) -> Dict:
    """Inject the integration's API auth header server-side (secret opened here)."""
    api = rec.get("api") or {}
    token = _open(api.get("auth", ""))
    for k, v in _policy.auth_header(api.get("auth_scheme") or "bearer", token,
                                    api.get("auth_header", "")).items():
        headers.setdefault(k, v)
    return headers


async def _audit(event: str, rec: Dict, **extra) -> None:
    await emit_event({"type": f"integration.{event}", "id": rec.get("id"),
                      "label": rec.get("label"), "kind": rec.get("kind"), **extra})


# ═════════════════════════════════════════════════════════════════════════════
#  CRUD
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "integration.list",
    http_method="GET", http_path="/integrations/list", http_tags=["integration"],
    memory="off", silent=True,
    description="List all integrations (API secrets redacted). Output: "
                "{integrations:[{id,label,kind,base_url,source,access,sensitive,"
                "identity_verified,in_mesh,mcp_id,ssh_host_id}], count}.",
)
async def cap_list(trace_id=None) -> Dict:
    recs = [_redact(r) for r in await _all()]
    return {"integrations": recs, "count": len(recs)}


@capability(
    "integration.get",
    http_method="GET", http_path="/integrations/get", http_tags=["integration"],
    memory="off", silent=True,
    description="Get one integration (API secret redacted). Input: id (str!). "
                "Output: {ok, integration}.",
)
async def cap_get(id: str = "", trace_id=None) -> Dict:
    rec = await _get(id)
    if not rec:
        return {"error": "not found"}
    return {"ok": True, "integration": _redact(rec)}


@capability(
    "integration.save",
    http_method="POST", http_path="/integrations/save", http_tags=["integration"],
    memory="on",
    description="Create/update an integration. Inputs: id (str — update if given), "
                "label (str), kind (n8n|homeassistant|gitea|github|grafana|"
                "wordpress|portainer|prometheus|generic), host (str), port (int), "
                "scheme (http|https), base_url (str — overrides host/port), source "
                "(local|manual|identity|cloud), mcp_id (str), ssh_host_id (str), "
                "conn_id (str), api_token (str — SEALED; blank keeps existing), "
                "api_scheme (bearer|token|header|basic|none), api_header (str), "
                "sensitive (bool). New records start default-locked. Output: "
                "{ok, integration}.",
    schema={"properties": {
        "kind": {"enum": list(KIND_SPECS.keys())},
        "scheme": {"enum": ["http", "https"]},
        "source": {"enum": ["local", "manual", "identity", "cloud"]},
        "api_scheme": {"enum": ["bearer", "token", "header", "basic", "none"]},
    }},
)
async def cap_save(id: str = "", label: str = "", kind: str = "generic",
                   host: str = "", port: int = 0, scheme: str = "http",
                   base_url: str = "", source: str = "manual",
                   mcp_id: str = "", ssh_host_id: str = "", conn_id: str = "",
                   api_token: str = "", api_scheme: str = "", api_header: str = "",
                   sensitive: Optional[bool] = None, trace_id=None) -> Dict:
    existing = await _get(id) if id else None
    rec = dict(existing) if existing else {
        "id": uuid.uuid4().hex[:12], "created": now_iso(),
        "access": dict(DEFAULT_ACCESS), "sensitive": False,
    }
    if label:
        rec["label"] = label
    elif not rec.get("label"):
        rec["label"] = label or host or base_url or f"integration-{rec['id'][:6]}"
    for k, v in (("kind", kind), ("host", host), ("scheme", scheme),
                 ("base_url", base_url.rstrip("/") if base_url else ""),
                 ("source", source), ("mcp_id", mcp_id),
                 ("ssh_host_id", ssh_host_id), ("conn_id", conn_id)):
        if v != "" or k not in rec:
            rec[k] = v
    if port:
        rec["port"] = int(port)
    if sensitive is not None:
        rec["sensitive"] = bool(sensitive)
    # API connector (token sealed).
    api = dict(rec.get("api") or {})
    if api_scheme:
        api["auth_scheme"] = api_scheme
    if api_header:
        api["auth_header"] = api_header
    if api_token:
        try:
            api["auth"] = _seal(api_token)
        except RuntimeError as e:
            return {"error": str(e)}
    api.setdefault("auth_scheme",
                   KIND_SPECS.get(rec.get("kind", "generic"), {}).get("auth_scheme", "bearer"))
    rec["api"] = api
    rec.setdefault("access", dict(DEFAULT_ACCESS))
    saved = await _put(rec)
    if saved.get("error"):
        return saved
    await _audit("saved", saved)
    return {"ok": True, "integration": _redact(saved)}


@capability(
    "integration.delete",
    http_method="POST", http_path="/integrations/delete", http_tags=["integration"],
    memory="on",
    description="Delete an integration by id (does not touch the underlying "
                "service). Input: id (str!). Output: {ok}.",
)
async def cap_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    rec = await _get(id)
    await r.hdel(KEY_INTEGRATIONS, id)
    if rec:
        await _audit("deleted", rec)
    return {"ok": True, "deleted": id}


@capability(
    "integration.import_apps",
    http_method="POST", http_path="/integrations/import_apps", http_tags=["integration"],
    memory="on",
    description="Fold the legacy Workspaces app-mount records (app.list / "
                "vera:remote:apps) into integrations so both share ONE registry. "
                "Idempotent (dedup by host:port); keeps a link back via app_id. "
                "New records are default-locked. Input: commit (bool=true). "
                "Output: {imported:[...], count, skipped, total}.",
)
async def cap_import_apps(commit: bool = True, trace_id=None) -> Dict:
    lst = _cap_raw("app.list")
    if not lst:
        return {"error": "app.list unavailable (workspace module not loaded)"}
    apps = (await lst() or {}).get("apps", [])
    by_hp = {(r.get("host"), r.get("port")): r for r in await _all()}
    imported: List[Dict] = []
    skipped = 0
    for a in apps:
        key = (a.get("host"), int(a.get("port") or 0))
        if not key[0] or not key[1] or key in by_hp:
            skipped += 1
            continue
        kind = _guess_kind(key[1], "", a.get("label", ""))
        rec = {
            "id": uuid.uuid4().hex[:12], "created": now_iso(),
            "label": a.get("label") or f"{kind}:{key[1]}", "kind": kind,
            "host": key[0], "port": key[1], "scheme": a.get("scheme", "http"),
            "source": "local", "access": dict(DEFAULT_ACCESS), "sensitive": False,
            "mcp_id": a.get("mcp_id", ""), "conn_id": a.get("conn_id", ""),
            "app_id": a.get("id", ""),   # link back to the legacy mounted app
            "api": {"auth_scheme": KIND_SPECS.get(kind, {}).get("auth_scheme", "bearer")},
        }
        by_hp[key] = rec
        if commit:
            await _put(rec)
            await _audit("imported_app", rec)
        imported.append(_redact(rec))
    return {"imported": imported, "count": len(imported), "skipped": skipped,
            "total": len(apps)}


@capability(
    "integration.access.set",
    http_method="POST", http_path="/integrations/access/set", http_tags=["integration"],
    memory="on",
    description="Set the per-integration access policy — the enforced toggles for "
                "embed / interact / api / mcp / ssh — and the `sensitive` flag "
                "(which hard-locks interact/api/mcp regardless of their toggles). "
                "Every change is audited. Inputs: id (str!), embed/interact/api/"
                "mcp/ssh (bool — omit to leave unchanged), sensitive (bool). "
                "Output: {ok, access, sensitive}.",
)
async def cap_access_set(id: str = "", embed: Optional[bool] = None,
                         interact: Optional[bool] = None, api: Optional[bool] = None,
                         mcp: Optional[bool] = None, ssh: Optional[bool] = None,
                         sensitive: Optional[bool] = None, trace_id=None) -> Dict:
    rec = await _get(id)
    if not rec:
        return {"error": "not found"}
    acc = dict(rec.get("access") or DEFAULT_ACCESS)
    before = dict(acc)
    for mode, val in (("embed", embed), ("interact", interact), ("api", api),
                      ("mcp", mcp), ("ssh", ssh)):
        if val is not None:
            acc[mode] = bool(val)
    rec["access"] = acc
    if sensitive is not None:
        rec["sensitive"] = bool(sensitive)
    await _put(rec)
    await _audit("access_changed", rec, before=before, after=acc,
                 sensitive=rec.get("sensitive"))
    return {"ok": True, "access": acc, "sensitive": rec.get("sensitive")}


# ═════════════════════════════════════════════════════════════════════════════
#  ACTIVE ENTRY POINTS  (each gated by policy.require_access)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "integration.operate",
    http_method="POST", http_path="/integrations/operate", http_tags=["integration"],
    memory="on",
    description="Drive an integration's web UI with the operator (observe→think→"
                "act). REQUIRES access.interact (and the integration must not be "
                "`sensitive`). Inputs: id (str!), goal (str — task; blank just "
                "opens a session for manual driving), max_steps (int=15). Output: "
                "the operator result, or {error, code:403} if interaction is "
                "disabled.",
)
async def cap_operate(id: str = "", goal: str = "", max_steps: int = 15,
                      trace_id=None) -> Dict:
    rec = await _get(id)
    gate = _require_access(rec, "interact")
    if gate:
        return gate
    base = _base_url(rec)
    if not base:
        return {"error": "integration has no resolvable URL"}
    await _audit("operate", rec, goal=goal)
    if goal:
        run = _cap_raw("operator.run")
        if not run:
            return {"error": "operator.run unavailable (operator not loaded)"}
        return await run(goal=goal, url=base, base_url=base, max_steps=int(max_steps))
    start = _cap_raw("operator.session.start")
    if not start:
        return {"error": "operator.session.start unavailable"}
    return await start(url=base, base_url=base)


@capability(
    "integration.api.call",
    http_method="POST", http_path="/integrations/api/call", http_tags=["integration"],
    memory="on",
    redact_args=["path", "query", "body", "headers", "idempotency_key",
                 "approval_receipt_ref"],
    redact_result=True,
    description="Call an integration's HTTP API through an authenticated "
                "passthrough (the sealed token is injected server-side and never "
                "reaches the browser). REQUIRES access.api. Inputs: id (str!), "
                "method (GET|POST|PUT|DELETE|PATCH), path (str — appended to the "
                "kind's api_base, e.g. '/repos'), query (dict), body (dict/str), "
                "headers (dict — extra), idempotency_key, approval_receipt_ref, "
                "retry (policy evidence only; not forwarded). Mutating calls are "
                "blocked only when the deployment gate, current approval, and "
                "contract-bound activation all agree. Output: "
                "{ok, status, body, json?, effect_shadow, effect_enforcement} or "
                "{error, code:403}.",
    schema={"properties": {"method": {"enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]}}},
)
async def cap_api_call(id: str = "", method: str = "GET", path: str = "",
                       query: Optional[Dict] = None, body: Any = None,
                       headers: Optional[Dict] = None, idempotency_key: str = "",
                       approval_receipt_ref: str = "", retry: bool = False,
                       trace_id=None) -> Dict:
    rec = await _get(id)
    gate = _require_access(rec, "api")
    if gate:
        return gate
    base = _base_url(rec)
    if not base:
        return {"error": "integration has no resolvable URL"}
    try:
        shadow = _plan_api_effect_shadow(
            integration_id=id, method=method, path=path,
            idempotency_key=idempotency_key,
            approval_receipt_ref=approval_receipt_ref, retry=retry)
        if (shadow["plan"]["mutating"] and
                shadow["plan"]["admission"]["allowed"]):
            replay = default_external_effect_receipt_ledger().replay_status(shadow["plan"])
            already = bool(replay["already_succeeded"])
            shadow["replay"] = {
                "already_succeeded": already, "would_suppress": already,
                "successful_receipt_id": replay["successful_receipt_id"]}
            shadow["decision"]["would_execute"] = not already
    except Exception:
        shadow = {"schema": "vera.external-effect-shadow/v1",
                  "enforcement": "observe_only", "error": "shadow_unavailable",
                  "decision": {"would_admit": False, "would_execute": False,
                               "reasons": ["invalid_policy_evidence"]},
                  "blocks_current_call": False, "forwards_control_references": False,
                  "records_completion": False, "executes": False}
    try:
        default_external_effect_shadow_evidence().record(shadow)
    except Exception:
        log.exception("external-effect shadow evidence record failed")
    runtime_gate = _effect_enforcement_runtime_gate()
    try:
        operator_decision = default_external_effect_enforcement_decisions().current()
        activation = default_external_effect_enforcement_activations().current(
            operator_decision, runtime_gate=runtime_gate)
    except Exception:
        activation = {"enforcement_enabled": False, "effective_mode": "observe_only",
                      "error": "activation_state_unavailable"}
        if runtime_gate and (shadow.get("plan") or {}).get("mutating"):
            return {"error": "effect enforcement state unavailable", "code": 503,
                    "effect_shadow": shadow, "effect_enforcement": activation}
    shadow["enforcement"] = activation["effective_mode"]
    if activation["enforcement_enabled"] and not shadow["decision"]["would_execute"]:
        await _audit("api_call_blocked", rec, method=method,
                     effect_plan_id=(shadow.get("plan") or {}).get("plan_id", ""),
                     effect_reasons=shadow["decision"]["reasons"])
        return {"error": "external effect rejected by policy", "code": 403,
                "effect_shadow": shadow, "effect_enforcement": activation}
    spec = KIND_SPECS.get(rec.get("kind", "generic"), {})
    api_base = (rec.get("api") or {}).get("api_base", spec.get("api_base", ""))
    url = base + api_base + ("/" + path.lstrip("/") if path else "")
    hdrs = dict(headers or {})
    _apply_api_auth(rec, hdrs)
    verify = rec.get("scheme") == "https" and rec.get("verify_tls", False)
    await _audit("api_call", rec, method=method,
                 effect_plan_id=(shadow.get("plan") or {}).get("plan_id", ""),
                 effect_would_admit=shadow["decision"]["would_admit"],
                 effect_would_execute=shadow["decision"]["would_execute"],
                 effect_reasons=shadow["decision"]["reasons"])
    try:
        async with httpx.AsyncClient(timeout=30, verify=verify,
                                     follow_redirects=True) as c:
            r = await c.request(method.upper(), url, params=query or None,
                                json=body if isinstance(body, (dict, list)) else None,
                                content=body if isinstance(body, str) else None,
                                headers=hdrs)
    except Exception as e:
        return {"error": f"upstream {type(e).__name__}: {e}",
                "effect_shadow": shadow, "effect_enforcement": activation}
    out: Dict[str, Any] = {"ok": r.status_code < 400, "status": r.status_code,
                           "url": url}
    try:
        out["json"] = r.json()
    except Exception:
        out["body"] = r.text[:20000]
    out["effect_shadow"] = shadow
    out["effect_enforcement"] = activation
    return out


@capability(
    "integration.mcp.call",
    http_method="POST", http_path="/integrations/mcp/call", http_tags=["integration"],
    memory="on",
    description="Activate/connect the MCP server paired to this integration so its "
                "tools become callable (via mcp.catalog.connect). REQUIRES "
                "access.mcp. Inputs: id (str!). Output: the mcp.catalog.connect "
                "result, or {error, code:403}.",
)
async def cap_mcp_call(id: str = "", trace_id=None) -> Dict:
    rec = await _get(id)
    gate = _require_access(rec, "mcp")
    if gate:
        return gate
    mcp_id = rec.get("mcp_id")
    if not mcp_id:
        return {"error": "no MCP server paired to this integration (set mcp_id)"}
    connect = _cap_raw("mcp.catalog.connect")
    if not connect:
        return {"error": "mcp.catalog.connect unavailable"}
    await _audit("mcp_connect", rec, mcp_id=mcp_id)
    return await connect(id=mcp_id)


@capability(
    "integration.identity.register",
    http_method="POST", http_path="/integrations/identity/register", http_tags=["integration", "identity"],
    memory="on",
    description="Register this integration in the directory (FreeIPA-first via "
                "identity.resolve.app: DNS + service principal + TLS cert; graceful "
                "skip if FreeIPA is down), then stamp identity_fqdn / "
                "identity_verified / cert on the record. Inputs: id (str!), fqdn "
                "(str — defaults from host). Output: {ok, backend, result, integration}.",
)
async def cap_identity_register(id: str = "", fqdn: str = "", trace_id=None) -> Dict:
    rec = await _get(id)
    if not rec:
        return {"error": "not found"}
    fqdn = fqdn or rec.get("identity_fqdn") or rec.get("host") or ""
    if not fqdn:
        return {"error": "no fqdn/host to register"}
    reg = _cap_raw("identity.resolve.app") or _cap_raw("identity.app.register")
    if not reg:
        return {"error": "identity resolver unavailable (identity module not loaded)"}
    name = fqdn.split(".")[0]
    r = await reg(name=name, ip=rec.get("host", ""), port=int(rec.get("port") or 0),
                  ssh_host_id=rec.get("ssh_host_id", ""))
    ok = bool(r.get("ok"))
    rec["identity_fqdn"] = r.get("fqdn", fqdn)
    rec["identity_verified"] = ok
    cert = r.get("cert") if isinstance(r.get("cert"), dict) else {}
    if cert.get("expires"):
        rec["cert"] = {"fqdn": rec["identity_fqdn"], "expires": cert["expires"]}
    await _put(rec)
    await _audit("identity_register", rec, backend=r.get("backend"), ok=ok)
    return {"ok": ok, "backend": r.get("backend", ""), "result": r,
            "integration": _redact(rec)}


# ═════════════════════════════════════════════════════════════════════════════
#  CONNECTIONS VIEW  (MCP / API / SSH / identity / cert / mesh, to+from a service)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "integration.connections",
    http_method="POST", http_path="/integrations/connections", http_tags=["integration"],
    memory="off",
    description="Summarise every connection to/from an integration: reverse-proxy "
                "embed path, operator (interact), API connector + auth presence, "
                "paired MCP server (from mcp.catalog), SSH host (from the exec host "
                "store), and identity/cert/mesh status. Inputs: id (str — one) or "
                "blank for a whole-graph summary. Output: {integration, "
                "connections:[{type,target,enabled,detail}]} or {graph:{nodes,edges}}.",
)
async def cap_connections(id: str = "", trace_id=None) -> Dict:
    if id:
        rec = await _get(id)
        if not rec:
            return {"error": "not found"}
        return {"integration": _redact(rec),
                "connections": await _connections_for(rec)}
    recs = await _all()
    nodes = [{"id": "vera", "label": "Vera", "type": "hub"}]
    edges: List[Dict] = []
    for rec in recs:
        nodes.append({"id": rec["id"], "label": rec.get("label"),
                      "type": rec.get("kind"), "verified": rec.get("identity_verified"),
                      "sensitive": rec.get("sensitive")})
        for c in await _connections_for(rec):
            if c["enabled"]:
                edges.append({"from": "vera", "to": rec["id"], "protocol": c["type"]})
    return {"graph": {"nodes": nodes, "edges": edges}, "count": len(recs)}


@capability(
    "integration.connections.project", http_method="GET",
    http_path="/integrations/connections/project",
    http_tags=["integration", "accounts", "providers"], memory="off", silent=True,
    description="Build a deterministic read-only connection projection across "
                "the integration, account, and model-provider registries. Reports "
                "source authority, sanitized endpoint origins, credential presence, "
                "explicit references, unresolved links, and collisions. It never "
                "opens secrets, probes endpoints, merges records, grants access, or "
                "activates a connection.",
)
async def cap_connections_project(trace_id=None) -> Dict:
    available_sources = {"integration"}
    integrations = [_redact(record) for record in await _all()]
    accounts: List[Dict] = []
    providers: List[Dict] = []
    account_list = _cap_raw("acct.list")
    if account_list:
        try:
            result = await account_list()
            accounts = list((result or {}).get("accounts") or [])
            available_sources.add("account")
        except Exception:
            pass
    provider_list = _cap_raw("providers.list")
    if provider_list:
        try:
            result = await provider_list()
            providers = list((result or {}).get("providers") or [])
            available_sources.add("provider")
        except Exception:
            pass
    return project_connections(
        integrations=integrations, accounts=accounts, providers=providers,
        available_sources=available_sources)


async def _connections_for(rec: Dict) -> List[Dict]:
    acc = rec.get("access") or {}
    conns: List[Dict] = []
    conns.append({"type": "embed", "target": f"/integrations/{rec['id']}/embed",
                  "enabled": bool(acc.get("embed")), "detail": _base_url(rec)})
    conns.append({"type": "interact", "target": "operator",
                  "enabled": bool(acc.get("interact")) and not rec.get("sensitive"),
                  "detail": "operator observe→think→act"})
    api = rec.get("api") or {}
    conns.append({"type": "api", "target": _base_url(rec) + api.get("api_base", ""),
                  "enabled": bool(acc.get("api")) and not rec.get("sensitive"),
                  "detail": f"auth={api.get('auth_scheme', 'none')}"
                            f"{' (set)' if api.get('auth') else ''}"})
    mcp_detail = ""
    if rec.get("mcp_id"):
        get_mcp = _cap_raw("mcp.catalog.get")
        if get_mcp:
            try:
                m = await get_mcp(id=rec["mcp_id"])
                srv = (m or {}).get("server") or m or {}
                mcp_detail = f"{srv.get('label', rec['mcp_id'])} " \
                             f"[{srv.get('transport', '?')}] {srv.get('status', '')}"
            except Exception:
                mcp_detail = rec["mcp_id"]
    conns.append({"type": "mcp", "target": rec.get("mcp_id", ""),
                  "enabled": bool(acc.get("mcp")) and not rec.get("sensitive"),
                  "detail": mcp_detail})
    ssh_detail = ""
    if rec.get("ssh_host_id"):
        lst = _cap_raw("ssh.host.list") or _cap_raw("exec.ssh.hosts.list")
        if lst:
            try:
                for h in (await lst() or {}).get("hosts", []):
                    if h.get("id") == rec["ssh_host_id"]:
                        ssh_detail = f"{h.get('user')}@{h.get('host')}:{h.get('port', 22)}"
                        break
            except Exception:
                pass
    conns.append({"type": "ssh", "target": rec.get("ssh_host_id", ""),
                  "enabled": bool(acc.get("ssh")), "detail": ssh_detail})
    conns.append({"type": "identity", "target": rec.get("identity_fqdn", ""),
                  "enabled": bool(rec.get("identity_verified")),
                  "detail": ("verified" if rec.get("identity_verified") else "unregistered")
                            + (f" · cert→{rec['cert'].get('expires')}" if rec.get("cert") else "")
                            + (" · mesh" if rec.get("in_mesh") else "")})
    return conns


# ═════════════════════════════════════════════════════════════════════════════
#  DISCOVERY  (local Docker + detected apps/MCP + directory hosts → candidates)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "integration.discover",
    http_method="POST", http_path="/integrations/discover", http_tags=["integration"],
    memory="on",
    description="Auto-discover services and surface them as integrations, created "
                "DEFAULT-LOCKED (embed on; interact/api/mcp/ssh off) so nothing is "
                "reachable until explicitly enabled. Sources: local Docker "
                "containers with published ports, an optional host port-scan "
                "(app.detect), network MCP servers (mcp.detect), and directory "
                "hosts (identity.host.list → identity_verified). Inputs: host (str "
                "— extra host to app.detect/mcp.detect), docker (bool=true), "
                "identity (bool=true), commit (bool=true — false = preview only). "
                "Output: {found:[...], created:[...], updated:[...]}.",
)
async def cap_discover(host: str = "", docker: bool = True, identity: bool = True,
                       commit: bool = True, trace_id=None) -> Dict:
    existing = await _all()
    by_hostport = {(r.get("host"), r.get("port")): r for r in existing}
    verified_fqdns: set = set()

    if identity:
        hl = _cap_raw("identity.host.list")
        if hl:
            try:
                verified_fqdns = {h.get("fqdn") for h in (await hl() or {}).get("hosts", [])
                                  if h.get("fqdn")}
            except Exception:
                pass

    found: List[Dict] = []
    if docker:
        found.extend(await _discover_docker())

    if host:
        det = _cap_raw("app.detect")
        if det:
            try:
                for a in (await det(host=host) or {}).get("apps", []):
                    found.append({"host": host, "port": a.get("port"),
                                  "scheme": a.get("scheme", "http"),
                                  "label": a.get("label", ""), "source": "local",
                                  "kind": _guess_kind(a.get("port", 0), "", a.get("label", ""))})
            except Exception:
                pass
        mdet = _cap_raw("mcp.detect")
        if mdet:
            try:
                for m in (await mdet(host=host, register=True) or {}).get("found", []):
                    found.append({"host": host, "port": m.get("port"),
                                  "scheme": "http", "label": f"MCP :{m.get('port')}",
                                  "source": "local", "kind": "generic",
                                  "mcp_url": m.get("url"), "mcp_id": m.get("catalog_id")})
            except Exception:
                pass

    created, updated = [], []
    for f in found:
        key = (f.get("host"), f.get("port"))
        if not key[0] or not key[1]:
            continue
        prior = by_hostport.get(key)
        if prior:
            changed = False
            for k in ("mcp_id", "mcp_url"):
                if f.get(k) and not prior.get(k):
                    prior[k] = f[k]
                    changed = True
            if changed and commit:
                await _put(prior)
                updated.append(prior["id"])
            continue
        kind = f.get("kind", "generic")
        rec = {
            "id": uuid.uuid4().hex[:12], "created": now_iso(),
            "label": f.get("label") or f"{kind}:{f.get('port')}",
            "kind": kind, "host": f["host"], "port": int(f["port"]),
            "scheme": f.get("scheme", "http"),
            "source": f.get("source", "local"),
            "access": dict(DEFAULT_ACCESS), "sensitive": False,
            "mcp_id": f.get("mcp_id", ""),
            "api": {"auth_scheme": KIND_SPECS.get(kind, {}).get("auth_scheme", "bearer")},
        }
        rec["identity_verified"] = any(f["host"] in (fq or "") or (fq or "").startswith(f["host"])
                                       for fq in verified_fqdns)
        by_hostport[key] = rec
        if commit:
            await _put(rec)
            await _audit("discovered", rec, source=rec["source"])
        created.append(_redact(rec))

    return {"found": len(found), "created": created, "updated": updated,
            "committed": commit,
            "note": "New integrations are locked (embed only). Enable interact/api/"
                    "mcp per integration with integration.access.set."}


async def _discover_docker() -> List[Dict]:
    hosts_list = _cap_raw("docker.hosts.list")
    ps = _cap_raw("docker.ps")
    if not (hosts_list and ps):
        return []
    out: List[Dict] = []
    try:
        hosts = (await hosts_list() or {}).get("hosts", [])
    except Exception:
        return []
    for h in hosts:
        addr = _docker_addr(h)
        if not addr:
            continue
        try:
            rows = (await ps(host_id=h.get("id", ""), all=False) or {}).get("containers", [])
        except Exception:
            continue
        seen: set = set()
        for c in rows:
            names = c.get("Names") or ["?"]
            cname = (names[0] if isinstance(names, list) and names else str(names)).lstrip("/")
            image = c.get("Image", "")
            for p in (c.get("Ports") or []):
                pub = p.get("PublicPort")
                if not pub or int(pub) in (22, 2375, 2376) or int(pub) in seen:
                    continue
                seen.add(int(pub))
                kind = _guess_kind(int(pub), image, cname)
                out.append({"host": addr, "port": int(pub),
                            "scheme": "https" if int(pub) in (443, 8443, 9443) else "http",
                            "label": f"{cname}", "source": "local", "kind": kind})
    return out


def _docker_addr(d: Dict) -> str:
    url = d.get("url", "") or ""
    if "://" in url:
        return urlparse(url.replace("tcp://", "http://")).hostname or ""
    if d.get("kind") == "local":
        return "127.0.0.1"
    return ""


# ═════════════════════════════════════════════════════════════════════════════
#  GATED EMBED REVERSE PROXY   /integrations/{id}/embed  (+ /{path})
# ═════════════════════════════════════════════════════════════════════════════
_HOP = {"connection", "keep-alive", "proxy-authenticate", "content-length",
        "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
        "content-encoding"}


def _inject_base(html: bytes, base: str) -> bytes:
    try:
        s = html.decode("utf-8", "replace")
    except Exception:
        return html
    if "<base " in s.lower():
        return html
    i = s.lower().find("<head")
    tag = f'<base href="{base}">'
    if i >= 0:
        j = s.find(">", i)
        if j >= 0:
            return (s[:j + 1] + tag + s[j + 1:]).encode("utf-8", "replace")
    return (tag + s).encode("utf-8", "replace")


@APP.api_route("/integrations/{iid}/embed", methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
               include_in_schema=False)
@APP.api_route("/integrations/{iid}/embed/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def integration_embed_proxy(iid: str, request: Request, path: str = ""):
    rec = await _get(iid)
    gate = _require_access(rec, "embed")
    if gate:
        return JSONResponse(gate, status_code=gate.get("code", 403))
    base = _base_url(rec)
    if not base:
        return JSONResponse({"error": "no target URL"}, status_code=502)
    qs = request.url.query
    proxy_base = f"/integrations/{iid}/embed/"
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _HOP and k.lower() != "host"}
    fwd["accept-encoding"] = "identity"   # uncompressed → no garbled/binary body
    body = await request.body()
    verify = bool(rec.get("verify_tls"))

    async def _fetch(b: str):
        target = b + "/" + path + (("?" + qs) if qs else "")
        async with httpx.AsyncClient(verify=verify, timeout=45,
                                     follow_redirects=False) as c:
            return await c.request(request.method, target, headers=fwd,
                                   content=body if body else None)

    switched = False
    try:
        up = await _fetch(base)
        # Auto-heal a scheme mismatch ("HTTP request sent to an HTTPS server").
        if (base.startswith("http://") and up.status_code == 400
                and b"HTTPS server" in (up.content or b"")[:500]):
            base = "https://" + base[len("http://"):]
            up = await _fetch(base); switched = True
    except Exception:
        if base.startswith("http://"):
            try:
                base = "https://" + base[len("http://"):]
                up = await _fetch(base); switched = True
            except Exception as e2:
                return JSONResponse({"error": f"upstream {type(e2).__name__}: {e2}"},
                                    status_code=502)
        else:
            return JSONResponse({"error": "upstream unreachable"}, status_code=502)
    if switched and not rec.get("base_url") and rec.get("scheme") != "https":
        rec["scheme"] = "https"                # remember so next time we go direct
        try:
            await _put(rec)
        except Exception:
            pass

    resp_headers = {}
    for k, v in up.headers.items():
        lk = k.lower()
        if lk in _HOP:
            continue
        if lk == "location":
            if v.startswith(base):
                v = proxy_base + v[len(base):].lstrip("/")
            elif v.startswith("/"):          # root-relative (e.g. /onboarding.html, /login)
                v = proxy_base + v.lstrip("/")
        resp_headers[k] = v
    ctype = up.headers.get("content-type", "")
    content = up.content
    if "text/html" in ctype.lower():
        content = _inject_base(content, proxy_base)
    return Response(content=content, status_code=up.status_code, headers=resp_headers,
                    media_type=ctype.split(";")[0] if ctype else None)


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "integration.effect.plan", http_method="POST",
    http_path="/integrations/effect/plan", http_tags=["integration", "policy"],
    memory="off",
    description="Plan admission for one outbound integration operation without "
                "executing it. Classifies reads, idempotent writes, and non-idempotent "
                "writes; requires opaque approval-receipt and idempotency references "
                "where appropriate and returns only their SHA-256 digests. Inputs: "
                "connection_id, operation, method, idempotency_key, "
                "approval_receipt_ref, retry.",
)
async def integration_effect_plan(connection_id: str = "", operation: str = "",
                                  method: str = "GET", idempotency_key: str = "",
                                  approval_receipt_ref: str = "", retry: bool = False,
                                  trace_id=None):
    try:
        return _plan_external_effect(
            connection_id=connection_id, operation=operation, method=method,
            idempotency_key=idempotency_key,
            approval_receipt_ref=approval_receipt_ref, retry=retry)
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-plan/v1", "error": str(exc),
                "admission": {"allowed": False, "reasons": ["invalid_request"]},
                "executes": False, "resolves_secrets": False,
                "retains_payload": False}


@capability(
    "integration.effect.replay.status", http_method="POST",
    http_path="/integrations/effect/replay/status",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Inspect durable replay evidence for one previously generated "
                "external-effect plan without performing or retrying it. Input: plan "
                "(the complete vera.external-effect-plan/v1 object). Returns whether "
                "a matching successful receipt already exists; payloads and raw opaque "
                "references are never stored or returned.",
)
async def integration_effect_replay_status(plan: Optional[Dict] = None, trace_id=None):
    try:
        return default_external_effect_receipt_ledger().replay_status(plan or {})
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-replay-status/v1",
                "error": str(exc), "already_succeeded": False,
                "decision": "invalid_plan", "executes": False,
                "retries": False, "retains_payload": False}


@capability(
    "integration.effect.enforcement.activation", http_method="GET",
    http_path="/integrations/effect/enforcement/activation",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Read contract-bound generic API enforcement activation and bounded history. "
                "Effective enforcement requires the deployment gate, a matching current "
                "operator approval, and a fresh activation record.",
)
async def integration_effect_enforcement_activation(history_limit: int = 20, trace_id=None):
    try:
        decision = default_external_effect_enforcement_decisions().current()
        return default_external_effect_enforcement_activations().current(
            decision, runtime_gate=_effect_enforcement_runtime_gate(),
            history_limit=history_limit)
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-enforcement-activation/v1",
                "error": str(exc), "code": "invalid_request",
                "effective_mode": "observe_only", "enforcement_enabled": False}
    except Exception:
        log.exception("effect enforcement activation read failed")
        return {"schema": "vera.external-effect-enforcement-activation/v1",
                "error": "activation_state_unavailable",
                "code": "activation_state_unavailable",
                "effective_mode": "observe_only", "enforcement_enabled": False}


@capability(
    "integration.effect.enforcement.activate", http_method="POST",
    http_path="/integrations/effect/enforcement/activation",
    http_tags=["integration", "policy"], memory="on",
    redact_args=["actor_ref", "activation_receipt_ref"],
    description="Activate or deactivate generic Integration API effect enforcement. Inputs: "
                "action, expected_revision, actor_ref, and activation_receipt_ref for activation. "
                "Activation requires the deployment gate and matching current approval; "
                "deactivation is always available. It never retries an operation.",
)
async def integration_effect_enforcement_activate(
        action: str = "deactivate", expected_revision: int = 0,
        actor_ref: str = "", activation_receipt_ref: str = "", trace_id=None):
    try:
        decision = default_external_effect_enforcement_decisions().current()
        ledger = default_external_effect_enforcement_activations()
    except Exception:
        log.exception("effect enforcement activation state unavailable")
        return {"error": "activation_state_unavailable",
                "code": "activation_state_unavailable",
                "effective_mode": "observe_only", "enforcement_enabled": False}
    try:
        return ledger.apply(action=action, expected_revision=expected_revision,
                            decision=decision, actor_ref=actor_ref,
                            activation_receipt_ref=activation_receipt_ref,
                            runtime_gate=_effect_enforcement_runtime_gate())
    except ActivationConflict as exc:
        return {"error": str(exc), "code": "revision_conflict",
                "current": ledger.current(decision, runtime_gate=_effect_enforcement_runtime_gate())}
    except (TypeError, ValueError) as exc:
        return {"error": str(exc), "code": "invalid_activation",
                "current": ledger.current(decision, runtime_gate=_effect_enforcement_runtime_gate())}


@capability(
    "integration.effect.enforcement.decision", http_method="GET",
    http_path="/integrations/effect/enforcement/decision",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Read the current revision-guarded operator decision and bounded history "
                "for future external-effect enforcement. Effective runtime mode remains "
                "observe-only; no payloads or raw identities are returned.",
)
async def integration_effect_enforcement_decision(history_limit: int = 20, trace_id=None):
    try:
        return default_external_effect_enforcement_decisions().current(
            history_limit=history_limit)
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-enforcement-decision/v1",
                "error": str(exc), "revision": 0,
                "decision": "continue_observing", "requested_mode": "observe_only",
                "effective_mode": "observe_only", "enforcement_enabled": False,
                "history": [], "executes": False, "changes_runtime_policy": False,
                "retains_payload": False}


@capability(
    "integration.effect.enforcement.decide", http_method="POST",
    http_path="/integrations/effect/enforcement/decision",
    http_tags=["integration", "policy"], memory="on",
    redact_args=["actor_ref", "approval_receipt_ref"],
    description="Record a reversible operator decision to continue observation or approve "
                "a future enforcement rollout. Inputs: decision, expected_revision, actor_ref, "
                "and approval_receipt_ref for approval. Approval requires current readiness. "
                "This records hashed intent only; effective runtime mode remains observe-only.",
)
async def integration_effect_enforcement_decide(
        decision: str = "continue_observing", expected_revision: int = 0,
        actor_ref: str = "", approval_receipt_ref: str = "", trace_id=None):
    from Vera.vera.integrations.effect_shadow_evidence import evaluate_enforcement_readiness
    try:
        readiness = evaluate_enforcement_readiness(
            default_external_effect_shadow_evidence().summary(limit=200))
    except Exception:
        return {"error": "evidence_unavailable", "code": "evidence_unavailable",
                "effective_mode": "observe_only", "enforcement_enabled": False}
    try:
        return default_external_effect_enforcement_decisions().decide(
            decision=decision, expected_revision=expected_revision,
            actor_ref=actor_ref, approval_receipt_ref=approval_receipt_ref,
            readiness=readiness)
    except DecisionConflict as exc:
        return {"error": str(exc), "code": "revision_conflict",
                "current": default_external_effect_enforcement_decisions().current()}
    except (TypeError, ValueError) as exc:
        return {"error": str(exc), "code": "invalid_decision",
                "current": default_external_effect_enforcement_decisions().current()}


@capability(
    "integration.effect.enforcement.readiness", http_method="GET",
    http_path="/integrations/effect/enforcement/readiness",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Assess whether observe-only external-effect evidence is sufficiently "
                "representative for operator review. This fail-closed assessment does not "
                "prove safety, authorize enforcement, change policy, execute, or retain payloads.",
)
async def integration_effect_enforcement_readiness(trace_id=None):
    from Vera.vera.integrations.effect_shadow_evidence import evaluate_enforcement_readiness
    try:
        evidence = default_external_effect_shadow_evidence().summary(limit=200)
        return evaluate_enforcement_readiness(evidence)
    except Exception:
        result = evaluate_enforcement_readiness({"totals": {}, "classifications": {}})
        result["error"] = "evidence_unavailable"
        return result


@capability(
    "integration.effect.shadow.evidence", http_method="GET",
    http_path="/integrations/effect/shadow/evidence",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Inspect bounded payload-free aggregates of observe-only external-effect "
                "decisions. Returns admission, execution and replay-suppression counts plus "
                "reason codes; it cannot enforce, execute, retry, open secrets, or retain payloads.",
)
async def integration_effect_shadow_evidence(limit: int = 50, trace_id=None):
    try:
        return default_external_effect_shadow_evidence().summary(limit=limit)
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-shadow-evidence/v1", "error": str(exc),
                "totals": {"observations": 0, "would_admit": 0,
                           "would_execute": 0, "would_suppress": 0},
                "classifications": {}, "reasons_in_window": {}, "recent": [],
                "window": {"requested": 0, "returned": 0},
                "enforcement": "observe_only", "executes": False,
                "retries": False, "retains_payload": False}


@capability(
    "integration.effect.receipts", http_method="GET",
    http_path="/integrations/effect/receipts",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Inspect a bounded payload-free summary of durable external-effect "
                "receipts. Inputs: limit and optional SHA-256 plan_id. Returns "
                "counts plus recent hashed identities and outcome evidence; it "
                "cannot execute, retry, open secrets, or record a receipt.",
)
async def integration_effect_receipts(limit: int = 50, plan_id: str = "",
                                      trace_id=None):
    try:
        return default_external_effect_receipt_ledger().summary(
            limit=limit, plan_id=plan_id)
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-receipt-summary/v1",
                "error": str(exc), "totals": {"plans": 0, "receipts": 0,
                                               "observations": 0},
                "outcomes": {}, "recent": [],
                "window": {"requested": 0, "returned": 0},
                "executes": False, "retries": False, "retains_payload": False}


@capability(
    "integration.effect.retry.policy", http_method="GET",
    http_path="/integrations/effect/retry/policy",
    http_tags=["integration", "policy"], memory="off", silent=True,
    description="Describe the bounded retry decision vocabulary used for external "
                "effects. Returns transient outcome classes, refusal explanations, "
                "requirements, and hard bounds; it never executes, sleeps, retries, "
                "opens secrets, records receipts, or retains payloads.",
)
async def integration_effect_retry_policy(trace_id=None):
    from Vera.vera.integrations.effect_retry import describe_retry_policy
    return describe_retry_policy()


@capability(
    "integration.effect.retry.plan", http_method="POST",
    http_path="/integrations/effect/retry/plan",
    http_tags=["integration", "policy"], memory="off",
    description="Plan whether and when a failed or rate-limited external-effect "
                "attempt may be retried. Inputs: plan, attempts_completed, "
                "max_attempts, status_code or a stable error_code, optional "
                "retry/rate-limit timing, successful_receipt, and bounded "
                "backoff settings. Returns a delay window and reason codes; it "
                "never sleeps, retries, executes, resolves secrets, or records a receipt.",
)
async def integration_effect_retry_plan(
        plan: Optional[Dict] = None, attempts_completed: int = 1,
        max_attempts: int = 3, status_code: int = 0, error_code: str = "",
        retry_after_ms: Optional[int] = None, rate_limit: Optional[int] = None,
        rate_remaining: Optional[int] = None,
        rate_reset_after_ms: Optional[int] = None,
        successful_receipt: bool = False, base_delay_ms: int = 250,
        backoff_cap_ms: int = 30_000, trace_id=None):
    try:
        return _plan_effect_retry(
            plan or {}, attempts_completed=attempts_completed,
            max_attempts=max_attempts, status_code=status_code,
            error_code=error_code, retry_after_ms=retry_after_ms,
            rate_limit=rate_limit, rate_remaining=rate_remaining,
            rate_reset_after_ms=rate_reset_after_ms,
            successful_receipt=successful_receipt,
            base_delay_ms=base_delay_ms, backoff_cap_ms=backoff_cap_ms)
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-effect-retry-plan/v1",
                "error": str(exc),
                "schedule": {"allowed": False, "reasons": ["invalid_request"],
                             "earliest_delay_ms": 0, "latest_delay_ms": 0,
                             "selection": "none"},
                "executes": False, "sleeps": False, "records_receipt": False,
                "resolves_secrets": False, "retains_payload": False}


@capability(
    "integration.source.lifecycle", http_method="GET",
    http_path="/integrations/source/lifecycle", http_tags=["integration", "intake"],
    memory="off", silent=True,
    description="Return the deterministic W3-06 external-source lifecycle contract. "
                "This inspection surface performs no fetch, install, build, secret "
                "resolution, activation, model call, network request, or execution.",
)
async def integration_source_lifecycle(trace_id=None):
    return _source_lifecycle_contract()


@capability(
    "integration.source.inspect", http_method="POST",
    http_path="/integrations/source/inspect", http_tags=["integration", "intake"],
    memory="off",
    description="Inspect one bounded inline MCP descriptor or OpenAPI document and "
                "project unauthorised capability candidates. Inputs: kind (mcp|openapi), "
                "document (object!), source_id (optional for OpenAPI). No URL is fetched "
                "and no catalog/integration record is written.",
)
async def integration_source_inspect(kind: str = "", document: Optional[Dict] = None,
                                     source_id: str = "", trace_id=None):
    try:
        return _inspect_source(kind, document or {}, source_id=source_id).to_dict()
    except (TypeError, ValueError) as exc:
        return {"error": str(exc), "kind": kind, "accepted": False,
                "registers": False, "network_io": False, "executes": False}


@capability(
    "integration.source.transition.plan", http_method="POST",
    http_path="/integrations/source/transition/plan",
    http_tags=["integration", "intake"], memory="off",
    description="Plan one adjacent external-source lifecycle transition without "
                "applying it. W3-06 permits inspected/proposed planning only; build and "
                "later states remain queued. Inputs: source_id, current, target, "
                "evidence_refs (list).",
)
async def integration_source_transition_plan(
        source_id: str = "", current: str = "", target: str = "",
        evidence_refs: Optional[List[str]] = None, trace_id=None):
    try:
        return _plan_source_transition(
            source_id, current, target, tuple(evidence_refs or ()))
    except (TypeError, ValueError) as exc:
        return {"error": str(exc), "allowed": False, "applied": False,
                "installs": False, "builds": False, "activates": False,
                "executes": False}


@capability(
    "integration.source.build.status", http_method="GET",
    http_path="/integrations/source/build/status",
    http_tags=["integration", "intake"], memory="off", silent=True,
    description="Return the deterministic W3-07 build/activation proposal contract. "
                "Python, CLI, OCI, and repository execution remain queued; this "
                "surface performs no fetch, install, build, activation, secret "
                "resolution, network request, or external execution.",
)
async def integration_source_build_status(trace_id=None):
    return _source_build_plan_contract()


@capability(
    "integration.source.build.plan", http_method="POST",
    http_path="/integrations/source/build/plan",
    http_tags=["integration", "intake"], memory="off",
    description="Validate one bounded inline Python, CLI, OCI, or repository "
                "descriptor and return an inert provenance/evidence/approval/"
                "rollback plan. Input: document (object!). The plan is not applied.",
)
async def integration_source_build_plan(document: Optional[Dict] = None,
                                        trace_id=None):
    try:
        return _plan_source_build(document or {}).to_dict()
    except (TypeError, ValueError) as exc:
        return {"schema": "vera.external-source-build-plan/v1",
                "error": str(exc), "accepted": False,
                "ready_for_build": False, "ready_for_activation": False,
                "credentials_resolved": False, "network_io": False,
                "fetches": False, "installs": False, "builds": False,
                "activates": False, "registers": False, "executes": False}


@capability(
    "integration.panel.html",
    http_method="GET", http_path="/integrations/panel", http_tags=["integration", "ui"],
    memory="off", silent=True,
    description="Serve the Integrations Hub panel HTML.",
)
async def cap_panel(trace_id=None):
    p = _HERE / "integrations_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>integrations_panel.html not found</p>")


register_ui(
    "integrations",
    "Integrations",
    "⛓",
    html="""<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/integrations/panel" style="flex:1;border:none;width:100%;height:100%;
          background:var(--bg0,#0d0f12)" allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    ui_caps=[
        "integration.list", "integration.get", "integration.save",
        "integration.delete", "integration.access.set", "integration.operate",
        "integration.api.call", "integration.mcp.call", "integration.connections",
        "integration.connections.project",
        "integration.discover", "integration.identity.register",
        "integration.import_apps", "identity.resolve.status",
        "integration.source.lifecycle", "integration.source.inspect",
        "integration.source.transition.plan",
        "integration.source.build.status", "integration.source.build.plan",
        "integration.effect.plan", "integration.effect.replay.status",
        "integration.effect.retry.plan", "integration.effect.retry.policy",
        "integration.effect.enforcement.readiness",
        "integration.effect.enforcement.decision",
        "integration.effect.enforcement.decide",
        "integration.effect.enforcement.activation",
        "integration.effect.enforcement.activate",
        "integration.effect.shadow.evidence",
        "integration.effect.receipts",
        # the one-click "register & secure everything" button drives autoenroll
        "autoenroll.scan", "autoenroll.run", "autoenroll.pending",
    ],
    mode="tab",
    tab_order=58,
)


log.info("integrations_capabilities loaded — integration.* (Integrations Hub)")
