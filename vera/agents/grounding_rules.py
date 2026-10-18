"""Grounding rules: one text, several readers, and a check of an answer against the evidence it had.

Info-seeking plan SK-1.3 (shared-planning/intelligence/PLAN-INFO-SEEKING.md §3). The owner, 2026-10-10: chat "uses
memories and fills in the gaps with guesses instead of going and finding the facts". The chat panel, the aide prompt
and the loop's CAP_ROUTING each said something different about when to look things up; this module is the one
statement, phrased per surface, and `check` is the deterministic half of "did the answer stay inside its evidence":
citations that point at nothing, and numbers or dates the sources never said. No model call; pure.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

SURFACES = ("chat", "aide", "loop")

# the cap a surface reaches for, per job (loops cannot start background report jobs)
_CAPS = {
    "chat": {"seek": "info.seek", "report": "research.run (a background report, 5-30 minutes)",
             "tracked": "intel.lookup", "url": "web.fetch"},
    "aide": {"seek": "info.seek", "report": "research.run (a background report, 5-30 minutes)",
             "tracked": "intel.lookup", "url": "web.fetch"},
    "loop": {"seek": "info.seek", "report": "info.seek need=overview with start_report=false",
             "tracked": "intel.lookup", "url": "web.fetch"},
}


def rules(surface: str = "chat") -> List[str]:
    """The five rules, worded for `surface` (chat | aide | loop)."""
    c = _CAPS.get(surface, _CAPS["chat"])
    return [
        f"Anything current, external, specific or possibly changed since your training - a fact, the latest / news / "
        f"updates on something, a price - is looked up first with {c['seek']} (it checks what Vera tracks and the web, "
        f"and returns numbered, dated evidence). For one known URL use {c['url']}; for 'what has Vera's tracking "
        f"found' use {c['tracked']}; for a long write-up the user asked for use {c['report']}.",
        "Memories and notes are dated records of what was said or seen, not proof the world is still that way. Use "
        "them for the user's own preferences and history; for an outside fact that could have changed, look it up "
        "and give the date of what you found.",
        "Never fill a gap with a plausible guess. If the evidence does not answer part of the question, say what was "
        "looked for and not found.",
        "Cite what you use as [n], the number of the evidence item, with its date; never cite a number you were not "
        "given, and never state a figure or date the evidence does not contain.",
        "Answer directly, without looking anything up, only from this conversation or from knowledge that cannot "
        "have changed (definitions, maths, how-to).",
    ]


def text(surface: str = "chat") -> str:
    """The rules as one prompt block for `surface`."""
    return "GROUNDING - never guess where you can look\n" + "\n".join(f"- {r}" for r in rules(surface))


# ── check: does an answer stay inside its evidence ─────────────────────────────────────────────────────────────

# [3], [E3], and lists: [1, 3] / [E1; E2]. Loops number a run's evidence E1..En across packs (compute-roles, 10 Oct);
# a single info.seek pack numbers its items 1..n.
_CITE = re.compile(r"\[\s*([Ee]?\d{1,4}(?:\s*[,;]\s*[Ee]?\d{1,4})*)\s*\]")
# figures worth checking: percentages, money, decimals, multi-digit numbers and years; not list markers or [n]
_NUM = re.compile(r"(?<![\w.\[])(?:[£$€]\s?)?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.\[])(?:[£$€]\s?)?\d+(?:\.\d+)?\s?%"
                  r"|(?<![\w.\[])(?:[£$€]\s?)?\d+\.\d+(?:\s?(?:bn|m|k|billion|million))?|(?<![\w.\[])[£$€]\s?\d+"
                  r"(?:\s?(?:bn|m|k|billion|million))?|(?<![\w.\[\d])\d{2,}(?![\]\d])")
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _norm_num(tok: str) -> str:
    t = tok.lower().replace(",", "").replace(" ", "")
    t = re.sub(r"^[£$€]", "", t).rstrip("%")
    m = re.match(r"^(\d+(?:\.\d+)?)(billion|bn|million|m|k)$", t)
    if m:                                       # "£271k" is the sources' "£271,000"
        mult = {"billion": 1e9, "bn": 1e9, "million": 1e6, "m": 1e6, "k": 1e3}[m.group(2)]
        v = float(m.group(1)) * mult
        t = str(int(v)) if v == int(v) else str(v)
    return t


def _numbers(text: str) -> List[str]:
    stripped = _CITE.sub(" ", text or "")
    stripped = re.sub(r"(?m)^\s*\d{1,2}[.)]\s", " ", stripped)          # list markers
    return [m.group(0).strip() for m in _NUM.finditer(stripped)]


def _cited(text: str) -> List[str]:
    """The ids an answer cites, upper-cased as written: "[1, E3]" -> ["1", "E3"]."""
    out: List[str] = []
    for m in _CITE.finditer(text or ""):
        out += [t.strip().upper() for t in re.split(r"[,;]", m.group(1)) if t.strip()]
    return out


def check(answer: str, items: Sequence[Dict[str, Any]], id_key: str = "n") -> Dict[str, Any]:
    """{ok, bad_citations, uncited_numbers, unsupported}: citations to items that do not exist, figures and dates in
    the answer that no item's title or text contains, and the sentences carrying them. A number counts as supported
    when its digits appear in any item (formatting - commas, currency, %, "bn" - ignored). Deterministic; a model
    judge can sit on top of it, never under it.

    `id_key` names the item field the answer's citations refer to: "n" (an info.seek pack's own numbers, cited
    [3]) or a run-wide one such as "e" / "E" (cited [E3]; a bare number in that field also answers to [E<n>])."""
    valid = set()
    for it in items or []:
        v = it.get(id_key)
        if v is None or str(v).strip() == "":
            continue
        s = str(v).strip().upper()
        valid.add(s)
        if id_key != "n" and s.isdigit():
            valid.add("E" + s)
    cited = _cited(answer)
    bad_ids = sorted({c for c in cited if c not in valid}, key=lambda x: (len(x), x))
    bad = [int(c) if c.isdigit() else c for c in bad_ids]
    corpus = " ".join(f"{it.get('title') or ''} {it.get('text') or it.get('snippet') or ''} {it.get('published') or ''}"
                      for it in items or [])
    hay = {_norm_num(t) for t in _numbers(corpus)} | set(re.findall(r"\d+(?:\.\d+)?", corpus.replace(",", "")))
    uncited, unsupported = [], []
    for sent in _SENT.split(answer or ""):
        missing = [t for t in _numbers(sent) if _norm_num(t) not in hay]
        if missing:
            uncited += [m for m in missing if m not in uncited]
            unsupported.append(sent.strip()[:240])
    return {"ok": not bad and not uncited, "bad_citations": bad, "uncited_numbers": uncited,
            "unsupported": unsupported}
