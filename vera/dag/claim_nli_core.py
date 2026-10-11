"""Claims in an answer, paired with the evidence that could support them, for an NLI check.

Compute-roles plan P3 (PLAN-COMPUTE-AND-INFO-IN-LOOPS, 10 Oct 2026): judged research answers keep
inventing dates and versions (I2 research-web 2/10, 11 Oct). grounding_rules.check catches a figure
no source contains; it cannot catch a figure the sources DO contain put on the wrong thing ("Safari
15 introduced WebGPU" when a source says Safari 26). An NLI model (the nodes' DeBERTa-v3
mnli-fever-anli, CPU, ~100 ms a pair) can: premise = the evidence window, hypothesis = the claim.

This module is the pure half: which sentences are claims worth checking, which evidence windows to
pair each with, and how the NLI scores become a verdict per claim. The model call is the caller's
(nlp.nli on a node, or a local pipeline in the replay tool).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

MAX_CLAIMS = 16
MAX_PAIRS_PER_CLAIM = 3
WINDOW_CHARS = 480
CLAIM_CHARS = 300
ENTAIL_OK = 0.5          # a premise entails the claim -> supported
CONTRA_BAD = 0.7         # a premise contradicts it (and none entails) -> contradicted

_CITE = re.compile(r"\[\s*([Ee]?\d{1,4}(?:\s*[,;]\s*[Ee]?\d{1,4})*)\s*\]")
# what makes a sentence checkable: a year, a date, a version, a figure (%, money, decimal, 2+ digits)
_FACT = re.compile(r"\b(?:19|20)\d{2}\b|\bv?\d+\.\d+(?:\.\d+)?\b|\d+(?:\.\d+)?\s?%|[£$€]\s?\d|\b\d{2,}\b")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[*])|\n+")
_MD = re.compile(r"^\s*(?:[-*+]\s+|\d{1,2}[.)]\s+|#{1,6}\s+|>\s*)|\*\*|__|`")
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_STOP = frozenset("the a an and or of to in on for with by from at as is are was were be been it its this that "
                  "these those which who what when where how than then into over under about after before "
                  "since until not no can will would should may might has have had do does did".split())


def _clean(sentence: str) -> str:
    s = _MD.sub("", sentence or "")
    s = _CITE.sub("", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", s))
    return s.strip(" -:;")


def _cites(sentence: str) -> List[str]:
    out: List[str] = []
    for m in _CITE.finditer(sentence or ""):
        out += [t.strip().upper() for t in re.split(r"[,;]", m.group(1)) if t.strip()]
    return out


def claims(answer: str, max_claims: int = MAX_CLAIMS) -> List[Dict[str, Any]]:
    """[{i, text, cites}] - the answer's sentences that state a year, date, version or figure, in
    order, cleaned of markdown and citation marks (the cited ids are kept). Headings, table rules
    and fragments under 4 words are skipped."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in _SENT.split(answer or ""):
        if re.match(r"^\s*(#{1,6}\s|\|?\s*:?-{3,})", raw or ""):
            continue
        text = _clean(raw)
        if len(text.split()) < 4 or not _FACT.search(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"i": len(out), "text": text[:CLAIM_CHARS], "cites": _cites(raw)})
        if len(out) >= max_claims:
            break
    return out


def _tokens(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def _windows(text: str, size: int = WINDOW_CHARS) -> List[str]:
    """Overlapping windows of an item's text, cut at sentence ends where it can."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= size:
        return [t] if t else []
    out, start = [], 0
    while start < len(t):
        end = min(len(t), start + size)
        if end < len(t):
            dot = t.rfind(". ", start + size // 2, end)
            end = dot + 1 if dot > 0 else end
        out.append(t[start:end].strip())
        if end >= len(t):
            break
        start = max(end - size // 4, start + 1)
    return out


def _item_id(it: Dict[str, Any]) -> str:
    e = it.get("e")
    if e not in (None, ""):
        s = str(e).strip().upper()
        return s if s.startswith("E") else "E" + s
    return str(it.get("n") or "").strip().upper()


def pairs(cl: Sequence[Dict[str, Any]], items: Sequence[Dict[str, Any]],
          per_claim: int = MAX_PAIRS_PER_CLAIM) -> List[Dict[str, Any]]:
    """[{claim, item, premise, overlap}] - for each claim, its best evidence windows: the windows of
    the items it cites first, then the windows with the most shared words (a claim's figures count
    double), at most `per_claim` per claim. A claim with no window sharing a word gets no pair."""
    wins: List[Dict[str, Any]] = []
    for it in items or []:
        body = " ".join(str(it.get(k) or "") for k in ("title", "published")) + " " + \
               str(it.get("text") or it.get("snippet") or "")
        for w in _windows(body):
            wins.append({"item": _item_id(it), "text": w, "tok": _tokens(w)})
    out: List[Dict[str, Any]] = []
    for c in cl or []:
        ct = _tokens(c.get("text"))
        figs = {t for t in ct if any(ch.isdigit() for ch in t)}
        cited = {x.lstrip("E") for x in (c.get("cites") or [])}
        scored = []
        for k, w in enumerate(wins):
            ov = min(99, len(ct & w["tok"]) + len(figs & w["tok"]))
            boost = 100 if w["item"].lstrip("E") in cited else 0
            if ov or boost:
                scored.append((boost + ov, k))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for score, k in scored[:per_claim]:
            out.append({"claim": c["i"], "item": wins[k]["item"], "premise": wins[k]["text"],
                        "overlap": score % 100})
    return out


def verdicts(cl: Sequence[Dict[str, Any]], prs: Sequence[Dict[str, Any]],
             scores: Sequence[Dict[str, float]]) -> List[Dict[str, Any]]:
    """Per claim: supported (a premise entails it), contradicted (one contradicts it and none
    entails it) or unsupported (no pair, or the evidence is neutral). `scores[k]` is the NLI
    result for `prs[k]`: {entailment, neutral, contradiction} probabilities."""
    best: Dict[int, Dict[str, Any]] = {}
    for p, s in zip(prs or [], scores or []):
        b = best.setdefault(p["claim"], {"entail": 0.0, "contra": 0.0, "by": None})
        e, c = float(s.get("entailment", 0.0)), float(s.get("contradiction", 0.0))
        if e > b["entail"]:
            b["entail"], b["by"] = e, p["item"]
        b["contra"] = max(b["contra"], c)
    out = []
    for c in cl or []:
        b = best.get(c["i"])
        if b is None:
            v = "unsupported"
        elif b["entail"] >= ENTAIL_OK:
            v = "supported"
        elif b["contra"] >= CONTRA_BAD:
            v = "contradicted"
        else:
            v = "unsupported"
        out.append({"i": c["i"], "text": c["text"], "verdict": v,
                    "entail": round(b["entail"], 3) if b else None,
                    "contra": round(b["contra"], 3) if b else None,
                    "by": b["by"] if b else None})
    return out


def summary(vs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """{claims, supported, contradicted, unsupported, flagged: [text of the not-supported]}."""
    n = {k: sum(1 for v in vs if v["verdict"] == k) for k in ("supported", "contradicted", "unsupported")}
    return dict(claims=len(vs), **n, flagged=[v["text"] for v in vs if v["verdict"] != "supported"])
