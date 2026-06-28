"""
accounts_capabilities.py — Vera Unified Accounts registry
=========================================================

A single shared store of *accounts* (identities) used by both the Calendar and
Email modules, so credentials are configured once and reused.

Each account is an identity (label + email) carrying whatever credential blocks
it needs:

  • Mail block   — IMAP/SMTP host settings + an app-password  → used by Email.
  • Calendar block — CalDAV url/user/password and/or an ICS URL (e.g. a Google
    "secret address in iCal format") → used by Calendar sources.

Secrets (`app_password`, `caldav_password`) are sealed at rest with the shared
Fernet helper (vera/security/secrets.py) and never returned to the UI — list/get
redact them to `has_*` flags.

Capabilities (group `acct.*`)
─────────────────────────────
  acct.list / acct.get / acct.upsert / acct.delete / acct.test
  acct.panel.html  (serves /accounts/panel)

Importable helpers (other modules use sys.modules.get("accounts_capabilities")):
  get_account(account_id)      -> dict | None   (secrets OPENED)
  list_accounts(opened=False)  -> list[dict]
  default_mail_account()       -> dict | None

Redis layout
────────────
  vera:accounts   hash  id -> JSON   (secrets sealed)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, now_iso, register_ui,
)
from Vera.vera.config import cfg
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.accounts")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "accounts_panel.html"

KEY_ACCOUNTS = "vera:accounts"

# Secret fields sealed before storage and never returned to the UI.
_SECRET_FIELDS = ("app_password", "caldav_password",
                  "oauth_client_secret", "oauth_refresh_token")

ACCOUNT_DEFAULTS: Dict[str, Any] = {
    "label": "", "email": "", "color": "#4a9eff",
    # Mail block
    "mail_enabled": False,
    "imap_host": "", "imap_port": 993, "imap_ssl": True,
    "smtp_host": "", "smtp_port": 587, "smtp_tls": True,
    "mail_username": "", "app_password": "",
    # Calendar block
    "caldav_url": "", "caldav_username": "", "caldav_password": "",
    "ics_url": "",
    # OAuth block (provider-agnostic; google implemented). client_id/secret are
    # optional per-account — blank falls back to the shared cfg client so one
    # registered app can serve every account ("one key for everything"), while
    # accounts that set their own get an isolated, split-per-use-case grant.
    "oauth_provider": "",          # "" | "google"
    "oauth_client_id": "",
    "oauth_client_secret": "",     # sealed
    "oauth_refresh_token": "",     # sealed — present once connected
    "oauth_scopes": [],            # granted scope KEYS (see OAUTH_PROVIDERS)
}

# Provider presets surfaced to the UI to prefill host/port. (Lives here now —
# the single source of truth for both Email and Accounts panels.)
PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "gmail":    {"imap_host": "imap.gmail.com", "imap_port": 993,
                 "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_tls": True,
                 "ics_hint": "Google Calendar → Settings → your calendar → "
                             "'Secret address in iCal format'"},
    "outlook":  {"imap_host": "outlook.office365.com", "imap_port": 993,
                 "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_tls": True},
    "fastmail": {"imap_host": "imap.fastmail.com", "imap_port": 993,
                 "smtp_host": "smtp.fastmail.com", "smtp_port": 465, "smtp_tls": True,
                 "caldav_hint": "https://caldav.fastmail.com/dav/calendars/user/<you>/"},
    "yahoo":    {"imap_host": "imap.mail.yahoo.com", "imap_port": 993,
                 "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 587, "smtp_tls": True},
}


def _redis():
    return getattr(_orch, "REDIS", None)


# ─────────────────────────────────────────────────────────────────────────────
# STORE
# ─────────────────────────────────────────────────────────────────────────────

async def _all_raw() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    items = await r.hgetall(KEY_ACCOUNTS)
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    return out


def _open_secrets(acct: Dict) -> Dict:
    """Return a copy with secret fields decrypted (for internal use)."""
    out = dict(acct)
    for f in _SECRET_FIELDS:
        out[f] = vsecrets.open_secret(acct.get(f, ""))
    return out


def _redact(acct: Dict) -> Dict:
    """UI-safe copy: secret fields stripped, replaced with has_* flags."""
    out = {}
    for k, v in acct.items():
        if k in _SECRET_FIELDS:
            out[f"has_{k}"] = bool(v)
        else:
            out[k] = v
    return out


# ── Importable helpers for other modules ─────────────────────────────────────

async def get_account(account_id: str) -> Optional[Dict]:
    """Fetch one account with secrets OPENED. None if missing."""
    if not account_id:
        return None
    r = _redis()
    if not r:
        return None
    raw = await r.hget(KEY_ACCOUNTS, account_id)
    if not raw:
        return None
    try:
        return _open_secrets(json.loads(raw))
    except Exception:
        return None


async def list_accounts(opened: bool = False) -> List[Dict]:
    """All accounts. opened=True decrypts secrets (internal); else redacted."""
    accts = await _all_raw()
    return [_open_secrets(a) if opened else _redact(a) for a in accts]


async def default_mail_account() -> Optional[Dict]:
    """First mail-enabled account with an app-password (secrets opened)."""
    for a in await _all_raw():
        if a.get("mail_enabled") and a.get("app_password"):
            return _open_secrets(a)
    return None


async def upsert_account(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Create/update an account from a dict. Keys present = updated; absent =
    left unchanged. `app_password`/`caldav_password`, when truthy, are sealed.
    Shared by the cap and by other modules' migrations. Returns
    {ok, account(redacted)} or {error}.
    """
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    aid = fields.get("id", "")
    existing = None
    if aid:
        raw = await r.hget(KEY_ACCOUNTS, aid)
        existing = json.loads(raw) if raw else None
    acct = dict(existing) if existing else {**ACCOUNT_DEFAULTS,
                                            "id": str(uuid.uuid4()),
                                            "created": now_iso()}
    for k in ("label", "email", "color", "mail_enabled", "imap_host",
              "imap_port", "imap_ssl", "smtp_host", "smtp_port", "smtp_tls",
              "mail_username", "caldav_url", "caldav_username", "ics_url",
              "oauth_provider", "oauth_client_id", "oauth_scopes"):
        if fields.get(k) is not None:
            acct[k] = fields[k]
    if not acct.get("label"):
        acct["label"] = acct.get("email") or "Account"
    try:
        if fields.get("app_password"):
            acct["app_password"] = vsecrets.seal(fields["app_password"])
        if fields.get("caldav_password"):
            acct["caldav_password"] = vsecrets.seal(fields["caldav_password"])
        if fields.get("oauth_client_secret"):
            acct["oauth_client_secret"] = vsecrets.seal(fields["oauth_client_secret"])
        if fields.get("oauth_refresh_token"):
            acct["oauth_refresh_token"] = vsecrets.seal(fields["oauth_refresh_token"])
    except RuntimeError as e:
        return {"error": str(e)}
    acct["updated"] = now_iso()
    await r.hset(KEY_ACCOUNTS, acct["id"], json.dumps(acct))
    return {"ok": True, "account": _redact(acct)}


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "acct.list", http_method="GET", http_path="/accounts/list",
    http_tags=["accounts"], memory="off", silent=True,
    description="List saved accounts (REDACTED — secrets never returned, only "
                "has_app_password / has_caldav_password). Also returns provider "
                "presets. Output: {accounts:[...], presets:{...}}.",
)
async def cap_list(trace_id=None):
    return {"accounts": [_redact(a) for a in await _all_raw()],
            "presets": PROVIDER_PRESETS}


