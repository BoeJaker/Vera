"""
transport.py — Email transport adapter (IMAP/SMTP now, Gmail-API seam for later)
================================================================================

A thin adapter so the email capabilities don't care *how* mail is moved. The
default `ImapSmtpTransport` uses Python stdlib (`imaplib`, `smtplib`, `email`)
— no third-party deps, provider-agnostic, and needs only an app-password
(e.g. a Gmail App Password with 2-Step Verification on). A `gmail_api` type is
reserved behind the same interface for a future OAuth implementation.

Blocking socket calls are run in a worker thread via `asyncio.to_thread` so the
event loop is never stalled.

Config dict shape (see email_capabilities.DEFAULT_CONFIG):
  transport_type, imap_host, imap_port, imap_ssl,
  smtp_host, smtp_port, smtp_tls, username, password (plaintext at call time),
  from_addr, from_name, signature
"""

from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import logging
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.email.transport")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _xoauth2_bytes(user: str, token: str) -> bytes:
    """SASL XOAUTH2 initial-client-response (un-base64'd; callers/imaplib encode)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


def _dh(value: str) -> str:
    """Decode a possibly RFC-2047-encoded header into a plain string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message) -> str:
    """Extract a readable plain-text body, falling back to stripped HTML."""
    if msg.is_multipart():
        # Prefer the first text/plain part.
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_content().strip()
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return _strip_html(part.get_content())
                except Exception:
                    pass
        return ""
    try:
        content = msg.get_content()
    except Exception:
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html(content)
    return (content or "").strip()


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _attachments(msg: email.message.Message) -> List[Dict[str, Any]]:
    out = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp or part.get_filename():
                out.append({"filename": _dh(part.get_filename() or "attachment"),
                            "content_type": part.get_content_type()})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# BASE INTERFACE (the adapter seam)
# ─────────────────────────────────────────────────────────────────────────────

