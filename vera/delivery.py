"""
delivery.py — Vera shared delivery-channel registry
===================================================

The routing twin of :mod:`vera.output_formats`. Where ``output_formats`` answers
*"how should the answer be shaped"*, this module answers *"where should the
finished answer be sent"*. Both are pluggable palettes fed by the Skills library:

  * output_formats.FORMAT_PROFILES   — length/voice/structure (apply_format)
  * delivery.DELIVERY_CHANNELS       — telegram / memory / notebook / email / chat

Historically the dream ``deliver`` stage carried a hard-coded ``if`` ladder over
``telegram | memory | notebook`` and could never grow an email or chat sink
without editing the stage. This module lifts the channel set out into a registry
so ANY caller (today: the dream deliver stage; tomorrow: chat, the agent runner)
can route a finished report to a named channel — and so the Skills library can
contribute new channel presets (``delivery_channel`` skills) the same way it
contributes ``output_format`` profiles.

Design mirrors output_formats.py deliberately:

  * Nothing here imports the orchestrator, so it is safe to import from anywhere
    (dream_capabilities, chat, skills) without circular deps.
  * The registry holds **descriptors + pure argument builders**, never live
    capability handles. The actual send is performed by the caller, which owns
    CAPABILITY_REGISTRY: it looks up the channel, renders the report through the
    channel's format profile (apply_format), then calls ``build_args`` to turn
    the rendered text + a context dict into kwargs for the channel's ``cap``.

Each channel descriptor:
    label              human label for pickers
    cap                capability name the caller invokes to send (mail.send, …)
    default_format     output_formats profile id the report is shaped through
                       before sending ('' = send the report verbatim)
    needs_target       True when the channel needs a per-use address
    target_field       the cap kwarg the resolved target maps to (to / session_id)
    target_label       human hint for the UI target input
    fixed_target       a preset target baked into the channel (skill presets)
    always_on          True for implicit sinks that run regardless of selection
    source             'builtin' | 'skill'
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# ARGUMENT BUILDERS — pure (rendered_report, ctx) -> cap kwargs.
#
# Keyed by capability name, NOT by channel id, so a skill-authored channel that
# targets a known cap (e.g. a "telegram-admin" preset over tg.notify) reuses the
# right builder automatically. Unknown caps fall back to _build_generic.
#
# ctx keys the builders may read (all optional except where noted):
#   trigger      the dream trigger dict (label/name live here)
#   label,name   convenience copies of trigger label/name
#   themes       list[str] tags/themes for the cycle
#   cycle_id     id used for memory session grouping
#   target       the resolved per-use target (email address / chat session id)
#   notebook_md  pre-assembled notebook body (report + journal) when available
# ─────────────────────────────────────────────────────────────────────────────


def _header(ctx: Dict[str, Any]) -> str:
    label = ctx.get("label") or ctx.get("name") or "cycle"
    return f"Dream: {label}"


def _build_telegram(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {"text": f"{_header(ctx)}\n\n{rendered[:3500]}"}


def _build_memory(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    name = ctx.get("name") or "cycle"
    tag_list = ["dream", name] + list(ctx.get("themes", []) or [])[:5]
    return {
        "text":        rendered[:4000],
        "category":    "dream",
        # memory.store parses tags via tags.split(",") — comma-separated string.
        "tags":        ",".join(str(t) for t in tag_list if t),
        "record_type": "dream_report",
        "session_id":  f"dream:{name}",
        "importance":  0.4,
    }


def _build_notebook(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    label = ctx.get("label") or ctx.get("name") or "cycle"
    return {
        "title":   f"Dream — {label}",
        # notebook_md carries report + journal when the caller assembled it.
        "content": ctx.get("notebook_md") or rendered,
        "tags":    ["dream"] + list(ctx.get("themes", []) or [])[:5],
    }


def _build_email(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    # First non-empty line makes a sensible subject; fall back to the header.
    first = next((ln.strip().lstrip("# ").strip()
                  for ln in rendered.splitlines() if ln.strip()), "")
    subject = (first[:120] or _header(ctx))
    args: Dict[str, Any] = {"subject": subject, "body": rendered}
    if ctx.get("target"):
        args["to"] = ctx["target"]
    return args


def _build_chat(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "title":  _header(ctx),
        "report": rendered,
    }
    if ctx.get("target"):
        args["session_id"] = ctx["target"]
    return args


def _build_generic(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort builder for skill channels over an unrecognised cap."""
    args: Dict[str, Any] = {"text": rendered}
    if ctx.get("target"):
        args["target"] = ctx["target"]
    return args


