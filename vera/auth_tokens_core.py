"""Caller tokens for non-browser callers (B0.3b, pure, no I/O).

A token is an opaque bearer `vat_<id>.<secret>`: a 12-hex id and a 256-bit url-safe secret. The server keeps only a
record with the secret's SHA-256 (never the secret): {id, kind, subject, scopes, digest, created, expires,
last_used, revoked}. The secret is shown once, through keydrop or a 0600 file, never over HTTP.

Rollout (DEEP-28 §11.8): R2 issues tokens and accepts them as optional; the server records who sends one and when,
and refuses nothing. R3a later refuses an off-host non-browser call without a valid token.

Kinds name who holds the token (DEEP-28 §11.5): mcp_bridge, codex, tool, sandbox, node, n8n, openclaw.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

PREFIX = "vat_"
KINDS = ("mcp_bridge", "codex", "tool", "sandbox", "node", "n8n", "openclaw")
MAX_TTL_DAYS = 365
DEFAULT_TTL_DAYS = 90
LAST_USED_EVERY_S = 60.0      # a token's last_used is written at most this often

_ID = re.compile(r"^[0-9a-f]{12}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SUBJECT = re.compile(r"[^A-Za-z0-9@._:-]")


def digest(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def mint(token_id: Optional[str] = None) -> Tuple[str, str, str]:
    """(token, id, secret) for a new token. The caller stores only digest(secret)."""
    tid = token_id or secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)                 # 256 bits -> 43 characters
    return f"{PREFIX}{tid}.{secret}", tid, secret


def parse(token: str) -> Optional[Tuple[str, str]]:
    """'vat_<id>.<secret>' -> (id, secret), or None for anything else."""
    t = str(token or "").strip()
    if not t.startswith(PREFIX) or "." not in t:
        return None
    tid, _, secret = t[len(PREFIX):].partition(".")
    if not _ID.match(tid) or not _SECRET.match(secret):
        return None
    return tid, secret


def from_headers(headers: Mapping[str, str]) -> str:
    """The token a request carries: `Authorization: Bearer vat_...` or `X-Vera-Token: vat_...` (lower-cased names)."""
    auth = str(headers.get("authorization") or "").strip()
    if auth[:7].lower() == "bearer ":
        cand = auth[7:].strip()
        if cand.startswith(PREFIX):
            return cand
    cand = str(headers.get("x-vera-token") or "").strip()
    return cand if cand.startswith(PREFIX) else ""


def clean_subject(subject: str) -> str:
    return _SUBJECT.sub("", str(subject or "").strip())[:80]


def record(token_id: str, secret: str, *, kind: str, subject: str, scopes: Sequence[str] = (), now: float,
           ttl_days: float = DEFAULT_TTL_DAYS, note: str = "") -> Dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    subj = clean_subject(subject)
    if not subj:
        raise ValueError("subject required (who holds the token, e.g. claude@lenovo)")
    days = max(1.0, min(float(ttl_days or DEFAULT_TTL_DAYS), float(MAX_TTL_DAYS)))
    return {"id": token_id, "kind": kind, "subject": subj,
            "scopes": [str(s).strip() for s in (scopes or []) if str(s).strip()][:64],
            "digest": digest(secret), "created": now, "expires": now + days * 86400,
            "last_used": 0.0, "last_peer": "", "uses": 0, "revoked": False, "note": str(note or "")[:200]}


def verify(token: str, rec: Optional[Mapping[str, Any]], now: float) -> Tuple[bool, str]:
    """(True, "") when `token` matches `rec` and the record is live; else (False, why)."""
    p = parse(token)
    if p is None:
        return False, "malformed"
    if not rec:
        return False, "unknown"
    tid, secret = p
    if rec.get("id") != tid or not hmac.compare_digest(str(rec.get("digest") or ""), digest(secret)):
        return False, "wrong secret"
    if rec.get("revoked"):
        return False, "revoked"
    if float(rec.get("expires") or 0) <= now:
        return False, "expired"
    return True, ""


def public(rec: Mapping[str, Any]) -> Dict[str, Any]:
    """The record without its digest: what list/issue may show."""
    return {k: v for k, v in rec.items() if k != "digest"}


def peer_class(peer: str) -> str:
    """'loopback' | 'docker' | 'lan' | 'other' for the TCP peer, for the evidence counters."""
    p = str(peer or "")
    if p in ("127.0.0.1", "::1") or p.startswith("127."):
        return "loopback"
    if p.startswith("172."):
        try:
            if 16 <= int(p.split(".")[1]) <= 31:
                return "docker"
        except (IndexError, ValueError):
            return "other"
    if p.startswith(("192.168.", "10.")) or p.startswith("fe80:"):
        return "lan"
    return "other"


def evidence_key(*, has_origin: bool, peer: str, result: str) -> str:
    """One counter per (browser?, peer class, token result): result is valid | invalid:<why> | none."""
    return f"{'browser' if has_origin else 'program'}|{peer_class(peer)}|{result}"
