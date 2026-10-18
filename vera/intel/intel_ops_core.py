"""Intelligence - pure rules for the operational layer (no app, no I/O).

Kept apart from intel_core (the topic/signal/report model) so each stays
readable: this module holds what the n8n flows did and what the estate offers -
the analyst persona, NLP-tier scoring, competitor watch, the cross-topic brief,
drop-folder planning (notes / inbox / print / podcast), the automation advisor,
the text command, sessions and material-change alerts.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── the reader ───────────────────────────────────────────────────────────────

DEFAULT_PERSONA = (
    "You are an intelligence analyst supporting an AI & automation engineer at Bidwells, a UK property "
    "consultancy (commercial, residential, rural, and science & technology real estate across the "
    "Oxford-Cambridge arc). Be concrete and sceptical. Prefer what is new over what is merely mentioned. "
    "British English. Never invent facts that are not in the material given.")

#: the n8n source groups and the names their brief gave them
N8N_GROUP_NAMES = {
    "ai_ml": "AI & ML",
    "ai_real_estate": "AI in real estate, labs & industrial",
    "ai_itops": "AI in IT operations, service desk, security & DevOps",
    "competitors": "Competitor watch",
    "notes": "From your notes",
    "general": "General",
}

NOTES_TOPIC = "from-your-notes"
SAVED_TOPIC = "saved"

#: entity types GLiNER is asked for
GLINER_LABELS = ["organization", "company", "person", "product", "technology", "software",
                 "law", "regulation", "government agency", "event", "money", "location"]

# GLiNER is zero-shot over its labels, and it scores a descriptive label better than
# a bare word, so each kind of topic asks for the things that kind of topic is about.
LABEL_SETS: Dict[str, List[str]] = {
    "general": GLINER_LABELS,
    "finance": ["company", "stock market index", "currency", "central bank", "financial regulator",
                "economic policy", "commodity", "cryptocurrency", "investment fund", "person",
                "government agency", "money"],
    "property": ["property developer", "real estate investment trust", "property agency or consultancy",
                 "planning authority", "development site", "building or scheme", "company", "investor",
                 "person", "location", "money", "law or regulation"],
    "osint": ["vessel", "aircraft", "military unit or armed group", "military facility", "weapon system",
              "government agency", "political leader", "person", "city or region", "country", "event",
              "organization"],
    "tech": ["company", "AI model", "software product", "open source project", "hardware or chip",
             "research lab", "programming language or framework", "person", "technology standard",
             "law or regulation", "money"],
}
_KIND_WORDS: Dict[str, Tuple[str, ...]] = {
    "finance": ("market", "stock", "equity", "bond", "rate", "inflation", "fed ", "central bank", "crypto",
                "bitcoin", "forex", "currency", "earnings", "macro", "commodit", "oil price", "yield"),
    "property": ("property", "real estate", "estate agent", "reit", "planning", "valuation", "landlord",
                 "housing", "residential", "commercial real", "lettings", "industrial", "development site",
                 "proptech"),
    "osint": ("military", "conflict", "war ", "vessel", "shipping", "aircraft", "sanction", "osint",
              "geopolit", "border", "missile", "navy", "troops", "election"),
    "tech": (" ai ", "ai-", "llm", "model", "software", "open source", "github", "chip", "gpu", "agentic",
             "machine learning", "developer", "framework", "robot"),
}
TOPIC_KINDS = tuple(LABEL_SETS)


def topic_kind(topic: Dict[str, Any]) -> str:
    """The topic's kind: what the user set, otherwise the kind whose words its name,
    brief and searches use most (general when nothing stands out)."""
    k = str((topic or {}).get("kind") or "").strip().lower()
    if k in LABEL_SETS:
        return k
    text = " " + " ".join([str(topic.get("name") or ""), str(topic.get("brief") or ""),
                           " ".join(topic.get("queries") or [])]).lower() + " "
    votes = {kind: sum(text.count(w) for w in words) for kind, words in _KIND_WORDS.items()}
    best = max(votes.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else "general"


def topic_labels(topic: Dict[str, Any], limit: int = 16) -> List[str]:
    """The GLiNER labels for a topic: its own first, then its kind's."""
    out: List[str] = []
    for lab in list((topic or {}).get("labels") or []) + LABEL_SETS[topic_kind(topic or {})]:
        lab = str(lab).strip()
        if lab and lab.lower() not in (x.lower() for x in out):
            out.append(lab)
    return out[:limit]


def _dice(a: str, b: str) -> float:
    def grams(s: str) -> Dict[str, int]:
        s = re.sub(r"[^a-z0-9 ]", "", (s or "").lower())
        g: Dict[str, int] = {}
        for i in range(len(s) - 1):
            g[s[i:i + 2]] = g.get(s[i:i + 2], 0) + 1
        return g
    ga, gb = grams(a), grams(b)
    na, nb = sum(ga.values()), sum(gb.values())
    if not na + nb:
        return 0.0
    hit = sum(min(v, gb.get(k, 0)) for k, v in ga.items())
    return 2 * hit / (na + nb)


# ── NLP-tier scoring ─────────────────────────────────────────────────────────

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest(vec: Sequence[float], others: Sequence[Tuple[str, Sequence[float]]]) -> Tuple[str, float]:
    """The most similar earlier signal (id, cosine)."""
    best_id, best = "", 0.0
    for oid, ov in others:
        c = cosine(vec, ov)
        if c > best:
            best_id, best = oid, c
    return best_id, best


