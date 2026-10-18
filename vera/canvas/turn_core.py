"""What a turn is about: the things it names, found by the NLP tier and a few patterns, merged into one list.

The NLP tier's entity model (nlp.ner on the CPU nodes - an OntoNotes model: PERSON, ORG, GPE, LOC, DATE, MONEY ...)
reads a passage at a time, so a reply is cut into sentence-aligned chunks, each read on its own, and the mentions
put back together: one entity per (type, name), with how often it was named and the sentences it was named in.
Patterns add what a general tagger does not know: tickers (BTC, $ETH, BTC/USD) and addresses.

Pure: text and the tagger's answers in, a list and a canvas item out. The capabilities (turn.understand,
turn.entity.look) do the I/O. See shared-planning/canvas-enrichment/PLAN.md, slice 1.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

#: the labels worth a chip - a thing the reply is ABOUT. Counts, ordinals, percentages and times of day are not.
SALIENT = ("PERSON", "ORG", "GPE", "LOC", "NORP", "FAC", "PRODUCT", "EVENT", "WORK_OF_ART", "LAW", "MONEY", "DATE",
           "TICKER", "URL")
#: how each is named to a reader
LABEL = {"PERSON": "people", "ORG": "organisations", "GPE": "places", "LOC": "places", "NORP": "groups",
         "FAC": "places", "PRODUCT": "products", "EVENT": "events", "WORK_OF_ART": "works", "LAW": "laws",
         "MONEY": "money", "DATE": "dates", "TICKER": "tickers", "URL": "links"}
#: the order the groups are shown in
ORDER = ("people", "organisations", "places", "groups", "products", "events", "works", "laws", "tickers", "money",
         "dates", "links")

MIN_SCORE = 0.6
CHUNK = 1200

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_FENCE = re.compile(r"```[\s\S]*?```")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
# BTC, ETH/USD, $SOL, BTC-USD: a ticker is short, upper case and stands as a word; a lone common acronym is not one
_TICKER = re.compile(r"(?<![\w$])\$?([A-Z]{2,6})(?:[/-](USD[TC]?|EUR|GBP|BTC|ETH))?(?![\w])")
_KNOWN_TICKERS = set("""BTC ETH SOL XRP ADA DOGE DOT AVAX MATIC LINK LTC BCH XLM ATOM UNI TRX ETC FIL APT ARB OP NEAR
SHIB PEPE BNB TON SUI USDT USDC DAI AAPL MSFT NVDA TSLA AMZN GOOG GOOGL META NFLX AMD INTC SPY QQQ""".split())
# typed in lower case ("how is btc/usd doing"): a ticker no English word spells stands alone; any known one in a pair
# ("dot/usd") - "dot", "link", "near", "etc", "meta" alone stay words
_TICKER_LC = re.compile(r"(?<![\w$])\$?([a-z]{2,6})(?:[/-](usd[tc]?|eur|gbp|btc|eth))?(?![\w])")
_LOWER_ALONE = set("""BTC ETH SOL XRP DOGE AVAX MATIC LTC BCH XLM TRX SHIB PEPE BNB USDT USDC AAPL MSFT NVDA TSLA AMZN
GOOGL NFLX QQQ""".split())


_COINS = {"bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "dogecoin": "DOGE", "cardano": "ADA",
          "ripple": "XRP", "litecoin": "LTC", "polkadot": "DOT", "avalanche": "AVAX", "chainlink": "LINK", "tether": "USDT"}
_COIN = re.compile(r"\b(" + "|".join(_COINS) + r")\b", re.I)


def prose(text: str) -> str:
    """The prose of a reply: code fences out (a function name is not a person), whitespace kept."""
    return _FENCE.sub(" ", str(text or ""))


def chunks(text: str, size: int = CHUNK) -> List[Tuple[int, str]]:
    """Sentence-aligned pieces no longer than `size`, each with its offset in the text."""
    t = str(text or "")
    if not t.strip():
        return []
    out: List[Tuple[int, str]] = []
    start, cur = 0, ""
    pos = 0
    for s in _SENT.split(t):
        i = t.find(s, pos)
        if i < 0:
            i = pos
        pos = i + len(s)
        if cur and len(cur) + 1 + len(s) > size:
            out.append((start, cur))
            start, cur = i, s
        else:
            if not cur:
                start = i
            cur = (cur + " " + s) if cur else s
        while len(cur) > size:                     # one sentence longer than a chunk: cut it at a space
            cut = cur.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            out.append((start, cur[:cut]))
            start, cur = start + cut, cur[cut:].lstrip()
    if cur.strip():
        out.append((start, cur))
    return out


def sentence_at(text: str, at: int) -> str:
    """The sentence a mention sits in (for the chip's detail)."""
    t = str(text or "")
    a = max(t.rfind(". ", 0, at), t.rfind("\n", 0, at), t.rfind("? ", 0, at), t.rfind("! ", 0, at))
    b = min([x for x in (t.find(". ", at), t.find("\n", at), t.find("? ", at), t.find("! ", at)) if x >= 0] or [len(t)])
    return t[a + 1 if a >= 0 else 0: b + 1].strip()[:300]


def _norm(word: str) -> str:
    w = re.sub(r"\s+", " ", str(word or "")).strip().strip(".,;:()[]\"'")
    w = re.sub(r"'s$|’s$", "", w)                       # "Apple's" is Apple
    return re.sub(r"^(?:the|The)\s+", "", w)                 # "The European Union" is the European Union


def _whole_words(text: str, word: str, at: int) -> Tuple[str, int]:
    """A name the tagger cut inside a word ("italik Buterin" when "V" was tagged apart) widened to the words it sits in -
    only where the offset really points at the name in the text; otherwise the tagger's word as it came."""
    w = str(word or "").replace("##", "").strip()
    if not w or at < 0 or text[at:at + len(w)].lower() != w.lower():
        return word, at
    a, b = at, at + len(w)
    while a > 0 and text[a - 1].isalnum():
        a -= 1
    while b < len(text) and text[b].isalnum():
        b += 1
    return text[a:b], a


def merge(text: str, tagged: Iterable[Tuple[int, Dict[str, Any]]], min_score: float = MIN_SCORE) -> List[Dict[str, Any]]:
    """The tagger's answers (each with the offset of the chunk it read) and the patterns, as one list: one entity
    per (type, name), counted, with up to three sentences it was named in, most named first."""
    t = str(text or "")
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(typ: str, word: str, at: int, score: float):
        w = _norm(word)
        if len(w) < 2 or typ not in SALIENT:
            return
        k = (typ, w.lower())
        e = found.get(k)
        if not e:
            e = found[k] = {"text": w, "type": typ, "group": LABEL.get(typ, typ.lower()), "count": 0, "score": 0.0,
                            "spans": [], "sentences": []}
        e["count"] += 1
        e["score"] = max(e["score"], round(float(score), 3))
        if len(e["spans"]) < 6:
            e["spans"].append([at, at + len(word)])
        s = sentence_at(t, at)
        if s and s not in e["sentences"] and len(e["sentences"]) < 3:
            e["sentences"].append(s)

    for off, ans in tagged:
        for x in (ans or {}).get("entities") or []:
            try:
                sc = float(x.get("score") or 0)
            except (TypeError, ValueError):
                sc = 0.0
            if sc < min_score:
                continue
            typ = str(x.get("entity") or x.get("entity_group") or x.get("label") or "").upper().replace("B-", "").replace("I-", "")
            word, at = str(x.get("word") or x.get("text") or ""), off + int(x.get("start") or 0)
            word, at = _whole_words(t, word, at)
            add(typ, word, at, sc)
    for m in _URL.finditer(t):
        add("URL", m.group(0), m.start(), 1.0)
    for m in _TICKER.finditer(t):
        sym = m.group(1)
        if sym in _KNOWN_TICKERS or m.group(0).startswith("$") or m.group(2):
            add("TICKER", m.group(0).lstrip("$"), m.start(), 1.0)
    for m in _TICKER_LC.finditer(t):
        sym = m.group(1).upper()
        if sym in _LOWER_ALONE or (m.group(2) and sym in _KNOWN_TICKERS) or (m.group(0).startswith("$") and sym in _KNOWN_TICKERS):
            add("TICKER", m.group(0).lstrip("$").upper(), m.start(), 1.0)
    # a coin said by name is its ticker ("how is bitcoin doing" asks about BTC)
    for m in _COIN.finditer(t):
        add("TICKER", _COINS[m.group(1).lower()], m.start(), 1.0)
    out = _fold_short_names(list(found.values()))
    out.sort(key=lambda e: (-e["count"], -e["score"], e["text"].lower()))
    return out


def _fold_short_names(ents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A person named in full once and by surname after ("Tim Cook" ... "Cook") is one person: the short form's
    mentions go to the full name when exactly one full name of that type ends (or starts) with it."""
    keep: List[Dict[str, Any]] = []
    for e in ents:
        if e["type"] in ("PERSON", "ORG") and " " not in e["text"]:
            low = e["text"].lower()
            full = [f for f in ents if f is not e and f["type"] == e["type"] and " " in f["text"]
                    and (f["text"].lower().endswith(" " + low) or f["text"].lower().startswith(low + " "))]
            if len(full) == 1:
                f = full[0]
                f["count"] += e["count"]
                f["spans"] = (f["spans"] + e["spans"])[:6]
                for s in e["sentences"]:
                    if s not in f["sentences"] and len(f["sentences"]) < 3:
                        f["sentences"].append(s)
                continue
        keep.append(e)
    return keep


def salient(entities: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The things the reply is about - not its dates, money or links, which say when and how much, not what."""
    return [e for e in entities if e.get("type") not in ("DATE", "MONEY", "URL")]


#: the kinds of name worth fetching for unasked: things in the world the fabric or the web can say something about
LOOKABLE = ("PERSON", "ORG", "PRODUCT", "EVENT", "GPE", "LOC", "FAC", "NORP", "WORK_OF_ART", "LAW", "TICKER")


def to_look(entities: Sequence[Dict[str, Any]], n: int = 2) -> List[Dict[str, Any]]:
    """The names a turn is most about, to fetch for it unasked - DETERMINISTIC (owner, 2026-10-10: "better use of NLP and
    deterministic fetching of items for the desk"): the same entities always give the same names in the same order -
    most named, then the model's confidence, then the name - each name once, whatever its type was tagged."""
    seen, out = set(), []
    if max(0, int(n or 0)) == 0:
        return out
    cands = [e for e in salient(entities) if e.get("type") in LOOKABLE and len(_norm(str(e.get("text") or ""))) >= 3]
    for e in sorted(cands, key=lambda e: (-int(e.get("count") or 0), -float(e.get("score") or 0.0), str(e.get("text") or "").lower())):
        k = str(e.get("text") or "").strip().lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
        if len(out) >= max(0, int(n or 0)):
            break
    return out


def item(entities: Sequence[Dict[str, Any]], *, mid: str = "", subject: str = "", limit: int = 40) -> Dict[str, Any]:
    """The canvas `entities` item: the groups in a fixed order, each its entities, most named first."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in list(entities)[:limit]:
        groups.setdefault(e.get("group") or "other", []).append(
            {"text": e["text"], "type": e["type"], "count": e["count"], "sentences": e.get("sentences", [])[:3]})
    ordered = [{"group": g, "entities": groups[g]} for g in ORDER if g in groups]
    ordered += [{"group": g, "entities": v} for g, v in groups.items() if g not in ORDER]
    n = sum(len(g["entities"]) for g in ordered)
    return {"title": ("Mentioned · " + subject) if subject else "Mentioned", "mid": mid, "groups": ordered,
            "count": n}
