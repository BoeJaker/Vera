"""
telegram_capabilities.py  —  Vera Telegram Integration
=======================================================
Brings the Vera capability framework into Telegram.

Features
────────
  • Bidirectional bot — long-poll getUpdates loop, never blocks the orchestrator
  • Per-chat session_id ("tg:{chat_id}") so all activity flows onto the
    memory graph and shows up in the UI just like web sessions
  • Slash commands: /help /id /caps /agents /agent /run /status /think /reset
  • Free-text messages routed to a configurable default agent (agent.chat)
  • Per-chat allow-list — admin chat is always allowed, others must be
    whitelisted via tg.chats.allow or the panel
  • Event bridge — selected vera:events stream events forwarded to a target
    chat (DAG complete, research finished, error, capability of interest)
  • Persistent config in Redis (vera:tg:*) — survives restart, auto-resumes
  • UI panel registered as a top-level harness tab (mode="tab")
  • Full ingest of inbound messages into the data fabric (dataset "tg.messages")

Capabilities registered
───────────────────────
  tg.config.set         tg.config.get
  tg.bot.start          tg.bot.stop          tg.bot.status
  tg.send               tg.send_markdown     tg.broadcast
  tg.notify             tg.history
  tg.chats.list         tg.chats.allow       tg.chats.revoke
  tg.events.configure   tg.events.status
  tg.panel.html         (serves /telegram/panel)

UI panels registered
────────────────────
  telegram-panel        — Bot config, chat manager, message viewer (mode=tab)

Redis keys
──────────
  vera:tg:config            JSON {token, admin_chat_id, default_agent,
                                  auto_start, allow_unknown, think_default,
                                  reply_to_self}
  vera:tg:offset            int     last update_id
  vera:tg:chats             hash    chat_id → JSON chat record
  vera:tg:history:{cid}     list    capped 200 messages, newest at head
  vera:tg:events            JSON    {target_chat_id, types, enabled}
  vera:tg:bot_info          JSON    cached getMe response
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    register_ui,
    schedule,
)

log = logging.getLogger("vera.telegram")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS / STATE
# ─────────────────────────────────────────────────────────────────────────────

_HERE              = Path(__file__).parent
_PANEL_HTML_PATH   = _HERE / "telegram_panel.html"

TG_API_BASE        = "https://api.telegram.org"
HISTORY_CAP        = 200
POLL_TIMEOUT_S     = 25
POLL_BACKOFF_MAX_S = 30

KEY_CONFIG    = "vera:tg:config"
KEY_OFFSET    = "vera:tg:offset"
KEY_CHATS     = "vera:tg:chats"
KEY_HIST_FMT  = "vera:tg:history:{cid}"
KEY_EVENTS    = "vera:tg:events"
KEY_BOT_INFO  = "vera:tg:bot_info"

# In-process runtime state
_BOT_TASK:        Optional[asyncio.Task] = None
_EVENT_TASK:      Optional[asyncio.Task] = None
_BOT_RUNNING:     bool                   = False
_BOT_INFO:        Dict[str, Any]         = {}
_BOT_LAST_UPDATE: Dict[str, Any]         = {"ts": None, "offset": 0, "count": 0, "error": None}
_PER_CHAT_AGENT:  Dict[str, str]         = {}   # transient overrides via /agent
_PER_CHAT_THINK:  Dict[str, bool]        = {}

DEFAULT_CONFIG: Dict[str, Any] = {
    "token":          "",
    "admin_chat_id":  "",
    "default_agent":  "assistant",
    "auto_start":     False,
    "allow_unknown":  False,     # if True, any chat can talk to the bot
    "think_default":  False,
    "max_reply_chars": 3800,     # Telegram hard cap is 4096
}

DEFAULT_EVENTS: Dict[str, Any] = {
    "enabled":        False,
    "target_chat_id": "",
    "types":          ["dag.completed", "research.completed", "cap.error"],
}


def _redis():
    """Return the live shared Redis pool — never open a new connection."""
    return _orch.REDIS


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG / STATE PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

async def _get_config() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(DEFAULT_CONFIG)
    try:
        raw = await r.get(KEY_CONFIG)
        if not raw:
            return dict(DEFAULT_CONFIG)
        data = json.loads(raw)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except Exception as e:
        log.warning("tg config read: %s", e)
        return dict(DEFAULT_CONFIG)


async def _save_config(cfg_data: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_CONFIG, json.dumps(cfg_data))
    except Exception as e:
        log.warning("tg config save: %s", e)


async def _get_events_cfg() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(DEFAULT_EVENTS)
    try:
        raw = await r.get(KEY_EVENTS)
        if not raw:
            return dict(DEFAULT_EVENTS)
        data = json.loads(raw)
        merged = dict(DEFAULT_EVENTS)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_EVENTS)


async def _save_events_cfg(ev: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_EVENTS, json.dumps(ev))
    except Exception as e:
        log.warning("tg events save: %s", e)


async def _get_offset() -> int:
    r = _redis()
    if not r:
        return 0
    try:
        v = await r.get(KEY_OFFSET)
        return int(v) if v else 0
    except Exception:
        return 0


async def _set_offset(o: int):
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_OFFSET, str(o))
    except Exception:
        pass


async def _list_chats() -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_CHATS)
        out = []
        for k, v in (items or {}).items():
            try:
                cid = k.decode() if isinstance(k, bytes) else str(k)
                rec = json.loads(v.decode() if isinstance(v, bytes) else v)
                rec["chat_id"] = cid
                out.append(rec)
            except Exception:
                continue
        out.sort(key=lambda c: c.get("last_seen", ""), reverse=True)
        return out
    except Exception as e:
        log.warning("tg list chats: %s", e)
        return []


async def _get_chat(chat_id: str) -> Optional[Dict[str, Any]]:
    r = _redis()
    if not r:
        return None
    try:
        v = await r.hget(KEY_CHATS, str(chat_id))
        if not v:
            return None
        rec = json.loads(v.decode() if isinstance(v, bytes) else v)
        rec["chat_id"] = str(chat_id)
        return rec
    except Exception:
        return None


async def _save_chat(chat_id: str, rec: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        await r.hset(KEY_CHATS, str(chat_id), json.dumps(rec))
    except Exception as e:
        log.warning("tg save chat: %s", e)


async def _push_history(chat_id: str, entry: Dict[str, Any]):
    r = _redis()
    if not r:
        return
    try:
        key = KEY_HIST_FMT.format(cid=chat_id)
        await r.lpush(key, json.dumps(entry))
        await r.ltrim(key, 0, HISTORY_CAP - 1)
    except Exception as e:
        log.warning("tg history push: %s", e)


async def _get_history(chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        key = KEY_HIST_FMT.format(cid=chat_id)
        rows = await r.lrange(key, 0, limit - 1)
        out = []
        for row in rows or []:
            try:
                out.append(json.loads(row.decode() if isinstance(row, bytes) else row))
            except Exception:
                continue
        return out
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM API HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _tg_api(token: str, method: str, params: Dict[str, Any] = None,
                  timeout: float = 35.0) -> Dict[str, Any]:
    if not token:
        return {"ok": False, "error": "no token configured"}
    url = f"{TG_API_BASE}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=params or {})
            try:
                return r.json()
            except Exception:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _split_long(text: str, limit: int) -> List[str]:
    """Split a string into Telegram-safe chunks at sensible boundaries."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    remainder = text
    while len(remainder) > limit:
        cut = remainder.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remainder.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remainder.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remainder[:cut].rstrip())
        remainder = remainder[cut:].lstrip()
    if remainder:
        parts.append(remainder)
    return parts