def squash(x: Any) -> float:
    """A reranker score as 0..1 - cross-encoders return logits, some servers
    return probabilities; both come out on the same scale."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 <= v <= 1.0:
        return v
    return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, v))))


def blend_score(*, relevance: Optional[float], heuristic: float, novelty: float) -> float:
    """0-10: the reranker's judgement leads when there is one; the keyword/recency
    heuristic and novelty refine it. Without a reranker the heuristic stands."""
    if relevance is None:
        return round(heuristic, 1)
    s = 10 * (0.6 * relevance + 0.25 * min(1.0, heuristic / 10) + 0.15 * novelty)
    return round(max(0.0, min(10.0, s)), 1)


def question_text(q: Any) -> str:
    """An open question as text. The model sometimes writes questions as objects
    ({question, answer}); older topics hold them stringified ("{'question': ...}")."""
    if isinstance(q, dict):
        return str(q.get("question") or q.get("text") or "").strip()
    s = str(q or "").strip()
    if s.startswith("{") and "question" in s:
        try:
            import ast as _ast
            d = _ast.literal_eval(s)
            if isinstance(d, dict):
                return str(d.get("question") or "").strip()
        except (ValueError, SyntaxError):
            pass
    return s


def profile_text(topic: Dict[str, Any]) -> str:
    """What a reranker compares a signal with: the topic itself - name, what it is,
    why it matters, who it follows. Its open questions are scored separately
    (question_profile): written by the model about whatever it last read, they are
    narrow ("hydrology tools in flood-risk zones"), and mixed into one query they
    drowned the topic - 9 Oct 2026, "UK house prices fall" scored -4.1 against the
    mixed profile and +5.3 against the topic's name, and every signal fell below
    the model gate."""
    parts = [topic.get("name", ""), topic.get("brief", ""), topic.get("why", "")]
    parts += (topic.get("entities") or [])[:6]
    return ". ".join(p for p in parts if p)[:1200]


def question_profile(topic: Dict[str, Any]) -> str:
    """The topic's open questions as one rerank query ('' when it has none): a source
    that answers one of them is relevant even when it reads off the topic's centre."""
    qs = [question_text(q) for q in (topic.get("questions") or [])[:5]]
    return " ".join(q for q in qs if q)[:1200]


#: tokens that come from page chrome or are too generic to be an entity: browser
#: user-agent strings left in a page, and words every source in a topic repeats
ENTITY_STOP = {"ai", "a.i.", "artificial intelligence", "machine learning", "ml", "llm", "llms", "genai",
               "mozilla", "chrome", "safari", "applewebkit", "khtml", "gecko", "firefox", "edge", "windows nt",
               "javascript", "cookies", "cookie", "privacy policy", "newsletter", "linkedin", "twitter", "facebook",
               "x", "youtube", "instagram", "email", "home", "menu", "login", "sign in", "subscribe"}


def entity_ok(word: str) -> bool:
    w = word.strip().lower()
    if not w or w in ENTITY_STOP or len(w) < 2:
        return False
    if re.fullmatch(r"[$£€]?[\d.,]+\s*(%|bn|m|k|billion|million|thousand)?", w):
        return False                  # a bare amount is not an entity
    return True


