"""
identity_capabilities.py — Vera Security Suite · Identity core (Iteration 4)
===========================================================================

FreeIPA is the identity core: LDAP + Kerberos + DNS + (Dogtag) CA + host / user /
service registration. Vera talks to it over its JSON-RPC API and exposes
registration as capabilities so devices, users and applications can be enrolled
(by hand or automatically from the provisioning flow). Windows clients are
handled by an optional Samba AD DC joined via an IPA↔AD cross-forest **trust**
(`identity.trust.add`), the supported topology.

Run model is "both" — adopt an existing FreeIPA (probe + login) or, in a later
step, provision one as an appliance.

Capabilities (groups `identity.*`)
──────────────────────────────────
  identity.config.get / .save      — IPA connection (admin password sealed)
  identity.status                  — reachable + logged-in probe
  identity.host.register / .list   — register/enrol a device (host)
  identity.user.register / .list   — register a user
  identity.service.register        — register a Kerberos service (an app backend)
  identity.dns.record              — add a DNS A record
  identity.app.register            — one-shot app: DNS + service + TLS cert
  identity.trust.add               — establish the AD cross-forest trust (Windows)

FreeIPA JSON-RPC
────────────────
Login: POST /ipa/session/login_password (form user+password) → session cookie.
Call:  POST /ipa/session/json {method, params:[[args],{options}], id} with a
Referer header (FreeIPA requires it). httpx keeps the cookie jar per client, so
login + call share one AsyncClient.

Redis layout
────────────
  vera:provisioning:identity   hash  field 'main' -> JSON   (ipa_password sealed)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.identity")

_HERE = Path(__file__).parent
KEY_STATE = "vera:provisioning:identity"
_STATE_FIELD = "main"
_SECRET_FIELDS = ("ipa_password",)

_DEFAULTS: Dict[str, Any] = {
    "ipa_url": "", "ipa_domain": "", "ipa_realm": "", "ipa_user": "admin",
    "dns_zone": "", "verify_tls": False,
}


# ═════════════════════════════════════════════════════════════════════════════
#  STATE
# ═════════════════════════════════════════════════════════════════════════════
def _redis():
    return getattr(_orch, "REDIS", None)


async def _state_raw() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(_DEFAULTS)
    raw = await r.hget(KEY_STATE, _STATE_FIELD)
    if not raw:
        return dict(_DEFAULTS)
    try:
        return {**_DEFAULTS, **json.loads(raw)}
    except Exception:
        return dict(_DEFAULTS)


async def _state_opened() -> Dict[str, Any]:
    st = await _state_raw()
    for f in _SECRET_FIELDS:
        st[f] = vsecrets.open_secret(st.get(f, ""))
    return st


def _redact(st: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in st.items():
        out[f"has_{k}"] = bool(v) if k in _SECRET_FIELDS else None
        if k not in _SECRET_FIELDS:
            out[k] = v
    return {k: v for k, v in out.items() if v is not None or k.startswith("has_")}


async def _state_save(patch: Dict[str, Any]) -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {"error": "store unavailable (no Redis)"}
    cur = await _state_raw()
    for k, v in patch.items():
        if k in _SECRET_FIELDS:
            if v:
                cur[k] = vsecrets.seal(v)
        elif v is not None:
            cur[k] = v
    cur["updated"] = now_iso()
    await r.hset(KEY_STATE, _STATE_FIELD, json.dumps(cur))
    return cur


# ═════════════════════════════════════════════════════════════════════════════
#  FREEIPA JSON-RPC CLIENT
# ═════════════════════════════════════════════════════════════════════════════
async def _ipa_call(st: Dict, method: str, args: Optional[List] = None,
                    options: Optional[Dict] = None) -> Tuple[Optional[Any], str]:
    url = (st.get("ipa_url") or "").rstrip("/")
    if not url:
        return None, "ipa_url not configured"
    user, pw = st.get("ipa_user", ""), st.get("ipa_password", "")
    if not user or not pw:
        return None, "IPA admin credentials not configured"
    referer = url + "/ipa"
    opts = dict(options or {})
    opts.setdefault("version", st.get("ipa_api_version", "2.253"))
    payload = {"method": method, "params": [args or [], opts], "id": 0}
    try:
        async with httpx.AsyncClient(timeout=30, verify=bool(st.get("verify_tls"))) as c:
            lr = await c.post(url + "/ipa/session/login_password",
                              data={"user": user, "password": pw},
                              headers={"referer": referer,
                                       "Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "text/plain"})
            if lr.status_code >= 400:
                return None, f"login HTTP {lr.status_code}: {lr.text[:200]}"
            jr = await c.post(url + "/ipa/session/json", json=payload,
                              headers={"referer": referer, "Accept": "application/json",
                                       "Content-Type": "application/json"})
            if jr.status_code >= 400:
                return None, f"json HTTP {jr.status_code}: {jr.text[:200]}"
            d = jr.json()
            if d.get("error"):
                return None, (d["error"] or {}).get("message", "IPA error")
            return d.get("result"), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _cap(name: str):
    """Look up another capability's callable (cross-module, optional)."""
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG + STATUS
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "identity.config.get",
    http_method="GET", http_path="/identity/config", http_tags=["identity"],
    memory="off", silent=True,
    description="Get FreeIPA connection config (admin password redacted to "
                "has_ipa_password). Output: config object.",
)
async def cap_config_get(trace_id=None) -> Dict:
    return _redact(await _state_raw())


