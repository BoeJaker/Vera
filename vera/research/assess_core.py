# -*- coding: utf-8 -*-
"""
Assessment — what a text or a body of code IS, distilled into scores with evidence (EXPLODE.md §6, track F).

Pure: every scorer here takes the text (and, for code, the Explode contract the extractor produced) and returns
ASSESSMENTS in the contract's shape:

    {key, label, score 0–1, confidence 0–1, by, on: "source" | <card id>, evidence: [{span, note}], badge?}

Rules that keep a verdict honest:
  * `by` names what produced the score. A heuristic says so in its `by` and carries a LOW confidence; nothing
    here pretends a pattern is a model.
  * every score has evidence spans where it can — a trust score with nothing to click on is an opinion.
  * a per-card assessment (`on` = a card id) shows on that card as a badge; a source-level one on the verdict rail.

Prose (no model):     readability · structure · sources · ai_likelihood (stylometry) · trust (composite)
Code (no model):      complexity · smells · clones · tests (given the test corpus) · provenance (given git facts)
Model-backed scorers (an LLM rubric, a two-model AI detector, a claim checker) register through the same
registry in assess_capabilities.py when they exist; none is invented here.
"""
from __future__ import annotations

import ast
import hashlib
import re
from typing import Dict, List, Optional, Tuple

# ── text structure ────────────────────────────────────────────────────────────────────────────────
_SENT_RX = re.compile(r"[^.!?\n]+[.!?]+(?:\s|$)|[^.!?\n]+$", re.M)
_WORD_RX = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_URL_RX = re.compile(r"https?://[^\s)>\]\"']{6,300}")
_CITE_RX = re.compile(r"\[\d{1,3}\]|\((?:[A-Z][\w\-]+(?: [A-Z][\w\-]+){0,3}(?: et al\.)?,? (?:19|20)\d{2}[a-z]?)\)|\b(?:according to|reported by|cited in|as reported|per the|source:)\b", re.I)
_HEADING_RX = re.compile(r"^\s*(#{1,6}\s+\S|[A-Z][A-Z0-9 \-:]{6,}$)", re.M)
_LIST_RX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.M)
_AI_PHRASES = ["delve", "it is important to note", "it's important to note", "in conclusion", "as an ai", "as a language model",
               "moreover", "furthermore", "additionally", "in today's fast-paced", "a testament to", "navigate the", "tapestry",
               "in the realm of", "it is worth noting", "ultimately,", "overall,", "in summary", "let's dive", "unlock the",
               "game-changer", "seamlessly", "leverage", "robust", "holistic", "cutting-edge", "landscape of"]


def sentences(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _SENT_RX.finditer(text) if m.group(0).strip()]


def _syllables(word: str) -> int:
    w = word.lower().strip("'")
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _span(path: str, s: int, e: int) -> Dict:
    return {"path": path or "", "start": int(s), "end": int(e)}


# ── prose scorers ─────────────────────────────────────────────────────────────────────────────────
def readability(text: str, path: str = "") -> Optional[Dict]:
    """Flesch reading ease, normalised to 0–1 (1 = plain). Evidence: the hardest sentence."""
    sents = sentences(text)
    words = _WORD_RX.findall(text)
    if len(words) < 20 or not sents:
        return None
    syl = sum(_syllables(w) for w in words)
    fre = 206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syl / len(words))
    hardest = None
    for s, e in sents:
        ws = _WORD_RX.findall(text[s:e])
        if len(ws) >= 6:
            sy = sum(_syllables(w) for w in ws) / len(ws)
            k = len(ws) * 0.4 + sy * 12
            if hardest is None or k > hardest[0]:
                hardest = (k, s, e)
    ev = [{"span": _span(path, hardest[1], hardest[2]), "note": "the hardest sentence"}] if hardest else []
    return {"key": "readability", "label": "readable", "score": round(_clamp(fre / 100.0), 3), "confidence": 0.6,
            "by": "Flesch reading ease", "on": "source", "evidence": ev, "detail": {"fre": round(fre, 1), "words": len(words), "sentences": len(sents)}}


