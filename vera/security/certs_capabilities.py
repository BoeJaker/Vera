"""certs.list - one certificate list across the estate.

Reads the certificate every known HTTPS service presents (Vera, each Proxmox
cluster's API, PBS storages, FreeIPA, and Integrations records with https
addresses), the certificates FreeIPA's CA issued (cert_find), what Vera recorded
from step-ca, and netctl's Let's Encrypt status (read inside netctl's guest
through Proxmox). certs_core decides names, days left and findings. Read-only;
cached five minutes. Estate > Trust > Certificates shows it, and the Overview
carries its findings.
"""
from __future__ import annotations

import asyncio
import calendar
import inspect
import json
import logging
import re
import ssl
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability
from Vera.vera.estate import estate_nav_core as _nav
from Vera.vera.security import certs_core as core

log = logging.getLogger("vera.security.certs")

CACHE_TTL_S = 300.0
PROBE_TIMEOUT_S = 5.0
SOURCE_TIMEOUT_S = 40.0
_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_PANEL = Path(__file__).parent / "certs_panel.html"


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


def _flag(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _host_port(url: str, default_port: int) -> Tuple[str, int]:
    u = urlparse(url or "")
    try:
        return (u.hostname or ""), (u.port or default_port)
    except ValueError:
        return "", default_port


def _parse_der(der: bytes) -> Dict[str, Any]:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    cert = x509.load_der_x509_certificate(der, default_backend())
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
            .get_values_for_type(x509.DNSName)
    except Exception:
        names = []
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    subject, issuer = cert.subject.rfc4514_string(), cert.issuer.rfc4514_string()
    return {"subject": subject, "issuer": issuer, "names": list(names),
            "not_after": float(calendar.timegm(not_after.utctimetuple())), "self_signed": subject == issuer}


async def _probe(name: str, host: str, port: int) -> Dict[str, Any]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), PROBE_TIMEOUT_S)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
        return {"name": name, "host": host, "port": port, **_parse_der(der)}
    except Exception as e:
        return {"name": name, "host": host, "port": port, "error": f"{type(e).__name__}: {e}"[:160]}


async def _targets() -> Dict[Tuple[str, int], str]:
    """Every HTTPS service the estate knows about, (host, port) -> name."""
    targets: Dict[Tuple[str, int], str] = {("127.0.0.1", 8999): "Vera"}
    px = _module_of("proxmox.status") or {}
    if all(k in px for k in ("_all_raw", "_open", "_pve")):
        records = await px["_all_raw"]()
        for rec in records:
            host, port = _host_port(rec.get("api_url", ""), 8006)
            if host:
                targets.setdefault((host, port), f"Proxmox ({rec.get('label') or host})")
        if records:
            try:
                storages, _err = await px["_pve"](px["_open"](records[0]), "GET", "/storage")
                for s in storages or []:
                    if s.get("type") == "pbs" and s.get("server"):
                        targets.setdefault((s["server"], int(s.get("port") or 8007)), f"PBS ({s.get('storage')})")
            except Exception as e:
                log.debug("certs: storage list failed: %s", e)
    idm = _module_of("identity.status") or {}
    if "_state_raw" in idm:
        host, port = _host_port((await idm["_state_raw"]()).get("ipa_url", ""), 443)
        if host:
            targets.setdefault((host, port), "FreeIPA")
    for rec in (await _call("integration.list")).get("integrations") or []:
        base = rec.get("base_url") or ""
        if base.startswith("https://"):
            host, port = _host_port(base, 443)
            if host and host not in ("127.0.0.1", "localhost"):
                targets.setdefault((host, port), rec.get("label") or host)
    return targets


async def _endpoints() -> List[Dict[str, Any]]:
    targets = await _targets()
    return list(await asyncio.gather(*[_probe(name, host, port) for (host, port), name in targets.items()]))