def ner_entities(result: Dict[str, Any], min_score: float = 0.45, limit: int = 20) -> List[str]:
    """Distinct entity strings from an nlp.ner answer, best first, without page
    chrome or generic words."""
    seen: Dict[str, float] = {}
    for e in (result or {}).get("entities") or []:
        if not isinstance(e, dict):
            continue
        w = re.sub(r"\s+", " ", str(e.get("word") or e.get("text") or "")).strip(" .,;:'\"")
        try:
            sc = float(e.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        if len(w) < 2 or sc < min_score or w.lower() in ("the", "a", "an") or not entity_ok(w):
            continue
        if w not in seen or sc > seen[w]:
            seen[w] = sc
    return [w for w, _ in sorted(seen.items(), key=lambda kv: -kv[1])][:limit]


_YEAR = re.compile(r"(?<!\d)(20[0-4]\d|19[89]\d)(?!\d)")


def newest_year(text: str, now_year: int) -> Optional[int]:
    """The latest plausible year a text mentions (no later than next year)."""
    ys = [int(y) for y in _YEAR.findall(str(text or "")) if int(y) <= now_year + 1]
    return max(ys) if ys else None


def staleness_factor(signal: Dict[str, Any], now_year: int) -> float:
    """Undated text whose newest year is two or more years back is probably old
    news: 0.7. Signals with a publication date are judged by it elsewhere."""
    if signal.get("published"):
        return 1.0
    y = newest_year(f"{signal.get('title') or ''} {signal.get('snippet') or ''}", now_year)
    return 0.7 if (y is not None and y <= now_year - 2) else 1.0


def entity_types(result: Dict[str, Any], keep: Iterable[str]) -> Dict[str, str]:
    """{entity: label} for the entities kept, from the best-scoring mention of each."""
    keep_s = set(keep)
    best: Dict[str, Tuple[float, str]] = {}
    for e in (result or {}).get("entities") or []:
        if not isinstance(e, dict):
            continue
        w = re.sub(r"\s+", " ", str(e.get("word") or e.get("text") or "")).strip(" .,;:'\"")
        lab = str(e.get("entity") or e.get("label") or e.get("entity_group") or "").strip()
        if w not in keep_s or not lab:
            continue
        try:
            sc = float(e.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        if w not in best or sc > best[w][0]:
            best[w] = (sc, lab)
    return {w: lab for w, (_, lab) in best.items()}


# ── which named organisations have a price ───────────────────────────────────

# Entity types (from the topic label sets) that can be a listed company or fund.
ORG_LABELS = {"company", "organization", "property developer", "real estate investment trust",
              "property agency or consultancy", "investor", "investment fund", "research lab",
              "hardware or chip", "property firm", "cryptocurrency"}
_ORG_SUFFIX = re.compile(r"\b(plc|p\.l\.c|inc|incorporated|corp|corporation|ltd|limited|group|holdings?|"
                         r"co|company|sa|ag|nv|se|llc|the|class [a-c])\b\.?")
LISTED = {"stock", "etf", "equity", "crypto", "fund", None, ""}


def org_key(name: str) -> str:
    """A company name without legal suffixes and punctuation: 'Savills plc' -> 'savills'."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower().replace("&", " and "))
    s = _ORG_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# The few brands whose listing goes by another name.
ALIASES = {"google": "alphabet", "meta": "meta platforms", "facebook": "meta platforms", "amazon": "amazon com",
           "jll": "jones lang lasalle", "ibm": "international business machines", "tsmc": "taiwan semiconductor manufacturing",
           "landsec": "land securities", "british land": "british land company"}


def name_match(entity: str, listed_name: str, symbol: str = "") -> float:
    """How surely a listing IS the named organisation: 1 for the same name (after
    legal suffixes), a known alias, or a name that IS the ticker; otherwise the
    bigram overlap (a prefix is not enough: "Modus" is not "Modus Therapeutics")."""
    a, b = org_key(entity), org_key(listed_name)
    if not a or not b:
        return 0.0
    if a == b or ALIASES.get(a) == b:
        return 1.0
    root = str(symbol or "").split(".")[0].split("-")[0].lower()
    if root and a.replace(" ", "") == root and "/" not in symbol:
        return 1.0
    return round(min(_dice(a, b), 0.79), 3)


def org_mentions(signals: Sequence[Dict[str, Any]], *, follow: Sequence[str] = (), min_mentions: int = 2,
                 limit: int = 8) -> List[Tuple[str, int]]:
    """The organisations a topic's sources name most (each signal counts once).
    Typed entities count only when their type can be listed; untyped ones (older
    signals) count, and the listing match filters them later. Followed entities
    always come first."""
    counts: Dict[str, int] = {}
    names: Dict[str, str] = {}
    for s in signals:
        types = s.get("entity_types") or {}
        seen = set()
        for e in s.get("entities") or []:
            if types and types.get(e) not in ORG_LABELS:
                continue
            k = org_key(e)
            if not k or k in seen or not entity_ok(e):
                continue
            seen.add(k)
            counts[k] = counts.get(k, 0) + 1
            names.setdefault(k, e)
    out = [(f, 999) for f in follow if org_key(f)]
    have = {org_key(f) for f, _ in out}
    out += [(names[k], n) for k, n in sorted(counts.items(), key=lambda kv: -kv[1])
            if n >= min_mentions and k not in have]
    return out[:limit]


def pick_listing(entity: str, results: Sequence[Dict[str, Any]], *, crypto: bool = False,
                 threshold: float = 0.95) -> Optional[Dict[str, Any]]:
    """The listing for a named organisation: the lookup's first stock (or, when the
    entity is a cryptocurrency, coin) whose name or ticker IS the organisation's.
    Exchange perps named after a company and ETFs named after a theme never count."""
    for r in results or []:
        sym, cls = str(r.get("symbol") or ""), r.get("asset_class")
        if r.get("provider") == "custom" or cls not in LISTED:
            continue
        if ("/" in sym or cls == "crypto") != crypto:
            continue
        if cls == "etf" and org_key(entity) != org_key(r.get("name") or ""):
            continue
        if name_match(entity, r.get("name") or sym, sym) >= threshold:
            return r
    return None


def qa_answer(question: str, result: Dict[str, Any], threshold: float = 0.35) -> Optional[Dict[str, Any]]:
    """An extractive answer worth showing (a span with a score), or None."""
    r = result or {}
    ans = str(r.get("answer") or "").strip()
    try:
        sc = float(r.get("score") or 0)
    except (TypeError, ValueError):
        sc = 0.0
    if not ans or sc < threshold or len(ans) < 2:
        return None
    return {"question": question, "answer": ans[:400], "score": round(sc, 3), "extractive": True}


#: what kind of page a hit is - descriptive sentences, because zero-shot NLI scores
#: sentences far better than bare words (intent_zeroshot_core)
GENRE_LABELS = {
    "news": "a news report or an analysis article about developments",
    "research": "an academic research paper or technical documentation",
    "marketing": "a page selling a product, service or paid market research report",
    "listing": "a listing, directory, rental or job advert page",
    "repo": "a software repository or code project page",
    "index": "a news index, section front or aggregator page listing many headlines or links",
}
_GENRE_OF = {v: k for k, v in GENRE_LABELS.items()}
#: genres that are rarely intelligence: their score is scaled down, and they never
#: reach the model unless the topic asks for vendors
LOW_GENRES = {"marketing": 0.5, "listing": 0.35, "index": 0.4}

_INDEX_PATH = re.compile(r"^/?(news|latest|updates|topics?|blog|insights|articles|h)(/[^/]*)?/?$", re.I)
_INDEX_TITLE = re.compile(r"\b(latest\s+)?news\s*(and|&|,)\s*(updates|analysis|house prices|views)\b|"
                          r"\bnews,|\bnews\s*[|-]|\bnewsnow\b|\bheadlines\b|\b(property|housing|market)\s+news\b", re.I)


def index_hint(url: str, title: str) -> bool:
    """A page that is a list of stories rather than a story: a section front or
    aggregator by its address (/news, /latest, /h/...) together with a title that
    names news in general ('News & Updates', 'Property News', '- NewsNow')."""
    from urllib.parse import urlsplit
    try:
        path = urlsplit(str(url or "")).path or "/"
    except ValueError:
        return False
    t = str(title or "")
    by_path = bool(_INDEX_PATH.match(path)) or path in ("", "/")
    return bool(_INDEX_TITLE.search(t)) and (by_path or "newsnow" in str(url).lower())


# Pages that sell market-research reports ("UK Commercial Real Estate Market Size &
# Trends 2031", "Industry Analysis, Size, Share, Growth ... Forecast 2025-2032"): on-topic
# by every word, worth nothing as intelligence, and the classifier read them as research
# because they call themselves reports (10 Oct 2026: five of the ten items that passed
# one sweep). Their titles have one shape, so a pattern decides, as for index pages.
_REPORT_SELLER = re.compile(
    r"\bmarket\s+(?:size|share|report|outlook\s*&\s*report|research\s+report)\b.*\b20\d\d\b"
    r"|\b(?:industry\s+analysis|size,?\s+share|share,?\s+growth|growth,?\s+trends)\b"
    r"|\bcagr\b|\brequest\s+(?:a\s+)?(?:free\s+)?sample\b|\bforecast\s+(?:to\s+|20\d\d\s*[-–]\s*)20\d\d\b"
    r"|\bmarket\s+size\b",
    re.I)


def report_seller_hint(title: str, snippet: str = "") -> bool:
    """A page selling a market-research report (see _REPORT_SELLER) - genre marketing."""
    return bool(_REPORT_SELLER.search(f"{title or ''} {snippet or ''}"))


def genre_of(result: Dict[str, Any], threshold: float = 0.5) -> Tuple[str, float]:
    """(genre, p) from an nlp.zeroshot answer over GENRE_LABELS; ('', 0) when unsure."""
    best, p = "", 0.0
    for row in (result or {}).get("labels") or []:
        if not isinstance(row, dict):
            continue
        g = _GENRE_OF.get(str(row.get("label") or ""))
        try:
            sc = float(row.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        if g and sc > p:
            best, p = g, sc
    if not best:
        g = _GENRE_OF.get(str((result or {}).get("top") or ""))
        best, p = (g, 0.5) if g else ("", 0.0)
    return (best, round(p, 3)) if p >= threshold else ("", round(p, 3))


def genre_factor(genre: str, vendors_ok: bool = False) -> float:
    if vendors_ok and genre == "marketing":
        return 1.0
    return LOW_GENRES.get(genre, 1.0)


def stance(result: Dict[str, Any]) -> str:
    top = str((result or {}).get("top") or "").lower()
    return {"negative": "negative", "neutral": "neutral", "positive": "positive",
            "label_0": "negative", "label_1": "neutral", "label_2": "positive"}.get(top, "")


# ── competitor watch ─────────────────────────────────────────────────────────

def competitor_hits(text: str, entities: Iterable[str], competitors: Sequence[str]) -> List[str]:
    """Which watched firms a signal names (whole words, case-insensitive)."""
    hay = " ".join([text or ""] + [str(e) for e in (entities or [])])
    out: List[str] = []
    for c in competitors or []:
        c = str(c).strip()
        if c and re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", hay, re.I) and c not in out:
            out.append(c)
    return out


# ── the brief: a model writes the top line across topics ────────────────────

def brief_prompt(date: str, sections: Sequence[Dict[str, Any]], competitors: Sequence[Dict[str, Any]],
                 words: int = 500) -> str:
    lines: List[str] = []
    for s in sections:
        lines.append(f"## {s.get('topic')}")
        if s.get("headline"):
            lines.append(f"Assessment: {s['headline']}")
        for c in (s.get("changed") or [])[:4]:
            lines.append(f"- changed: {c}")
        for sig in (s.get("signals") or [])[:6]:
            lines.append((f"- [{sig.get('score')}] {sig.get('title')}: {sig.get('summary') or sig.get('snippet') or ''}")[:400]
                         + f" ({sig.get('url')})")
    if competitors:
        lines.append("## Competitor mentions")
        for c in competitors[:10]:
            lines.append(f"- {c.get('firm')}: {c.get('title')} ({c.get('url')})")
    return (
        f"Write the top of today's intelligence brief for {date} from the material below.\n"
        f"Answer in JSON only. Your whole answer is about {words} words.\n"
        "JSON keys:\n"
        "  top_line   - exactly 3 strings: the most important things across ALL topics, each ending with its\n"
        "               source URL in parentheses\n"
        "  follow_ups - up to 4 concrete research questions worth chasing next (strings)\n"
        "  competitor_watch - one sentence on what competitors did, or \"\" if nothing\n\n"
        "Drop anything plainly old or irrelevant. Never invent facts.\n\nMATERIAL:\n" + "\n".join(lines)[:14000])


def apply_brief(doc: Dict[str, Any], parsed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    p = parsed or {}
    top = [str(x).strip() for x in (p.get("top_line") or []) if str(x).strip()][:3]
    fu = [str(x).strip() for x in (p.get("follow_ups") or []) if str(x).strip()][:4]
    cw = str(p.get("competitor_watch") or "").strip()
    out = dict(doc)
    if top:
        out["top_line"] = top
    if fu:
        out["follow_ups"] = fu
    if cw:
        out["competitor_note"] = cw[:600]
    return out


#: The background queue's busy reasons (background_work.defer_reason / quiet_gate), said for a reader.
#: First match wins - the quiet-gate and GPU-cooldown reasons mention "census" and "interactive" in passing.
_BLOCKERS = (("of quiet so far", "the box never had ten quiet minutes"), ("gpu gate", "the models were busy"),
             ("census", "a census was running"), ("agent loop", "an agent loop was running"),
             ("dream", "a dream cycle was running"), ("interactive", "you were using Vera"))


def blocker_phrase(reason: str) -> str:
    """The background queue's busy reason as a short phrase for the brief; '' when there is none."""
    r = str(reason or "").lower()
    if not r:
        return ""
    return next((say for key, say in _BLOCKERS if key in r), "other work was running")


def night_note(night: Dict[str, Any], blocker: str = "") -> str:
    """What the morning brief owes the reader when the night did not finish:
    how much was refreshed, why the rest waited (the queue's busy reason at brief
    time, when known) and that the rest follows."""
    n = night or {}
    total = int((n.get("stats") or {}).get("topics") or 0)
    left = len(n.get("pending") or [])
    if not total or not left:
        return ""
    done = max(0, total - left)
    why = blocker_phrase(blocker)
    why = (f"background work needs ten quiet minutes and the night had none (at brief time {why})" if why
           else "background work waits until Vera is quiet")
    return (f"Overnight: {done} of {total} topics refreshed. The other {left} are still queued - {why} - and will "
            f"follow in a later message.")


def night_summary(stats: Dict[str, Any]) -> str:
    s = stats or {}
    msg = (f"Intel night: {s.get('topics', 0)} topics, {s.get('searched', 0)} searches, {s.get('new', 0)} new, "
           f"{s.get('assessed', 0)} assessed")
    if s.get("notes"):
        msg += f", {s['notes']} notes"
    if s.get("yielded"):
        msg += f", {s['yielded']} paused for you"
    if s.get("failed"):
        msg += f", {s['failed']} failed"
    return msg + "."


# ── drop folders (notes / inbox / print / podcast) ──────────────────────────

FOLDER_KINDS = ("notes", "inbox", "print", "podcast")
_TEXT_EXT = re.compile(r"\.(md|markdown|txt)$", re.I)
_INBOX_EXT = re.compile(r"\.(md|markdown|txt|csv|json|log|ya?ml)$", re.I)
MAX_FILE_BYTES = 400_000


def norm_text(t: str) -> str:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in str(t or "").replace("\r\n", "\n").split("\n")]
    return "\n".join(x for x in lines if x)