async def _send_message(chat_id: str, text: str, parse_mode: str = None,
                        disable_preview: bool = True) -> Dict[str, Any]:
    cfg_data = await _get_config()
    token    = cfg_data.get("token", "")
    if not token:
        return {"ok": False, "error": "no token"}
    limit  = int(cfg_data.get("max_reply_chars", 3800))
    chunks = _split_long(text or "", limit)
    last: Dict[str, Any] = {"ok": True}
    for chunk in chunks:
        params = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        last = await _tg_api(token, "sendMessage", params)
        if not last.get("ok"):
            # Markdown can fail on weird chars — retry as plain text
            if parse_mode:
                params.pop("parse_mode", None)
                last = await _tg_api(token, "sendMessage", params)
            if not last.get("ok"):
                break
    return last


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_chat_record(msg: Dict[str, Any], cfg_data: Dict[str, Any]) -> Dict[str, Any]:
    chat = msg.get("chat", {}) or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return {}
    existing = await _get_chat(chat_id) or {}
    rec = {
        "chat_id":    chat_id,
        "type":       chat.get("type", existing.get("type", "")),
        "title":      chat.get("title", existing.get("title", "")),
        "username":   chat.get("username", existing.get("username", "")),
        "first_name": chat.get("first_name", existing.get("first_name", "")),
        "last_name":  chat.get("last_name", existing.get("last_name", "")),
        "allowed":    existing.get("allowed", False),
        "first_seen": existing.get("first_seen", now_iso()),
        "last_seen":  now_iso(),
        "msg_count":  int(existing.get("msg_count", 0)) + 1,
    }
    # Admin chat is implicitly allowed
    admin = str(cfg_data.get("admin_chat_id", "") or "")
    if admin and chat_id == admin:
        rec["allowed"] = True
    await _save_chat(chat_id, rec)
    return rec