@capability(
    "acct.get", http_method="GET", http_path="/accounts/get",
    http_tags=["accounts"], memory="off", silent=True,
    description="Get one account (REDACTED). Input: id (str!). Output: account.",
)
async def cap_get(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    r = _redis()
    raw = await r.hget(KEY_ACCOUNTS, id) if r else None
    if not raw:
        return {"error": "account not found"}
    return _redact(json.loads(raw))


@capability(
    "acct.upsert", http_method="POST", http_path="/accounts/upsert",
    http_tags=["accounts"], memory="on",
    description="Create or update an account. Secrets (app_password, "
                "caldav_password) are sealed before storage; leave a secret "
                "blank on edit to keep the existing value. "
                "Input: id (omit to create), label, email, color, mail_enabled "
                "(bool), imap_host, imap_port (int), imap_ssl (bool), smtp_host, "
                "smtp_port (int), smtp_tls (bool), mail_username, app_password, "
                "caldav_url, caldav_username, caldav_password, ics_url, "
                "oauth_provider (''|google), oauth_client_id, oauth_client_secret "
                "(blank → use the shared cfg client). Connecting an OAuth grant "
                "is done via acct.oauth.* not here. Output: {ok, account (redacted)}.",
)
async def cap_upsert(
    id: str = "", label: str = "", email: str = "", color: str = "",
    mail_enabled: Optional[bool] = None,
    imap_host: Optional[str] = None, imap_port: Optional[int] = None,
    imap_ssl: Optional[bool] = None, smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None, smtp_tls: Optional[bool] = None,
    mail_username: Optional[str] = None, app_password: str = "",
    caldav_url: Optional[str] = None, caldav_username: Optional[str] = None,
    caldav_password: str = "", ics_url: Optional[str] = None,
    oauth_provider: Optional[str] = None, oauth_client_id: Optional[str] = None,
    oauth_client_secret: str = "", trace_id=None,
):
    fields: Dict[str, Any] = {"id": id}
    for k, v in (("label", label or None), ("email", email or None),
                 ("color", color or None), ("mail_enabled", mail_enabled),
                 ("imap_host", imap_host), ("imap_port", imap_port),
                 ("imap_ssl", imap_ssl), ("smtp_host", smtp_host),
                 ("smtp_port", smtp_port), ("smtp_tls", smtp_tls),
                 ("mail_username", mail_username), ("caldav_url", caldav_url),
                 ("caldav_username", caldav_username), ("ics_url", ics_url),
                 ("oauth_provider", oauth_provider),
                 ("oauth_client_id", oauth_client_id)):
        if v is not None:
            fields[k] = v
    if app_password:
        fields["app_password"] = app_password
    if caldav_password:
        fields["caldav_password"] = caldav_password
    if oauth_client_secret:
        fields["oauth_client_secret"] = oauth_client_secret
    return await upsert_account(fields)


@capability(
    "acct.delete", http_method="POST", http_path="/accounts/delete",
    http_tags=["accounts"], memory="on",
    description="Delete an account by id. Input: id (str!). Output: {ok}. "
                "Note: Calendar sources / Email referencing it will need updating.",
)
async def cap_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    return {"ok": bool(await r.hdel(KEY_ACCOUNTS, id))}


@capability(
    "acct.test", http_method="POST", http_path="/accounts/test",
    http_tags=["accounts"], memory="on",
    description="Test an account's connectivity: IMAP+SMTP if a mail block is "
                "set, and a best-effort CalDAV check if a calendar block is set. "
                "Input: id (str!). Output: {mail?:{...}, caldav?:{ok,error?}}.",
)
async def cap_test(id: str = "", trace_id=None):
    acct = await get_account(id)
    if not acct:
        return {"error": "account not found"}
    out: Dict[str, Any] = {}
    # Mail leg — reuse the email transport.
    if acct.get("app_password") and (acct.get("imap_host") or acct.get("smtp_host")):
        try:
            from Vera.vera.email import transport as mail_transport
            cfg = _account_to_mail_cfg(acct)
            out["mail"] = await mail_transport.ImapSmtpTransport(cfg).test()
        except Exception as e:
            out["mail"] = {"ok": False, "error": str(e)}
    # CalDAV leg — best-effort PROPFIND.
    if acct.get("caldav_url"):
        out["caldav"] = await _caldav_check(acct)
    if not out:
        out = {"error": "account has no mail or caldav credentials to test"}
    return out


def _account_to_mail_cfg(acct: Dict) -> Dict[str, Any]:
    """Map an (opened) account into the cfg dict the mail transport expects."""
    return {
        "transport_type": "imap_smtp",
        "imap_host": acct.get("imap_host", ""), "imap_port": acct.get("imap_port", 993),
        "imap_ssl": acct.get("imap_ssl", True),
        "smtp_host": acct.get("smtp_host", ""), "smtp_port": acct.get("smtp_port", 587),
        "smtp_tls": acct.get("smtp_tls", True),
        "username": acct.get("mail_username") or acct.get("email", ""),
        "password": acct.get("app_password", ""),
        "from_addr": acct.get("email", ""), "from_name": acct.get("label", ""),
    }


async def _caldav_check(acct: Dict) -> Dict[str, Any]:
    import httpx
    try:
        auth = (acct.get("caldav_username", ""), acct.get("caldav_password", "")) \
            if acct.get("caldav_username") else None
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, auth=auth) as c:
            resp = await c.request("PROPFIND", acct["caldav_url"],
                                   headers={"Depth": "0"})
            return {"ok": resp.status_code < 400, "status": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
#  UNIFIED OAUTH  (provider-agnostic; google implemented)
# ═════════════════════════════════════════════════════════════════════════════
#
# OAuth lives on the *account*, not on individual consumers (calendar source,
# email config). One grant per account; the scopes the user ticks decide what it
# can do. Because the Accounts registry is already multi-account, you can EITHER
# keep one account with several scopes ("one key for everything") OR split into
# purpose-specific accounts ("calendar key", "mail key") — both work the same
# way. The generic callback /accounts/oauth/callback replaces the calendar-only
# /cal/google/oauth_callback so the redirect URI no longer implies "calendar".

# Catalog of providers + the scopes Vera knows how to use. Scope KEYS are stored
# on the account; `scope` is the real provider scope URL. `group`/`label`/`for`
# drive the picker UI.
OAUTH_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "google": {
        "label": "Google",
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        # Identity scopes always requested so we can confirm which account
        # granted the tokens.
        "base_scopes": ["openid",
                        "https://www.googleapis.com/auth/userinfo.email"],
        "scopes": {
            "calendar":        {"label": "Calendar (read)",   "group": "Calendar",
                                "scope": "https://www.googleapis.com/auth/calendar.readonly",
                                "for": "Calendar sync"},
            "calendar_manage": {"label": "Calendar (manage)", "group": "Calendar",
                                "scope": "https://www.googleapis.com/auth/calendar",
                                "for": "Create / edit events"},
            "mail":            {"label": "Mail (read & send)", "group": "Mail",
                                "scope": "https://mail.google.com/",
                                "for": "Email over IMAP/SMTP (XOAUTH2)"},
            "contacts":        {"label": "Contacts (read)",   "group": "Contacts",
                                "scope": "https://www.googleapis.com/auth/contacts.readonly",
                                "for": "Address book"},
        },
    },
}

