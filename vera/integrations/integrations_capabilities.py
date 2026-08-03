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

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                   # pragma: no cover
    vsecrets = None                                 # type: ignore

log = logging.getLogger("vera.integrations")

_HERE = Path(__file__).parent
KEY_INTEGRATIONS = "vera:integrations"

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
    description="Call an integration's HTTP API through an authenticated "
                "passthrough (the sealed token is injected server-side and never "
                "reaches the browser). REQUIRES access.api. Inputs: id (str!), "
                "method (GET|POST|PUT|DELETE|PATCH), path (str — appended to the "
                "kind's api_base, e.g. '/repos'), query (dict), body (dict/str), "
                "headers (dict — extra). Output: {ok, status, body, json?} or "
                "{error, code:403}.",
    schema={"properties": {"method": {"enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]}}},
)
async def cap_api_call(id: str = "", method: str = "GET", path: str = "",
                       query: Optional[Dict] = None, body: Any = None,
                       headers: Optional[Dict] = None, trace_id=None) -> Dict:
    rec = await _get(id)
    gate = _require_access(rec, "api")
    if gate:
        return gate
    base = _base_url(rec)
    if not base:
        return {"error": "integration has no resolvable URL"}
    spec = KIND_SPECS.get(rec.get("kind", "generic"), {})
    api_base = (rec.get("api") or {}).get("api_base", spec.get("api_base", ""))
    url = base + api_base + ("/" + path.lstrip("/") if path else "")
    hdrs = dict(headers or {})
    _apply_api_auth(rec, hdrs)
    verify = rec.get("scheme") == "https" and rec.get("verify_tls", False)
    await _audit("api_call", rec, method=method, path=path)
    try:
        async with httpx.AsyncClient(timeout=30, verify=verify,
                                     follow_redirects=True) as c:
            r = await c.request(method.upper(), url, params=query or None,
                                json=body if isinstance(body, (dict, list)) else None,
                                content=body if isinstance(body, str) else None,
                                headers=hdrs)
    except Exception as e:
        return {"error": f"upstream {type(e).__name__}: {e}"}
    out: Dict[str, Any] = {"ok": r.status_code < 400, "status": r.status_code,
                           "url": url}
    try:
        out["json"] = r.json()
    except Exception:
        out["body"] = r.text[:20000]
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
    target = base + "/" + path + (("?" + qs) if qs else "")
    proxy_base = f"/integrations/{iid}/embed/"
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _HOP and k.lower() != "host"}
    body = await request.body()
    verify = bool(rec.get("verify_tls"))
    try:
        async with httpx.AsyncClient(verify=verify, timeout=45,
                                     follow_redirects=False) as c:
            up = await c.request(request.method, target, headers=fwd,
                                 content=body if body else None)
    except Exception as e:
        return JSONResponse({"error": f"upstream {type(e).__name__}: {e}"}, status_code=502)
    resp_headers = {}
    for k, v in up.headers.items():
        lk = k.lower()
        if lk in _HOP:
            continue
        if lk == "location" and v.startswith(base):
            v = proxy_base + v[len(base):].lstrip("/")
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
    "🧩",
    html="""<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/integrations/panel" style="flex:1;border:none;width:100%;height:100%;
          background:var(--bg0,#0d0f12)" allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    ui_caps=[
        "integration.list", "integration.get", "integration.save",
        "integration.delete", "integration.access.set", "integration.operate",
        "integration.api.call", "integration.mcp.call", "integration.connections",
        "integration.discover", "integration.identity.register",
        "identity.resolve.status",
        # the one-click "register & secure everything" button drives autoenroll
        "autoenroll.scan", "autoenroll.run", "autoenroll.pending",
    ],
    mode="tab",
    tab_order=58,
)


log.info("integrations_capabilities loaded — integration.* (Integrations Hub)")