async def _ingest_message_to_fabric(chat_rec: Dict[str, Any], msg: Dict[str, Any], text: str):
    """Best-effort fabric ingest — failures are silent."""
    try:
        import sys as _sys
        fabric = _sys.modules.get("data_fabric")
        if not fabric or not hasattr(fabric, "ingest_dataset"):
            return
        await fabric.ingest_dataset(
            dataset_id="tg.messages",
            data=[{
                "text":      text,
                "chat_id":   chat_rec.get("chat_id", ""),
                "username":  chat_rec.get("username", ""),
                "from":      f"{chat_rec.get('first_name','')} {chat_rec.get('last_name','')}".strip(),
                "ts":        now_iso(),
                "msg_id":    msg.get("message_id"),
            }],
            source="telegram",
            source_id=chat_rec.get("chat_id", ""),
            tags=["telegram", chat_rec.get("type", "private")],
        )
    except Exception as e:
        log.debug("tg fabric ingest: %s", e)


async def _route_to_agent(chat_id: str, text: str, cfg_data: Dict[str, Any]) -> str:
    """Call agent.chat through the capability registry — full memory + tracing."""
    agent_name = _PER_CHAT_AGENT.get(chat_id) or cfg_data.get("default_agent", "assistant")
    think      = _PER_CHAT_THINK.get(chat_id, bool(cfg_data.get("think_default", False)))
    session_id = f"tg:{chat_id}"

    cap = CAPABILITY_REGISTRY.get("agent.chat")
    if not cap:
        return "[agent.chat capability not loaded]"
    try:
        result = await cap["func"](
            message=text,
            agent_name=agent_name,
            session_id=session_id,
            think=think,
        )
        if isinstance(result, dict):
            return (result.get("text")
                    or result.get("response")
                    or result.get("content")
                    or json.dumps(result)[:1500])
        return str(result)[:3500]
    except Exception as e:
        log.warning("tg agent.chat: %s", e)
        return f"[agent error: {e}]"


# ── Slash commands ───────────────────────────────────────────────────────────

HELP_TEXT = (
    "Vera Telegram bot\n"
    "─────────────────\n"
    "/help              this message\n"
    "/id                show your chat id\n"
    "/status            bot + cluster status\n"
    "/caps [filter]     list capabilities (optionally filtered)\n"
    "/agents            list configured agents\n"
    "/agent <name>      set this chat's default agent\n"
    "/run cap.name {json}  invoke a capability with JSON args\n"
    "/think on|off      toggle reasoning mode for this chat\n"
    "/reset             clear this chat's transient overrides\n"
    "\n"
    "Anything else is sent to the active agent."
)


async def _cmd_caps(args: str) -> str:
    flt = (args or "").strip().lower()
    names = sorted(CAPABILITY_REGISTRY.keys())
    if flt:
        names = [n for n in names if flt in n.lower()]
    if not names:
        return "No capabilities match." if flt else "No capabilities loaded."
    if len(names) > 60:
        names = names[:60] + [f"... (+{len(names) - 60} more)"]
    return "Capabilities" + (f" matching '{flt}'" if flt else "") + ":\n" + "\n".join(f"• {n}" for n in names)


async def _cmd_agents() -> str:
    cap = CAPABILITY_REGISTRY.get("agent.list")
    if not cap:
        return "agent.list capability unavailable."
    try:
        result = await cap["func"]()
        agents = result.get("agents") if isinstance(result, dict) else result
        if not agents:
            return "No agents configured."
        lines = ["Agents:"]
        for a in agents[:30]:
            label = a.get("label") or a.get("name", "?")
            name  = a.get("name", "?")
            desc  = (a.get("description") or "")[:60]
            lines.append(f"• {name}  ({label})  {desc}")
        return "\n".join(lines)
    except Exception as e:
        return f"agent.list error: {e}"