@capability(
    "identity.config.save",
    http_method="POST", http_path="/identity/config/save", http_tags=["identity"],
    memory="off",
    description="Set FreeIPA connection. ipa_password is SEALED (Fernet); blank "
                "keeps existing. Inputs: ipa_url ('https://ipa.lan'), ipa_domain "
                "('lan'), ipa_realm ('LAN'), ipa_user ('admin'), ipa_password, "
                "dns_zone ('lan.'), verify_tls (bool). Output: redacted config.",
)
async def cap_config_save(
    ipa_url: str = "", ipa_domain: str = "", ipa_realm: str = "",
    ipa_user: str = "", ipa_password: str = "", dns_zone: str = "",
    verify_tls: Optional[bool] = None, trace_id=None,
) -> Dict:
    patch = {"ipa_url": ipa_url or None, "ipa_domain": ipa_domain or None,
             "ipa_realm": ipa_realm or None, "ipa_user": ipa_user or None,
             "ipa_password": ipa_password, "dns_zone": dns_zone or None,
             "verify_tls": verify_tls}
    saved = await _state_save({k: v for k, v in patch.items() if v is not None})
    if saved.get("error"):
        return saved
    return _redact(saved)


@capability(
    "identity.status",
    http_method="GET", http_path="/identity/status", http_tags=["identity"],
    memory="off", silent=True,
    description="Probe FreeIPA: reachable + admin login OK. Output: "
                "{configured, reachable, version, error}.",
)
async def cap_status(trace_id=None) -> Dict:
    st = await _state_opened()
    out = {"configured": bool(st.get("ipa_url")), "reachable": False, "version": ""}
    if not out["configured"]:
        return out
    res, err = await _ipa_call(st, "env", args=[["version"]])
    if res is not None:
        out["reachable"] = True
        try:
            out["version"] = (res.get("result") or {}).get("version", "") \
                if isinstance(res, dict) else ""
        except Exception:
            pass
    else:
        out["error"] = err
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "identity.host.register",
    http_method="POST", http_path="/identity/host/register", http_tags=["identity"],
    memory="off",
    description="Register (enrol) a device as an IPA host. Inputs: fqdn (str!), "
                "ip (str — also creates a DNS A record when set), os (str), "
                "random_otp (bool=true — return a one-time enrolment password). "
                "Output: {ok, fqdn, otp} or {error}.",
)
async def cap_host_register(fqdn: str = "", ip: str = "", os: str = "",
                            random_otp: bool = True, trace_id=None) -> Dict:
    if not fqdn:
        return {"error": "fqdn required"}
    st = await _state_opened()
    opts: Dict[str, Any] = {"force": True}
    if random_otp:
        opts["random"] = True
    if ip:
        opts["ip_address"] = ip          # FreeIPA auto-creates the A record
    if os:
        opts["nsosversion"] = os
    res, err = await _ipa_call(st, "host_add", args=[fqdn], options=opts)
    if res is None:
        return {"error": err}
    otp = ""
    try:
        otp = (res.get("result") or {}).get("randompassword", "")
    except Exception:
        pass
    await emit_event({"type": "identity.host.registered", "fqdn": fqdn})
    return {"ok": True, "fqdn": fqdn, "otp": otp}