def structure(text: str, paragraphs: Optional[List[Dict]] = None, path: str = "") -> Optional[Dict]:
    """Is the text shaped — paragraphs of a readable length, headings or lists where it is long. Evidence: a wall
    of text, when there is one."""
    words = _WORD_RX.findall(text)
    if len(words) < 20:
        return None
    paras = paragraphs or [{"start": m.start(), "end": m.end()} for m in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text)]
    lens = [len(_WORD_RX.findall(text[p["start"]:p["end"]])) for p in paras] or [len(words)]
    long_ = [(p, n) for p, n in zip(paras, lens) if n > 220]
    headings = len(_HEADING_RX.findall(text)); lists = len(_LIST_RX.findall(text))
    score = 1.0
    if lens and max(lens) > 220:
        score -= 0.25 * min(2, len(long_)) / 2
    if len(words) > 600 and headings == 0 and lists == 0:
        score -= 0.25
    if len(paras) == 1 and len(words) > 250:
        score -= 0.3
    ev = [{"span": _span(path, p["start"], p["end"]), "note": "%d words in one paragraph" % n} for p, n in long_[:3]]
    return {"key": "structure", "label": "structured", "score": round(_clamp(score), 3), "confidence": 0.5, "by": "paragraph shape · headings · lists",
            "on": "source", "evidence": ev, "detail": {"paragraphs": len(paras), "headings": headings, "lists": lists, "longest": max(lens) if lens else 0}}


def sources(text: str, path: str = "") -> Optional[Dict]:
    """How sourced the text is: links, citations and attributions per thousand words. Evidence: the links."""
    words = _WORD_RX.findall(text)
    if len(words) < 20:
        return None
    urls = list(_URL_RX.finditer(text)); cites = list(_CITE_RX.finditer(text))
    per_k = (len(urls) + len(cites)) / max(1, len(words)) * 1000
    ev = [{"span": _span(path, m.start(), m.end()), "note": "link"} for m in urls[:8]] + [{"span": _span(path, m.start(), m.end()), "note": "citation / attribution"} for m in cites[:8]]
    return {"key": "sources", "label": "sourced", "score": round(_clamp(per_k / 4.0), 3), "confidence": 0.55, "by": "links · citations · attributions per 1k words",
            "on": "source", "evidence": ev, "detail": {"links": len(urls), "citations": len(cites), "per_1k_words": round(per_k, 2)}}


def ai_likelihood_stylometry(text: str, path: str = "") -> Optional[Dict]:
    """A stylometric estimate — NOT a detector model. Machine prose tends to even sentence lengths (low burstiness),
    a thinner vocabulary for its length, few dashes / parentheses / questions, and a set of stock phrases. Each
    signal is weak; the sum is offered at low confidence with the phrases it found as evidence. A two-model
    detector (Binoculars) replaces this when one is registered."""
    sents = sentences(text); words = _WORD_RX.findall(text)
    if len(words) < 60 or len(sents) < 4:
        return None
    lens = [len(_WORD_RX.findall(text[s:e])) for s, e in sents]
    lens = [n for n in lens if n > 0] or [1]
    mean = sum(lens) / len(lens)
    cv = (sum((n - mean) ** 2 for n in lens) / len(lens)) ** 0.5 / max(1e-6, mean)       # burstiness: humans ~0.6+, models ~0.3-0.45
    low = [w.lower() for w in words]
    ttr = len(set(low[:400])) / max(1, min(400, len(low)))                                  # type-token ratio on a fixed window
    punct = sum(text.count(c) for c in ("—", "–", ";", "(", "?", "!", ":")) / max(1, len(words)) * 100
    lower_text = text.lower()
    found = []
    for ph in _AI_PHRASES:
        for m in re.finditer(re.escape(ph), lower_text):
            found.append((ph, m.start(), m.end()))
            if len(found) >= 12:
                break
        if len(found) >= 12:
            break
    starts = [text[s:e].strip().split(" ")[0].lower() for s, e in sents if text[s:e].strip()]
    rep_start = 1 - len(set(starts)) / max(1, len(starts))
    sig_burst = _clamp((0.55 - cv) / 0.35)                    # 1 when sentences are very even
    sig_ttr = _clamp((0.62 - ttr) / 0.25)                     # 1 when the vocabulary is thin
    sig_punct = _clamp((1.2 - punct) / 1.2)                   # 1 when there is almost no expressive punctuation
    sig_phr = _clamp(len(found) / max(4.0, len(words) / 150.0))
    score = 0.35 * sig_burst + 0.2 * sig_ttr + 0.15 * sig_punct + 0.2 * sig_phr + 0.1 * _clamp(rep_start * 2)
    ev = [{"span": _span(path, s, e), "note": "stock phrase: " + ph} for ph, s, e in found[:8]]
    return {"key": "ai_likelihood", "label": "AI-generated", "score": round(_clamp(score), 3), "confidence": 0.35,
            "by": "stylometry · heuristic (no model)", "on": "source", "evidence": ev,
            "detail": {"burstiness": round(cv, 3), "type_token": round(ttr, 3), "punct_per_100w": round(punct, 2), "stock_phrases": len(found), "repeated_starts": round(rep_start, 3)}}