async def _cmd_run(args: str) -> str:
    parts = (args or "").strip().split(maxsplit=1)
    if not parts:
        return "Usage: /run cap.name {\"key\": \"value\"}"
    cap_name = parts[0]
    raw_json = parts[1] if len(parts) > 1 else "{}"
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return f"Unknown capability: {cap_name}"
    try:
        kwargs = json.loads(raw_json) if raw_json.strip() else {}
        if not isinstance(kwargs, dict):
            return "JSON args must be an object"
    except Exception as e:
        return f"Invalid JSON: {e}"
    try:
        result = await cap["func"](**kwargs)
        out = json.dumps(result, indent=2, default=str) if isinstance(result, (dict, list)) else str(result)
        if len(out) > 3500:
            out = out[:3500] + "\n... (truncated)"
        return f"{cap_name} →\n{out}"
    except Exception as e:
        return f"Error running {cap_name}: {e}"


async def _cmd_status() -> str:
    cfg_data = await _get_config()
    bot      = _BOT_INFO or {}
    last     = _BOT_LAST_UPDATE
    ev_cfg   = await _get_events_cfg()
    chats    = await _list_chats()
    allowed  = sum(1 for c in chats if c.get("allowed"))
    lines = [
        "Vera Telegram",
        f"  bot:        @{bot.get('username','?')}  ({'running' if _BOT_RUNNING else 'stopped'})",
        f"  capabilities: {len(CAPABILITY_REGISTRY)}",
        f"  default agent: {cfg_data.get('default_agent','assistant')}",
        f"  chats: {len(chats)}  allowed: {allowed}",
        f"  last poll: {last.get('ts') or '—'}  updates: {last.get('count', 0)}",
        f"  event bridge: {'on' if ev_cfg.get('enabled') else 'off'}",
    ]
    if last.get("error"):
        lines.append(f"  last error: {last['error']}")
    return "\n".join(lines)


async def _handle_command(chat_id: str, text: str) -> str:
    raw = text.strip()
    if not raw.startswith("/"):
        return ""
    body = raw[1:]
    if "@" in body.split(" ", 1)[0]:
        head, _, rest = body.partition(" ")
        body = head.split("@", 1)[0] + ((" " + rest) if rest else "")
    cmd, _, args = body.partition(" ")
    cmd = cmd.lower().strip()

    if cmd in ("help", "start"):
        return HELP_TEXT
    if cmd == "id":
        return f"chat_id: {chat_id}"
    if cmd == "status":
        return await _cmd_status()
    if cmd == "caps":
        return await _cmd_caps(args)
    if cmd == "agents":
        return await _cmd_agents()
    if cmd == "agent":
        new = args.strip()
        if not new:
            current = _PER_CHAT_AGENT.get(chat_id) or (await _get_config()).get("default_agent")
            return f"Current agent for this chat: {current}\nUsage: /agent <name>"
        _PER_CHAT_AGENT[chat_id] = new
        return f"Agent for this chat set to: {new}"
    if cmd == "run":
        return await _cmd_run(args)
    if cmd == "think":
        val = args.strip().lower()
        if val in ("on", "true", "1", "yes"):
            _PER_CHAT_THINK[chat_id] = True
            return "Think mode: on"
        if val in ("off", "false", "0", "no"):
            _PER_CHAT_THINK[chat_id] = False
            return "Think mode: off"
        return f"Think mode: {'on' if _PER_CHAT_THINK.get(chat_id) else 'off'}\nUsage: /think on|off"
    if cmd == "reset":
        _PER_CHAT_AGENT.pop(chat_id, None)
        _PER_CHAT_THINK.pop(chat_id, None)
        return "Chat overrides cleared."
    return f"Unknown command: /{cmd}\nTry /help"


# ── Main update dispatcher ───────────────────────────────────────────────────

