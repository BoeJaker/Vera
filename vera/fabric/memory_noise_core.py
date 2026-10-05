"""Which memories are noise: kept in the store, ranked after everything else in recall.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.fabric.memory_noise_core`.

Owner, 2026-10-04: noisy records stay in the fabric (completeness) but rank last. Measured on prod 2026-10-06 over
the ~14k records left in default recall (tool calls and imported transcripts already out): the most repeated texts
were status events ("Remote IDE instance created: ..." x1,540, "Session started with agent ..." x600, bare
"[http.get]"), prompt scaffolding stored as if the user had said it ("The capability X ran and returned: ... Answer
the user's question", "Goal: ... Execution history so far"), injected context saved with the user's message
("hello [Retrieved Context] [memory] hello ..."), raw tool JSON, empty reports and one-word lines.

Rules only, each named, so a hit can say why it ranked last.
"""
from __future__ import annotations

import re
from typing import Any, List

_FLAGS = re.IGNORECASE | re.DOTALL

# Status lines written by the system about itself: what ran, what started, what was snapshotted.
_STATUS = [re.compile(p, _FLAGS) for p in (
    r"^Remote IDE instance (created|updated):",
    r"^Session started with agent \S+\s*$",
    r"^\[inspect\] snapshot ",
    r"^IDE workspace: ",
    r"^\[(CALL|OUT) [\w.]+\]",
    r"^\[[a-z][\w.]*\]\s*$",                              # a bare capability name
    r"^(Cap|DAG): [\w.]+: ",
    r"^DAG\(\w+\): ",
    r"^Step \d+: [\w.]+ \S+ [^\n]*\((complete|failed|error|skipped)\)\s*$",
    r"^\[[a-z]\w*\.[\w.]+\] ",                            # a capability echo: "[browser.search] ..."
    r"^\[remote/\w+\] ",
    r"^Research \[[\w/]+\]: ",
    r"^Ran capability ",
    r"^\[agentic loop[^\]]*\] run ended",
    r"^File: \S+ \(\d+b\)",
    r"^\[review\] \S+ \S+ \d+ issues?\s*$",
    r"^\[plan #\d+\]",
)]

# Prompt text the system wrote for a model, stored as a message.
_SCAFFOLD = [re.compile(p, _FLAGS) for p in (
    r"^Goal: .*\b(Execution history so far|Data so far|Style preference:)",
    r"\[Capability execution: ENABLED\b",
    r"^The capability \"?[\w.]+\"? (ran and returned|was called but failed|does not exist)",
    r"\[INTEGRATED CAPABILITY MODE\b",
    r"\[STUDIO \S+ visual & execution tools",
    r"^## Result \S+ lead with the direct answer",
    r"^Step \d+ of \d+\.",
)]

_INJECTED = re.compile(r"\[Retrieved Context\]")
_TOOL_JSON = re.compile(r'^\{\s*"\w+"\s*:')                # a raw JSON object, as a tool returned it
_EMPTY_REPORT = [re.compile(p, _FLAGS) for p in (
    r"\b0 file\(s\) reviewed\b",
    r"^.{0,120}\bno signal\b",                             # "# Project Reflection - no signal  All sensors returned ..."
    r"\bproduced no synthesis\b",
)]
_TITLE_TYPES = ("event",)
TITLE_MAX_WORDS = 5
_DIRECTIVE_ONLY = re.compile(r"^\[\[[^\]]*\]\]\s*$|^\[\[(cap|loop|web)[:.]", _FLAGS)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")

NEAR_EMPTY_WORDS = 3


def _get(rec: Any, key: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def noise_reasons(rec: Any) -> List[str]:
    """Why a record (a dict or a MemoryRecord) is noise; empty when it is not."""
    text = str(_get(rec, "text") or "").strip()
    out: List[str] = []
    if any(p.search(text) for p in _STATUS):
        out.append("status")
    if any(p.search(text) for p in _SCAFFOLD):
        out.append("scaffold")
    if _INJECTED.search(text):
        out.append("injected-context")
    if _TOOL_JSON.match(text):
        out.append("tool-json")
    if any(p.search(text) for p in _EMPTY_REPORT):
        out.append("empty-report")
    if _DIRECTIVE_ONLY.search(text):
        out.append("directive")
    if not out:
        words = len(_WORD.findall(text))
        if words < NEAR_EMPTY_WORDS:
            out.append("near-empty")
        elif (str(_get(rec, "record_type") or "").lower() in _TITLE_TYPES and words <= TITLE_MAX_WORDS
              and "\n" not in text and not re.search(r"[.:!?]", text)):
            out.append("title")                            # a session or dream-topic title: "Bash User Query"
    return out


def is_noise(rec: Any) -> bool:
    return bool(noise_reasons(rec))