def trust(assessments: List[Dict]) -> Optional[Dict]:
    """A composite of what was measured — sources, AI-likelihood inverted, readability, structure, language
    confidence, factual agreement when a checker ran — with the weights stated. The confidence is the lowest of its
    inputs': a composite is never surer than its weakest part."""
    by_key = {a["key"]: a for a in assessments if a.get("on") in (None, "", "source") and a.get("score") is not None}
    parts = []   # (weight, value, key)
    if "sources" in by_key: parts.append((0.3, by_key["sources"]["score"], "sources"))
    if "ai_likelihood" in by_key: parts.append((0.2, 1 - by_key["ai_likelihood"]["score"], "not AI-generated"))
    if "readability" in by_key: parts.append((0.15, by_key["readability"]["score"], "readability"))
    if "structure" in by_key: parts.append((0.1, by_key["structure"]["score"], "structure"))
    if "lang" in by_key: parts.append((0.1, by_key["lang"]["score"], "language"))
    if "factual" in by_key: parts.append((0.4, by_key["factual"]["score"], "factual"))
    if len(parts) < 2:
        return None
    wsum = sum(w for w, _, _ in parts)
    score = sum(w * v for w, v, _ in parts) / wsum
    conf = min(a.get("confidence", 0.5) for a in by_key.values() if a["key"] in ("sources", "ai_likelihood", "readability", "structure", "lang", "factual"))
    return {"key": "trust", "label": "trust", "score": round(_clamp(score), 3), "confidence": round(conf, 2),
            "by": "composite · " + " · ".join("%s %.0f%%" % (k, w / wsum * 100) for w, _, k in parts), "on": "source", "evidence": [],
            "detail": {k: round(v, 3) for _, v, k in parts}}


# ── code scorers ──────────────────────────────────────────────────────────────────────────────────
_BRANCH_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.Assert, ast.IfExp)
_JS_BRANCH_RX = re.compile(r"\b(if|for|while|case|catch)\b|&&|\|\||\?")