async def _handle_update(update: Dict[str, Any], cfg_data: Dict[str, Any]):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text = msg.get("text") or msg.get("caption") or ""
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return

    chat_rec = await _ensure_chat_record(msg, cfg_data)

    # History + fabric ingest happen for everyone
    await _push_history(chat_id, {
        "ts":       now_iso(),
        "from":     "user",
        "text":     text,
        "msg_id":   msg.get("message_id"),
        "username": chat_rec.get("username", ""),
    })
    await _ingest_message_to_fabric(chat_rec, msg, text)

    await emit_event({
        "type":     "tg.message_in",
        "chat_id":  chat_id,
        "username": chat_rec.get("username", ""),
        "text":     text[:200],
        "ts":       now_iso(),
    })

    # Allow check
    allow_unknown = bool(cfg_data.get("allow_unknown", False))
    if not (chat_rec.get("allowed") or allow_unknown):
        # Tell new users their id once so the admin can whitelist them
        if int(chat_rec.get("msg_count", 0)) <= 1:
            await _send_message(
                chat_id,
                "This bot is restricted. Ask the admin to whitelist this chat.\n"
                f"Your chat_id: {chat_id}",
            )
        return

    # Dispatch
    if text.startswith("/"):
        reply = await _handle_command(chat_id, text)
    else:
        reply = await _route_to_agent(chat_id, text, cfg_data)

    if not reply:
        return

    sent = await _send_message(chat_id, reply)

    await _push_history(chat_id, {
        "ts":   now_iso(),
        "from": "bot",
        "text": reply[:1500],
        "ok":   bool(sent.get("ok")),
    })
    await emit_event({
        "type":    "tg.message_out",
        "chat_id": chat_id,
        "text":    reply[:200],
        "ok":      bool(sent.get("ok")),
        "ts":      now_iso(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# POLLING LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def _poll_loop():
    global _BOT_RUNNING, _BOT_INFO, _BOT_LAST_UPDATE
    cfg_data = await _get_config()
    token    = cfg_data.get("token", "")
    if not token:
        log.info("tg poll: no token configured — not starting")
        _BOT_RUNNING = False
        return

    me = await _tg_api(token, "getMe", timeout=10)
    if not me.get("ok"):
        log.error("tg getMe failed: %s", me.get("error") or me.get("description"))
        _BOT_RUNNING = False
        _BOT_LAST_UPDATE["error"] = me.get("error") or me.get("description") or "getMe failed"
        return
    _BOT_INFO = me.get("result", {})
    r = _redis()
    if r:
        try:
            await r.set(KEY_BOT_INFO, json.dumps(_BOT_INFO))
        except Exception:
            pass
    log.info("tg bot online: @%s", _BOT_INFO.get("username", "?"))

    offset  = await _get_offset()
    backoff = 1.0

    while _BOT_RUNNING:
        try:
            cfg_data = await _get_config()  # re-read so live edits take effect
            res = await _tg_api(token, "getUpdates", {
                "offset":          offset,
                "timeout":         POLL_TIMEOUT_S,
                "allowed_updates": ["message", "edited_message"],
            }, timeout=POLL_TIMEOUT_S + 10)

            if not res.get("ok"):
                err = res.get("error") or res.get("description") or "unknown"
                _BOT_LAST_UPDATE["error"] = str(err)[:200]
                log.warning("tg getUpdates: %s", err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, POLL_BACKOFF_MAX_S)
                continue

            backoff = 1.0
            updates = res.get("result", []) or []
            for u in updates:
                offset = max(offset, u.get("update_id", 0) + 1)
                try:
                    await _handle_update(u, cfg_data)
                except Exception as e:
                    log.exception("tg handle_update: %s", e)
            if updates:
                await _set_offset(offset)
            _BOT_LAST_UPDATE = {
                "ts":     now_iso(),
                "offset": offset,
                "count":  len(updates),
                "error":  None,
            }
        except asyncio.CancelledError:
            log.info("tg poll cancelled")
            break
        except Exception as e:
            log.warning("tg poll loop: %s", e)
            _BOT_LAST_UPDATE["error"] = str(e)[:200]
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, POLL_BACKOFF_MAX_S)

    log.info("tg poll stopped")


# ─────────────────────────────────────────────────────────────────────────────
# EVENT BRIDGE — forward selected vera:events to a target chat
# ─────────────────────────────────────────────────────────────────────────────

async def _event_bridge_loop():
    """Subscribe to vera:events pubsub channel and forward matching types."""
    try:
        r = _redis()
        if not r:
            return
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe("vera:events")
        except Exception as e:
            log.warning("tg event bridge subscribe: %s", e)
            return
        log.info("tg event bridge: listening on vera:events")
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
            target = str(ev_cfg.get("target_chat_id", "") or "")
            if not target:
                continue
            wanted = ev_cfg.get("types") or []
            etype = ev.get("type", "")
            if not any(etype == t or etype.startswith(t + ".") for t in wanted):
                continue

            preview = ev.get("text") or ev.get("name") or ev.get("message") or ""
            line = f"[{etype}] {json.dumps({k: v for k, v in ev.items() if k != 'type'}, default=str)[:600]}"
            if preview:
                line = f"[{etype}] {str(preview)[:600]}"
            try:
                await _send_message(target, line)
            except Exception as e:
                log.debug("tg event forward: %s", e)
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.warning("tg event bridge: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _start_bot_internal() -> Dict[str, Any]:
    global _BOT_TASK, _BOT_RUNNING, _EVENT_TASK
    if _BOT_RUNNING and _BOT_TASK and not _BOT_TASK.done():
        return {"running": True, "info": _BOT_INFO, "note": "already running"}
    cfg_data = await _get_config()
    if not cfg_data.get("token"):
        return {"running": False, "error": "no token configured"}
    _BOT_RUNNING = True
    _BOT_TASK = asyncio.create_task(_poll_loop())
    if not _EVENT_TASK or _EVENT_TASK.done():
        _EVENT_TASK = asyncio.create_task(_event_bridge_loop())
    # Brief sleep so getMe has a chance to populate _BOT_INFO before returning
    await asyncio.sleep(0.6)
    return {"running": _BOT_RUNNING, "info": _BOT_INFO,
            "error": _BOT_LAST_UPDATE.get("error")}


async def _stop_bot_internal() -> Dict[str, Any]:
    global _BOT_TASK, _BOT_RUNNING, _EVENT_TASK
    _BOT_RUNNING = False
    if _BOT_TASK and not _BOT_TASK.done():
        _BOT_TASK.cancel()
        try:
            await asyncio.wait_for(_BOT_TASK, timeout=3)
        except Exception:
            pass
    if _EVENT_TASK and not _EVENT_TASK.done():
        _EVENT_TASK.cancel()
        try:
            await asyncio.wait_for(_EVENT_TASK, timeout=3)
        except Exception:
            pass
    _BOT_TASK = None
    _EVENT_TASK = None
    return {"running": False}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — CONFIG
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.config.set", memory="off",
    http_method="POST", http_path="/tg/config", http_tags=["telegram"],
    description="Set Telegram bot config. Any field can be omitted to leave unchanged. "
                "Pass token='' to clear. Restarts the poller if it was running and the "
                "token changed.",
)
async def tg_config_set(
    token:           Optional[str] = None,
    admin_chat_id:   Optional[str] = None,
    default_agent:   Optional[str] = None,
    auto_start:      Optional[bool] = None,
    allow_unknown:   Optional[bool] = None,
    think_default:   Optional[bool] = None,
    max_reply_chars: Optional[int]  = None,
    trace_id=None,
):
    current = await _get_config()
    old_token = current.get("token", "")

    if token           is not None: current["token"]           = token.strip()
    if admin_chat_id   is not None: current["admin_chat_id"]   = str(admin_chat_id).strip()
    if default_agent   is not None: current["default_agent"]   = default_agent.strip() or "assistant"
    if auto_start      is not None: current["auto_start"]      = bool(auto_start)
    if allow_unknown   is not None: current["allow_unknown"]   = bool(allow_unknown)
    if think_default   is not None: current["think_default"]   = bool(think_default)
    if max_reply_chars is not None: current["max_reply_chars"] = max(200, min(int(max_reply_chars), 4096))

    await _save_config(current)

    # If admin chat is set, mark it allowed
    admin = current.get("admin_chat_id", "")
    if admin:
        rec = await _get_chat(admin) or {
            "first_seen": now_iso(), "msg_count": 0,
            "type": "private", "username": "", "first_name": "admin",
        }
        rec["allowed"]   = True
        rec["last_seen"] = rec.get("last_seen") or now_iso()
        await _save_chat(admin, rec)

    restarted = False
    if _BOT_RUNNING and current.get("token") != old_token:
        await _stop_bot_internal()
        await _start_bot_internal()
        restarted = True

    redacted = dict(current)
    if redacted.get("token"):
        t = redacted["token"]
        redacted["token"] = (t[:6] + "…" + t[-4:]) if len(t) > 12 else "***"
    return {"ok": True, "restarted": restarted, "config": redacted}


@capability(
    "tg.config.get", memory="off", silent=True,
    http_method="GET", http_path="/tg/config", http_tags=["telegram"],
    description="Get Telegram bot config (token redacted).",
)
async def tg_config_get(trace_id=None):
    cfg_data = await _get_config()
    redacted = dict(cfg_data)
    if redacted.get("token"):
        t = redacted["token"]
        redacted["token"]      = (t[:6] + "…" + t[-4:]) if len(t) > 12 else "***"
        redacted["token_set"]  = True
    else:
        redacted["token_set"]  = False
    return {"config": redacted}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — BOT LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.bot.start", memory="off",
    http_method="POST", http_path="/tg/start", http_tags=["telegram"],
    description="Start the Telegram polling loop.",
)
async def tg_bot_start(trace_id=None):
    return await _start_bot_internal()


@capability(
    "tg.bot.stop", memory="off",
    http_method="POST", http_path="/tg/stop", http_tags=["telegram"],
    description="Stop the Telegram polling loop.",
)
async def tg_bot_stop(trace_id=None):
    return await _stop_bot_internal()


@capability(
    "tg.bot.status", memory="off", silent=True,
    http_method="GET", http_path="/tg/status", http_tags=["telegram"],
    description="Telegram bot status: running/stopped, bot info, last poll details.",
)
async def tg_bot_status(trace_id=None):
    return {
        "running":     _BOT_RUNNING and bool(_BOT_TASK) and not (_BOT_TASK.done() if _BOT_TASK else True),
        "bot":         _BOT_INFO,
        "last_update": _BOT_LAST_UPDATE,
        "per_chat_agent": _PER_CHAT_AGENT,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — SEND
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.send", memory="off",
    http_method="POST", http_path="/tg/send", http_tags=["telegram"],
    description="Send a plain text message to a Telegram chat.",
)
async def tg_send(chat_id: str, text: str, trace_id=None):
    if not chat_id:
        return {"ok": False, "error": "chat_id required"}
    res = await _send_message(str(chat_id), text or "")
    return {"ok": bool(res.get("ok")), "result": res}


@capability(
    "tg.send_markdown", memory="off",
    http_method="POST", http_path="/tg/send_markdown", http_tags=["telegram"],
    description="Send a MarkdownV2-formatted message. Falls back to plain text on parse error.",
)
async def tg_send_markdown(chat_id: str, text: str, trace_id=None):
    if not chat_id:
        return {"ok": False, "error": "chat_id required"}
    res = await _send_message(str(chat_id), text or "", parse_mode="Markdown")
    return {"ok": bool(res.get("ok")), "result": res}


@capability(
    "tg.notify", memory="off",
    http_method="POST", http_path="/tg/notify", http_tags=["telegram"],
    description="Send a notification to the configured admin chat.",
)
async def tg_notify(text: str, trace_id=None):
    cfg_data = await _get_config()
    admin = cfg_data.get("admin_chat_id", "")
    if not admin:
        return {"ok": False, "error": "no admin_chat_id configured"}
    res = await _send_message(str(admin), text or "")
    return {"ok": bool(res.get("ok")), "result": res}


@capability(
    "tg.broadcast", memory="off",
    http_method="POST", http_path="/tg/broadcast", http_tags=["telegram"],
    description="Send a message to every allow-listed chat.",
)
async def tg_broadcast(text: str, trace_id=None):
    chats = await _list_chats()
    sent = failed = 0
    for c in chats:
        if not c.get("allowed"):
            continue
        res = await _send_message(c["chat_id"], text or "")
        if res.get("ok"): sent += 1
        else:             failed += 1
    return {"ok": True, "sent": sent, "failed": failed}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — CHATS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.chats.list", memory="off", silent=True,
    http_method="GET", http_path="/tg/chats", http_tags=["telegram"],
    description="List known Telegram chats with allow status and last-seen timestamps.",
)
async def tg_chats_list(trace_id=None):
    chats = await _list_chats()
    return {"chats": chats, "count": len(chats),
            "allowed_count": sum(1 for c in chats if c.get("allowed"))}


