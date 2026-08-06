"""
openbao_identity.py — wire OpenBao (Vault) auth to FreeIPA
=========================================================

Consolidates the secrets vault under the FreeIPA identity core: enables OpenBao's
**LDAP auth method** against FreeIPA so users/apps unlock the vault with their
FreeIPA credentials, and maps a FreeIPA group → a Vault policy. Everything is
derived from the already-stored identity config (`vera:provisioning:identity`) —
no re-entered secrets — and the OpenBao root token is resolved from env or the
secprov deploy record (`vera:provisioning:security`).

Caveats surfaced in the result:
  • If OpenBao is running in **dev mode** (in-memory, as secprov deploys it), this
    config is lost on container restart — move OpenBao to a sealed/persistent
    server to make it durable.
  • The OpenBao container may not resolve FreeIPA's FQDN (its own resolv.conf);
    `use_ip=true` (default) points the LDAP URL at the resolved IP with
    `insecure_tls` so it connects regardless. Provide the FreeIPA CA for full
    verification once the container can resolve the name.

Capabilities (group `identity.openbao.*`)
─────────────────────────────────────────
  identity.openbao.status            — reachable? ldap auth configured? against what?
  identity.openbao.ldap.setup        — enable + configure LDAP auth vs FreeIPA + group map
  identity.openbao.seal.status       — seal/init state + is KMS auto-unseal configured?
  identity.openbao.autounseal.setup  — register unseal keys wrapped by a KMS backend
  identity.openbao.unseal            — auto-unseal via the KMS-wrapped keys
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                   # pragma: no cover
    vsecrets = None  # type: ignore

log = logging.getLogger("vera.identity.openbao")

KEY_IDENTITY = "vera:provisioning:identity"
KEY_SEC = "vera:provisioning:security"


def _redis():
    return getattr(_orch, "REDIS", None)


def _open(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.open_secret(v)
        except Exception:
            return v
    return v


async def _identity_cfg() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {}
    raw = await r.hget(KEY_IDENTITY, "main")
    if not raw:
        return {}
    try:
        c = json.loads(raw)
    except Exception:
        return {}
    c["ipa_password"] = _open(c.get("ipa_password", "")) if c.get("ipa_password") else ""
    return c


async def _openbao_conn() -> Tuple[str, str]:
    """Resolve (addr, token) for OpenBao: env first, else the secprov record."""
    addr = (os.getenv("BAO_ADDR") or os.getenv("VAULT_ADDR") or "http://localhost:8200").rstrip("/")
    token = (os.getenv("BAO_TOKEN") or os.getenv("VAULT_TOKEN") or "").strip()
    if token:
        return addr, token
    r = _redis()
    if r:
        try:
            rows = await r.hgetall(KEY_SEC) or {}
        except Exception:
            rows = {}
        for f, blob in rows.items():
            fk = f.decode() if isinstance(f, bytes) else f
            if fk.endswith(":openbao"):
                try:
                    rec = json.loads(blob)
                except Exception:
                    continue
                sealed = (rec.get("sealed") or {}).get("root_token", "")
                if sealed:
                    token = _open(sealed)
                break
    return addr, token


def _base_dn(domain: str) -> str:
    return ",".join(f"dc={p}" for p in domain.split(".") if p) if domain else ""


async def _bao(method: str, addr: str, token: str, path: str,
               body: Dict = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.request(method, addr + "/v1" + path,
                               headers={"X-Vault-Token": token}, json=body)


@capability(
    "identity.openbao.status",
    http_method="GET", http_path="/identity/openbao/status", http_tags=["identity"],
    memory="off", silent=True,
    description="Is OpenBao reachable and is its LDAP auth wired to FreeIPA? "
                "Output: {reachable, addr, ldap_configured, ldap_url, auth_methods}.",
)
async def cap_openbao_status(trace_id=None) -> Dict:
    addr, token = await _openbao_conn()
    if not token:
        return {"error": "no OpenBao token (set BAO_TOKEN or deploy openbao via "
                         "Provision → Security)", "reachable": False}
    try:
        h = await _bao("GET", addr, token, "/sys/health")
        auths = await _bao("GET", addr, token, "/sys/auth")
        am = auths.json() if auths.status_code < 300 else {}
        ldap = "ldap/" in am
        url = ""
        if ldap:
            cr = await _bao("GET", addr, token, "/auth/ldap/config")
            if cr.status_code < 300:
                url = ((cr.json() or {}).get("data") or {}).get("url", "")
        return {"reachable": h.status_code in (200, 429, 472, 473, 501, 503),
                "addr": addr, "ldap_configured": ldap, "ldap_url": url,
                "auth_methods": [k.rstrip("/") for k in am.keys()]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "reachable": False}


@capability(
    "identity.openbao.ldap.setup",
    http_method="POST", http_path="/identity/openbao/ldap/setup", http_tags=["identity"],
    memory="on",
    description="Wire OpenBao's LDAP auth to FreeIPA (derived from the stored "
                "identity config) and map a FreeIPA group to a Vault policy. "
                "Idempotent. Inputs: group (str='admins' — FreeIPA group to grant), "
                "policies (str='default' — Vault policy/policies csv), insecure_tls "
                "(bool=true), use_ip (bool=true — target FreeIPA by resolved IP so "
                "the OpenBao container connects without needing DNS). Output: "
                "{ok, ldap_url, base_dn, group_mapped, policies, note}.",
)
async def cap_openbao_ldap_setup(group: str = "admins", policies: str = "default",
                                 insecure_tls: bool = True, use_ip: bool = True,
                                 trace_id=None) -> Dict:
    cfg = await _identity_cfg()
    if not cfg.get("ipa_url"):
        return {"error": "FreeIPA not configured — set it up in Provision → Identity first"}
    if not cfg.get("ipa_password"):
        return {"error": "FreeIPA admin password not stored — save it in identity config"}
    addr, token = await _openbao_conn()
    if not token:
        return {"error": "no OpenBao token (deploy openbao via Provision → Security)"}

    host = urlparse(cfg["ipa_url"]).hostname or ""
    ldap_host = host
    if use_ip and host:
        try:
            ldap_host = socket.gethostbyname(host)
        except Exception:
            ldap_host = host
    bdn = _base_dn(cfg.get("ipa_domain", ""))
    if not bdn:
        return {"error": "could not derive base DN (ipa_domain missing from identity config)"}
    user = cfg.get("ipa_user", "admin")

    # 1) enable the ldap auth method (ignore 'already enabled')
    await _bao("POST", addr, token, "/sys/auth/ldap", {"type": "ldap"})
    # 2) configure it against FreeIPA
    conf = {
        "url": f"ldaps://{ldap_host}:636", "insecure_tls": bool(insecure_tls),
        "binddn": f"uid={user},cn=users,cn=accounts,{bdn}",
        "bindpass": cfg["ipa_password"],
        "userdn": f"cn=users,cn=accounts,{bdn}", "userattr": "uid",
        "groupdn": f"cn=groups,cn=accounts,{bdn}", "groupattr": "cn",
        "groupfilter": "(&(objectClass=groupOfNames)(member={{.UserDN}}))",
    }
    cr = await _bao("POST", addr, token, "/auth/ldap/config", conf)
    if cr.status_code >= 300:
        return {"error": f"ldap config failed HTTP {cr.status_code}: {cr.text[:200]}"}
    # 3) map a FreeIPA group to a Vault policy
    gr = await _bao("POST", addr, token, f"/auth/ldap/groups/{group}",
                    {"policies": policies})
    await emit_event({"type": "identity.openbao.ldap.configured",
                      "ldap_host": ldap_host, "group": group})
    return {
        "ok": True, "ldap_url": conf["url"], "base_dn": bdn,
        "group_mapped": group if gr.status_code < 300 else "",
        "policies": policies,
        "note": "FreeIPA users can now log into OpenBao "
                "(auth/ldap/login/<user>). If OpenBao is in dev mode this resets on "
                "restart — move it to a sealed/persistent server to make it durable.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# KMS auto-unseal — take OpenBao out of dev mode into a sealed/persistent server,
# and let Vera auto-unseal it on boot using unseal keys wrapped by a KMS backend:
#   • vera_fernet — seal the keys with Vera's own master key (secrets.seal). Zero
#     extra infra; Vera's ~/.vera/secret.key (or OpenBao-backed store) is the KMS.
#   • transit     — wrap the keys through a Vault/OpenBao **transit** engine, i.e.
#     the same KMS pattern Vault's native transit auto-unseal uses. The transit
#     mount is the root of trust; Vera decrypts via /transit/decrypt on boot.
# Unseal keys are NEVER persisted in plaintext. Vera submits shares to
# /sys/unseal until the vault opens.
# ─────────────────────────────────────────────────────────────────────────────
KEY_AUTOUNSEAL = "vera:provisioning:openbao_autounseal"


async def _seal_status(addr: str) -> Dict:
    """GET /sys/seal-status — needs no token."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(addr + "/v1/sys/seal-status")
        return r.json() if r.status_code < 500 else {}


