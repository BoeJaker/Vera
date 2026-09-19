"""More than one Telegram bot, without breaking the one that exists.

Vera's Telegram bridge grew up with a single token in `vera:tg:config`. Two
consumers now want to poll Telegram from this host (Vera and OpenClaw), and one
bot token can only have ONE poller: Telegram answers the second with
`409 Conflict: terminated by other getUpdates request` - about 3,000 of them a
day on 2026-09-13. The way out is one bot per consumer, so the config becomes a
LIST of bots, each with its own token, admin chat and poller.

Compatibility is the whole design: the legacy top-level `token` /
`admin_chat_id` ARE a bot, called "default". Its Redis keys keep their old,
unsuffixed names, so an existing install upgrades with no data movement and
nothing that reads `cfg["token"]` today has to change.

Pure: no I/O, no app imports. The capability module does the Redis and HTTP.
"""
import json
from typing import Any, Dict, List, Optional

DEFAULT_BOT_ID = "default"


def _clean_id(raw: Any) -> str:
    s = "".join(ch for ch in str(raw or "").strip().lower() if ch.isalnum() or ch in "-_")
    return s or DEFAULT_BOT_ID


def normalise_bots(cfg: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The bots a config describes, legacy fields included, default first.

    Legacy `token`/`admin_chat_id` become the "default" bot unless the list
    already carries one (then the list wins and the legacy fields are ignored).
    Ids are de-duplicated: the first occurrence stands.
    """
    cfg = cfg or {}
    out: List[Dict[str, Any]] = []
    seen = set()
    listed = cfg.get("bots") or []
    if not isinstance(listed, list):
        listed = []
    legacy_token = str(cfg.get("token") or "").strip()
    if legacy_token and not any(isinstance(b, dict) and _clean_id(b.get("id")) == DEFAULT_BOT_ID
                                for b in listed):
        out.append({"id": DEFAULT_BOT_ID, "label": str(cfg.get("label") or "default"),
                    "token": legacy_token,
                    "admin_chat_id": str(cfg.get("admin_chat_id") or "").strip(),
                    "enabled": True})
        seen.add(DEFAULT_BOT_ID)
    for b in listed:
        if not isinstance(b, dict):
            continue
        bid = _clean_id(b.get("id"))
        if bid in seen:
            continue
        seen.add(bid)
        out.append({"id": bid, "label": str(b.get("label") or bid),
                    "token": str(b.get("token") or "").strip(),
                    "admin_chat_id": str(b.get("admin_chat_id") or "").strip(),
                    "enabled": bool(b.get("enabled", True))})
    out.sort(key=lambda b: (b["id"] != DEFAULT_BOT_ID, b["id"]))
    return out


def enabled_bots(bots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [b for b in bots if b.get("enabled", True) and b.get("token")]


def default_bot(bots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The bot outbound messages use when a chat has no bot of its own."""
    live = enabled_bots(bots)
    for b in live:
        if b["id"] == DEFAULT_BOT_ID:
            return b
    return live[0] if live else None


def bot_by_id(bots: List[Dict[str, Any]], bot_id: Any) -> Optional[Dict[str, Any]]:
    if not bot_id:
        return None
    want = _clean_id(bot_id)
    for b in bots:
        if b["id"] == want:
            return b
    return None


def key_for(base: str, bot_id: Any) -> str:
    """Per-bot Redis key. The default bot keeps the legacy unsuffixed key, so an
    upgrade moves no data: its offset and bot_info are where they always were."""
    bid = _clean_id(bot_id)
    return base if bid == DEFAULT_BOT_ID else f"{base}:{bid}"


def merge_bots(current: Any, incoming: Any) -> List[Dict[str, Any]]:
    """Apply `incoming` (a list, or its JSON text) to the stored list by id.

    Fields given update the entry; missing fields keep their value, so a
    caller can flip `enabled` without re-sending the token. `{"id": X,
    "remove": true}` deletes X. A malformed `incoming` changes nothing.
    """
    if isinstance(incoming, str):
        try:
            incoming = json.loads(incoming or "[]")
        except ValueError:
            return list(current or [])
    if isinstance(incoming, dict):
        incoming = [incoming]
    if not isinstance(incoming, list):
        return list(current or [])
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for b in current or []:
        if isinstance(b, dict):
            bid = _clean_id(b.get("id"))
            if bid not in by_id:
                order.append(bid)
            by_id[bid] = dict(b, id=bid)
    for b in incoming:
        if not isinstance(b, dict):
            continue
        bid = _clean_id(b.get("id"))
        if b.get("remove"):
            by_id.pop(bid, None)
            order = [o for o in order if o != bid]
            continue
        cur = by_id.get(bid, {"id": bid})
        for k in ("label", "token", "admin_chat_id", "enabled"):
            if k in b and b[k] is not None:
                cur[k] = b[k].strip() if isinstance(b[k], str) else b[k]
        cur["id"] = bid
        if bid not in by_id:
            order.append(bid)
        by_id[bid] = cur
    return [by_id[o] for o in order if o in by_id]


def redact_token(t: Any) -> str:
    t = str(t or "")
    if not t:
        return ""
    return (t[:6] + "…" + t[-4:]) if len(t) > 12 else "***"


def redacted_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The config with every token masked and `token_set` added per bot."""
    out = dict(cfg or {})
    tok = str(out.get("token") or "")
    out["token"] = redact_token(tok)
    out["token_set"] = bool(tok)
    bots = []
    for b in normalise_bots(cfg):
        bots.append({**b, "token": redact_token(b.get("token")), "token_set": bool(b.get("token"))})
    out["bots"] = bots
    return out