KEY_OAUTH_STATE = "vera:accounts:oauth:state:"   # + state token -> JSON payload
KEY_OAUTH_TOKEN = "vera:accounts:oauth:token:"   # + account id  -> token redis key  # pragma: allowlist secret
_OAUTH_STATE_TTL = 600
OAUTH_CALLBACK_PATH = "/accounts/oauth/callback"


def _oauth_redirect_uri() -> str:
    """The single callback URI to register on the Google OAuth client. Identical
    string must be sent on both the consent request and the token exchange."""
    return cfg.GOOGLE_OAUTH_REDIRECT_BASE.rstrip("/") + OAUTH_CALLBACK_PATH


def _provider(name: str) -> Optional[Dict]:
    return OAUTH_PROVIDERS.get((name or "").lower())


def _account_client(acct: Dict) -> tuple:
    """Effective (client_id, client_secret) for an account: its own when set,
    else the shared cfg client. `acct` must have secrets OPENED."""
    cid = acct.get("oauth_client_id") or cfg.GOOGLE_OAUTH_CLIENT_ID
    csec = acct.get("oauth_client_secret") or cfg.GOOGLE_OAUTH_CLIENT_SECRET
    return cid, csec


def _resolve_scope_urls(provider: Dict, keys: List[str]) -> List[str]:
    out = list(provider.get("base_scopes", []))
    for k in keys:
        s = provider["scopes"].get(k)
        if s and s["scope"] not in out:
            out.append(s["scope"])
    return out