def _py_functions(text: str):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []

    def walk(node, qual):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.ClassDef):
                walk(c, (qual + "." if qual else "") + c.name)
            elif isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = (qual + "." if qual else "") + c.name
                out.append((q, c))
                walk(c, q)
            elif isinstance(c, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                walk(c, qual)
    walk(tree, "")
    return out


def _cyclomatic(fn) -> int:
    n = 1
    for sub in ast.walk(fn):
        if isinstance(sub, _BRANCH_NODES):
            n += 1
        elif isinstance(sub, ast.BoolOp):
            n += max(0, len(sub.values) - 1)
        elif isinstance(sub, (ast.comprehension,)):
            n += 1 + len(sub.ifs)
        elif hasattr(ast, "Match") and isinstance(sub, getattr(ast, "Match")):
            n += len(sub.cases)
    return n


def complexity(doc: Dict, texts: Dict[str, str]) -> List[Dict]:
    """Cyclomatic complexity per function (ast for Python; a branch-token count for JS). Per card: a badge when
    it is high; on the source: the share of functions at or under 10."""
    out: List[Dict] = []
    by_id = {c["id"]: c for c in doc.get("cards", [])}
    per: List[Tuple[str, int, Dict]] = []
    for path, text in texts.items():
        if path.endswith((".py", ".pyi")) or (not path.rsplit(".", 1)[-1] in ("js", "mjs", "cjs", "ts", "tsx", "jsx") and "def " in text):
            for qual, fn in _py_functions(text):
                cid = "%s::%s" % (path, qual)
                if cid in by_id:
                    per.append((cid, _cyclomatic(fn), by_id[cid]))
        elif path.rsplit(".", 1)[-1] in ("js", "mjs", "cjs", "ts", "tsx", "jsx"):
            for c in doc.get("cards", []):
                if c["span"].get("path") == path and c["kind"] in ("function", "method"):
                    body = text[c["span"]["start"]:c["span"]["end"]]
                    per.append((c["id"], 1 + len(_JS_BRANCH_RX.findall(body)), c))
    if not per:
        return out
    for cid, cc, card in per:
        if cc > 10:
            out.append({"key": "complexity", "label": "complexity", "score": round(_clamp(1 - (cc - 10) / 40.0), 3), "confidence": 0.9, "by": "cyclomatic",
                        "on": cid, "badge": "cc %d" % cc, "evidence": [{"span": card["span"], "note": "cyclomatic complexity %d" % cc}]})
    ok = sum(1 for _, cc, _ in per if cc <= 10)
    worst = sorted(per, key=lambda t: -t[1])[:5]
    out.append({"key": "complexity", "label": "simple", "score": round(ok / len(per), 3), "confidence": 0.9, "by": "cyclomatic · share of functions ≤ 10", "on": "source",
                "evidence": [{"span": c["span"], "note": "%s: %d" % (c["title"], cc)} for _, cc, c in worst if cc > 10],
                "detail": {"functions": len(per), "at_or_under_10": ok, "max": worst[0][1] if worst else 0}})
    return out


_SMELLS = [
    (re.compile(r"\beval\s*\("), "eval()", 0.9), (re.compile(r"\bexec\s*\("), "exec()", 0.9),
    (re.compile(r"shell\s*=\s*True"), "subprocess with shell=True", 0.8), (re.compile(r"\bos\.system\s*\("), "os.system()", 0.8),
    (re.compile(r"\bpickle\.loads?\s*\("), "pickle.load — arbitrary code on load", 0.8), (re.compile(r"\byaml\.load\s*\((?![^)]*Loader)"), "yaml.load without a Loader", 0.7),
    (re.compile(r"\bhashlib\.md5\s*\("), "md5 (not for anything security-relevant)", 0.4), (re.compile(r"verify\s*=\s*False"), "TLS verification off", 0.7),
    (re.compile(r"(?i)\b(password|passwd|secret|api_key|apikey|token)\s*=\s*['\"][^'\"]{6,}['\"]"), "a credential in the source", 0.9),
    (re.compile(r"\.innerHTML\s*=(?!=)"), "innerHTML assignment (XSS surface)", 0.6), (re.compile(r"\bdocument\.write\s*\("), "document.write()", 0.6),
    (re.compile(r"\bnew\s+Function\s*\("), "new Function()", 0.9), (re.compile(r"\bdangerouslySetInnerHTML\b"), "dangerouslySetInnerHTML", 0.6),
]


def smells(doc: Dict, texts: Dict[str, str]) -> List[Dict]:
    """Security-relevant patterns, each an evidence span on the card that holds it. Patterns, not a scanner:
    a hit is a place to look, the absence of hits is not a clean bill."""
    out: List[Dict] = []
    cards = [c for c in doc.get("cards", []) if c["kind"] in ("function", "method", "class", "module")]
    hits: Dict[str, List[Dict]] = {}
    n_all = 0
    for path, text in texts.items():
        for rx, what, sev in _SMELLS:
            for m in rx.finditer(text):
                n_all += 1
                # the innermost card that holds the span
                holder = None
                for c in cards:
                    sp = c["span"]
                    if sp.get("path") == path and sp["start"] <= m.start() < sp["end"] and c["kind"] != "module":
                        if holder is None or (sp["end"] - sp["start"]) < (holder["span"]["end"] - holder["span"]["start"]):
                            holder = c
                if holder is None:
                    holder = next((c for c in cards if c["kind"] == "module" and c["span"].get("path") == path), None)
                if holder is None:
                    continue
                hits.setdefault(holder["id"], []).append({"span": _span(path, m.start(), m.end()), "note": what, "severity": sev})
    for cid, evs in hits.items():
        sev = max(e["severity"] for e in evs)
        out.append({"key": "smells", "label": "smell", "score": round(1 - sev, 3), "confidence": 0.5, "by": "security patterns", "on": cid,
                    "badge": "smell" + ("" if len(evs) == 1 else " ×%d" % len(evs)), "evidence": [{"span": e["span"], "note": e["note"]} for e in evs[:6]]})
    fns = max(1, sum(1 for c in cards if c["kind"] in ("function", "method")))
    all_ev = [e for evs in hits.values() for e in evs]
    out.append({"key": "smells", "label": "no smells", "score": round(_clamp(1 - n_all / (fns / 5.0 + 1)), 3), "confidence": 0.5, "by": "security patterns (a hit is a place to look)",
                "on": "source", "evidence": [{"span": e["span"], "note": e["note"]} for e in sorted(all_ev, key=lambda e: -e["severity"])[:10]], "detail": {"hits": n_all, "functions": fns}})
    return out


def clones(doc: Dict, texts: Dict[str, str], min_chars: int = 60) -> List[Dict]:
    """Functions whose bodies are the same once whitespace and comments are gone — a clone is a maintenance debt
    the diagram should show. Exact after normalisation; near-clones are a later scorer. min_chars keeps one-line
    getters out (a four-line body normalises to ~70 characters)."""
    out: List[Dict] = []
    groups: Dict[str, List[Dict]] = {}
    fns = [c for c in doc.get("cards", []) if c["kind"] in ("function", "method")]
    for c in fns:
        t = texts.get(c["span"].get("path") or "", "")
        body = t[c["span"]["start"]:c["span"]["end"]]
        body = re.sub(r"#.*|//.*", "", body)
        body = re.sub(r"^\s*(async\s+)?(def|function)\s+\w+", "", body.strip())   # the name is not the body
        norm = re.sub(r"\s+", "", body)
        if len(norm) < min_chars:
            continue
        groups.setdefault(hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest(), []).append(c)
    dup = [g for g in groups.values() if len(g) > 1]
    for g in dup:
        for c in g:
            others = [o["title"] for o in g if o is not c]
            out.append({"key": "clones", "label": "clone", "score": 0.0, "confidence": 0.85, "by": "normalised body hash", "on": c["id"], "badge": "clone",
                        "evidence": [{"span": o["span"], "note": "the same body as " + o["title"]} for o in g if o is not c][:4]})
    n_in = sum(len(g) for g in dup)
    out.append({"key": "clones", "label": "no clones", "score": round(1 - n_in / max(1, len(fns)), 3), "confidence": 0.85, "by": "normalised body hash",
                "on": "source", "evidence": [{"span": g[0]["span"], "note": " = ".join(o["title"] for o in g)} for g in dup[:6]], "detail": {"functions": len(fns), "in_clone_sets": n_in, "sets": len(dup)}})
    return out


def tests_reference(doc: Dict, test_corpus: str) -> List[Dict]:
    """Which public functions and classes the test corpus names at all — a coarse, honest 'is it tested'. Per card:
    a `tested` badge; on the source: the share of public symbols named."""
    out: List[Dict] = []
    if not test_corpus:
        return out
    syms = [c for c in doc.get("cards", []) if c["kind"] in ("function", "method", "class") and not c["title"].startswith("_") and len(c["title"]) >= 4]
    if not syms:
        return out
    named = 0
    for c in syms:
        if re.search(r"\b" + re.escape(c["title"]) + r"\b", test_corpus):
            named += 1
            out.append({"key": "tests", "label": "tested", "score": 1.0, "confidence": 0.5, "by": "named in tests/", "on": c["id"], "badge": "tested", "evidence": []})
    out.append({"key": "tests", "label": "tested", "score": round(named / len(syms), 3), "confidence": 0.5, "by": "share of public symbols named in tests/", "on": "source",
                "evidence": [], "detail": {"public_symbols": len(syms), "named": named}})
    return out


def provenance(doc: Dict, git_facts: Dict[str, Dict]) -> List[Dict]:
    """git facts per file ({path: {sha, author, date}}) → a field on each module card and a source score: the share
    of files under version control."""
    out: List[Dict] = []
    mods = [c for c in doc.get("cards", []) if c["kind"] == "module"]
    if not mods:
        return out
    tracked = 0
    for c in mods:
        f = git_facts.get(c["span"].get("path") or "")
        if f and f.get("sha"):
            tracked += 1
            out.append({"key": "provenance", "label": "provenance", "score": 1.0, "confidence": 0.95, "by": "git log", "on": c["id"],
                        "badge": "%s · %s" % (f.get("date", "")[:10], f.get("sha", "")[:7]), "evidence": [{"span": c["span"], "note": "last change %s by %s (%s)" % (f.get("date", ""), f.get("author", ""), f.get("sha", "")[:7])}]})
    out.append({"key": "provenance", "label": "under git", "score": round(tracked / len(mods), 3), "confidence": 0.95, "by": "git log", "on": "source", "evidence": [],
                "detail": {"files": len(mods), "tracked": tracked}})
    return out


def code_summary(assessments: List[Dict]) -> Optional[Dict]:
    """A composite for code — parses · resolved · simple · no smells · no clones · tested — with the weights stated."""
    by_key = {a["key"]: a for a in assessments if a.get("on") in (None, "", "source") and a.get("score") is not None}
    w = {"syntax": 0.25, "resolved": 0.1, "complexity": 0.15, "smells": 0.2, "clones": 0.1, "tests": 0.2}
    parts = [(w[k], by_key[k]["score"], k) for k in w if k in by_key]
    if len(parts) < 2:
        return None
    wsum = sum(p[0] for p in parts)
    return {"key": "health", "label": "health", "score": round(sum(p[0] * p[1] for p in parts) / wsum, 3), "confidence": round(min(by_key[k]["confidence"] for _, _, k in parts), 2),
            "by": "composite · " + " · ".join("%s %.0f%%" % (k, ww / wsum * 100) for ww, _, k in parts), "on": "source", "evidence": [], "detail": {k: round(v, 3) for _, v, k in parts}}
