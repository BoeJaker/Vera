"""
email_capabilities.py — Vera Email (multi-account, IMAP/SMTP, AI-assisted)
==========================================================================

A Telegram-style email module backed by the shared **Accounts registry**
(vera/accounts), so credentials are configured once and reused by Calendar too,
and multiple email accounts are supported.

  • Read & search inbox — GATED behind a global `reading_enabled` flag (OFF by default).
  • Send & reply via SMTP, from any configured mail account.
  • AI draft / summarise using the local Ollama cluster.
  • Event-notification bridge — forward selected vera:events to an address.
  • Its own "Email" UI tab (account picker on Inbox / Compose).

Email no longer stores credentials itself — they live (sealed) in the Accounts
registry. Email keeps only global settings + the notification bridge config.

Capabilities (group `mail.*`)
─────────────────────────────
  mail.config.get / mail.config.set            (global settings: reading, model, signature, default_account)
  mail.accounts.list                           (mail-capable accounts, redacted)
  mail.test                                    (test an account)
  mail.inbox.list / mail.message.get / mail.search   (gated by reading_enabled)
  mail.send / mail.reply / mail.draft          (all accept account=<id>)
  mail.events.configure / mail.events.status
  mail.panel.html  (serves /mail/panel)

Redis layout
────────────
  vera:mail:settings  string JSON  {reading_enabled, model, signature, default_account}
  vera:mail:events    string JSON  {enabled, to_addr, types[]}
  vera:mail:config    string JSON  (LEGACY single-account — migrated on startup)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
    ollama_generate,
    register_ui,
    schedule,
)
from Vera.vera.security import secrets as vsecrets
from Vera.vera.email import transport as mail_transport

log = logging.getLogger("vera.email")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "email_panel.html"

KEY_SETTINGS      = "vera:mail:settings"
KEY_EVENTS        = "vera:mail:events"
KEY_CONFIG_LEGACY = "vera:mail:config"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "reading_enabled": False,      # READS ARE OFF UNTIL EXPLICITLY ENABLED
    "model":           "",         # ollama model for drafting (blank = default)
    "signature":       "",
    "default_account": "",         # account id used when none is specified
}

DEFAULT_EVENTS: Dict[str, Any] = {
    "enabled":  False,
    "to_addr":  "",
    "types":    ["dag.completed", "research.completed", "cap.error"],
}


def _redis():
    return getattr(_orch, "REDIS", None)


def _accounts():
    """Resolve the shared accounts module lazily (loaded at startup)."""
    return sys.modules.get("accounts_capabilities")


# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

async def _get_settings() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(DEFAULT_SETTINGS)
    try:
        raw = await r.get(KEY_SETTINGS)
        merged = dict(DEFAULT_SETTINGS)
        if raw:
            merged.update(json.loads(raw))
        return merged
    except Exception as e:
        log.warning("mail settings read: %s", e)
        return dict(DEFAULT_SETTINGS)


async def _save_settings(s: Dict[str, Any]):
    r = _redis()
    if r:
        await r.set(KEY_SETTINGS, json.dumps(s))


# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNT → TRANSPORT
# ─────────────────────────────────────────────────────────────────────────────

def _build_mail_cfg(acct: Dict) -> Dict[str, Any]:
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


async def _resolve_account(account_id: str = "") -> Optional[Dict]:
    """Resolve the account to use (opened secrets): explicit id → default → first."""
    am = _accounts()
    if not am:
        return None
    if account_id:
        return await am.get_account(account_id)
    settings = await _get_settings()
    if settings.get("default_account"):
        a = await am.get_account(settings["default_account"])
        if a and a.get("app_password"):
            return a
    return await am.default_mail_account()


async def _transport(account_id: str = ""):
    """Return (transport, account) for the resolved account, or (None, None)."""
    acct = await _resolve_account(account_id)
    if not acct or not acct.get("app_password"):
        return None, None
    return mail_transport.get_transport(_build_mail_cfg(acct)), acct


_NO_ACCOUNT = {"error": "no mail account configured — add one in the Accounts tab"}


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS / ACCOUNTS CAPS
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "mail.config.get", http_method="GET", http_path="/mail/config",
    http_tags=["email"], memory="off", silent=True,
    description="Get global email settings + the list of mail-capable accounts "
                "for the picker. Output: {reading_enabled, model, signature, "
                "default_account, accounts:[{id,label,email,has_app_password}]}.",
)
async def cap_config_get(trace_id=None):
    out = dict(await _get_settings())
    out["accounts"] = await _mail_accounts()
    return out


@capability(
    "mail.config.set", http_method="POST", http_path="/mail/config/set",
    http_tags=["email"], memory="on",
    description="Update global email settings (credentials live in the Accounts "
                "tab). Input: reading_enabled (bool), model (str), signature "
                "(str), default_account (account id). Output: {ok, settings}.",
)
async def cap_config_set(reading_enabled: Optional[bool] = None, model: Optional[str] = None,
                         signature: Optional[str] = None, default_account: Optional[str] = None,
                         trace_id=None):
    s = await _get_settings()
    for k, v in (("reading_enabled", reading_enabled), ("model", model),
                 ("signature", signature), ("default_account", default_account)):
        if v is not None:
            s[k] = v
    await _save_settings(s)
    return {"ok": True, "settings": {k: s[k] for k in DEFAULT_SETTINGS}}


async def _mail_accounts() -> List[Dict]:
    """Redacted accounts that have a mail block, for UI pickers."""
    am = _accounts()
    if not am:
        return []
    out = []
    for a in await am.list_accounts(opened=False):
        if a.get("mail_enabled") or a.get("imap_host") or a.get("smtp_host"):
            out.append({"id": a.get("id"), "label": a.get("label"),
                        "email": a.get("email"), "mail_enabled": a.get("mail_enabled"),
                        "has_app_password": a.get("has_app_password", False)})
    return out


@capability(
    "mail.accounts.list", http_method="GET", http_path="/mail/accounts",
    http_tags=["email"], memory="off", silent=True,
    description="List mail-capable accounts (redacted) for the account picker. "
                "Output: {accounts:[{id,label,email,has_app_password}], default}.",
)
async def cap_accounts_list(trace_id=None):
    s = await _get_settings()
    return {"accounts": await _mail_accounts(), "default": s.get("default_account", "")}


@capability(
    "mail.test", http_method="POST", http_path="/mail/test",
    http_tags=["email"], memory="on",
    description="Test IMAP login and SMTP handshake for an account. "
                "Input: account (account id — default account if blank). "
                "Output: {ok, imap:{ok,error?}, smtp:{ok,error?}}.",
)
async def cap_test(account: str = "", trace_id=None):
    t, acct = await _transport(account)
    if not t:
        return _NO_ACCOUNT
    try:
        return await t.test()
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
#  READING  (gated behind reading_enabled)
# ═════════════════════════════════════════════════════════════════════════════

_READING_DISABLED = {"disabled": True,
                     "error": "reading disabled — enable it in Email → Settings"}


async def _reading_gate() -> Optional[Dict]:
    s = await _get_settings()
    if not s.get("reading_enabled"):
        return _READING_DISABLED
    return None


@capability(
    "mail.inbox.list", http_method="GET", http_path="/mail/inbox",
    http_tags=["email"], memory="off", silent=True,
    description="List recent inbox messages (headers only). GATED: returns "
                "{disabled:true} unless reading is enabled in settings. "
                "Input: account (id), limit (int, default 25), folder (INBOX). "
                "Output: {messages:[{uid,from,subject,date,seen}]}.",
)
async def cap_inbox_list(account: str = "", limit: int = 25, folder: str = "INBOX", trace_id=None):
    gate = await _reading_gate()
    if gate:
        return gate
    t, _ = await _transport(account)
    if not t:
        return _NO_ACCOUNT
    try:
        msgs = await t.list_inbox(limit=limit, folder=folder)
        return {"messages": msgs, "count": len(msgs)}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "mail.message.get", http_method="GET", http_path="/mail/message",
    http_tags=["email"], memory="off", silent=True,
    description="Fetch a full message by uid. GATED by reading_enabled. "
                "Input: account (id), uid (str!), folder (default INBOX). "
                "Output: {uid,from,to,subject,date,body,attachments}.",
)
async def cap_message_get(uid: str = "", account: str = "", folder: str = "INBOX", trace_id=None):
    if not uid:
        return {"error": "uid is required"}
    gate = await _reading_gate()
    if gate:
        return gate
    t, _ = await _transport(account)
    if not t:
        return _NO_ACCOUNT
    try:
        return await t.get_message(uid, folder=folder)
    except Exception as e:
        return {"error": str(e)}


@capability(
    "mail.search", http_method="GET", http_path="/mail/search",
    http_tags=["email"], memory="off", silent=True,
    description="Search the inbox by from/subject/body text. GATED by "
                "reading_enabled. Input: account (id), q (str!), limit (int, 25). "
                "Output: {messages:[...]}.",
)
async def cap_search(q: str = "", account: str = "", limit: int = 25, trace_id=None):
    gate = await _reading_gate()
    if gate:
        return gate
    t, _ = await _transport(account)
    if not t:
        return _NO_ACCOUNT
    try:
        msgs = await t.search(q, limit=limit)
        return {"messages": msgs, "count": len(msgs)}
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
#  SENDING
# ═════════════════════════════════════════════════════════════════════════════

def _with_signature(body: str, signature: str) -> str:
    if signature and signature.strip() and signature.strip() not in body:
        return f"{body}\n\n-- \n{signature}"
    return body


@capability(
    "mail.send", http_method="POST", http_path="/mail/send",
    http_tags=["email"], memory="on",
    description="Send a new email via SMTP. Input: account (id — default if "
                "blank), to (str!), subject (str!), body (str!), cc (str), "
                "bcc (str), html (bool). Output: {ok, to, message_id}.",
)
async def cap_send(to: str = "", subject: str = "", body: str = "", account: str = "",
                   cc: str = "", bcc: str = "", html: bool = False, trace_id=None):
    if not to:
        return {"error": "to is required"}
    if not (subject or body):
        return {"error": "subject or body is required"}
    t, acct = await _transport(account)
    if not t:
        return _NO_ACCOUNT
    s = await _get_settings()
    try:
        res = await t.send(to, subject, _with_signature(body, s.get("signature", "")),
                           cc=cc, bcc=bcc, html=html)
        await emit_event({"type": "mail.sent", "stage": "send",
                          "message": f"sent to {to}", "subject": subject,
                          "account": acct.get("email", "")})
        return res
    except Exception as e:
        return {"error": str(e)}


@capability(
    "mail.reply", http_method="POST", http_path="/mail/reply",
    http_tags=["email"], memory="on",
    description="Reply to a message by uid (threaded). Input: account (id), "
                "uid (str!), body (str!), reply_all (bool), html (bool). "
                "Output: {ok, to, message_id}.",
)
async def cap_reply(uid: str = "", body: str = "", account: str = "",
                    reply_all: bool = False, html: bool = False, trace_id=None):
    if not uid:
        return {"error": "uid is required"}
    if not body:
        return {"error": "body is required"}
    t, acct = await _transport(account)
    if not t:
        return _NO_ACCOUNT
    s = await _get_settings()
    try:
        orig = await t.get_message(uid)
        if orig.get("error"):
            return {"error": f"could not load original: {orig['error']}"}
        from email.utils import parseaddr
        to_addr = parseaddr(orig.get("from", ""))[1]
        cc = orig.get("cc", "") if reply_all else ""
        subj = orig.get("subject", "")
        if not subj.lower().startswith("re:"):
            subj = "Re: " + subj
        refs = (orig.get("references", "") + " " + orig.get("message_id", "")).strip()
        res = await t.send(to_addr, subj, _with_signature(body, s.get("signature", "")),
                           cc=cc, reply_headers={"in_reply_to": orig.get("message_id", ""),
                                                 "references": refs}, html=html)
        await emit_event({"type": "mail.sent", "stage": "reply",
                          "message": f"replied to {to_addr}", "subject": subj})
        return res
    except Exception as e:
        return {"error": str(e)}


@capability(
    "mail.draft", http_method="POST", http_path="/mail/draft",
    http_tags=["email"], memory="on",
    description="Draft or summarise an email with the local Ollama LLM. "
                "Input: instruction (str!), context (str), account (id), "
                "uid (str — load an inbox message as context, needs reading), "
                "mode (compose|summarise). Output: {subject, body}.",
)
async def cap_draft(instruction: str = "", context: str = "", account: str = "",
                    uid: str = "", mode: str = "compose", trace_id=None):
    if not instruction:
        return {"error": "instruction is required"}
    s = await _get_settings()
    if uid:
        gate = await _reading_gate()
        if gate:
            return gate
        t, _ = await _transport(account)
        if not t:
            return _NO_ACCOUNT
        try:
            msg = await t.get_message(uid)
            if not msg.get("error"):
                context = (f"From: {msg.get('from')}\nSubject: {msg.get('subject')}\n\n"
                           f"{msg.get('body', '')}\n\n{context}").strip()
        except Exception as e:
            return {"error": f"could not load context message: {e}"}

    if mode == "summarise":
        system = ("You summarise emails clearly and briefly. Reply with only the "
                  "summary — key points and any action items as short bullets.")
        prompt = f"Summarise the following email:\n\n{context}"
    else:
        system = ("You are an email-writing assistant. Write a clear, professional "
                  "email that fulfils the user's instruction. Return ONLY valid "
                  'minified JSON: {"subject": "...", "body": "..."} with no prose '
                  "or code fences. Keep the body plain text with normal line breaks.")
        prompt = instruction
        if context:
            prompt += f"\n\nRelevant context / email being replied to:\n{context}"

    try:
        raw = await ollama_generate(
            prompt, system=system, json_mode=(mode != "summarise"),
            model=s.get("model") or None, prefer_gpu=True,
            caller_override={"caller_file": "email_capabilities.py",
                             "caller_func": "mail_draft", "cap_name": "mail.draft"})
    except Exception as e:
        return {"error": f"LLM error: {e}"}

    if mode == "summarise":
        return {"subject": "", "body": raw.strip()}
    import re
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i != -1 and j != -1:
        txt = txt[i:j + 1]
    try:
        data = json.loads(txt)
        return {"subject": data.get("subject", ""), "body": data.get("body", "")}
    except Exception:
        return {"subject": "", "body": raw.strip()}


# ═════════════════════════════════════════════════════════════════════════════
#  EVENT NOTIFICATION BRIDGE
# ═════════════════════════════════════════════════════════════════════════════

async def _get_events_cfg() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(DEFAULT_EVENTS)
    try:
        raw = await r.get(KEY_EVENTS)
        merged = dict(DEFAULT_EVENTS)
        if raw:
            merged.update(json.loads(raw))
        return merged
    except Exception:
        return dict(DEFAULT_EVENTS)


async def _save_events_cfg(ev: Dict[str, Any]):
    r = _redis()
    if r:
        await r.set(KEY_EVENTS, json.dumps(ev))


_EVENT_TASK = None


async def _event_bridge_loop():
    """Subscribe to vera:events and email matching event types to to_addr."""
    try:
        r = _redis()
        if not r:
            return
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe("vera:events")
        except Exception as e:
            log.warning("mail event bridge subscribe: %s", e)
            return
        log.info("mail event bridge: listening on vera:events")
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "ignore")
                ev = json.loads(data)
            except Exception:
                continue

            ev_cfg = await _get_events_cfg()
            if not ev_cfg.get("enabled"):
                continue
            to_addr = str(ev_cfg.get("to_addr", "") or "")
            if not to_addr:
                continue
            wanted = ev_cfg.get("types") or []
            etype = ev.get("type", "")
            if not any(etype == t or etype.startswith(t + ".") for t in wanted):
                continue

            preview = ev.get("text") or ev.get("name") or ev.get("message") or ""
            body = (f"Event: {etype}\n\n"
                    f"{json.dumps({k: v for k, v in ev.items() if k != 'type'}, default=str, indent=2)[:1500]}")
            try:
                t, _acct = await _transport()      # default account
                if t:
                    await t.send(to_addr, f"[Vera] {etype}: {str(preview)[:80]}", body)
            except Exception as e:
                log.debug("mail event forward: %s", e)
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.warning("mail event bridge: %s", e)


@capability(
    "mail.events.configure", http_method="POST", http_path="/mail/events",
    http_tags=["email"], memory="off",
    description="Configure the email notification bridge. Input: enabled (bool), "
                "to_addr (str), types (list of event-type prefixes). "
                "Output: {ok, events}.",
)
async def cap_events_configure(enabled: Optional[bool] = None, to_addr: Optional[str] = None,
                               types: Optional[List[str]] = None, trace_id=None):
    cur = await _get_events_cfg()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if to_addr is not None:
        cur["to_addr"] = to_addr
    if types is not None:
        cur["types"] = types
    await _save_events_cfg(cur)
    _ensure_bridge()
    return {"ok": True, "events": cur}


@capability(
    "mail.events.status", http_method="GET", http_path="/mail/events",
    http_tags=["email"], memory="off", silent=True,
    description="Get the email notification bridge config. Output: {events}.",
)
async def cap_events_status(trace_id=None):
    return {"events": await _get_events_cfg(),
            "bridge_running": bool(_EVENT_TASK and not _EVENT_TASK.done())}


def _ensure_bridge():
    global _EVENT_TASK
    if _EVENT_TASK and not _EVENT_TASK.done():
        return
    try:
        _EVENT_TASK = asyncio.create_task(_event_bridge_loop())
    except RuntimeError:
        pass  # no running loop yet (import time) — _startup will retry


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "mail.panel.html", http_method="GET", http_path="/mail/panel",
    http_tags=["email", "ui"], memory="off", silent=True,
    description="Serve the Email panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;"
                "color:#ef5b5b;font-family:monospace;padding:40px'>"
                "<h2>email_panel.html not found</h2>"
                f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")
    return HTMLResponse(html)


@APP.get("/mail/panel", include_in_schema=False)
async def _mail_panel_route():
    p = _HERE / "email_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>email_panel.html not found</p>")


# ═════════════════════════════════════════════════════════════════════════════
#  STARTUP (migration + bridge) + REGISTER TAB
# ═════════════════════════════════════════════════════════════════════════════

async def _migrate_legacy():
    """Move a legacy single-account `vera:mail:config` into the Accounts registry
    (once). The legacy key is preserved; a flag prevents re-migration."""
    r = _redis()
    am = _accounts()
    if not r or not am:
        return
    settings = await _get_settings()
    if settings.get("_migrated_legacy"):
        return
    raw = await r.get(KEY_CONFIG_LEGACY)
    if raw:
        try:
            old = json.loads(raw)
        except Exception:
            old = {}
        if old.get("username") or old.get("imap_host"):
            pw = vsecrets.open_secret(old.get("password", ""))
            res = await am.upsert_account({
                "label": old.get("from_name") or old.get("username") or "Email",
                "email": old.get("from_addr") or old.get("username", ""),
                "mail_enabled": True,
                "imap_host": old.get("imap_host", ""), "imap_port": old.get("imap_port", 993),
                "imap_ssl": old.get("imap_ssl", True),
                "smtp_host": old.get("smtp_host", ""), "smtp_port": old.get("smtp_port", 587),
                "smtp_tls": old.get("smtp_tls", True),
                "mail_username": old.get("username", ""), "app_password": pw,
            })
            if res.get("ok"):
                settings["default_account"] = res["account"]["id"]
            for k in ("reading_enabled", "model", "signature"):
                if old.get(k) is not None:
                    settings[k] = old[k]
            log.info("mail: migrated legacy config into account %s",
                     res.get("account", {}).get("id"))
    settings["_migrated_legacy"] = True
    await _save_settings(settings)


async def _startup():
    for _ in range(20):
        if _redis() is not None:
            break
        await asyncio.sleep(0.5)
    try:
        await _migrate_legacy()
    except Exception as e:
        log.warning("mail migration: %s", e)
    ev = await _get_events_cfg()
    if ev.get("enabled"):
        _ensure_bridge()
    s = await _get_settings()
    log.info("mail ready — reading_enabled=%s, default_account=%s, notify=%s",
             s.get("reading_enabled"), bool(s.get("default_account")), ev.get("enabled"))


schedule(_startup, interval=999999, name="email_startup")


register_ui(
    "email-panel",
    "Email",
    "✉",
    """<div id="email-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/mail/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "mail.config.get", "mail.config.set", "mail.accounts.list", "mail.test",
        "mail.inbox.list", "mail.message.get", "mail.search",
        "mail.send", "mail.reply", "mail.draft",
        "mail.events.configure", "mail.events.status",
    ],
    mode="tab",
    tab_order=76,
)