@capability(
    "tg.chats.allow", memory="off",
    http_method="POST", http_path="/tg/chats/allow", http_tags=["telegram"],
    description="Whitelist a chat so it can talk to the bot.",
)
async def tg_chats_allow(chat_id: str, trace_id=None):
    if not chat_id:
        return {"ok": False, "error": "chat_id required"}
    rec = await _get_chat(str(chat_id)) or {
        "type": "private", "username": "", "first_name": "",
        "last_name": "", "first_seen": now_iso(), "msg_count": 0,
    }
    rec["allowed"]   = True
    rec["last_seen"] = rec.get("last_seen") or now_iso()
    await _save_chat(str(chat_id), rec)
    return {"ok": True, "chat_id": str(chat_id), "allowed": True}


@capability(
    "tg.chats.revoke", memory="off",
    http_method="POST", http_path="/tg/chats/revoke", http_tags=["telegram"],
    description="Remove a chat from the allow-list.",
)
async def tg_chats_revoke(chat_id: str, trace_id=None):
    if not chat_id:
        return {"ok": False, "error": "chat_id required"}
    rec = await _get_chat(str(chat_id))
    if not rec:
        return {"ok": False, "error": "unknown chat"}
    rec["allowed"] = False
    await _save_chat(str(chat_id), rec)
    return {"ok": True, "chat_id": str(chat_id), "allowed": False}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — HISTORY
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.history", memory="off", silent=True,
    http_method="GET", http_path="/tg/history", http_tags=["telegram"],
    description="Recent messages for a Telegram chat (newest first).",
)
async def tg_history(chat_id: str, limit: int = 50, trace_id=None):
    if not chat_id:
        return {"messages": [], "error": "chat_id required"}
    msgs = await _get_history(str(chat_id), limit=int(limit))
    return {"chat_id": str(chat_id), "count": len(msgs), "messages": msgs}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — EVENT BRIDGE
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.events.configure", memory="off",
    http_method="POST", http_path="/tg/events", http_tags=["telegram"],
    description="Configure the event bridge — forwards matching vera:events to a chat. "
                "types is a comma-separated list of event-type prefixes.",
)
async def tg_events_configure(
    enabled:        Optional[bool] = None,
    target_chat_id: Optional[str]  = None,
    types:          Optional[str]  = None,
    trace_id=None,
):
    cur = await _get_events_cfg()
    if enabled        is not None: cur["enabled"]        = bool(enabled)
    if target_chat_id is not None: cur["target_chat_id"] = str(target_chat_id).strip()
    if types          is not None:
        cur["types"] = [t.strip() for t in types.split(",") if t.strip()]
    await _save_events_cfg(cur)
    return {"ok": True, "events": cur}