@capability(
    "identity.host.list",
    http_method="GET", http_path="/identity/host/list", http_tags=["identity"],
    memory="off", silent=True,
    description="List registered IPA hosts. Output: {hosts:[{fqdn, enrolled}]}.",
)
async def cap_host_list(trace_id=None) -> Dict:
    st = await _state_opened()
    res, err = await _ipa_call(st, "host_find", args=[""], options={"sizelimit": 200})
    if res is None:
        return {"error": err, "hosts": []}
    hosts = []
    for h in (res.get("result") or []):
        fq = (h.get("fqdn") or [""])[0] if isinstance(h.get("fqdn"), list) else h.get("fqdn", "")
        hosts.append({"fqdn": fq, "enrolled": bool(h.get("has_keytab"))})
    return {"hosts": hosts}


@capability(
    "identity.host.delete",
    http_method="POST", http_path="/identity/host/delete", http_tags=["identity"],
    memory="off",
    description="Deregister (remove) an IPA host — the decommission counterpart of "
                "identity.host.register, so a torn-down guest doesn't leave a stale "
                "host record + DNS entry (real-VM E2E finding 2026-08-10). Inputs: "
                "fqdn (str!), updatedns (bool=true — also remove its DNS A record). "
                "Output: {ok, fqdn} or {error}.",
)
async def cap_host_delete(fqdn: str = "", updatedns: bool = True, trace_id=None) -> Dict:
    if not fqdn:
        return {"error": "fqdn required"}
    st = await _state_opened()
    opts: Dict[str, Any] = {"updatedns": True} if updatedns else {}
    res, err = await _ipa_call(st, "host_del", args=[fqdn], options=opts)
    if res is None:
        return {"error": err}
    await emit_event({"type": "identity.host.deleted", "fqdn": fqdn})
    return {"ok": True, "fqdn": fqdn}


@capability(
    "identity.user.register",
    http_method="POST", http_path="/identity/user/register", http_tags=["identity"],
    memory="off",
    description="Register an IPA user. Inputs: login (str!), first (str!), "
                "last (str!), email (str), password (str — initial). "
                "Output: {ok, login} or {error}.",
)
async def cap_user_register(login: str = "", first: str = "", last: str = "",
                            email: str = "", password: str = "", trace_id=None) -> Dict:
    if not (login and first and last):
        return {"error": "login, first, last required"}
    st = await _state_opened()
    opts = {"givenname": first, "sn": last}
    if email:
        opts["mail"] = email
    if password:
        opts["userpassword"] = password
    res, err = await _ipa_call(st, "user_add", args=[login], options=opts)
    if res is None:
        return {"error": err}
    await emit_event({"type": "identity.user.registered", "login": login})
    return {"ok": True, "login": login}