async def _store_oauth_state(state: str, payload: Dict) -> None:
    r = _redis()
    if r:
        await r.set(KEY_OAUTH_STATE + state, json.dumps(payload), ex=_OAUTH_STATE_TTL)


async def _consume_oauth_state(state: str) -> Optional[Dict]:
    r = _redis()
    if not r or not state:
        return None
    raw = await r.get(KEY_OAUTH_STATE + state)
    if raw is None:
        return None
    await r.delete(KEY_OAUTH_STATE + state)
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── Importable helper for consumers (calendar, email) ────────────────────────

async def get_oauth_token(account_id: str, scope_key: str = "") -> Optional[str]:
    """Return a fresh OAuth access token for an account, refreshing from the
    stored refresh token and caching it in Redis to just before expiry.

    Returns None if the account isn't connected, or — when `scope_key` is given —
    if that scope wasn't granted (so callers fail closed rather than 403 later).
    """
    acct = await get_account(account_id)            # secrets OPENED
    if not acct or not acct.get("oauth_refresh_token"):
        return None
    if scope_key and scope_key not in (acct.get("oauth_scopes") or []):
        return None
    r = _redis()
    if r:
        cached = await r.get(KEY_OAUTH_TOKEN + account_id)
        if cached:
            return cached.decode() if isinstance(cached, (bytes, bytearray)) else cached
    prov = _provider(acct.get("oauth_provider"))
    if not prov:
        return None
    cid, csec = _account_client(acct)
    if not (cid and csec):
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(prov["token_uri"], data={
                "client_id": cid, "client_secret": csec,
                "refresh_token": acct["oauth_refresh_token"],
                "grant_type": "refresh_token"})
            resp.raise_for_status()
            tok = resp.json()
    except Exception as e:
        log.warning("oauth refresh for account %s failed: %s", account_id, e)
        return None
    at = tok.get("access_token", "")
    if at and r:
        ttl = max(60, int(tok.get("expires_in", 3600) or 3600) - 60)
        await r.set(KEY_OAUTH_TOKEN + account_id, at, ex=ttl)
    return at or None