def content_hash(t: str) -> str:
    return hashlib.sha1(norm_text(t).encode("utf-8")).hexdigest()[:16]


def cyrb53(text: str, seed: int = 0) -> str:
    """The hash n8n's files-night stored (cyrb53 over UTF-16 code units), so its
    'already handled' marks can be matched to exact content."""
    M = 0xFFFFFFFF

    def imul(a: int, b: int) -> int:
        return (a * b) & M
    h1, h2 = (0xdeadbeef ^ seed) & M, (0x41c6ce57 ^ seed) & M
    data = (text or "").encode("utf-16-le")
    for i in range(0, len(data), 2):
        ch = data[i] | (data[i + 1] << 8)
        h1 = imul(h1 ^ ch, 2654435761)
        h2 = imul(h2 ^ ch, 1597334677)
    h1 = imul(h1 ^ (h1 >> 16), 2246822507)
    h1 ^= imul(h2 ^ (h2 >> 13), 3266489909)
    h2 = imul(h2 ^ (h2 >> 16), 2246822507)
    h2 ^= imul(h1 ^ (h1 >> 13), 3266489909)
    return format(4294967296 * (2097151 & h2) + (h1 & M), "x")


def wanted_file(kind: str, name: str, size: Any) -> bool:
    if not name or name.startswith((".", "_")):
        return False
    try:
        if int(size or 0) > MAX_FILE_BYTES:
            return False
    except (TypeError, ValueError):
        pass
    return bool((_INBOX_EXT if kind == "inbox" else _TEXT_EXT).search(name))