async def _autounseal_cfg() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {}
    raw = await r.hget(KEY_AUTOUNSEAL, "main")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


async def _transit_encrypt(addr, token, key, plaintext) -> str:
    b = base64.b64encode(plaintext.encode()).decode()
    r = await _bao("POST", addr, token, f"/transit/encrypt/{key}", {"plaintext": b})
    return ((r.json() or {}).get("data") or {}).get("ciphertext", "") if r.status_code < 300 else ""


async def _transit_decrypt(addr, token, key, ciphertext) -> str:
    r = await _bao("POST", addr, token, f"/transit/decrypt/{key}", {"ciphertext": ciphertext})
    if r.status_code >= 300:
        return ""
    pt = ((r.json() or {}).get("data") or {}).get("plaintext", "")
    return base64.b64decode(pt).decode() if pt else ""


def _seal(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


@capability(
    "identity.openbao.seal.status",
    http_method="GET", http_path="/identity/openbao/seal/status", http_tags=["identity"],
    memory="off", silent=True,
    description="OpenBao seal/initialisation state and whether Vera KMS auto-unseal "
                "is configured. Output: {reachable, initialized, sealed, type, "
                "threshold, shares, progress, version, kms_backend, "
                "autounseal_configured}.",
)
async def cap_openbao_seal_status(trace_id=None) -> Dict:
    addr, _ = await _openbao_conn()
    try:
        s = await _seal_status(addr)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "reachable": False}
    au = await _autounseal_cfg()
    return {
        "reachable": bool(s), "addr": addr,
        "initialized": s.get("initialized"), "sealed": s.get("sealed"),
        "type": s.get("type"), "threshold": s.get("t"), "shares": s.get("n"),
        "progress": s.get("progress"), "version": s.get("version"),
        "kms_backend": au.get("kms_backend", ""),
        "autounseal_configured": bool(au.get("wrapped_keys")),
    }


