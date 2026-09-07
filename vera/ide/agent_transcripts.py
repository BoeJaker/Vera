"""Two coding agents, two transcript formats, one session list.

The Dispatch panel ingests Claude Code's own `~/.claude/projects/**/**.jsonl`
transcripts. Codex writes its own, in a different shape, in a different place,
and nothing read them — so the Swarm and Sessions views showed only half the
estate even though codex agents hold sandboxes in it (`owner: codex` appears in
evolve.sandbox.list).

Both formats are JSONL, one object per line, and both carry the same three
things a session list needs: who spoke, when, and what they said. The rest is
per-agent packaging. This module is the packaging.

FORMATS — OBSERVED, NOT ASSUMED (2026-09-07, real files on the host)
--------------------------------------------------------------------
Claude Code — `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`
    {"type": "user"|"assistant", "sessionId": …, "cwd": …, "timestamp": …,
     "message": {"role": …, "content": … }}

Codex — `~/.codex/sessions/YYYY/MM/DD/rollout-<iso-ts>-<uuid>.jsonl`
    {"timestamp": …, "type": …, "payload": {…}}
    where type ∈ session_meta | event_msg | response_item | turn_context |
                 world_state | compacted
      session_meta.payload → {id, session_id, cwd, originator, cli_version,
                              model_provider, git, …}
      event_msg.payload.type ∈ user_message | agent_message | task_started |
                              task_complete | token_count | …
      response_item.payload.type ∈ message | reasoning | function_call |
                              function_call_output | custom_tool_call | …

The conversation is taken from `event_msg` (user_message / agent_message)
rather than `response_item.message`. Both carry the exchange, but response_item
also carries the `developer` role — the injected skills/system preamble, which
in the observed file was the FIRST message role and would have become every
codex session's title. event_msg is what a person said and what the agent said
back.

Pure: lines in, turns out. No filesystem, no database.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional

#: Agent kinds this module can read. The value is what lands on each turn, so a
#: session list can say which agent produced it.
CLAUDE = "claude"
CODEX = "codex"

#: rollout-<iso timestamp>-<uuid>.jsonl
_ROLLOUT_RE = re.compile(
    r"^rollout-(?P<ts>\d{4}-\d{2}-\d{2}T[\d-]+)-(?P<uuid>[0-9a-fA-F-]{36})\.jsonl$")

#: Codex event_msg payload types that ARE the conversation.
_CODEX_SPEECH = {"user_message": "user", "agent_message": "assistant"}


def detect_kind(path: str) -> str:
    """Which agent wrote this transcript, from its path alone.

    Cheap enough to run over a whole scan, and it never opens the file — a
    75MB rollout should not be read just to find out whose it is.
    """
    name = os.path.basename(str(path or ""))
    if _ROLLOUT_RE.match(name):
        return CODEX
    norm = str(path or "").replace("\\", "/")
    if "/.codex/" in norm:
        return CODEX
    return CLAUDE


def session_id_from_path(path: str) -> str:
    """The session uuid a filename encodes, or "" if it encodes none.

    Codex names the file after the session; Claude names it exactly the session
    id. Used only as a fallback — the file's own metadata wins where present,
    because a renamed or copied file must not invent a new session.
    """
    name = os.path.basename(str(path or ""))
    m = _ROLLOUT_RE.match(name)
    if m:
        return m.group("uuid")
    stem = name[:-6] if name.endswith(".jsonl") else name
    return stem if re.fullmatch(r"[0-9a-fA-F-]{36}", stem) else ""


def _text_of(content: Any) -> str:
    """Flatten a message body to text, whatever shape it arrived in."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and c.get("text"):
                parts.append(str(c["text"]))
        return "\n".join(parts)
    return ""


def parse_codex_line(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One codex rollout line → a turn, session metadata, or None.

    Returns {"kind": "meta"|"turn", ...}. Anything that is neither — reasoning,
    token counts, tool traffic, world state — is None, which the caller skips.
    """
    if not isinstance(obj, dict):
        return None
    typ = str(obj.get("type") or "")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    ts = str(obj.get("timestamp") or payload.get("timestamp") or "")
    if typ == "session_meta":
        return {"kind": "meta",
                "session_id": str(payload.get("id") or payload.get("session_id") or ""),
                "cwd": str(payload.get("cwd") or ""),
                "originator": str(payload.get("originator") or ""),
                "cli_version": str(payload.get("cli_version") or ""),
                "ts": ts}
    if typ == "event_msg":
        role = _CODEX_SPEECH.get(str(payload.get("type") or ""))
        if not role:
            return None
        text = _text_of(payload.get("message") or payload.get("text")
                        or payload.get("content"))
        if not text.strip():
            return None
        return {"kind": "turn", "role": role, "text": text, "ts": ts}
    return None


def parse_claude_line(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One Claude Code transcript line → a turn or session metadata."""
    if not isinstance(obj, dict):
        return None
    role = str(obj.get("type") or "")
    if role not in ("user", "assistant"):
        return None
    msg = obj.get("message")
    text = _text_of(msg.get("content") if isinstance(msg, dict) else msg)
    if not text.strip():
        return None
    return {"kind": "turn", "role": role, "text": text,
            "ts": str(obj.get("timestamp") or ""),
            "session_id": str(obj.get("sessionId") or ""),
            "cwd": str(obj.get("cwd") or "")}


def read_transcript(lines: Iterable[str], *, kind: str = CLAUDE,
                    path: str = "", max_turns: int = 0) -> Dict[str, Any]:
    """Turn a transcript's lines into {agent, session_id, cwd, turns:[…]}.

    A malformed line is skipped, never fatal: these files are appended to by a
    live agent and can be read mid-write.
    """
    parse = parse_codex_line if kind == CODEX else parse_claude_line
    out: Dict[str, Any] = {"agent": kind, "session_id": session_id_from_path(path),
                           "cwd": "", "originator": "", "turns": []}
    for raw in lines or []:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        rec = parse(obj)
        if rec is None:
            continue
        if rec["kind"] == "meta":
            # Metadata wins over the filename, but never overwrites a value it
            # does not have.
            for k in ("session_id", "cwd", "originator"):
                if rec.get(k):
                    out[k] = rec[k]
            continue
        if rec.get("session_id") and not out["session_id"]:
            out["session_id"] = rec["session_id"]
        if rec.get("cwd") and not out["cwd"]:
            out["cwd"] = rec["cwd"]
        out["turns"].append({"role": rec["role"], "text": rec["text"],
                             "ts": rec["ts"]})
        if max_turns and len(out["turns"]) >= max_turns:
            break
    return out