async def _exchange_code(account_id: str, code: str,
                         scope_keys: List[str]) -> Dict[str, Any]:
    """Exchange an authorisation code for a refresh token and seal it onto the
    account, merging the newly-granted scope keys with any already present.
    Shared by the auto callback and the manual paste fallback."""
    acct = await get_account(account_id)            # secrets OPENED
    if not acct:
        return {"error": "account not found"}
    prov = _provider(acct.get("oauth_provider"))
    if not prov:
        return {"error": "account has no oauth_provider configured"}
    if not code:
        return {"error": "code is required"}
    cid, csec = _account_client(acct)
    if not (cid and csec):
        return {"error": "no client_id/client_secret (set one on the account "
                         "or configure the shared GOOGLE_OAUTH_CLIENT_* cfg)"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(prov["token_uri"], data={
                "client_id": cid, "client_secret": csec, "code": code.strip(),
                "grant_type": "authorization_code",
                "redirect_uri": _oauth_redirect_uri()})
            resp.raise_for_status()
            tok = resp.json()
    except Exception as e:
        return {"error": f"token exchange failed: {e}"}
    rt = tok.get("refresh_token")
    if not rt:
        return {"error": "no refresh_token returned (revoke the prior grant at "
                         "myaccount.google.com and retry)"}
    merged = list(dict.fromkeys((acct.get("oauth_scopes") or []) + list(scope_keys)))
    res = await upsert_account({"id": account_id, "oauth_refresh_token": rt,
                                "oauth_scopes": merged})
    if res.get("error"):
        return res
    return {"ok": True, "scopes": merged}


def _oauth_result_page(ok: bool, msg: str) -> str:
    accent = "#28c28a" if ok else "#ef5b5b"
    title = "Connected ✓" if ok else "Authorisation failed"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Vera · Connect account</title></head>"
        "<body style='background:#0d0f12;color:#e8eaed;margin:0;min-height:100vh;"
        "display:flex;align-items:center;justify-content:center;font-family:"
        "system-ui,-apple-system,Segoe UI,Roboto,sans-serif'>"
        "<div style='text-align:center;max-width:440px;padding:32px'>"
        f"<h2 style='color:{accent};margin:0 0 12px'>{title}</h2>"
        f"<p style='color:#aab;line-height:1.5'>{_html_escape(str(msg))}</p>"
        "<p style='color:#667;font-size:13px;margin-top:24px'>You can close this "
        "tab and return to Vera.</p></div>"
        "<script>setTimeout(function(){window.close()},2500)</script>"
        "</body></html>")


