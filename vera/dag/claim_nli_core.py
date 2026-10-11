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
# the run narrating itself ("Source Retrieval: Fetched the docs...", "A comprehensive Markdown document
# detailing...") is not a claim about the world (replay 11 Oct: 6 of 32 delivered "claims")
_NARRATION = re.compile(r"^(?:[A-Z][\w /&-]{2,32}:\s*)?(?:fetched|retrieved|queried|ran|searched|gathered|collected|"
                        r"called|performed|executed|looked up|checked|consulted|used|reviewed|compiled|wrote|saved|"
                        r"created|produced|based on)\b|^(?:a|the|this)\s+(?:\w+\s+){0,2}(?:markdown|document|"
                        r"report|file|summary|table)\b", re.I)
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
        if len(text.split()) < 4 or not _FACT.search(text) or _NARRATION.search(text):
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


def windows(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{item, text}] - every evidence item cut into windows (title and date lead its first one)."""
    wins: List[Dict[str, Any]] = []
    for it in items or []:
        body = " ".join(str(it.get(k) or "") for k in ("title", "published")) + " " + \
               str(it.get("text") or it.get("snippet") or "")
        for w in _windows(body):
            wins.append({"item": _item_id(it), "text": w})
    return wins


def candidates(c: Dict[str, Any], wins: Sequence[Dict[str, Any]], limit: int = 12) -> List[int]:
    """Indices of the windows worth showing a ranker for claim `c`: the cited items' windows, then
    the windows sharing the most words with it (figures count double). [] = none share a word."""
    ct = _tokens(c.get("text"))
    figs = {t for t in ct if any(ch.isdigit() for ch in t)}
    cited = {x.lstrip("E") for x in (c.get("cites") or [])}
    scored = []
    for k, w in enumerate(wins):
        tok = w.get("tok") or _tokens(w["text"])
        ov = min(99, len(ct & tok) + len(figs & tok))
        boost = 100 if w["item"].lstrip("E") in cited else 0
        if ov or boost:
            scored.append((boost + ov, k))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [k for _s, k in scored[:limit]]


def pairs(cl: Sequence[Dict[str, Any]], items: Sequence[Dict[str, Any]],
          per_claim: int = MAX_PAIRS_PER_CLAIM,
          ranked: Dict[int, List[int]] = None) -> List[Dict[str, Any]]:
    """[{claim, item, premise}] - each claim with its best evidence windows, at most `per_claim`.
    `ranked` {claim i: [window index, best first]} is a ranker's order (nlp.rerank over
    candidates()); without it the word-overlap order of candidates() is used. Replay 11 Oct: the
    overlap order picked a page header over the sentence that said it, so a ranker is preferred."""
    wins = windows(items)
    out: List[Dict[str, Any]] = []
    for c in cl or []:
        order = (ranked or {}).get(c["i"])
        if order is None:
            order = candidates(c, wins)
        for k in order[:per_claim]:
            out.append({"claim": c["i"], "item": wins[k]["item"], "premise": wins[k]["text"]})
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


HINT_MAX = 10


def hint_block(flagged: Sequence[str]) -> str:
    """The not-entailed claims as a block for the answer check ("" when none). NLI over-flags
    paraphrase (replay 11 Oct: precision 0.47, recall 1.0), so the checker is told to VERIFY each
    against the evidence, not to treat it as wrong."""
    rows = [str(f).strip() for f in flagged or [] if str(f).strip()][:HINT_MAX]
    if not rows:
        return ""
    return ("CLAIMS NO EVIDENCE PASSAGE ENTAILS (an NLI model's check - it also flags correct "
            "paraphrases, so VERIFY each against the evidence above; correct or remove only the ones "
            "the evidence does not support):" + chr(10) + chr(10).join("- " + r for r in rows))


def summary(vs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """{claims, supported, contradicted, unsupported, flagged: [text of the not-supported]}."""
    n = {k: sum(1 for v in vs if v["verdict"] == k) for k in ("supported", "contradicted", "unsupported")}
    return dict(claims=len(vs), **n, flagged=[v["text"] for v in vs if v["verdict"] != "supported"])
