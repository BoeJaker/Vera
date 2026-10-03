"""What a step already fetched, handed to the writer.

Live 2026-10-03 (session 4d4ef790): web.research returned five sources, then the executor
called prose.author(task="... Use the research data gathered ...", context_files=[]). The
writer takes context BY FILE and the research lived only in the step's tool history, so it
saw none of it, wrote from general knowledge, and - following its own rule to say when
its sources are empty - put "(Note: The provided context files were empty ...)" into the
deliverable. The same gap leaves research write-ups without citations (census 3 Oct,
strategic-vector-db-report: "cites sources" failed).

Pure: the step history in, a bounded SOURCE MATERIAL text out ("" when there is none).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Sequence

#: Tool-name prefixes whose results are source material for a write-up.
SOURCE_PREFIXES = ("web.",)
MAX_SOURCES = 4
MAX_CHARS = 8000


def _is_source(h: Any) -> bool:
    return (isinstance(h, dict) and h.get("ok") is not False
            and str(h.get("tool") or "").startswith(SOURCE_PREFIXES)
            and bool(str(h.get("preview") or "").strip()))


def sources_block(history: Sequence[Any], max_sources: int = MAX_SOURCES,
                  max_chars: int = MAX_CHARS) -> str:
    """The step's successful web.* results, most recent first, as one bounded block."""
    picked: List[Dict[str, Any]] = []
    seen = set()
    for h in reversed(list(history or [])):
        if not _is_source(h):
            continue
        a = h.get("args") if isinstance(h.get("args"), dict) else {}
        key = (h.get("tool"), str(a.get("query") or a.get("url") or ""), str(h.get("preview"))[:120])
        if key in seen:
            continue                      # the same result, recorded for the run AND the step
        seen.add(key)
        picked.append(h)
        if len(picked) >= max_sources:
            break
    if not picked:
        return ""
    share = max(400, max_chars // len(picked))
    parts = []
    for h in picked:
        a = h.get("args") if isinstance(h.get("args"), dict) else {}
        what = a.get("query") or a.get("url") or a.get("q") or ""
        parts.append("[%s%s]\n%s" % (h.get("tool"), (" " + str(what)[:160]) if what else "",
                                     str(h.get("preview"))[:share]))
    return "\n\n".join(parts)[:max_chars]


def context_files_empty(value: Any) -> bool:
    """True for a missing/empty context_files argument, including the string "[]"."""
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return not any(str(v).strip() for v in value)
    s = str(value).strip()
    return s in ("", "[]", "null", "None")



# ── The RUN's research, not just the step's ─────────────────────────────────────
# Probe 3 Oct (1e84cf1d, strategic-vector-db-report): web.research/web.fetch ran in steps
# 1-2, prose.author in step 3 - whose own history held no web results, so nothing was
# handed over and the report cited "[data-dynamics.io blog post]" with no URL ("cites
# sources" failed). Successful web results are remembered per session, bounded.
MAX_RUNS = 64
MAX_PER_RUN = 12
_RUN: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()


def remember(session_id: str, entry: Any) -> None:
    """Record one step-history entry for the run when it is source material."""
    if not session_id or not _is_source(entry):
        return
    a = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    lst = _RUN.setdefault(session_id, [])
    _RUN.move_to_end(session_id)
    lst.append({"tool": entry.get("tool"), "ok": True,
                "args": {k: a[k] for k in ("query", "url", "q") if k in a},
                "preview": str(entry.get("preview"))[:4000]})
    del lst[:-MAX_PER_RUN]
    while len(_RUN) > MAX_RUNS:
        _RUN.popitem(last=False)


def recall(session_id: str) -> List[Dict[str, Any]]:
    return list(_RUN.get(session_id or "", []))



# ── When the executor names its OWN context ─────────────────────────────────────
# Probe 3 Oct (7c5bf52c): prose.author(context_files=["/workspace/journal.json"],
# sources="https://lushbinary.com/...") - the run's bookkeeping file and a bare URL. Both
# made the handoff stand down, so the writer got a link and none of the research.
BOOKKEEPING = frozenset({"journal.json"})
#: A `sources` value shorter than this is a pointer (a URL, a title), not material.
THIN_SOURCES_CHARS = 1500


def bookkeeping_only(value: Any) -> bool:
    """context_files naming only the run's own bookkeeping (journal.json)."""
    if context_files_empty(value):
        return False
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        s = str(value).strip().strip("[]")
        items = [x.strip().strip("'\"") for x in s.split(",")]
    names = [i.replace("\\", "/").rsplit("/", 1)[-1] for i in items if i.strip()]
    return bool(names) and all(n in BOOKKEEPING for n in names)


def wants_sources(context_files: Any, given_sources: Any) -> bool:
    """Should the loop hand the run's research to this writer call?"""
    no_material = context_files_empty(context_files) or bookkeeping_only(context_files)
    return no_material and len(str(given_sources or "").strip()) < THIN_SOURCES_CHARS