def _build_podcast(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a rendered report into a podcast episode: the report text becomes the
    single source and its first line the topic/title. Non-blocking (wait=False)
    so a long-running loop isn't held while TTS renders — the episode surfaces
    via podcast.progress events + the library when done."""
    header = _header(ctx)
    topic = (header or (rendered or "").strip().splitlines()[0] if rendered else "") or "Vera episode"
    args: Dict[str, Any] = {
        "topic": topic[:200],
        "sources": [{"type": "text", "text": rendered or "", "label": header or "report"}],
        "instructions": "Turn this material into a natural spoken episode; do not "
                        "invent facts beyond it.",
        "wait": False,
    }
    if ctx.get("target"):
        args["session_id"] = ctx["target"]
    return args


_BUILDERS: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {
    "tg.notify":        _build_telegram,
    "memory.store":     _build_memory,
    "notebook.create":  _build_notebook,
    "notebook.append":  _build_notebook,
    "mail.send":        _build_email,
    "chat.deliver":     _build_chat,
    "podcast.generate": _build_podcast,
}


# ─────────────────────────────────────────────────────────────────────────────
# BASELINE CHANNELS — the immutable in-code palette. Never mutated; skill
# channels overlay via _DYNAMIC_CHANNELS. Shapes/behaviour preserve exactly what
# the old dream deliver if-ladder did for telegram/memory/notebook.
# ─────────────────────────────────────────────────────────────────────────────

DELIVERY_CHANNELS: Dict[str, Dict[str, Any]] = {
    "telegram": {
        # Default verbatim (the report is already markdown) so existing telegram
        # triggers keep their exact output; pick 'short' in the UI to reshape.
        "label": "Telegram", "cap": "tg.notify", "default_format": "",
        "needs_target": False, "target_field": "", "target_label": "",
    },
    "memory": {
        "label": "Memory", "cap": "memory.store", "default_format": "",
        "needs_target": False, "target_field": "", "target_label": "",
    },
    "notebook": {
        "label": "Notebook", "cap": "notebook.create", "default_format": "markdown",
        "needs_target": False, "target_field": "", "target_label": "",
    },
    "email": {
        "label": "Email", "cap": "mail.send", "default_format": "email",
        "needs_target": True, "target_field": "to",
        "target_label": "Recipient email (blank = default account)",
    },
    "chat": {
        "label": "Chat", "cap": "chat.deliver", "default_format": "standard",
        "needs_target": True, "target_field": "session_id",
        "target_label": "Chat session id (blank = calling session)",
    },
    "podcast": {
        # 'audio' format shapes the text for the ear before it's voiced.
        "label": "Podcast", "cap": "podcast.generate", "default_format": "audio",
        "needs_target": False, "target_field": "", "target_label": "",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC CHANNELS — runtime contributions from the Skills library, exactly as
# output_formats._DYNAMIC_PROFILES. A `delivery_channel` skill registers here via
# register_channel(); a dynamic channel shadows a baseline of the same id.
# ─────────────────────────────────────────────────────────────────────────────

_DYNAMIC_CHANNELS: Dict[str, Dict[str, Any]] = {}


def _merged_channels() -> Dict[str, Dict[str, Any]]:
    return {**DELIVERY_CHANNELS, **_DYNAMIC_CHANNELS}


def _norm(rec: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    return {
        "label":          (rec.get("label") or "").strip() or rec.get("cap", "channel"),
        "cap":            (rec.get("cap") or "").strip(),
        "default_format": (rec.get("default_format") or "").strip(),
        "needs_target":   bool(rec.get("needs_target")),
        "target_field":   (rec.get("target_field") or "").strip(),
        "target_label":   (rec.get("target_label") or "").strip(),
        "fixed_target":   (rec.get("fixed_target") or "").strip(),
        "always_on":      bool(rec.get("always_on")),
        "source":         source,
    }


def register_channel(channel_id: str, *, label: str, cap: str,
                     default_format: str = "", needs_target: bool = False,
                     target_field: str = "", target_label: str = "",
                     fixed_target: str = "", source: str = "skill") -> None:
    """Add or replace a runtime delivery channel (used by the skills bridge).

    Re-registering the same id overwrites; a dynamic channel shadows a baseline
    channel of the same id in every public view. ``fixed_target`` lets a skill
    bake in a preset address (e.g. an "email-to-me" channel over mail.send).
    """
    cid = (channel_id or "").strip()
    if not cid:
        return
    _DYNAMIC_CHANNELS[cid] = _norm({
        "label": label, "cap": cap, "default_format": default_format,
        "needs_target": needs_target, "target_field": target_field,
        "target_label": target_label, "fixed_target": fixed_target,
    }, source=source)


def unregister_channel(channel_id: str) -> None:
    """Remove a previously registered runtime channel (no-op if absent)."""
    _DYNAMIC_CHANNELS.pop((channel_id or "").strip(), None)


def get_channel(channel_id: str) -> Optional[Dict[str, Any]]:
    """Look up one channel (dynamic first, then baseline). None if unknown."""
    cid = (channel_id or "").strip()
    rec = _merged_channels().get(cid)
    if rec is None:
        return None
    # Baseline records lack the 'source' flag — normalise on read.
    return _norm(rec, source=rec.get("source", "builtin"))


def build_args(channel_id: str, rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a rendered report + context into kwargs for the channel's cap.

    Picks the argument builder by the channel's cap name so skill channels over a
    known cap reuse the right builder; unknown caps fall back to _build_generic.
    """
    ch = get_channel(channel_id)
    if not ch:
        return {}
    builder = _BUILDERS.get(ch.get("cap", ""), _build_generic)
    return builder(rendered or "", ctx or {})


def list_channels() -> List[Dict[str, Any]]:
    """Public, picker-friendly view (no behaviour-bearing callables).

    Includes both the in-code baseline and any skill-contributed channels, with a
    `source` flag ('builtin' | 'skill') so pickers can group/badge them.
    """
    out: List[Dict[str, Any]] = []
    for cid, rec in _merged_channels().items():
        n = _norm(rec, source=rec.get("source", "builtin"))
        out.append({
            "id":             cid,
            "label":          n["label"],
            "cap":            n["cap"],
            "default_format": n["default_format"],
            "needs_target":   n["needs_target"],
            "target_field":   n["target_field"],
            "target_label":   n["target_label"],
            "fixed_target":   n["fixed_target"],
            "always_on":      n["always_on"],
            "source":         n["source"],
        })
    return out