@capability(
    "tg.events.status", memory="off", silent=True,
    http_method="GET", http_path="/tg/events", http_tags=["telegram"],
    description="Get the current event-bridge configuration.",
)
async def tg_events_status(trace_id=None):
    return {"events": await _get_events_cfg(),
            "running": bool(_EVENT_TASK and not _EVENT_TASK.done())}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — UI PANEL
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "tg.panel.html", memory="off", silent=True,
    http_method="GET", http_path="/telegram/panel", http_tags=["telegram", "ui"],
    description="Serve the Telegram panel HTML.",
)
async def tg_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = (
            "<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef4444;"
            "font-family:monospace;padding:40px'>"
            "<h2>telegram_panel.html not found</h2>"
            f"<p>Expected at: {_PANEL_HTML_PATH}</p>"
            "<p>Place telegram_panel.html alongside telegram_capabilities.py</p>"
            "</body></html>"
        )
    return HTMLResponse(html)

@APP.get("/telegram/panel", include_in_schema=False)
async def _research_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "telegram_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>telegram_panel.html not found</p>")


# ─────────────────────────────────────────────────────────────────────────────
# REGISTER UI PANEL  (mode=tab → top-level harness tab)
# ─────────────────────────────────────────────────────────────────────────────

register_ui(
    "telegram-panel",
    "Telegram",
    "✈",
    """<div id="telegram-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/telegram/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "tg.config.set", "tg.config.get",
        "tg.bot.start", "tg.bot.stop", "tg.bot.status",
        "tg.send", "tg.send_markdown", "tg.notify", "tg.broadcast",
        "tg.chats.list", "tg.chats.allow", "tg.chats.revoke",
        "tg.history",
        "tg.events.configure", "tg.events.status",
    ],
    mode="tab",
    tab_order=75,
)


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP — auto-resume bot if config has auto_start=True
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    # Wait briefly for Redis to be available before reading config
    for _ in range(20):
        if _redis() is not None:
            break
        await asyncio.sleep(0.5)
    try:
        cfg_data = await _get_config()
        if cfg_data.get("auto_start") and cfg_data.get("token"):
            log.info("tg auto-start enabled — launching poll loop")
            await _start_bot_internal()
        else:
            log.info("tg ready — auto_start=%s, token_set=%s",
                     cfg_data.get("auto_start"), bool(cfg_data.get("token")))
    except Exception as e:
        log.warning("tg startup: %s", e)


schedule(_startup, interval=999999, name="telegram_startup")