def plan_file(kind: str, path: str, text: str, known_note: Optional[Dict[str, Any]], seen: bool) -> Optional[Dict[str, Any]]:
    """What one file needs tonight, or None. Notes are diffed against what was
    known (a reword of every changed line is cosmetic and never reaches a model);
    anything else runs once per content hash."""
    n = norm_text(text)
    if not n:
        return None
    h = content_hash(n)
    base = {"path": path, "name": path.rsplit("/", 1)[-1], "hash": h, "norm": n}
    if kind == "notes":
        if known_note and known_note.get("hash") == h:
            return None
        if not known_note:
            return dict(base, kind="note_new", llm=True)
        old = [x for x in str(known_note.get("norm") or "").split("\n") if x]
        a, b = set(old), set(n.split("\n"))
        added = [x for x in n.split("\n") if x not in a]
        removed = [x for x in old if x not in b]
        if not added and not removed:
            return dict(base, kind="note_trivial", llm=False, prev=known_note)
        cosmetic = len(added) == len(removed) and all(any(_dice(x, r) >= 0.8 for r in removed) for x in added)
        return dict(base, kind="note_trivial" if cosmetic else "note_changed", llm=not cosmetic,
                    prev=known_note, added=added[:200], removed=removed[:100])
    if seen:
        return None
    return dict(base, kind=kind, llm=kind == "inbox")


def note_prompt(item: Dict[str, Any]) -> str:
    k = item.get("kind")
    if k == "note_new":
        return "\n".join([
            f"NEW NOTE: {item['name']}", "", "CONTENT:", item["norm"][:12000], "",
            "Your whole answer is about 700 words of JSON.",
            "Return ONLY a JSON object: title, summary (3-5 sentences), entities (array), action_items (array of short "
            "imperative lines), questions (array of open questions the note raises), search_terms (array of 3-8 short "
            "search queries that would keep this topic monitored), follow_ups (array of 1-3 things worth researching next)."])
    if k == "note_changed":
        prev = item.get("prev") or {}
        return "\n".join([
            f"NOTE CHANGED: {item['name']}", "", f"WHAT WE ALREADY KNEW (summary): {prev.get('summary', '')}",
            f"EXISTING ACTION ITEMS: {json.dumps(prev.get('action_items') or [])}", "",
            "LINES ADDED:", "\n".join(item.get("added") or []) or "(none)", "",
            "LINES REMOVED:", "\n".join(item.get("removed") or []) or "(none)", "",
            "If the change is cosmetic and adds no new fact, decision or task, return ONLY {\"trivial\": true}. Otherwise",
            "return ONLY a JSON object (about 500 words): delta_summary (1-3 sentences), summary (the UPDATED overall "
            "summary), new_action_items (array), resolved_action_items (array), new_search_terms (array), "
            "follow_ups (array, max 2)."])
    return "\n".join([
        f"DOCUMENT DROPPED IN THE INBOX: {item['name']}", "", "CONTENT:", item["norm"][:14000], "",
        "Your whole answer is about 400 words of JSON.",
        "Return ONLY a JSON object: title, summary (3-6 sentences), doc_type (note|report|data|log|other), entities "
        "(array), action_items (array), search_terms (array of 0-5 queries worth monitoring), keep (true if worth "
        "keeping in the data fabric long term)."])


