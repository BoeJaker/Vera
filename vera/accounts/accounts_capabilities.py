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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, now_iso, register_ui,
)
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.accounts")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "accounts_panel.html"

KEY_ACCOUNTS = "vera:accounts"

# Secret fields sealed before storage and never returned to the UI.
_SECRET_FIELDS = ("app_password", "caldav_password")

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
              "mail_username", "caldav_url", "caldav_username", "ics_url"):
        if fields.get(k) is not None:
            acct[k] = fields[k]
    if not acct.get("label"):
        acct["label"] = acct.get("email") or "Account"
    try:
        if fields.get("app_password"):
            acct["app_password"] = vsecrets.seal(fields["app_password"])
        if fields.get("caldav_password"):
            acct["caldav_password"] = vsecrets.seal(fields["caldav_password"])
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
                "caldav_url, caldav_username, caldav_password, ics_url. "
                "Output: {ok, account (redacted)}.",
)
async def cap_upsert(
    id: str = "", label: str = "", email: str = "", color: str = "",
    mail_enabled: Optional[bool] = None,
    imap_host: Optional[str] = None, imap_port: Optional[int] = None,
    imap_ssl: Optional[bool] = None, smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None, smtp_tls: Optional[bool] = None,
    mail_username: Optional[str] = None, app_password: str = "",
    caldav_url: Optional[str] = None, caldav_username: Optional[str] = None,
    caldav_password: str = "", ics_url: Optional[str] = None, trace_id=None,
):
    fields: Dict[str, Any] = {"id": id}
    for k, v in (("label", label or None), ("email", email or None),
                 ("color", color or None), ("mail_enabled", mail_enabled),
                 ("imap_host", imap_host), ("imap_port", imap_port),
                 ("imap_ssl", imap_ssl), ("smtp_host", smtp_host),
                 ("smtp_port", smtp_port), ("smtp_tls", smtp_tls),
                 ("mail_username", mail_username), ("caldav_url", caldav_url),
                 ("caldav_username", caldav_username), ("ics_url", ics_url)):
        if v is not None:
            fields[k] = v
    if app_password:
        fields["app_password"] = app_password
    if caldav_password:
        fields["caldav_password"] = caldav_password
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
    ui_caps=["acct.list", "acct.get", "acct.upsert", "acct.delete", "acct.test"],
    mode="tab",
    tab_order=69,
)