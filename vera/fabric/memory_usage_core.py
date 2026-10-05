"""Was a recalled memory used in the reply? A cheap judge, so every chat turn can say (context-and-ui CU-M.2).

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.fabric.memory_usage_core`.

Owner, 2026-10-04: memory should learn from use - "useless session memories fade, useful ones stay". The half-life
(CU-M.3) needs to know, per record, how often it was put in front of the model and how often the model drew on it.
This judge is the cheap first stage: word overlap between the record's distinctive words and the reply, plus a
shared run of six words. A semantic judge (MiniLM on a node) can replace it behind the same function later.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
_STOP = frozenset("""
the and for are but not you all any can had her was one our out has have his how its may new now old see two
way who did get let put say she too use that this with from they will what when where which while would there
their them then than were been being into over also just like some such only very more most other about after
again could should these those your yours ours here each both does doing done make made many much must need
said same well what's it's i'm you're don't can't won't isn't aren't user assistant memory record tool call
""".split())
_DISTINCT = 20           # the record's longest words stand for it; a long record is not judged on every word
_SHINGLE = 6


def content_words(text: Any) -> List[str]:
    """The record's content words, lower-case, stop words out, in order."""
    return [w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


def _shingles(words: List[str], n: int = _SHINGLE) -> set:
    return {" ".join(words[i:i + n]) for i in range(0, max(0, len(words) - n + 1))}


def judge(reply: Any, text: Any, min_share: float = 0.35, min_hits: int = 3) -> Tuple[bool, float]:
    """(used, score 0..1). Used when the reply carries enough of the record's distinctive words (at least min_hits,
    and min_share of the up-to-20 longest), or repeats six of the record's words in a row."""
    tw = content_words(text)
    if not tw:
        return False, 0.0
    rw = content_words(reply)
    if not rw:
        return False, 0.0
    distinct = sorted(set(tw), key=lambda w: (-len(w), w))[:_DISTINCT]
    rset = set(rw)
    hits = sum(1 for w in distinct if w in rset)
    share = hits / float(len(distinct))
    if len(tw) >= _SHINGLE and _shingles(tw) & _shingles(rw):
        return True, max(share, 0.9)
    return (hits >= min(min_hits, len(distinct)) and share >= min_share), round(share, 3)


def judge_all(reply: Any, items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{id, used, score}] for every item with an id and text, once per id (first wins)."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for it in items or []:
        rid = str((it or {}).get("id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        used, score = judge(reply, (it or {}).get("text", ""))
        out.append({"id": rid, "used": used, "score": score})
    return out


_SKIP_PREFIXES = ("census", "probe-", "probe_", "intent-core", "selftest")


def counts_toward_use(session_id: Any, reply: Any) -> bool:
    """A turn's feedback is recorded only for real conversation: never for an empty reply, a census goal or a probe
    (four-minds §3.14-15: self-telemetry and census turns stay out of the learning signal)."""
    sid = str(session_id or "").lower()
    return bool(str(reply or "").strip()) and bool(sid) and not sid.startswith(_SKIP_PREFIXES)