class BaseTransport:
    """Interface every transport implements. Methods are async."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    async def test(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def list_inbox(self, limit: int = 25, folder: str = "INBOX") -> List[Dict]:
        raise NotImplementedError

    async def get_message(self, uid: str, folder: str = "INBOX") -> Dict[str, Any]:
        raise NotImplementedError

    async def search(self, query: str, limit: int = 25,
                     folder: str = "INBOX") -> List[Dict]:
        raise NotImplementedError

    async def send(self, to: str, subject: str, body: str, cc: str = "",
                   bcc: str = "", reply_headers: Optional[Dict] = None,
                   html: bool = False) -> Dict[str, Any]:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# IMAP / SMTP TRANSPORT  (stdlib only)
# ─────────────────────────────────────────────────────────────────────────────

class ImapSmtpTransport(BaseTransport):

    # ── IMAP connection (sync, run via to_thread) ────────────────────────────
    def _xoauth2(self) -> bool:
        return (self.cfg.get("auth_mode") == "xoauth2"
                and bool(self.cfg.get("oauth_token")))

    def _imap_connect(self) -> imaplib.IMAP4:
        host = self.cfg.get("imap_host", "")
        port = int(self.cfg.get("imap_port", 993) or 993)
        user = self.cfg.get("username", "")
        xoauth = self._xoauth2()
        pw = self.cfg.get("password", "")
        if not (host and user and (xoauth or pw)):
            raise RuntimeError("IMAP host/username/credential not configured")
        if self.cfg.get("imap_ssl", True):
            M = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
        else:
            M = imaplib.IMAP4(host, port)
        if xoauth:
            # imaplib base64-encodes the bytes the callback returns.
            M.authenticate(
                "XOAUTH2",
                lambda _: _xoauth2_bytes(user, self.cfg["oauth_token"]))
        else:
            M.login(user, pw)
        return M

    def _smtp_connect(self) -> smtplib.SMTP:
        host = self.cfg.get("smtp_host", "")
        port = int(self.cfg.get("smtp_port", 587) or 587)
        user = self.cfg.get("username", "")
        xoauth = self._xoauth2()
        pw = self.cfg.get("password", "")
        if not (host and user and (xoauth or pw)):
            raise RuntimeError("SMTP host/username/credential not configured")
        if int(port) == 465:
            S = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
        else:
            S = smtplib.SMTP(host, port, timeout=30)
            if self.cfg.get("smtp_tls", True):
                S.starttls(context=ssl.create_default_context())
        if xoauth:
            S.ehlo()
            b64 = base64.b64encode(
                _xoauth2_bytes(user, self.cfg["oauth_token"])).decode()
            code, resp = S.docmd("AUTH", "XOAUTH2 " + b64)
            if code == 334:           # server wants the (base64) error read back
                code, resp = S.docmd("")
            if code != 235:
                raise smtplib.SMTPAuthenticationError(code, resp)
        else:
            S.login(user, pw)
        return S

    # ── test ─────────────────────────────────────────────────────────────────
    async def test(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self._test_sync)

    def _test_sync(self) -> Dict[str, Any]:
        out = {"imap": {"ok": False}, "smtp": {"ok": False}}
        try:
            M = self._imap_connect()
            M.select("INBOX", readonly=True)
            M.logout()
            out["imap"] = {"ok": True}
        except Exception as e:
            out["imap"] = {"ok": False, "error": str(e)}
        try:
            S = self._smtp_connect()
            S.quit()
            out["smtp"] = {"ok": True}
        except Exception as e:
            out["smtp"] = {"ok": False, "error": str(e)}
        out["ok"] = out["imap"]["ok"] and out["smtp"]["ok"]
        return out

    # ── list inbox ───────────────────────────────────────────────────────────
    async def list_inbox(self, limit: int = 25, folder: str = "INBOX") -> List[Dict]:
        return await asyncio.to_thread(self._list_sync, limit, folder, None)

    async def search(self, query: str, limit: int = 25,
                     folder: str = "INBOX") -> List[Dict]:
        # Build an IMAP search criterion: text search across FROM/SUBJECT/BODY.
        crit = ("OR", "OR", "FROM", _q(query), "SUBJECT", _q(query), "TEXT", _q(query)) \
            if query else ("ALL",)
        return await asyncio.to_thread(self._list_sync, limit, folder, crit)

    def _list_sync(self, limit: int, folder: str, crit) -> List[Dict]:
        M = self._imap_connect()
        try:
            M.select(folder, readonly=True)
            if crit:
                typ, data = M.search(None, *crit)
            else:
                typ, data = M.search(None, "ALL")
            if typ != "OK":
                return []
            ids = data[0].split()
            ids = ids[-limit:][::-1]          # newest first
            out = []
            for mid in ids:
                typ, mdata = M.fetch(mid, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
                if typ != "OK" or not mdata:
                    continue
                uid = _parse_uid(mdata)
                hdr = b""
                flags = b""
                for part in mdata:
                    if isinstance(part, tuple):
                        hdr += part[1]
                    elif isinstance(part, bytes):
                        flags += part
                h = BytesParser(policy=default_policy).parsebytes(hdr)
                out.append({
                    "uid": uid or mid.decode(),
                    "from": _dh(h.get("From", "")),
                    "subject": _dh(h.get("Subject", "(no subject)")),
                    "date": _dh(h.get("Date", "")),
                    "seen": b"\\Seen" in flags,
                })
            return out
        finally:
            try:
                M.logout()
            except Exception:
                pass

    # ── full message ─────────────────────────────────────────────────────────
    async def get_message(self, uid: str, folder: str = "INBOX") -> Dict[str, Any]:
        return await asyncio.to_thread(self._get_sync, uid, folder)

    def _get_sync(self, uid: str, folder: str) -> Dict[str, Any]:
        M = self._imap_connect()
        try:
            M.select(folder, readonly=True)
            typ, data = M.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                return {"error": "message not found"}
            raw = data[0][1]
            msg = BytesParser(policy=default_policy).parsebytes(raw)
            return {
                "uid": str(uid),
                "from": _dh(msg.get("From", "")),
                "to": _dh(msg.get("To", "")),
                "cc": _dh(msg.get("Cc", "")),
                "subject": _dh(msg.get("Subject", "(no subject)")),
                "date": _dh(msg.get("Date", "")),
                "message_id": msg.get("Message-ID", ""),
                "references": msg.get("References", ""),
                "body": _body_text(msg),
                "attachments": _attachments(msg),
            }
        finally:
            try:
                M.logout()
            except Exception:
                pass

    # ── send ─────────────────────────────────────────────────────────────────
    async def send(self, to: str, subject: str, body: str, cc: str = "",
                   bcc: str = "", reply_headers: Optional[Dict] = None,
                   html: bool = False) -> Dict[str, Any]:
        return await asyncio.to_thread(self._send_sync, to, subject, body,
                                       cc, bcc, reply_headers or {}, html)

    def _send_sync(self, to, subject, body, cc, bcc, reply_headers, html) -> Dict[str, Any]:
        from_addr = self.cfg.get("from_addr") or self.cfg.get("username", "")
        from_name = self.cfg.get("from_name", "")
        msg = EmailMessage()
        msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        if reply_headers.get("in_reply_to"):
            msg["In-Reply-To"] = reply_headers["in_reply_to"]
        if reply_headers.get("references"):
            msg["References"] = reply_headers["references"]
        if html:
            msg.set_content(_strip_html(body))
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        recipients = [a for a in (
            *_split_addrs(to), *_split_addrs(cc), *_split_addrs(bcc)) if a]
        S = self._smtp_connect()
        try:
            S.send_message(msg, from_addr=from_addr, to_addrs=recipients)
        finally:
            try:
                S.quit()
            except Exception:
                pass
        return {"ok": True, "to": recipients, "subject": subject,
                "message_id": msg["Message-ID"]}


# ─────────────────────────────────────────────────────────────────────────────
# GMAIL API TRANSPORT  (reserved seam — not implemented)
# ─────────────────────────────────────────────────────────────────────────────

class GmailApiTransport(BaseTransport):
    def __init__(self, cfg):
        raise NotImplementedError(
            "Gmail-API transport is not implemented yet — use transport_type="
            "'imap_smtp' with a Gmail App Password (no API setup required).")


# ─────────────────────────────────────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def get_transport(cfg: Dict[str, Any]) -> BaseTransport:
    t = (cfg or {}).get("transport_type", "imap_smtp")
    if t == "imap_smtp":
        return ImapSmtpTransport(cfg)
    if t == "gmail_api":
        return GmailApiTransport(cfg)
    raise ValueError(f"unknown transport_type {t!r}")


# ── small utilities ──────────────────────────────────────────────────────────

def _q(s: str) -> str:
    return '"' + s.replace('"', "") + '"'


def _split_addrs(value: str) -> List[str]:
    if not value:
        return []
    out = []
    for chunk in value.replace(";", ",").split(","):
        addr = parseaddr(chunk)[1]
        if addr:
            out.append(addr)
    return out


def _parse_uid(mdata) -> str:
    import re
    for part in mdata:
        blob = part[0] if isinstance(part, tuple) else part
        if isinstance(blob, bytes):
            m = re.search(rb"UID (\d+)", blob)
            if m:
                return m.group(1).decode()
    return ""