def _html_escape(s: str) -> str:
    import html as _h
    return _h.escape(s)


# ── OAuth capabilities ────────────────────────────────────────────────────────

@capability(
    "acct.oauth.providers", http_method="GET", http_path="/accounts/oauth/providers",
    http_tags=["accounts"], memory="off", silent=True,
    description="List OAuth providers and their selectable scopes for the "
                "connect UI. Output: {providers:{google:{label, scopes:[{key,"
                "label,group,for}]}}, redirect_uri, shared_client (bool — whether "
                "a shared cfg client is configured)}.",
)
async def cap_oauth_providers(trace_id=None):
    out = {}
    for pname, prov in OAUTH_PROVIDERS.items():
        out[pname] = {
            "label": prov["label"],
            "scopes": [{"key": k, "label": v["label"], "group": v["group"],
                        "for": v.get("for", "")}
                       for k, v in prov["scopes"].items()],
        }
    return {"providers": out, "redirect_uri": _oauth_redirect_uri(),
            "shared_client": bool(cfg.GOOGLE_OAUTH_CLIENT_ID
                                  and cfg.GOOGLE_OAUTH_CLIENT_SECRET)}


@capability(
    "acct.oauth.auth_url", http_method="GET", http_path="/accounts/oauth/auth_url",
    http_tags=["accounts"], memory="off", silent=True,
    description="Build the OAuth consent URL for an account and a chosen set of "
                "scope keys. Input: id (account id!), scopes (comma-separated "
                "scope keys, e.g. 'calendar,mail'). The user visits the URL and "
                "approves; Google redirects to /accounts/oauth/callback which "
                "stores the refresh token automatically. Output: {url}.",
)
async def cap_oauth_auth_url(id: str = "", scopes: str = "", trace_id=None):
    acct = await get_account(id)
    if not acct:
        return {"error": "account not found"}
    prov = _provider(acct.get("oauth_provider"))
    if not prov:
        return {"error": "account has no oauth_provider set (configure it first)"}
    cid, _ = _account_client(acct)
    if not cid:
        return {"error": "no client_id — set one on the account or configure the "
                         "shared GOOGLE_OAUTH_CLIENT_ID"}
    keys = [s.strip() for s in (scopes or "").split(",")
            if s.strip() in prov["scopes"]]
    if not keys:
        return {"error": "select at least one valid scope"}
    state = uuid.uuid4().hex
    await _store_oauth_state(state, {"account_id": acct["id"], "scopes": keys})
    url = prov["auth_uri"] + "?" + urlencode({
        "client_id": cid, "redirect_uri": _oauth_redirect_uri(),
        "response_type": "code", "scope": " ".join(_resolve_scope_urls(prov, keys)),
        "state": state, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true"})
    return {"url": url}


@capability(
    "acct.oauth.complete", http_method="POST", http_path="/accounts/oauth/complete",
    http_tags=["accounts"], memory="on",
    description="Manual fallback: exchange an OAuth code for a refresh token and "
                "store it (sealed) on the account. Used when the auto callback "
                "can't reach Vera. Input: id (account id!), code (str!), scopes "
                "(comma-separated keys that were requested). Output: {ok, scopes}.",
)
async def cap_oauth_complete(id: str = "", code: str = "", scopes: str = "",
                             trace_id=None):
    keys = [s.strip() for s in (scopes or "").split(",") if s.strip()]
    return await _exchange_code(id, code, keys)


@capability(
    "acct.oauth.disconnect", http_method="POST",
    http_path="/accounts/oauth/disconnect", http_tags=["accounts"], memory="on",
    description="Revoke Vera's stored OAuth grant for an account (clears the "
                "refresh token and granted scopes; the client_id/secret config "
                "is kept). Input: id (account id!). Output: {ok}.",
)
async def cap_oauth_disconnect(id: str = "", trace_id=None):
    if not id:
        return {"error": "id is required"}
    r = _redis()
    if not r:
        return {"error": "store unavailable"}
    raw = await r.hget(KEY_ACCOUNTS, id)
    if not raw:
        return {"error": "account not found"}
    acct = json.loads(raw)
    acct["oauth_refresh_token"] = ""
    acct["oauth_scopes"] = []
    acct["updated"] = now_iso()
    await r.hset(KEY_ACCOUNTS, id, json.dumps(acct))
    await r.delete(KEY_OAUTH_TOKEN + id)
    return {"ok": True}


@APP.get(OAUTH_CALLBACK_PATH, include_in_schema=False)
async def _oauth_callback(request: Request):
    """Generic OAuth redirect target. Google sends the browser here with
    ?code=&state= after approval; we validate the state, exchange the code and
    seal the refresh token onto the matching account."""
    qp = request.query_params
    if qp.get("error"):
        return HTMLResponse(_oauth_result_page(
            False, "Provider returned: " + qp.get("error", "")), status_code=400)
    payload = await _consume_oauth_state(qp.get("state", ""))
    if not payload:
        return HTMLResponse(_oauth_result_page(
            False, "Invalid or expired authorisation state — start Connect again "
                   "from Vera."), status_code=400)
    res = await _exchange_code(payload.get("account_id", ""), qp.get("code", ""),
                               payload.get("scopes", []))
    if res.get("ok"):
        return HTMLResponse(_oauth_result_page(True, "Account connected."))
    return HTMLResponse(_oauth_result_page(
        False, res.get("error", "token exchange failed")), status_code=400)


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "acct.panel.html", http_method="GET", http_path="/accounts/panel",
    http_tags=["accounts", "ui"], memory="off", silent=True,
    description="Serve the Accounts panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;"
                "color:#ef5b5b;font-family:monospace;padding:40px'>"
                "<h2>accounts_panel.html not found</h2>"
                f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")
    return HTMLResponse(html)


@APP.get("/accounts/panel", include_in_schema=False)
async def _accounts_panel_route():
    p = _HERE / "accounts_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>accounts_panel.html not found</p>")


@APP.get("/comms/panel", include_in_schema=False)
async def _comms_panel_route():
    p = _HERE / "comms_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>comms_panel.html not found</p>")


# The Accounts panel is now a sub-tab inside the combined "Comms" tab below, so
# it registers as an inject (reachable from the panels menu) rather than its own
# top-level tab. Calendar / Email / Telegram do the same in their modules.
register_ui(
    "accounts-panel",
    "Accounts",
    "👤",
    """<div id="accounts-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/accounts/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["acct.list", "acct.get", "acct.upsert", "acct.delete", "acct.test",
             "acct.oauth.providers", "acct.oauth.auth_url", "acct.oauth.complete",
             "acct.oauth.disconnect"],
    mode="inject",
    tab_order=69,
)


# Combined Comms tab: Accounts / Calendar / Email / Telegram as sub-tabs. Each
# sub-panel keeps its own route and capabilities — this is purely a wrapper, so
# the underlying modules are untouched.
register_ui(
    "comms-panel",
    "Comms",
    "🛰",
    """<div id="comms-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/comms/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["acct.list", "acct.get", "acct.upsert", "acct.delete", "acct.test",
             "acct.oauth.providers", "acct.oauth.auth_url", "acct.oauth.complete",
             "acct.oauth.disconnect"],
    mode="tab",
    tab_order=68,
)