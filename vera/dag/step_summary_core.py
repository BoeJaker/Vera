"""A step summary the next step can read: a sentence, never a raw tool result.

Why (census run70-73, 24 Sep 2026): 65 of 130 step summaries began with `{`
or a code fence. They come from the paths that END a step for the executor -
the duplicate ceiling, the same-tool ceiling, the repeat-failure and
missing-path guards, and the final "no `done` was emitted" fallback - which
all summarise with the LAST TOOL PREVIEW verbatim, i.e. 300 characters of a
result's JSON. That is what the verifier, the assessor and the next step then
read. A sentence built from the fields that matter (what ran, rc, the first
stdout/stderr line; what was written, its size, whether it parsed) carries the
same facts and is readable.

Pure: strings in, a string out. Never raises (a summary must never take a step
down); anything it cannot parse is returned as its first line.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*")
_SCALAR_RE = re.compile(r'"(\w+)":\s*(true|false|null|-?\d+(?:\.\d+)?|"((?:[^"\\]|\\.)*)")')


def _first_line(s: Any, n: int = 200) -> str:
    t = str(s or "").strip()
    if not t:
        return ""
    line = t.splitlines()[0].strip()
    return line if len(line) <= n else line[:n] + "..."


def _parse(preview: str) -> Optional[dict]:
    t = preview.strip()
    if _FENCE_RE.match(t):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    if not t.startswith("{"):
        return None
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except Exception:
        # a preview is usually TRUNCATED json - salvage the leading scalar fields
        out: dict = {}
        for m in _SCALAR_RE.finditer(t):
            k, raw, s = m.group(1), m.group(2), m.group(3)
            if k in out:
                continue
            if s is not None:
                out[k] = s.encode().decode("unicode_escape", errors="ignore") if "\\" in s else s
            elif raw in ("true", "false"):
                out[k] = raw == "true"
            elif raw == "null":
                out[k] = None
            else:
                try:
                    out[k] = float(raw) if "." in raw else int(raw)
                except ValueError:
                    out[k] = raw
        return out or None


def sentence(tool: str, preview: str, reason: str = "") -> str:
    """The summary for a step that ended on `preview` (the last tool result).

    `reason` names what ended the step (a guard, the duplicate ceiling, ...)."""
    try:
        return _sentence(tool, preview, reason)
    except Exception:  # pragma: no cover - never let a summary raise
        return _first_line(preview) or (reason or "")


def _sentence(tool: str, preview: str, reason: str) -> str:
    p = str(preview or "")
    tool = str(tool or "").strip()
    reason = str(reason or "").strip()
    head = f"{reason} " if reason else ""
    d = _parse(p)
    if d is None:
        first = _first_line(p, 240)
        if not first:
            return reason
        return f"{head}{tool + ': ' if tool else ''}{first}".strip()
    ok = d.get("ok")
    parts = []
    if "rc" in d:                                   # an exec result
        rc = d.get("rc")
        what = d.get("path") or d.get("command") or tool or "the command"
        parts.append(f"{tool or 'exec'} ran {_first_line(what, 90)}: rc={rc}")
        out = _first_line(d.get("stdout"), 160)
        err = _first_line(d.get("stderr"), 160)
        if out:
            parts.append(f"stdout: {out}")
        if err:
            parts.append(f"stderr: {err}")
        if not out and not err:
            parts.append("no output")
    elif d.get("path") and (d.get("bytes") is not None or d.get("version") is not None or "syntax_ok" in d):
        size = f", {d['bytes']} bytes" if d.get("bytes") is not None else ""
        chk = ""
        if "syntax_ok" in d:
            chk = f", {d.get('checked_with') or 'parser'} {'ok' if d.get('syntax_ok') else 'FAILED'}"
        parts.append(f"{tool or 'the author'} wrote {d['path']}{size}{chk}")
        if d.get("error"):
            parts.append(f"error: {_first_line(d['error'], 160)}")
    elif d.get("error"):
        parts.append(f"{tool or 'the call'} failed: {_first_line(d['error'], 200)}")
    elif d.get("text"):
        parts.append(f"{tool or 'the call'} returned: {_first_line(d['text'], 200)}")
    elif d.get("summary"):
        parts.append(f"{tool or 'the call'}: {_first_line(d['summary'], 200)}")
    else:
        keys = ", ".join(f"{k}={_first_line(v, 40)}" for k, v in list(d.items())[:5])
        parts.append(f"{tool or 'the call'} returned {keys}")
    if ok is False and not any("failed" in x or "rc=" in x for x in parts):
        parts.append("(ok=false)")
    return (head + "; ".join(parts)).strip()