def _strs(v: Any, n: int = 20) -> List[str]:
    out: List[str] = []
    for x in v if isinstance(v, list) else []:
        s = x if isinstance(x, str) else (isinstance(x, dict) and (x.get("text") or x.get("task") or x.get("item")
                                                                    or x.get("title"))) or ""
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    return out[:n]


def apply_note(item: Dict[str, Any], parsed: Optional[Dict[str, Any]], now: str) -> Tuple[Dict[str, Any], List[str], str]:
    """(the note record to keep, new search terms, a one-line report) for a note.
    A failed or 'trivial' answer still records the new hash so it is never re-read."""
    p = parsed or {}
    prev = item.get("prev") or {}
    hist = list(prev.get("history") or [])
    rec = dict(prev, path=item["path"], name=item["name"], hash=item["hash"], norm=item["norm"], updated=now,
               revisions=int(prev.get("revisions") or 0) + 1)
    if item["kind"] == "note_new" and p.get("summary"):
        rec.update(title=str(p.get("title") or item["name"]), summary=str(p["summary"]),
                   entities=_strs(p.get("entities")), action_items=_strs(p.get("action_items")),
                   questions=_strs(p.get("questions")), search_terms=_strs(p.get("search_terms")),
                   follow_ups=_strs(p.get("follow_ups"), 3), first_seen=now, revisions=1,
                   history=[{"at": now, "hash": item["hash"], "kind": "new"}])
        return rec, rec["search_terms"], f"NEW note: {rec['title']} - {rec['summary'][:160]}"
    if item["kind"] == "note_changed" and not p.get("trivial") and (p.get("summary") or p.get("delta_summary")):
        resolved = set(_strs(p.get("resolved_action_items")))
        terms = _strs(p.get("new_search_terms"))
        rec.update(summary=str(p.get("summary") or prev.get("summary") or ""),
                   action_items=[a for a in prev.get("action_items") or [] if a not in resolved] + _strs(p.get("new_action_items")),
                   search_terms=list(dict.fromkeys((prev.get("search_terms") or []) + terms)),
                   follow_ups=_strs(p.get("follow_ups"), 2),
                   history=(hist + [{"at": now, "hash": item["hash"], "kind": "changed",
                                     "delta": str(p.get("delta_summary") or "")[:400],
                                     "added": len(item.get("added") or []), "removed": len(item.get("removed") or [])}])[-20:])
        return rec, terms, f"CHANGED note: {prev.get('title') or item['name']} - {str(p.get('delta_summary') or '')[:160]}"
    if item["kind"] != "note_trivial" and not p.get("trivial"):
        tries = int(prev.get("read_failures") or 0) + 1
        if tries < 3:                 # the model failed: keep what was known, so the edit is read again next time
            keep = dict(prev, read_failures=tries) if prev else {"path": item["path"], "name": item["name"],
                                                                  "read_failures": tries, "hash": "", "norm": ""}
            return keep, [], ""
    rec.pop("read_failures", None)
    kind = "trivial" if (item["kind"] == "note_trivial" or p.get("trivial")) else "unparsed"
    rec["history"] = (hist + [{"at": now, "hash": item["hash"], "kind": kind}])[-20:]
    return rec, [], ""


# ── the automation advisor (intel-automations) ──────────────────────────────

ADVISOR_CAPS = ("web.search, web.fetch, llm.generate, fabric.ingest, fabric.browse, fabric.query, tg.notify, "
                "report.html, print.text, podcast.generate, ha.states, ha.set, ha.scene, ha.notify, ha.estate.sync, "
                "n8n.workflow.list, n8n.execution.list, integration.list, estate.health, fs.list, fs.read, fs.write, "
                "research.run, cal.assistant.briefing, notes.append, background.enqueue, ollama.gate.status, jobs.stats, "
                "intel.topic.save, intel.sweep, intel.report.build")

_TOOLING = re.compile(r"(agent|automation|workflow|tool|devops|aiops|mcp|n8n|llm|pipeline|itsm|service desk|observab)", re.I)


def advisor_prompt(workflows: Sequence[Dict[str, Any]], services: Sequence[str], intel: Sequence[Dict[str, Any]],
                   previous: Sequence[str]) -> str:
    wf = [f"- {w.get('name')}{'' if w.get('active') else ' (inactive)'}" for w in workflows][:80]
    it = [f"- [{s.get('score')}] {s.get('title')}: {s.get('summary') or s.get('snippet') or ''}"[:300]
          for s in intel if _TOOLING.search(" ".join([str(s.get("summary") or ""), " ".join(s.get("tags") or [])]))][:15]
    return "\n".join([
        "You advise on automation for a homelab that runs Vera (an AI orchestrator exposing ~2500 capabilities over HTTP "
        "as name+arguments), n8n, Home Assistant, Jellyfin, Nextcloud, Gitea, Grafana, Proxmox, a thermal printer, "
        "Telegram and a data fabric.",
        "Propose 3 to 5 automations that are NOT yet running: new pipelines, improvements to existing ones, or techniques "
        "from the recent intelligence that are not being applied. Prefer concrete, small, testable flows over grand designs.",
        "", f"Vera capabilities you may use (name + arguments): {ADVISOR_CAPS}.", "",
        "EXISTING n8n WORKFLOWS:", *(wf or ["- none"]), "",
        "SERVICES ON THE ESTATE:", *([f"- {s}" for s in services[:60]] or ["- unknown"]), "",
        "RECENT INTELLIGENCE (tools, techniques):", *(it or ["- none this week"]), "",
        "ALREADY SUGGESTED BEFORE (do not repeat):", *([f"- {p}" for p in previous[-30:]] or ["- none"]), "",
        "Your whole answer is about 2500 words of JSON.",
        'Return ONLY a JSON object: {"suggestions":[{ "title": short name, "category": "new"|"improve"|"retire", '
        '"rationale": 2-3 sentences citing which service/intel/gap it addresses, "trigger": "manual"|"schedule:<minutes>"|'
        '"webhook", "risk": "low"|"medium"|"high", "effort": "small"|"medium"|"large", "steps": [{"name": short, '
        '"cap": a Vera capability name from the list, "args": {argument object with literal values}, "note": what this '
        'step does}] }]}. Every step must use a capability from the list above. Keep args literal and minimal.'])


