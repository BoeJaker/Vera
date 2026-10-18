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

import logging
import sys
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

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


def _build_print(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """print.notify takes title + body: the generic {"text"} was dropped by its wrapper, so the printer channel printed
    an empty note with a timestamp (S24 / D02-2)."""
    return {"title": _header(ctx)[:60], "body": rendered or ""}


def _cap_params(cap: str) -> Optional[set]:
    """The argument names a registered cap takes (its schema's properties); None when the registry or cap is not
    loaded here (the pure-module tests), so the caller keeps its default."""
    for mod_name in ("Vera.vera.capability_orchestration", "capability_orchestration", "vera.capability_orchestration"):
        reg = getattr(sys.modules.get(mod_name), "CAPABILITY_REGISTRY", None)
        if isinstance(reg, dict) and cap in reg:
            props = ((reg[cap] or {}).get("schema") or {}).get("properties")
            return set(props) if isinstance(props, dict) and props else None
    return None


# The names a cap's text argument goes by, most common first.
_TEXT_ARGS = ("text", "body", "content", "message", "report", "markdown", "md")


def _build_generic(rendered: str, ctx: Dict[str, Any], cap: str = "") -> Dict[str, Any]:
    """Builder for skill channels over a cap with no builder: the text goes to the argument the cap really takes
    (its schema), so a cap without a `text` argument no longer loses the content silently."""
    params = _cap_params(cap) if cap else None
    key = "text"
    if params is not None and "text" not in params:
        key = next((k for k in _TEXT_ARGS if k in params), "text")
        if key == "text":
            log.warning("delivery: cap %s takes none of %s - the content would be dropped", cap, ", ".join(_TEXT_ARGS))
    args: Dict[str, Any] = {key: rendered}
    if params is not None and "title" in params:
        args["title"] = _header(ctx)
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


def _build_html(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a rendered report into a rich, self-contained HTML document via
    report.html — the report text becomes the document content and the header its
    title. Served inline at /render/report/… and pinnable to the gallery."""
    label = ctx.get("label") or ctx.get("name") or "Report"
    args: Dict[str, Any] = {
        "content": rendered or "",
        "title": (label or "Report")[:140],
        "mode": ctx.get("html_mode") or "auto",
        "illustrate": int(ctx.get("illustrate") or 0),
    }
    if ctx.get("target"):
        args["session_id"] = ctx["target"]
    return args


def _canvas_where(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The canvas a delivery lands on: a canvas id (cv_…) names one; anything else is a chat session, whose own
    session canvas is resolved (and made) on demand."""
    tgt = str(ctx.get("target") or "").strip()
    if tgt.startswith("cv_"):
        return {"id": tgt}
    return {"session_id": tgt} if tgt else {}


def _build_canvas(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The answer as a reading item on the session canvas (canvas.add - the resolver, so the same delivery twice
    is brought back, not doubled)."""
    label = ctx.get("label") or ctx.get("name") or "Delivered"
    args: Dict[str, Any] = {"kind": "markdown", "content": {"md": rendered or "", "title": str(label)[:140]},
                            "size": "l", "at": "now"}
    if ctx.get("key"):
        args["key"] = str(ctx["key"])
    args.update(_canvas_where(ctx))
    return args


def _build_canvas_report(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """A rich HTML report (report.html) placed on the session canvas instead of opened in a tab (report.canvas)."""
    args = _build_html(rendered, {**ctx, "target": ""})
    args.update(_canvas_where(ctx))
    return args


def _build_store(rendered: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The rich HTML report (report.html), kept in the estate's object store (report.store) as well as served."""
    args = _build_html(rendered, {**ctx, "target": ""})
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
    "report.html":      _build_html,
    "canvas.add":       _build_canvas,
    "report.canvas":    _build_canvas_report,
    "report.store":     _build_store,
    "print.notify":     _build_print,
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
    "html": {
        # Rich, self-contained HTML document (report.html): themed layout,
        # Mermaid diagrams, inline-SVG infographics, optional imagine images.
        # Verbatim default_format — report.html shapes the content itself.
        "label": "HTML report", "cap": "report.html", "default_format": "",
        "needs_target": False, "target_field": "", "target_label": "",
    },
    # THE CANVAS IS A DELIVERY VECTOR (owner, 2026-10-02: "id like for html delivery to be able to go to the canvas
    # instead of automatically opening a new tab"). The session canvas beside the chat: the answer as a reading item,
    # or the rich HTML report drawn there in a sandboxed frame. Target = the chat session (or a cv_ canvas id).
    "canvas": {
        "label": "Canvas", "cap": "canvas.add", "default_format": "",
        "needs_target": True, "target_field": "session_id",
        "target_label": "Chat session id or canvas id (blank = calling session)",
    },
    "canvas_html": {
        "label": "HTML report on the canvas", "cap": "report.canvas", "default_format": "",
        "needs_target": True, "target_field": "session_id",
        "target_label": "Chat session id or canvas id (blank = calling session)",
    },
    # THE ESTATE'S STORAGE IS A DELIVERY VECTOR (owner, 2026-10-03: "can the vera estate cloud storage be a delivery
    # mechanism so we could save reports there as an output option"): the rich HTML report and its source, kept in the
    # estate's object store (the fabric's bucket, under reports/<year-month>/) with a link to fetch it.
    "storage": {
        "label": "Estate storage", "cap": "report.store", "default_format": "",
        "needs_target": False, "target_field": "session_id",
        "target_label": "Chat session id (blank = calling session)",
    },
}

# What each channel is FOR, in one line - the menu a model reads when it may pick where its answer goes
# (describe_for_model). A channel with no line here (a skill's) is offered by its label.
CHANNEL_USE: Dict[str, str] = {
    "canvas":      "a long or structured answer the user will keep open beside the chat (lists, plans, notes)",
    "canvas_html": "a rich visual document - sections, diagrams, infographics - shown on the canvas",
    "html":        "a rich standalone document the user may open in its own tab or keep in the gallery",
    "storage":     "a report the user wants kept as a file in the estate's storage",
    "podcast":     "something meant to be listened to - a briefing, a story, a long explanation",
    "email":       "something the user asked to receive by email",
    "telegram":    "a short note the user should get on their phone",
    "notebook":    "working notes or a write-up to keep in the notebook",
    "memory":      "a fact or decision worth remembering for later turns",
    "chat":        "another chat session the user named",
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
    fmt = (default_format or "").strip()
    if fmt:
        try:
            from . import output_formats as _of
            if _of.get_profile(fmt) is None:
                # apply_format ignores an unknown id silently (the printer registered "text"; the profile is "plain")
                log.warning("delivery: channel %s names unknown format %r - its answers go out unshaped", cid, fmt)
        except Exception:                          # pragma: no cover - import guard
            pass
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
    cap = ch.get("cap", "")
    builder = _BUILDERS.get(cap)
    if builder is None:
        return _build_generic(rendered or "", ctx or {}, cap=cap)
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


# The channels the chat itself never offers a model: it IS the chat, and the fabric gets everything anyway.
_NOT_FOR_MODEL = {"chat", "fabric"}


def describe_for_model(available: Optional[List[str]] = None,
                       formats: Optional[List[Dict[str, Any]]] = None) -> str:
    """The delivery menu as a model reads it: every channel it may send its finished answer through, what each is
    for, the styles it may shape it into, and the one marker that asks for it. `available` limits the channels
    (the caller knows which caps are loaded); `formats` is output_formats' list (id + label)."""
    chans = [c for c in list_channels()
             if c["id"] not in _NOT_FOR_MODEL and (available is None or c["id"] in available)]
    if not chans:
        return ""
    lines = ["## Delivery",
             "Your answer is shown in the chat. When the user would be better served by ALSO receiving it another "
             "way, end your answer with ONE marker on its own line:",
             "[[deliver:<channel>]]  or  [[deliver:<channel>|<style>]]",
             "Leave it out when the chat is enough - most answers. Never deliver the same answer twice.",
             "", "Channels:"]
    for c in chans:
        lines.append(f"- {c['id']} - {c['label']}: {CHANNEL_USE.get(c['id'], c['label'])}")
    fl = [f for f in (formats or []) if isinstance(f, dict) and f.get("id")]
    if fl:
        lines += ["", "Styles (optional): " + ", ".join(f"{f['id']} ({f.get('label') or f['id']})" for f in fl[:16])]
    return "\n".join(lines)


# What the request itself says about where the answer should go. A small model following a menu got the plainest
# asks wrong (measured 2026-10-02, qwen3.5:2b: "keep it somewhere I can see" -> none, "so I can listen to it on my
# drive" -> none), so an explicit ask is read here and decides; the model is asked only when the request says nothing.
# Most specific first: "email me the report" is an email, not a report.
# Every pattern needs the ask itself - a delivery verb or "so I can ..." - never the bare noun: "how do I set up email
# on my phone" is a question about email, "my audio driver is broken" is not a podcast, "the HTML canvas element" is
# not the canvas (S24 / D02-3).
_OBJ = r"(it|this|that|them|me|the \w+|a \w+)"
_ASKS = [
    ("email",       rf"\be-?mail (me|it|this|that|them|the \w+)\b|\bsend me an e-?mail\b"
                    rf"|\b(send|forward|mail) {_OBJ}( \w+)?( to me)? (by|via|over|to my|in an) (e-?mail|inbox)\b"),
    ("telegram",    rf"\b(send|text|ping|push|message) {_OBJ}( \w+)? (to|on) my phone\b|\btext me\b|\bping me\b"
                    rf"|\b(send|text|ping|push|message) {_OBJ}( \w+)? (on|over|via|by|through) telegram\b|\btelegram me\b"),
    ("podcast",     r"\b(so (that )?i can|let me|i want to|i'?d like to) (listen|hear)\b|\b(as|into|make it) a podcast\b"
                    r"|\bas audio\b|\b(read|say) (it|this|that) (out loud|aloud|to me)\b"
                    r"|\bread (it|this|me)\b.*\b(later|car|drive)\b"),
    ("notebook",    r"\b(save|put|add|write|keep|copy|log|jot)\b[^.?!]{0,30}\b(to|in|into) (my |the )?(notebook|notes)\b"),
    ("memory",      r"(^|[.!?]\s*|,\s*|\bplease\s+|\band\s+)remember (this|that|it)\b|\bdon'?t forget (this|that|it)\b"
                    r"|\bkeep (this|that|it) in mind\b"),
    ("canvas_html", r"\b(as a|proper|full) (document|doc|report)\b|\bwrite[- ]?up\b|\bwith diagrams\b|\bhtml report\b"),
    ("canvas",      rf"\b(put|place|pin|show|keep|add|drop|send) {_OBJ}( \w+)? (on|to|onto|in) (the |my )?canvas\b"
                    r"|\b(beside|next to) the chat\b|\bkeep (it|this) (open|somewhere|handy|visible|around)\b"
                    r"|\bwhere i can see\b|\bpin (it|this)\b"),
]


# ...and the request that asks to keep it short asks for the chat alone (measured 2026-10-02: "how does TLS work,
# briefly" went to an HTML report). A named place still wins: "briefly, email me" is an email.
_STAY = r"\bbrief(ly)?\b|\bquick(ly)?\b|\bin short\b|\btl;?dr\b|\bone line\b|\bin a sentence\b|\bjust tell me\b"


def explicit_ask(request: str, allowed: List[str]) -> str:
    """The channel the request asks for in so many words ('' when it asks for none, or for one not offered;
    'none' when it asks to keep the answer short - the chat alone)."""
    import re as _re
    q = str(request or "").lower()
    for cid, pat in _ASKS:
        if cid in allowed and _re.search(pat, q):
            return cid
    return "none" if _re.search(_STAY, q) else ""


#: An answer shorter than this stays in the chat without asking a model: nothing that short is worth a second place.
SHORT_ANSWER = 400

# Worked examples for the model - phrased unlike any rule above, so they teach the judgement, not the keywords.
_PICK_EXAMPLES = [
    ("a step-by-step plan for moving house, with a checklist per week", "canvas"),
    ("an in-depth comparison of three databases, sections and tables", "canvas_html"),
    ("the capital of Peru", "none"),
    ("a quick fix for the failing import", "none"),
]


def pick_prompt(content: str, request: str, channels: List[Dict[str, Any]],
                styles: Optional[List[Dict[str, Any]]] = None) -> str:
    """The question a small model answers to choose where a finished answer ALSO goes (output.deliver_auto) - asked
    only when the request names no place (explicit_ask) and the answer is not short."""
    lines = ["Choose whether a chat answer should ALSO be delivered another way. A long plan or reference worth keeping "
             "open goes to canvas, a rich document to canvas_html; most answers stay in the chat (none).",
             "", "Ways:"]
    for c in channels:
        lines.append(f"- {c['id']}: {CHANNEL_USE.get(c['id'], c.get('label') or c['id'])}")
    lines += ["", "Examples (what the answer is -> channel):"]
    for what, ch in _PICK_EXAMPLES:
        lines.append(f"- {what} -> {ch}")
    if styles:
        lines += ["", "Styles (optional): "
                  + ", ".join(str(s.get("id")) for s in styles if isinstance(s, dict) and s.get("id"))]
    lines += ["", "THE REQUEST: " + (request or "(not given)")[:1200], "",
              "THE ANSWER (start):", (content or "")[:2000], "",
              'Reply with ONE line of JSON and nothing else: {"channel": "<one id above, or none>", "style": "<id or empty>", "why": "<few words>"}']
    return "\n".join(lines)

def parse_pick(text: str, allowed: List[str], styles: Optional[List[str]] = None) -> Dict[str, str]:
    """Read a model's delivery pick. Anything that is not one of the allowed channels is 'none' - a pick never
    invents a channel or a style; a reply that is not JSON is read for a bare channel id as a last resort."""
    import json as _json
    import re as _re
    raw = str(text or "").strip()
    got: Dict[str, Any] = {}
    m = _re.search(r"\{.*\}", raw, _re.S)
    if m:
        try:
            got = _json.loads(m.group(0))
        except Exception:
            got = {}
    ch = str((got or {}).get("channel") or "").strip().lower()
    if not got:
        ch = next((a for a in allowed if _re.search(r"\b" + _re.escape(a) + r"\b", raw.lower())), "")
    if ch not in allowed:
        ch = "none"
    st = str((got or {}).get("style") or "").strip().lower()
    if styles is not None and st not in styles:
        st = ""
    return {"channel": ch, "style": st if ch != "none" else "", "why": str((got or {}).get("why") or "")[:200]}