@capability(
    "identity.user.list",
    http_method="GET", http_path="/identity/user/list", http_tags=["identity"],
    memory="off", silent=True,
    description="List IPA users. Output: {users:[{login, name}]}.",
)
async def cap_user_list(trace_id=None) -> Dict:
    st = await _state_opened()
    res, err = await _ipa_call(st, "user_find", args=[""], options={"sizelimit": 200})
    if res is None:
        return {"error": err, "users": []}
    users = []
    for u in (res.get("result") or []):
        uid = (u.get("uid") or [""])[0] if isinstance(u.get("uid"), list) else u.get("uid", "")
        cn = (u.get("cn") or [""])[0] if isinstance(u.get("cn"), list) else u.get("cn", "")
        users.append({"login": uid, "name": cn})
    return {"users": users}


@capability(
    "identity.group.list",
    http_method="GET", http_path="/identity/group/list", http_tags=["identity"],
    memory="off", silent=True,
    description="List IPA groups (permission sets). Output: {groups:[{name, "
                "description, members}]}.",
)
async def cap_group_list(trace_id=None) -> Dict:
    st = await _state_opened()
    res, err = await _ipa_call(st, "group_find", args=[""], options={"sizelimit": 200})
    if res is None:
        return {"error": err, "groups": []}
    groups = []
    for g in (res.get("result") or []):
        cn = (g.get("cn") or [""])[0] if isinstance(g.get("cn"), list) else g.get("cn", "")
        desc = (g.get("description") or [""])
        desc = desc[0] if isinstance(desc, list) else desc
        groups.append({"name": cn, "description": desc,
                       "members": g.get("member_user", []) or []})
    return {"groups": groups}


@capability(
    "identity.group.register",
    http_method="POST", http_path="/identity/group/register", http_tags=["identity"],
    memory="off",
    description="Create an IPA group (permission set). Inputs: name (str!), "
                "description (str). Output: {ok, name} or {error}.",
)
async def cap_group_register(name: str = "", description: str = "",
                             trace_id=None) -> Dict:
    if not name:
        return {"error": "name required"}
    st = await _state_opened()
    opts = {"description": description} if description else {}
    res, err = await _ipa_call(st, "group_add", args=[name], options=opts)
    if res is None:
        # tolerate "already exists" so migration is idempotent
        if "already exists" in (err or "").lower():
            return {"ok": True, "name": name, "existed": True}
        return {"error": err}
    await emit_event({"type": "identity.group.registered", "name": name})
    return {"ok": True, "name": name}


@capability(
    "identity.group.add_member",
    http_method="POST", http_path="/identity/group/add_member", http_tags=["identity"],
    memory="off",
    description="Add a user to an IPA group. Inputs: group (str!), login (str!). "
                "Output: {ok} or {error}.",
)
async def cap_group_add_member(group: str = "", login: str = "",
                               trace_id=None) -> Dict:
    if not group or not login:
        return {"error": "group and login required"}
    st = await _state_opened()
    res, err = await _ipa_call(st, "group_add_member", args=[group],
                               options={"user": [login]})
    if res is None:
        return {"error": err}
    return {"ok": True, "group": group, "login": login}


@capability(
    "identity.service.register",
    http_method="POST", http_path="/identity/service/register", http_tags=["identity"],
    memory="off",
    description="Register a Kerberos service principal (an application backend). "
                "Inputs: service (str — e.g. 'HTTP'), fqdn (str!). Builds "
                "'<service>/<fqdn>'. Output: {ok, principal} or {error}.",
)
async def cap_service_register(service: str = "HTTP", fqdn: str = "",
                               trace_id=None) -> Dict:
    if not fqdn:
        return {"error": "fqdn required"}
    st = await _state_opened()
    principal = f"{service}/{fqdn}"
    res, err = await _ipa_call(st, "service_add", args=[principal],
                               options={"force": True})
    if res is None:
        return {"error": err}
    return {"ok": True, "principal": principal}


