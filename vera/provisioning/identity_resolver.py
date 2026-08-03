"""
identity_resolver.py — one directory door, FreeIPA-first with lldap fallback
===========================================================================

The user runs (and wants to consolidate onto) FreeIPA as the real security layer
for users, devices and apps — but wants a *graceful fallback to lldap where
practical*. This module is that resolver: a thin routing layer over the two
existing directory backends so callers (autoenrol, the Integrations Hub, chat)
don't hard-code one.

Routing
───────
  users     → FreeIPA (identity.user.register) if reachable, else lldap
              (identity.lldap.user.create).  lldap CAN hold users/groups.
  hosts     → FreeIPA (identity.host.register) only. lldap has no host concept,
              so when FreeIPA is down this degrades to "skipped (device trust via
              cert+mesh only)" rather than a bad fallback.
  apps      → FreeIPA (identity.app.register: DNS + service principal + TLS) only,
              same degradation as hosts.

`identity.resolve.status` reports which backends are live so the UI can show the
right badges. Everything is best-effort and never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability

log = logging.getLogger("vera.identity.resolver")


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


async def _freeipa_up() -> bool:
    fn = _cap("identity.status")
    if not fn:
        return False
    try:
        st = await fn() or {}
        return bool(st.get("reachable"))
    except Exception:
        return False


async def _lldap_up() -> bool:
    fn = _cap("identity.lldap.status")
    if not fn:
        return False
    try:
        st = await fn() or {}
        return bool(st.get("reachable"))
    except Exception:
        return False


@capability(
    "identity.resolve.status",
    http_method="GET", http_path="/identity/resolve/status", http_tags=["identity"],
    memory="off", silent=True,
    description="Which directory backends are live, and the effective routing. "
                "Output: {freeipa, lldap, user_backend, host_backend, backend}.",
)
async def cap_status(trace_id=None) -> Dict:
    fip = await _freeipa_up()
    lld = await _lldap_up()
    return {
        "freeipa": fip, "lldap": lld,
        "user_backend": "freeipa" if fip else ("lldap" if lld else "none"),
        "host_backend": "freeipa" if fip else "none",   # lldap can't hold hosts
        "backend": "freeipa" if fip else ("lldap" if lld else "none"),
    }


@capability(
    "identity.resolve.host",
    http_method="POST", http_path="/identity/resolve/host", http_tags=["identity"],
    memory="on",
    description="Register a DEVICE/host in the directory, FreeIPA-first. lldap has "
                "no host concept, so if FreeIPA is unavailable this returns "
                "{skipped} (trust the device via cert+mesh instead). Inputs: fqdn "
                "(str!), ip (str). Output: {ok, backend, ...} or {skipped, reason}.",
)
async def cap_host(fqdn: str = "", ip: str = "", trace_id=None) -> Dict:
    if not fqdn:
        return {"error": "fqdn required"}
    if await _freeipa_up():
        reg = _cap("identity.host.register")
        if reg:
            r = await reg(fqdn=fqdn, ip=ip)
            r["backend"] = "freeipa"
            return r
    return {"skipped": True, "backend": "none",
            "reason": "FreeIPA not reachable and lldap cannot hold hosts — "
                      "device trust falls back to step-ca cert + mesh membership."}


@capability(
    "identity.resolve.app",
    http_method="POST", http_path="/identity/resolve/app", http_tags=["identity"],
    memory="on",
    description="Register an APP in the directory (FreeIPA: DNS A + HTTP service "
                "principal + TLS cert), FreeIPA-first. Degrades to {skipped} when "
                "FreeIPA is down. Inputs: name (str! — host label), ip (str), "
                "port (int), ssh_host_id (str). Output: FreeIPA app-register "
                "result + backend, or {skipped}.",
)
async def cap_app(name: str = "", ip: str = "", port: int = 0,
                  ssh_host_id: str = "", trace_id=None) -> Dict:
    if not name:
        return {"error": "name required"}
    if await _freeipa_up():
        reg = _cap("identity.app.register")
        if reg:
            r = await reg(name=name, ip=ip, port=port, ssh_host_id=ssh_host_id)
            r["backend"] = "freeipa"
            return r
    return {"skipped": True, "backend": "none",
            "reason": "FreeIPA not reachable; app service/DNS/cert registration "
                      "needs FreeIPA (lldap holds users/groups only)."}


@capability(
    "identity.resolve.user",
    http_method="POST", http_path="/identity/resolve/user", http_tags=["identity"],
    memory="on",
    description="Register a USER, FreeIPA-first with graceful lldap fallback "
                "(lldap CAN hold users/groups). Inputs: login (str!), first (str), "
                "last (str), email (str), password (str — FreeIPA only), group_id "
                "(int — lldap only). Output: {ok, backend, ...}.",
)
async def cap_user(login: str = "", first: str = "", last: str = "",
                   email: str = "", password: str = "",
                   group_id: Optional[int] = None, trace_id=None) -> Dict:
    if not login:
        return {"error": "login required"}
    if await _freeipa_up():
        reg = _cap("identity.user.register")
        if reg:
            r = await reg(login=login, first=first or login, last=last or login,
                          email=email, password=password)
            r["backend"] = "freeipa"
            return r
    if await _lldap_up():
        reg = _cap("identity.lldap.user.create")
        if reg:
            r = await reg(id=login, email=email or f"{login}@vera.local",
                          display_name=(f"{first} {last}".strip() or login),
                          first_name=first, last_name=last, group_id=group_id)
            r["backend"] = "lldap"
            return r
    return {"error": "no directory backend reachable (FreeIPA or lldap)",
            "backend": "none"}


log.info("identity_resolver ready — identity.resolve.* (FreeIPA-first, lldap fallback)")