def slugify(s: str, n: int = 50) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:n].rstrip("-")) or "suggestion"


def compile_draft(sg: Dict[str, Any], vera_url: str, today: str) -> Dict[str, Any]:
    """A proposal as a REAL n8n workflow - inactive, manual trigger - so it can be
    opened, read, run by hand and edited before anyone trusts it."""
    pre = ("const VERA=" + json.dumps(vera_url) + ";function content(j){const d=j||{};"
           "return (d.content!==undefined)?d.content:d;}\n")
    nodes: List[Dict[str, Any]] = [{"name": "Run by hand", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1,
                                    "parameters": {}, "position": [0, 0]}]
    conns: Dict[str, Any] = {}
    prev, x = "Run by hand", 220
    for i, st in enumerate([s for s in (sg.get("steps") or []) if isinstance(s, dict)][:8]):
        prep = f"Prep {i + 1}: {str(st.get('name') or st.get('cap') or 'step')[:28]}"
        call = f"Call {i + 1}: {str(st.get('cap') or '')[:28]}"
        args = st.get("args") if isinstance(st.get("args"), dict) else {}
        js = (pre + "const note=" + json.dumps(str(st.get("note") or "")[:200]) + ";\n"
              "const prior=content($json)||{};  // output of the previous step, if you want to wire it in\n"
              "return [{json:{reqBody:JSON.stringify({name:" + json.dumps(str(st.get("cap") or "")) +
              ",arguments:" + json.dumps(args) + "})}}];")
        nodes.append({"name": prep, "type": "n8n-nodes-base.code", "typeVersion": 2,
                      "parameters": {"mode": "runOnceForAllItems", "jsCode": js}, "position": [x, 0]})
        nodes.append({"name": call, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
                      "parameters": {"method": "POST", "url": vera_url, "sendBody": True, "specifyBody": "json",
                                     "jsonBody": "={{ $json.reqBody }}",
                                     "options": {"allowUnauthorizedCerts": True, "timeout": 900000,
                                                 "response": {"response": {"neverError": True}}}},
                      "alwaysOutputData": True, "position": [x + 220, 0]})
        conns[prev] = {"main": [[{"node": prep, "type": "main", "index": 0}]]}
        conns[prep] = {"main": [[{"node": call, "type": "main", "index": 0}]]}
        prev, x = call, x + 440
    note = "\n".join([f"DRAFT proposed by Vera intelligence on {today}. Inactive on purpose.", "",
                      f"Why: {sg.get('rationale', '')}",
                      f"Trigger suggested: {sg.get('trigger', 'manual')} | risk: {sg.get('risk', '?')} | effort: {sg.get('effort', '?')}",
                      "", "Each step is a Code node that builds one Vera call followed by the HTTP node that makes it.",
                      "Edit the args in each Prep node, run it by hand, and promote it by renaming and activating."])
    nodes.append({"name": "About this draft", "type": "n8n-nodes-base.stickyNote", "typeVersion": 1,
                  "parameters": {"content": note, "height": 260, "width": 520}, "position": [0, -320]})
    return {"name": "draft-" + slugify(sg.get("title") or "suggestion"), "nodes": nodes, "connections": conns,
            "settings": {"executionOrder": "v1"}}


# ── the text command (Telegram / phone shortcuts) ───────────────────────────

COMMANDS = ("list", "status", "add", "remove", "track", "sweep", "night", "files", "brief", "advise", "save", "help")
HELP = ("commands: list | status | add <topic> | add <topic> :: <search> | remove <topic id> | sweep [topic id] | "
        "night | files | brief | advise | save <url> [topic id]")


def parse_command(text: str) -> Tuple[str, str]:
    m = re.match(r"^\s*/?(\w+)\s*(.*)$", str(text or ""), re.S)
    if not m:
        return "help", ""
    cmd = m.group(1).lower()
    return (cmd if cmd in COMMANDS else "help"), m.group(2).strip()


# ── sessions: windows the platform may plan work into ───────────────────────