async def _freeipa() -> Dict[str, Any]:
    idm = _module_of("identity.status") or {}
    if not all(k in idm for k in ("_state_opened", "_ipa_call")):
        return {"error": "the identity module is not loaded"}
    st = await idm["_state_opened"]()
    if not st.get("ipa_url"):
        return {"skipped": "FreeIPA is not configured"}
    res, err = await idm["_ipa_call"](st, "cert_find", args=[], options={"sizelimit": 500})
    if res is None:
        return {"error": err}
    rows = res.get("result") if isinstance(res, dict) else res
    return {"certs": rows or []}


async def _letsencrypt() -> Dict[str, Any]:
    rec = _nav.netctl_record((await _call("integration.list")).get("integrations") or [])
    if not rec:
        return {"skipped": "netctl is not registered in Integrations"}
    host, _port = _host_port(rec.get("base_url", ""), 8088)
    machines = (await _call("estate.machines")).get("machines") or []
    guest = next((m for m in machines if m.get("kind") == "guest"
                  and (m.get("addr") == host or host in (m.get("ips") or []))), None)
    if not guest:
        return {"error": f"no Proxmox guest has netctl's address {host}"}
    res = await _call("proxmox.guest.exec", cluster_id=guest.get("cluster_id", ""), node=guest.get("node", ""),
                      guest_type=guest.get("type", "lxc"), vmid=int(guest.get("vmid") or 0),
                      command="cd /opt/netctl/app && python3 certs.py", timeout=60)
    if not res.get("ok"):
        return {"error": res.get("error") or (res.get("stderr") or "")[:200] or "netctl's certificate status did not run"}
    try:
        return {"status": json.loads(res.get("stdout") or "{}")}
    except ValueError:
        return {"error": "netctl's certificate status was not readable"}


async def _timed(coro, what: str) -> Any:
    try:
        return await asyncio.wait_for(coro, SOURCE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"error": f"{what} did not answer within {int(SOURCE_TIMEOUT_S)} s"}


@capability(
    "certs.list",
    http_method="GET", http_path="/certs/list", http_tags=["security", "estate"],
    memory="off", silent=True,
    description="One certificate list across the estate: the certificate each known HTTPS service "
                "presents (Vera, Proxmox APIs, PBS storages, FreeIPA, Integrations with https "
                "addresses), everything FreeIPA's CA issued (older copies marked superseded), what "
                "Vera recorded from step-ca, and netctl's Let's Encrypt wildcard. Each row has its "
                "issuer, names, expiry, days left and a state (ok, renew soon, expiring, expired, "
                "revoked, superseded, unreachable, recorded, not issued); findings in plain language. "
                "Read-only, cached 5 minutes. Inputs: refresh (bool). Output: {certs, counts, "
                "findings, soonest, skipped, checked_at, cached}.",
)
async def cap_certs_list(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    if not _flag(refresh) and _CACHE["value"] is not None and time.time() - _CACHE["at"] < CACHE_TTL_S:
        return dict(_CACHE["value"], cached=True)
    endpoints, ipa, step, le = await asyncio.gather(
        _timed(_endpoints(), "the service probes"), _timed(_freeipa(), "FreeIPA"),
        _timed(_call("pki.cert.list"), "step-ca records"), _timed(_letsencrypt(), "netctl"))
    errors: Dict[str, str] = {}
    if isinstance(endpoints, dict):
        errors["the services"] = endpoints.get("error", "")
        endpoints = []
    for source, res in (("FreeIPA", ipa), ("step-ca", step), ("netctl", le)):
        if res.get("error"):
            errors[source] = res["error"]
    value = core.summarize(endpoints, ipa.get("certs") or [], step.get("certs") or [], le.get("status"), errors)
    value["skipped"] = [res["skipped"] for res in (ipa, le) if res.get("skipped")]
    value["checked_at"] = int(time.time())
    _CACHE.update(at=time.time(), value=value)
    return dict(value, cached=False)


@APP.get("/certs/panel", include_in_schema=False)
async def _certs_panel():
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists()
                        else "<p style='color:red'>certs_panel.html not found</p>")


log.info("certs_capabilities ready - certs.list")