@capability(
    "identity.openbao.autounseal.setup",
    http_method="POST", http_path="/identity/openbao/autounseal/setup",
    http_tags=["identity"], memory="on",
    description="Register OpenBao unseal keys wrapped by a KMS so Vera can "
                "auto-unseal on boot (moves OpenBao off ephemeral dev mode). "
                "Backends: 'vera_fernet' (seal keys with Vera's master key — no "
                "extra infra) or 'transit' (wrap through a Vault/OpenBao transit "
                "KMS, like Vault's native auto-unseal). Inputs: unseal_keys "
                "(csv/list of base64 keys from `bao operator init`), root_token "
                "(str, optional — stored sealed), kms_backend "
                "('vera_fernet'|'transit'), transit_addr, transit_token, "
                "transit_key (default 'autounseal'), threshold (int, default = "
                "#keys). kms_backend is one of: vera_fernet | transit. Keys are "
                "never stored in plaintext. Output: "
                "{ok, kms_backend, shares_stored, threshold}.",
)
async def cap_openbao_autounseal_setup(unseal_keys="", root_token="",
        kms_backend="vera_fernet", transit_addr="", transit_token="",
        transit_key="autounseal", threshold: int = 0, trace_id=None) -> Dict:
    if isinstance(unseal_keys, str):
        keys = [k.strip() for k in re.split(r"[,\s]+", unseal_keys) if k.strip()]
    else:
        keys = [str(k).strip() for k in (unseal_keys or []) if str(k).strip()]
    if not keys:
        return {"error": "unseal_keys required (base64 keys from `bao operator init`)"}
    r = _redis()
    if not r:
        return {"error": "no redis"}
    kms_backend = (kms_backend or "vera_fernet").strip()
    wrapped: List[str] = []
    if kms_backend == "transit":
        if not (transit_addr and transit_token and transit_key):
            return {"error": "transit backend needs transit_addr, transit_token, transit_key"}
        t_addr = transit_addr.rstrip("/")
        for k in keys:
            ct = await _transit_encrypt(t_addr, transit_token, transit_key, k)
            if not ct:
                return {"error": "transit encrypt failed — check the transit mount, "
                                 "key and token"}
            wrapped.append(ct)
    else:
        kms_backend = "vera_fernet"
        if vsecrets is None:
            return {"error": "Vera secrets unavailable (cannot seal keys)"}
        wrapped = [vsecrets.seal(k) for k in keys]
    cfg: Dict[str, Any] = {
        "kms_backend": kms_backend, "wrapped_keys": wrapped,
        "threshold": int(threshold) or len(keys), "created": time.time(),
    }
    if kms_backend == "transit":
        cfg["transit_addr"] = transit_addr.rstrip("/")
        cfg["transit_token"] = _seal(transit_token)
        cfg["transit_key"] = transit_key
    if root_token:
        cfg["root_token"] = _seal(root_token)
    await r.hset(KEY_AUTOUNSEAL, "main", json.dumps(cfg))
    await emit_event({"type": "identity.openbao.autounseal.configured",
                      "kms_backend": kms_backend, "shares": len(keys)})
    return {"ok": True, "kms_backend": kms_backend, "shares_stored": len(keys),
            "threshold": cfg["threshold"]}


