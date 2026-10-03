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
    picked: List[Dict[str, Any]] = [h for h in reversed(list(history or [])) if _is_source(h)][:max_sources]
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