@capability(
    "identity.dns.record",
    http_method="POST", http_path="/identity/dns/record", http_tags=["identity"],
    memory="off",
    description="Add a DNS A record. Inputs: name (str! — host label, not FQDN), "
                "ip (str!), zone (str — defaults to configured dns_zone). "
                "Output: {ok} or {error}.",
)
async def cap_dns_record(name: str = "", ip: str = "", zone: str = "",
                         trace_id=None) -> Dict:
    if not name or not ip:
        return {"error": "name and ip required"}
    st = await _state_opened()
    zone = zone or st.get("dns_zone", "")
    if not zone:
        return {"error": "no zone (set dns_zone in config or pass zone)"}
    res, err = await _ipa_call(st, "dnsrecord_add", args=[zone, name],
                               options={"a_part_ip_address": ip})
    if res is None:
        return {"error": err}
    return {"ok": True}


@capability(
    "identity.app.register",
    http_method="POST", http_path="/identity/app/register", http_tags=["identity"],
    memory="off",
    description="One-shot application registration: DNS A record + HTTP service "
                "principal + TLS cert (via pki.cert.issue if the PKI is set up). "
                "Inputs: name (str! — host label), ip (str), port (int), "
                "ssh_host_id (str — for cert install). Output: {ok, fqdn, dns, "
                "service, cert}.",
)
async def cap_app_register(name: str = "", ip: str = "", port: int = 0,
                           ssh_host_id: str = "", trace_id=None) -> Dict:
    if not name:
        return {"error": "name required"}
    st = await _state_opened()
    zone = st.get("dns_zone", "").rstrip(".")
    domain = st.get("ipa_domain", "") or zone
    fqdn = name if "." in name else (f"{name}.{domain}" if domain else name)
    out: Dict[str, Any] = {"ok": True, "fqdn": fqdn}
    if ip:
        out["dns"] = await cap_dns_record(name=name, ip=ip)
    out["service"] = await cap_service_register(service="HTTP", fqdn=fqdn)
    issue = _cap("pki.cert.issue")
    if issue:
        out["cert"] = await issue(fqdn=fqdn, ssh_host_id=ssh_host_id, install=bool(ssh_host_id))
    else:
        out["cert"] = {"skipped": "PKI not loaded"}
    await emit_event({"type": "identity.app.registered", "fqdn": fqdn})
    return out


@capability(
    "identity.ca.cert",
    http_method="GET", http_path="/identity/ca/cert", http_tags=["identity"],
    memory="off", silent=True,
    description="Fetch the FreeIPA Dogtag CA certificate (PEM) — the org root of "
                "trust to distribute to clients (browsers, the VS Code extension, "
                "etc.) before migrating any cert onto it. Output: {ok, ca_pem}.",
)
async def cap_ca_cert(trace_id=None) -> Dict:
    st = await _state_opened()
    url = (st.get("ipa_url") or "").rstrip("/")
    if not url:
        return {"error": "ipa_url not configured"}
    try:
        async with httpx.AsyncClient(timeout=15, verify=bool(st.get("verify_tls"))) as c:
            r = await c.get(url + "/ipa/config/ca.crt")
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}"}
            return {"ok": True, "ca_pem": r.text}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@capability(
    "identity.cert.request",
    http_method="POST", http_path="/identity/cert/request", http_tags=["identity"],
    memory="on",
    description="Issue a TLS cert for an FQDN from FreeIPA's Dogtag CA (the org "
                "root). Submits a CSR via FreeIPA's cert_request; auto-adds the "
                "host + HTTP service principal (add=true). This is the mechanism to "
                "RE-ISSUE Vera's own cert onto the FreeIPA root. Inputs: fqdn "
                "(str!), csr (str! — PEM CSR whose CN/SAN is the fqdn), add "
                "(bool=true). Output: {ok, fqdn, cert_pem, serial} or {error}. "
                "Pair with identity.ca.cert to build the full chain, and distribute "
                "that CA to clients BEFORE swapping any live cert.",
)
async def cap_cert_request(fqdn: str = "", csr: str = "", add: bool = True,
                           trace_id=None) -> Dict:
    if not fqdn or not csr:
        return {"error": "fqdn and csr (PEM) are required"}
    st = await _state_opened()
    # Ensure the host exists (cert_request add= handles the service principal).
    await _ipa_call(st, "host_add", args=[fqdn], options={"force": True})
    principal = f"HTTP/{fqdn}"
    res, err = await _ipa_call(st, "cert_request", args=[csr],
                               options={"principal": principal, "add": bool(add),
                                        "cacn": "ipa"})
    if res is None:
        return {"error": err}
    result = res.get("result", res) if isinstance(res, dict) else {}
    b64 = result.get("certificate", "")
    cert_pem = ""
    if b64:
        body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
        cert_pem = f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"
    await emit_event({"type": "identity.cert.issued", "fqdn": fqdn,
                      "backend": "freeipa"})
    return {"ok": bool(cert_pem), "fqdn": fqdn, "cert_pem": cert_pem,
            "serial": result.get("serial_number", ""), "principal": principal}