@capability(
    "identity.openbao.unseal",
    http_method="POST", http_path="/identity/openbao/unseal", http_tags=["identity"],
    memory="off",
    description="Auto-unseal OpenBao using the KMS-wrapped unseal keys "
                "(identity.openbao.autounseal.setup). Unwraps via the configured "
                "KMS backend and submits shares until the vault opens. Safe to call "
                "when already unsealed or not configured. Output: {ok, sealed, "
                "progress, submitted, kms_backend}.",
)
async def cap_openbao_unseal(boot: bool = False, trace_id=None) -> Dict:
    cfg = await _autounseal_cfg()
    wrapped = cfg.get("wrapped_keys") or []
    if not wrapped:
        return {"ok": False, "skipped": "auto-unseal not configured"}
    addr, _ = await _openbao_conn()
    try:
        s = await _seal_status(addr)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if not s:
        return {"error": "OpenBao unreachable", "reachable": False}
    if not s.get("sealed"):
        return {"ok": True, "sealed": False, "submitted": 0, "note": "already unsealed"}
    backend = cfg.get("kms_backend", "vera_fernet")
    if backend == "transit":
        t_addr = cfg.get("transit_addr", "")
        t_token = _open(cfg.get("transit_token", ""))
        t_key = cfg.get("transit_key", "autounseal")
        keys = []
        for w in wrapped:
            k = await _transit_decrypt(t_addr, t_token, t_key, w)
            if k:
                keys.append(k)
    else:
        keys = [_open(w) for w in wrapped]
    submitted, last = 0, s
    async with httpx.AsyncClient(timeout=15) as c:
        for k in keys:
            rr = await c.put(addr + "/v1/sys/unseal", json={"key": k})
            submitted += 1
            if rr.status_code < 500:
                last = rr.json()
            if not last.get("sealed", True):
                break
    ok = not last.get("sealed", True)
    if ok:
        await emit_event({"type": "identity.openbao.unsealed", "boot": bool(boot)})
    return {"ok": ok, "sealed": last.get("sealed"), "progress": last.get("progress"),
            "submitted": submitted, "kms_backend": backend}


log.info("openbao_identity ready — identity.openbao.* (OpenBao ⇄ FreeIPA auth)")