def _hm(s: str) -> Optional[int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None


def normalise_session(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    start, end = _hm(raw.get("start")), _hm(raw.get("end"))
    if start is None or end is None or start == end:
        return None
    days = [int(d) for d in (raw.get("days") if isinstance(raw.get("days"), list) else range(7))
            if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6]
    mode = str(raw.get("mode") or "deepen")
    return {"name": str(raw.get("name") or f"{raw.get('start')}-{raw.get('end')}")[:60],
            "days": sorted(set(days)) or list(range(7)), "start": str(raw.get("start")), "end": str(raw.get("end")),
            "budget": max(1, min(50, int(raw.get("budget") or 6))),
            "mode": mode if mode in ("deepen", "refresh", "research") else "deepen"}


def active_session(sessions: Sequence[Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
    """The session `now` falls in (a window may cross midnight; its day is the
    day it started), with `ends` - the local datetime it closes."""
    from datetime import timedelta
    mins = now.hour * 60 + now.minute
    for raw in sessions or []:
        s = normalise_session(raw) if isinstance(raw, dict) else None
        if not s:
            continue
        st, en = _hm(s["start"]), _hm(s["end"])
        if st < en:
            if now.weekday() in s["days"] and st <= mins < en:
                return dict(s, ends=now.replace(hour=en // 60, minute=en % 60, second=0, microsecond=0))
        else:
            if mins >= st and now.weekday() in s["days"]:
                end_dt = (now + timedelta(days=1)).replace(hour=en // 60, minute=en % 60, second=0, microsecond=0)
                return dict(s, ends=end_dt)
            if mins < en and (now - timedelta(days=1)).weekday() in s["days"]:
                return dict(s, ends=now.replace(hour=en // 60, minute=en % 60, second=0, microsecond=0))
    return None


# ── proactive: when a change is worth interrupting for ──────────────────────

def material_change(prev: Optional[Dict[str, Any]], snap: Dict[str, Any], *, competitor_hits_n: int = 0,
                    moves: Sequence[str] = ()) -> Tuple[bool, str]:
    """(alert?, why). A first assessment is not news; a moved headline, a jump in
    confidence, several changed facts, a competitor move or a number the topic
    watches moving hard (with the news around it) are."""
    if not prev:
        return False, ""
    why: List[str] = []
    for m in list(moves)[:2]:
        why.append(m)
    if snap.get("headline") and _dice(snap.get("headline", ""), prev.get("headline", "")) < 0.55:
        why.append("the headline moved")
    order = {"low": 0, "medium": 1, "high": 2}
    if abs(order.get(snap.get("confidence"), 1) - order.get(prev.get("confidence"), 1)) >= 1 and \
            snap.get("confidence") != prev.get("confidence"):
        why.append(f"confidence {prev.get('confidence')} -> {snap.get('confidence')}")
    if len(snap.get("changed") or []) >= 3:
        why.append(f"{len(snap['changed'])} things changed")
    if competitor_hits_n:
        why.append(f"{competitor_hits_n} competitor mention(s)")
    strong = len(why) >= 2 or "the headline moved" in why or competitor_hits_n > 0 or bool(moves)
    return strong, "; ".join(why)


def alert_text(topic_name: str, snap: Dict[str, Any], why: str, url: str = "") -> str:
    lines = [f"⚠ {topic_name}: {snap.get('headline', '')}", f"why now: {why}"]
    lines += [f"• {c}" for c in (snap.get("changed") or [])[:4]]
    if url:
        lines.append(url)
    return "\n".join(lines)


# ── the worldview's corpus (four-minds FM-8.3 / FM-8.4 / FM-13.5) ────────────

CORPUS_SCHEMA = "vera.intel-corpus/v1"
PRIVATE_SOURCES = {"notes", "files", "drop", "note"}      # the owner's own files never leave intel (principle 16)
CORPUS_FIELDS = ("id", "topic_id", "title", "site", "kind", "url", "score", "entities", "entity_types", "stance",
                 "genre", "summary", "competitors", "source")


def _epoch(ts: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def corpus_rows(signals: Sequence[Dict[str, Any]], *, max_share: float = 0.15, min_score: float = 0.0,
                private_topics: Sequence[str] = ()) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Signals as corpus records (oldest first) and the stratification report. No
    record from the owner's notes or files; no site above `max_share` of the
    records (strongest kept); `t` is when the story first appeared."""
    keep = []
    dropped_private = 0
    for s in signals:
        if s.get("topic_id") in private_topics or str(s.get("source") or "") in PRIVATE_SOURCES:
            dropped_private += 1
            continue
        if (s.get("score") or 0) < min_score or not s.get("title"):
            continue
        t = _epoch(s.get("published")) or _epoch(s.get("seen"))
        if t is None:
            continue
        r = {k: s.get(k) for k in CORPUS_FIELDS if s.get(k) not in (None, "", [], {})}
        r["t"] = t
        r["seen"] = s.get("seen")
        r["also"] = len(s.get("also") or [])
        keep.append(r)
    by_site: Dict[str, List[Dict[str, Any]]] = {}
    for r in keep:
        by_site.setdefault(str(r.get("site") or "?"), []).append(r)
    cap = max(1, int(max_share * len(keep))) if keep and 0 < max_share < 1 else len(keep)
    out: List[Dict[str, Any]] = []
    capped = {}
    for site, rows in by_site.items():
        rows = sorted(rows, key=lambda r: (-(r.get("score") or 0), r["id"]))
        if len(rows) > cap and len(by_site) > 1:
            capped[site] = len(rows) - cap
            rows = rows[:cap]
        out += rows
    out.sort(key=lambda r: (r["t"], r["id"]))          # stable across exports (FM-8.4): time, then id
    shares = {}
    for r in out:
        shares[str(r.get("site") or "?")] = shares.get(str(r.get("site") or "?"), 0) + 1
    top = max(shares.values()) / len(out) if out else 0.0
    return out, {"records": len(out), "private_dropped": dropped_private, "capped": capped,
                 "sites": len(shares), "max_site_share": round(top, 3)}


def sequences(rows: Sequence[Dict[str, Any]], snapshots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Per topic, the observed order of stories and of assessments - real
    transitions (FM-8.4), not random walks."""
    stories: Dict[str, List[str]] = {}
    for r in sorted(rows, key=lambda r: (r["t"], r["id"])):
        stories.setdefault(r["topic_id"], []).append(r["id"])
    snaps: Dict[str, List[Dict[str, Any]]] = {}
    for sn in sorted(snapshots, key=lambda x: (str(x.get("ts") or ""), str(x.get("id") or ""))):
        snaps.setdefault(sn.get("topic_id"), []).append(
            {"id": sn.get("id"), "ts": sn.get("ts"), "headline": sn.get("headline"),
             "confidence": sn.get("confidence"), "changed": len(sn.get("changed") or [])})
    return {"stories": stories, "assessments": snaps}