@capability(
    "identity.trust.add",
    http_method="POST", http_path="/identity/trust/add", http_tags=["identity"],
    memory="off",
    description="Establish the IPA↔AD cross-forest trust so Windows clients in a "
                "Samba/AD domain interoperate. Inputs: ad_domain (str!), ad_admin "
                "(str!), ad_password (str! — used transiently, never stored). "
                "Output: {ok} or {error}. Requires ipa-server-trust-ad installed "
                "on the IPA master.",
)
async def cap_trust_add(ad_domain: str = "", ad_admin: str = "",
                        ad_password: str = "", trace_id=None) -> Dict:
    if not (ad_domain and ad_admin and ad_password):
        return {"error": "ad_domain, ad_admin, ad_password required"}
    st = await _state_opened()
    res, err = await _ipa_call(st, "trust_add", args=[ad_domain],
                               options={"realm_admin": ad_admin,
                                        "realm_passwd": ad_password,
                                        "trust_type": "ad"})
    if res is None:
        return {"error": err}
    return {"ok": True, "domain": ad_domain}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/identity/panel", include_in_schema=False)
async def _identity_panel():
    p = _HERE / "identity_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>identity_panel.html not found</p>")


async def _fetch_ca_pem() -> str:
    """Fetch the FreeIPA Dogtag CA PEM (empty string on failure)."""
    st = await _state_opened()
    url = (st.get("ipa_url") or "").rstrip("/")
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15, verify=bool(st.get("verify_tls"))) as c:
            r = await c.get(url + "/ipa/config/ca.crt")
            return r.text if r.status_code < 400 else ""
    except Exception:
        return ""


@APP.get("/identity/ca.crt", include_in_schema=False)
async def _identity_ca_crt():
    """Serve the CA as a double-click-installable .crt (launches the OS cert
    wizard on Windows/macOS)."""
    pem = await _fetch_ca_pem()
    if not pem:
        return HTMLResponse("CA unavailable — is FreeIPA configured?", status_code=502)
    from starlette.responses import Response as _Resp
    return _Resp(pem, media_type="application/x-x509-ca-cert",
                 headers={"Content-Disposition":
                          'attachment; filename="vera-freeipa-ca.crt"'})


@APP.get("/trust", include_in_schema=False)
async def _trust_page():
    p = _HERE / "trust_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>trust_panel.html not found</p>")


# Standalone top-level tab retired — embedded as the Identity sub-tab of the
# workers/Ollama panel's Provision pane. The /identity/panel route is kept.
register_ui = (lambda *a, **k: None)
register_ui(
    "identity-panel",
    "Identity",
    "🆔",
    """<div id="identity-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/identity/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "identity.config.get", "identity.config.save", "identity.status",
        "identity.host.register", "identity.host.list",
        "identity.user.register", "identity.user.list",
        "identity.service.register", "identity.dns.record",
        "identity.app.register", "identity.trust.add",
    ],
    mode="tab",
    tab_order=57,
)


log.info("identity_capabilities ready")
