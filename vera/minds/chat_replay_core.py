"""Chat replay capture (four-minds FM-0.3, §5.2 item 4; owner yes 17 Oct, §13 Q3): what is kept and how.

Most of a chat turn is assembled in the browser (the system prefix, the cap hint, the history), so the
server's own records cannot replay a turn faithfully. With `chat.replay_capture` on, the exact request
body of /agents/chat/stream is kept: credentials redacted (the log redactor, on every string), bounded,
for 30 days, in a private store that never goes to the repo. A captured case reaches the chat corpus
only after it is approved one by one.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Optional

RETAIN_DAYS = 30
# sessions that are not real use: the weekly Playwright corpus run types its own cases
SKIP_SESSION_PREFIXES = ("chat-corpus-",)
MAX_BODY_CHARS = 400_000        # a whole long history fits; anything past is cut, and says so
PREVIEW_CHARS = 160


def wanted(mode: str, session_id: str) -> bool:
    return str(mode) == "on" and not str(session_id or "").startswith(SKIP_SESSION_PREFIXES)


def _redact(v: Any, cred: Optional[Callable[[str], str]]) -> Any:
    if cred is None:
        return v
    if isinstance(v, str):
        try:
            return cred(v)
        except Exception:
            return v
    if isinstance(v, dict):
        return {k: _redact(x, cred) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact(x, cred) for x in v]
    return v


def row(body: Dict[str, Any], session_id: str, cred: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """The stored case: the redacted body, or (past MAX_BODY_CHARS) its JSON cut to the bound."""
    red = _redact(dict(body or {}), cred)
    text = json.dumps(red, ensure_ascii=False, default=str)
    truncated = len(text) > MAX_BODY_CHARS
    stored: Any = {"_truncated_json": text[:MAX_BODY_CHARS]} if truncated else red
    msg = str((body or {}).get("message") or "")
    return {
        "session_id": str(session_id or ""),
        "agent": str((body or {}).get("agent_name") or (body or {}).get("agent_id") or ""),
        "message_chars": len(msg),
        "body_chars": len(text),
        "truncated": truncated,
        "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "body": stored,
    }


def preview(message: str) -> str:
    m = " ".join(str(message or "").split())
    return m if len(m) <= PREVIEW_CHARS else m[:PREVIEW_CHARS - 1] + "…"


def corpus_case(case_id: Any, body: Dict[str, Any], note: str = "") -> Dict[str, Any]:
    """An approved capture in the shape the corpus keeps: the message, the history and what the browser
    added, so a replay sends the same request."""
    b = dict(body or {})
    return {"source": f"replay:{case_id}", "message": b.get("message", ""), "agent_name": b.get("agent_name", ""),
            "history": b.get("history", "[]"), "request": b, "note": note}
