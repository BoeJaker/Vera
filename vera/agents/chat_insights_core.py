"""Chat insights - an optional second look at a finished chat turn by the
long-horizon CPU model (user, 2026-09-27).

The reply is the GPU's job and nothing waits on this (compute-roles: cpu-247 =
long, higher-quality generation that is not needed immediately). After a reply
has streamed, and only when the chat's Insights toggle is on, the long-horizon
model reads the exchange and returns what a thoughtful colleague would add:
connections and implications the reply did not draw, caveats worth checking,
and follow-up questions. The chat shows them in a card under the reply; they
are NOT part of the conversation the chat model is sent next turn.

Pure: prompt building and parsing only. agents.py runs it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Sequence

JOB_TYPE = "chat_enrich"
MAX_ITEMS = 4
MAX_ITEM_CHARS = 280
MESSAGE_CHARS = 2500
REPLY_CHARS = 5000
HISTORY_TURNS = 4
HISTORY_TURN_CHARS = 400
#: A reply this short is an acknowledgement, not something to enrich.
MIN_REPLY_CHARS = 200
KINDS = ("insights", "caveats", "follow_ups")

SYSTEM = (
    "You are the reflective second reader beside a chat assistant. The assistant "
    "has ALREADY answered; you are not answering again. Read the exchange and add "
    "only what is worth the user's attention that the reply did not already say:\n"
    "- insights: a connection, implication or consequence the reply did not draw;\n"
    "- caveats: a claim in the reply that is doubtful, outdated or needs checking, "
    "and why;\n"
    "- follow_ups: questions the user would plausibly want to ask next, written as "
    "the user would ask them.\n"
    "Each item is one or two plain sentences, specific to THIS exchange. Never "
    "restate the reply. An empty list is the right answer when there is nothing "
    "worth adding. Respond ONLY with JSON: "
    '{"insights":[...],"caveats":[...],"follow_ups":[...]}'
)


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def wanted(enabled: bool, reply: str, use_tts: bool = False) -> bool:
    return bool(enabled) and len((reply or "").strip()) >= MIN_REPLY_CHARS and not use_tts


def build_prompt(message: str, reply: str, history: Sequence[Dict[str, Any]] = ()) -> str:
    turns: List[str] = []
    for h in list(history or [])[-HISTORY_TURNS:]:
        if not isinstance(h, dict):
            continue
        role = str(h.get("role") or "").strip() or "user"
        turns.append("%s: %s" % (role, _clip(h.get("content"), HISTORY_TURN_CHARS)))
    parts = []
    if turns:
        parts.append("# Earlier in the conversation\n" + "\n".join(turns))
    parts.append("# The user asked\n" + str(message or "")[:MESSAGE_CHARS])
    parts.append("# The assistant replied\n" + str(reply or "")[:REPLY_CHARS])
    parts.append("Now the JSON.")
    return "\n\n".join(parts)


def _loads(raw: str) -> Any:
    # A thinking model may still open with its reasoning; braces inside it
    # must not be mistaken for the answer.
    s = re.sub(r"<think>.*?(</think>|$)", "", str(raw or ""), flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except ValueError:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                return None
    return None


def parse(raw: str) -> Dict[str, List[str]]:
    """The three lists, clipped and de-duplicated; empty lists on anything
    unreadable (an insight pass that failed shows nothing, never an error)."""
    obj = _loads(raw)
    out: Dict[str, List[str]] = {k: [] for k in KINDS}
    if not isinstance(obj, dict):
        return out
    for k in KINDS:
        v = obj.get(k)
        items = v if isinstance(v, list) else ([v] if isinstance(v, str) else [])
        seen = set()
        for it in items:
            if isinstance(it, dict):
                it = it.get("text") or it.get("question") or it.get("insight") or ""
            t = _clip(it, MAX_ITEM_CHARS)
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out[k].append(t)
            if len(out[k]) >= MAX_ITEMS:
                break
    return out


def is_empty(parsed: Dict[str, List[str]]) -> bool:
    return not any(parsed.get(k) for k in KINDS)
