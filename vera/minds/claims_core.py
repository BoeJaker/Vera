"""Claims in a run's deliverable against the evidence the run read (four-minds FM-5.2), as an application of the
candidate matrix (owner, 17 Oct: introduce and test every option in parallel, no method chosen up front).

From a finished loop run: its deliverable (the `done` event's summary) and its evidence (the tool_done previews of
info.seek / web.research / web.fetch). The checkable claims are the deliverable's sentences that state a figure, a
date, a version or a name; each is paired with the evidence passage that shares the most words with it. Each pair is
one `output.claim_supported` decision on the ledger - the baseline answer is a word-overlap rule - and every judge in
the matrix (NLI, zero-shot, the cross-encoder, every small LLM, trained heads) answers it in shadow. Gold comes from
the owner's labels; nothing is rewritten, nothing flags a live answer yet.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

QUESTION = "output.claim_supported"
LABELS = ["supported", "unsupported"]
EVIDENCE_TOOLS = ("info.seek", "web.research", "web.fetch")
MAX_CLAIMS = 8
CLAIM_CHARS = 400
PASSAGE_CHARS = 1200
WINDOW, STRIDE = 600, 400
#: the baseline rule: a claim is "supported" when this share of its content words is in the best passage
OVERLAP_SUPPORTED = 0.6
RULE = "rule:word_overlap"

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_CHECKABLE = re.compile(r"\d|\b(?:January|February|March|April|May|June|July|August|September|October|November|"
                        r"December)\b|\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b|\bv?\d+\.\d+")
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
# Not claims about the world (18 Oct: 22 of the first 57 harvested were these): a heading or title (no closing
# punctuation), the run talking about its own output, advice in the imperative, a plan's milestone / week / step.
_TERMINAL = re.compile(r"[.!?:;]\s*[\"')\]]*\s*$")
_SELF = re.compile(r"\b(?:this|the) (?:run|report|document|explainer|summary|deliverable|answer)\b|\bartifacts?\b|"
                   r"^here(?: is|'s)\b|\bhas been (?:defined|created|written|prepared|drafted)\b|\bthe following\b|"
                   r"\bbelow\b", re.I)
_LABEL = re.compile(r"^[^:]{1,60}?\s*:\s+")
_ADVICE = re.compile(r"^(?:use|prefer|ensure|design|implement|avoid|consider|add|make|keep|run|install|create|build|"
                     r"define|check|try|apply|enable|set|write|test|deploy|configure)\b", re.I)
_PLAN_STEP = re.compile(r"^(?:milestone|week|phase|step|stage|day|sprint)\s*\d", re.I)
_STOP = set("the and for with that this from have has had are was were will would could should into onto about "
            "over under than then also more most such which while their there these those been being its it's "
            "is in on of to a an by as at or be not no yes can may".split())
_JSTR = re.compile(r'"(title|url|published|text|snippet|content)"\s*:\s*"((?:[^"\\]|\\.)*)')


def words(t: str) -> set:
    return {w for w in _WORD.findall((t or "").lower()) if (len(w) > 2 or w.isdigit()) and w not in _STOP}


def is_claim(s: str) -> bool:
    """A sentence that states something about the world - not a heading, not the run describing its own output,
    not advice or a plan step (those are neither supported nor unsupported by a page)."""
    s = (s or "").strip()
    if not _TERMINAL.search(s) or _SELF.search(s) or _PLAN_STEP.match(s) or _ADVICE.match(s):
        return False
    rest = _LABEL.sub("", s, count=1)
    return not _ADVICE.match(rest)


def claims(deliverable: str, limit: int = MAX_CLAIMS) -> List[str]:
    """The deliverable's checkable sentences (a figure, a date, a version or a name), in order, deduplicated."""
    text = re.sub(r"[#*_`>|]+", " ", str(deliverable or ""))
    out: List[str] = []
    seen = set()
    for line in text.splitlines():
        for s in _SENT.split(line.strip()):
            s = " ".join(s.split()).strip(" -")
            if len(s) < 25 or not _CHECKABLE.search(s) or len(words(s)) < 4 or not is_claim(s):
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s[:CLAIM_CHARS])
            if len(out) >= limit:
                return out
    return out


def evidence_items(preview: str) -> List[Dict[str, Any]]:
    """Items from a clipped tool_done preview: the whole JSON when it parses, else its string fields."""
    try:
        pk = json.loads(preview)
        pk = pk.get("result", pk) if isinstance(pk, dict) else {}
        for key in ("items", "sources", "results"):
            if isinstance(pk.get(key), list):
                return [i for i in pk[key] if isinstance(i, dict)]
        if pk.get("url") or pk.get("text") or pk.get("content"):
            return [pk]
    except Exception:
        pass
    items: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for m in _JSTR.finditer(preview or ""):
        k, v = m.group(1), m.group(2).replace('\\"', '"').replace("\\n", " ")
        if k in cur and k in ("title", "url"):
            items.append(cur)
            cur = {}
        cur["text" if k in ("snippet", "content") and "text" not in cur else k] = v
    if cur:
        items.append(cur)
    return items


def passages(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Overlapping windows of every evidence text the run read: [{text, url, title}]."""
    out = []
    for e in events:
        if not str(e.get("type") or "").endswith("tool_done") or e.get("tool") not in EVIDENCE_TOOLS or not e.get("ok"):
            continue
        for it in evidence_items(e.get("preview") or ""):
            text = " ".join(str(it.get("text") or it.get("content") or it.get("snippet") or "").split())
            for k in range(0, max(1, len(text)), STRIDE):
                w = text[k:k + WINDOW]
                if w.strip():
                    out.append({"text": w, "url": str(it.get("url") or "")[:300], "title": str(it.get("title") or "")[:200]})
    return out


def best_passage(claim: str, ps: Sequence[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
    """The passage sharing the most of the claim's content words, and that share (0-1)."""
    cw = words(claim)
    if not cw or not ps:
        return None, 0.0
    best, share = None, -1.0
    for p in ps:
        s = len(cw & words(p["text"])) / len(cw)
        if s > share:
            best, share = p, s
    return best, round(max(0.0, share), 3)


def deliverable_of(events: Iterable[Dict[str, Any]]) -> str:
    for e in reversed(list(events)):
        if str(e.get("type") or "").endswith(".done") and "tool" not in str(e.get("type")) and e.get("summary"):
            return str(e["summary"])
    return ""


def pairs(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One {claim, evidence, url, title, overlap} per checkable claim of the run that has evidence to check against."""
    ps = passages(events)
    if not ps:
        return []
    out = []
    for c in claims(deliverable_of(events)):
        p, share = best_passage(c, ps)
        if p is None:
            continue
        out.append({"claim": c, "evidence": p["text"][:PASSAGE_CHARS], "url": p["url"], "title": p["title"],
                    "overlap": share})
    return out


def claim_ref(session_id: str, claim: str) -> str:
    return hashlib.sha1(f"{session_id}|{claim}".encode("utf-8")).hexdigest()[:16]


def baseline(pair: Dict[str, Any]) -> str:
    return LABELS[0] if float(pair.get("overlap") or 0) >= OVERLAP_SUPPORTED else LABELS[1]
