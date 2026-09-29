"""Canvas enrichment: what ELSE on this estate is relevant to the turn, and how to show it.

A turn answers a question in prose. The canvas beside it used to hold only what the reply itself carried - the
model had to ask for every item by directive. Ask for "the latest crypto market data" and the answer is a few
paragraphs, while Vera already holds the market overview, the news feed for the same words, the records the
fabric has on it, and a web search a click away. This module decides which of those to bring forth, and turns
their answers into items the canvas can draw: a live widget for a dashboard read, a RECORDS browser (paged,
searchable, sortable) for anything that is a list of things to look through - web results, news, fabric
records, research sources.

Three kinds of source, each safe to run unasked:

  dashboard reads   argument-free capabilities the widget source registry MEASURED answering (a shape, a cost),
                    that read (widget_sources.is_read) and whose name has no verb that changes anything; picked
                    by caps.search relevance to the turn
  query reads       a short, fixed table of reads that take the turn's words (web search, news, the fabric) -
                    each gated by what the turn is about, so a question about a branch does not search the web
  memory            the fabric's records for the turn's words, over its relevance floor

Nothing here calls a model. Pure: candidates and answers in, a plan and items out. The capability that runs it
(canvas.enrich) does the I/O.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

#: words that never make a turn's topic
_STOP = set("""a an and are as at be but by can could do does for from get give had has have how i if in into is it
its just let like me more most my no not now of on or our please show so some tell than that the their them then there
these they this to up us want was we what when where which who why will with would you your about any all also
been being did doing each few here just only own same should very""".split())

#: a capability name with one of these words changes something - never run unasked, whatever the registry says
_WRITES = set("""add append apply approve archive ban book buy cancel claim clear close commit connect create
delete deploy disable dispatch drop enable enqueue evict exec execute fetch fire flush fork import ingest install
kill launch learn load merge migrate move open order pause post promote prune publish pull purge push put queue
reap rebuild record refresh register reindex reload remove rename reset restart restore resume revert rollback
rotate run save scan schedule seed sell send set shutdown snapshot spawn start stop submit subscribe swap sync
tick toggle train trigger undo unregister update upgrade upload upsert vote wake wipe write""".split())

#: a read whose tail says it SUMMARISES is what a glance wants; one whose tail says it lists the machinery (its
#: sources, templates, engines, settings) is not what anyone asking about the thing meant
_GLANCE = set("""overview summary status snapshot map heatmap quotes quote movers top trending board digest
dashboard health breadth sentiment leaders gainers losers pulse today latest""".split())
_MACHINERY = set("""sources templates template engines engine providers provider config settings schema types
catalog catalogue registry params options formats modes profiles accounts keys""".split())

#: the registry shapes that draw as something worth a tile
_DRAWABLE = ("items", "values", "series", "level", "events", "graph", "parts", "matrix", "ohlcv", "rate")

#: topic words that make a turn about the markets / the news / the world outside
_FINANCE = set("""crypto cryptocurrency bitcoin btc eth ether ethereum sol solana coin coins token tokens market
markets stock stocks share shares price prices trading trade forex fx usd usdt defi altcoin altcoins portfolio
ticker tickers exchange binance nasdaq sp500 index etf commodity gold oil""".split())
_FRESH = set("""latest news today current recent now update updates headline headlines breaking this week
yesterday tonight live happening trend trends trending""".split())
_WORLD = set("""who what where when why news article articles report reports research paper papers study
studies company companies people person country countries election weather event events""".split())

#: the reads that take the turn's words: (cap, args builder, records kind, the gate that must hold)
QUERY_READS: List[Dict[str, Any]] = [
    {"cap": "markets.overview", "kind": "widget", "gate": "finance", "args": lambda q: {},
     "why": "the market overview - the turn is about the markets"},
    {"cap": "markets.news.feed", "kind": "news", "gate": "finance", "args": lambda q: {"query": q, "limit": 20},
     "why": "market news for the turn's words"},
    {"cap": "web.search", "kind": "web", "gate": "fresh_or_world", "args": lambda q: {"query": q, "limit": 20},
     "why": "a web search - the turn asks about the world outside, or about now"},
    {"cap": "fabric.query", "kind": "memory", "gate": "always", "args": lambda q: {"text": q, "top_k": 30},
     "why": "what the fabric already holds on it"},
]


def terms(text: str) -> List[str]:
    """The turn's content words, in order, de-duplicated."""
    seen, out = set(), []
    for w in re.findall(r"[a-z0-9][a-z0-9+#._-]*", str(text or "").lower()):
        w = w.strip("._-")
        if len(w) < 2 or w in _STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def topic(text: str, limit: int = 12) -> str:
    """The query the reads get: the content words, not the whole sentence (search engines and the fabric's text
    score both do better on the words than on 'could you please show me the...')."""
    return " ".join(terms(text)[:limit])


#: words that say what KIND of thing is wanted, not what it is about - a record that shares only these with the turn
#: is not about the turn ("latest crypto market data": a record must say crypto, not merely data)
_GENERIC = _FRESH | set("""data info information details stuff thing things market markets price prices value values
report reports list lists show find search look get give tell summary overview status please help""".split())


def specific_terms(text: str) -> List[str]:
    """The words a record must share with the turn to be ABOUT it."""
    return [t for t in terms(text) if t not in _GENERIC and len(t) > 2]


def about(records: Sequence[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """Only the records that name at least one of the turn's specific words. The fabric's own score is not enough
    to show a record unasked: for "latest crypto market data" it ranked crawled job pages above crypto notes,
    because every one of them said "market". When the turn has no specific word, nothing is filtered."""
    sp = specific_terms(text)
    if not sp:
        return list(records)
    out = []
    for r in records:
        hay = (" ".join(str(r.get(k) or "") for k in ("title", "snippet", "domain")) + " " + " ".join(
            str(v) for v in (r.get("meta") or {}).values())).lower()
        if any(t in hay for t in sp):
            out.append(r)
    return out


def gates(text: str) -> Dict[str, bool]:
    t = set(terms(text))
    fin = bool(t & _FINANCE)
    fresh = bool(t & _FRESH)
    world = bool(t & _WORLD)
    return {"finance": fin, "fresh": fresh, "world": world, "fresh_or_world": fresh or world or fin, "always": True}


def writes(name: str) -> bool:
    """True when any word of the name says it changes something."""
    parts = [p for p in re.split(r"[._\-]", str(name or "").lower()) if p]
    return any(p in _WRITES for p in parts)


def rerank(score: float, name: str, description: str, turn_terms: Sequence[str]) -> float:
    """caps.search's score, adjusted for a GLANCE: caps.search is lexical first (+3 a word of the name, +2 a tag,
    +1 a word of the description, up to +3 embedding), so for a markets question every markets.* capability scores
    about the same. Among those, the one that summarises (overview, quotes, sentiment) is the one to show, the one
    that lists machinery (sources, templates) is not, and the one whose description carries more of the turn's
    words is closer to what was asked."""
    parts = [x for x in re.split(r"[._\-]", str(name or "").lower()) if x]
    tail = parts[-1] if parts else ""
    adj = float(score or 0)
    if tail in _GLANCE or any(x in _GLANCE for x in parts[1:]):
        adj += 2.0
    if tail in _MACHINERY:
        adj -= 4.0
    words = set(terms(description))
    adj += 0.75 * sum(1 for t in turn_terms if t in words)
    return round(adj, 3)


def dashboard_candidates(found: Iterable[Dict[str, Any]], measured: Dict[str, Dict[str, Any]],
                         is_read: Callable[[str], bool], *, min_score: float = 6.5, rel: float = 0.6,
                         max_ms: int = 2500, exclude: Sequence[str] = (), turn: str = "") -> List[Dict[str, Any]]:
    """caps.search hits that are safe and cheap to run with no arguments, best first.

    Safe: the registry measured it ANSWERING with a drawable shape (so it needs no argument and returned
    something), it reads (is_read), and no word of its name writes. Cheap: it answered under max_ms when measured.
    Relevant: re-ranked for a glance (rerank), at least min_score on caps.search's scale and within `rel` of the
    best safe candidate - so a weak tail of the list never makes the canvas just because it was safe.
    """
    out = []
    ex = set(exclude)
    tt = terms(turn)
    for f in found or []:
        name = str((f or {}).get("name") or "")
        try:
            score = float((f or {}).get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        m = measured.get(name) or {}
        if not name or name in ex or score < min_score:
            continue
        if not m.get("shape") or m.get("err") or m["shape"] not in _DRAWABLE:
            continue
        if "empty when measured" in str(m.get("note") or ""):
            continue
        if int(m.get("ms") or 0) > max_ms or writes(name) or not is_read(name):
            continue
        desc = str(f.get("description") or "")
        adj = rerank(score, name, desc, tt)
        first = re.split(r"(?<=[a-z0-9)])\. |\n", desc.strip())[0][:120] if desc.strip() else name
        out.append({"cap": name, "args": {}, "kind": "widget", "score": adj,
                    "why": "relevant to the turn: " + first})
    if not out:
        return out
    out.sort(key=lambda c: -c["score"])
    top = out[0]["score"]
    return [c for c in out if c["score"] >= max(min_score, rel * top)]


def plan(text: str, found: Iterable[Dict[str, Any]], measured: Dict[str, Dict[str, Any]],
         is_read: Callable[[str], bool], *, max_widgets: int = 3, sources: Sequence[str] = ("caps", "query", "memory"),
         min_score: float = 6.5) -> Dict[str, Any]:
    """What to bring forth for this turn: [{cap, args, kind, why}] - the query reads its gates allow, then the
    most relevant dashboard reads. Nothing is duplicated: a query read wins over the same cap as a dashboard read."""
    q = topic(text)
    g = gates(text)
    steps: List[Dict[str, Any]] = []
    if q:
        for r in QUERY_READS:
            kind = r["kind"]
            if kind == "memory" and "memory" not in sources:
                continue
            if kind != "memory" and "query" not in sources:
                continue
            if not g.get(r["gate"], False):
                continue
            steps.append({"cap": r["cap"], "args": r["args"](q), "kind": kind, "why": r["why"]})
    if "caps" in sources:
        taken = [s["cap"] for s in steps]
        steps += dashboard_candidates(found, measured, is_read, min_score=min_score, exclude=taken,
                                      turn=text)[:max(0, max_widgets)]
    return {"query": q, "gates": g, "steps": steps}


# ── answers → records ────────────────────────────────────────────────────────

def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _first_line(text: str, n: int = 140) -> str:
    s = " ".join(str(text or "").split())
    for sep in (". ", " - ", " | ", "\n"):
        if sep in s[:n]:
            return s[:s.index(sep)][:n]
    return s[:n]


def records_from(cap: str, result: Any, *, kind: str = "") -> List[Dict[str, Any]]:
    """Any list-shaped answer as records: {id, title, url?, snippet?, meta{}, when?, score?, ref?}.

    Knows the answers it is handed (web.search results, markets.news.feed headlines, fabric.query results,
    web.research / research sources, context.recall results) and falls back to any list of objects with a title,
    a name, a text or a url in it. Nothing is cut: the browser pages, it does not truncate."""
    r = result if isinstance(result, dict) else {"rows": result if isinstance(result, list) else []}
    rows = _rows_in(r)
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, x in enumerate(rows):
        if not isinstance(x, dict):
            if isinstance(x, str) and x.strip():
                out.append({"id": str(i), "title": _first_line(x), "snippet": x})
            continue
        url = str(x.get("url") or x.get("link") or x.get("href") or "")
        raw = html.unescape(str(x.get("text") or x.get("content") or ""))
        text = _clean(raw)
        # a record with no title (the fabric's chunks are text alone) is named by its FIRST LINE when it has one - a
        # board item's "W5-04 retrieval comparison\nDeterministic..." - and by its first sentence otherwise; the list
        # used to show every fabric record as one run-on line, its name and its body folded together
        head = _clean(raw.strip().split("\n", 1)[0]) if "\n" in raw.strip()[:160] else ""
        title = (str(x.get("title") or x.get("name") or x.get("label") or x.get("headline") or x.get("symbol") or x.get("key") or "")
                 or head or _first_line(text) or url)
        title = _clean(title)
        body = text[len(title):].lstrip(" .:-|") if title and text.startswith(title) else text
        snippet = _clean(x.get("snippet") or x.get("summary") or x.get("description") or "") or (body if body != title else "")
        # the same record twice (a page crawled twice, a chunk indexed twice) is one record; boilerplate is none
        sig = (url.split("#")[0].rstrip("/").lower() if url else "") or re.sub(r"\W+", " ", (title + " " + snippet[:160]).lower()).strip()
        if sig in seen or (not url and _BOILER.search(title)):
            continue
        seen.add(sig)
        rec: Dict[str, Any] = {"id": str(x.get("id") or url or i), "title": title[:300], "snippet": snippet[:1200]}
        if url:
            rec["url"] = url
            rec["domain"] = _host(url)
        when = x.get("published") or x.get("published_at") or x.get("date") or x.get("ts") or x.get("created_at")
        if when:
            rec["when"] = str(when)
        # RELEVANCE, not rank: the fabric's `score` is its rank fusion (~0.016 for the best record), which drew every
        # bar empty; its vector_score is the similarity (0.65 for the same record), which is what the bar means
        sc = x.get("vector_score") if x.get("vector_score") is not None else x.get("score")
        if sc is not None:
            try:
                rec["score"] = round(float(sc), 3)
            except (TypeError, ValueError):
                pass
        tags = x.get("tags")
        if isinstance(tags, list):
            tg = [str(t) for t in tags if isinstance(t, (str, int)) and str(t).strip()][:8]
            if tg:
                rec["tags"] = tg
        meta = {}
        for k in ("engine", "source", "dataset_id", "via", "author", "symbol", "_group"):
            if x.get(k):
                meta[k.lstrip("_")] = str(x.get(k))
        # the rest of a record's plain fields (a price, a change, a label) are what it IS - kept as its facts
        for k, v in x.items():
            if len(meta) >= 8 or k in _USED or k in meta or isinstance(v, (dict, list)) or v in (None, ""):
                continue
            meta[k] = _fmt(v)
        if meta:
            rec["meta"] = meta
        if cap.startswith(("fabric.", "memory.", "context.")) and x.get("id"):
            rec["ref"] = {"record_id": str(x.get("id")), "dataset_id": str(x.get("dataset_id") or "")}
        out.append(rec)
    return out


_BOILER = re.compile(r"^\s*(privacy policy|cookie|terms (of|and) (use|service)|sign in|log in|subscribe|accept all)", re.I)


def _clean(v: Any) -> str:
    """Text as a person reads it: entities decoded (&amp;, &#x27;), whitespace folded."""
    return " ".join(html.unescape(str(v or "")).split())


def overlap(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]]) -> float:
    """How much of the smaller list the larger one already holds, by page - news that IS the web search it ran is
    the same list twice."""
    ua = {str(x.get("url") or "").split("#")[0].rstrip("/").lower() for x in a if x.get("url")}
    ub = {str(x.get("url") or "").split("#")[0].rstrip("/").lower() for x in b if x.get("url")}
    if not ua or not ub:
        return 0.0
    return len(ua & ub) / float(min(len(ua), len(ub)))


_USED = {"id", "url", "link", "href", "text", "content", "title", "name", "label", "headline", "snippet", "summary",
         "description", "published", "published_at", "date", "ts", "created_at", "score", "engine", "source",
         "dataset_id", "via", "author", "symbol", "_group", "domain", "vector_score", "text_score", "tags"}


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return format(v, ",.6g") if abs(v) < 1e9 else format(v, ",.0f")   # 68,212.5 - never 6.821e+04
    return str(v)[:80]


def _rows_in(r: Dict[str, Any]) -> List[Any]:
    """The list an answer's records are: a known container first; else the LONGEST list of objects anywhere in
    it, one level of grouping flattened (markets.overview's groups[].assets are the assets, each keeping its
    group) - an answer that nests its rows is still a list to browse, not a JSON tree."""
    for k in ("results", "headlines", "sources", "items", "records", "rows", "entries", "articles", "data"):
        v = r.get(k)
        if isinstance(v, list) and v:
            return v
    best: List[Any] = []
    for k, v in r.items():
        if not (isinstance(v, list) and v and isinstance(v[0], dict)):
            continue
        inner = next((ik for ik, iv in v[0].items() if isinstance(iv, list) and iv and isinstance(iv[0], dict)), None)
        if inner:
            flat = []
            for g in v:
                if not isinstance(g, dict):
                    continue
                label = str(g.get("name") or g.get("title") or g.get("label") or "")
                for x in g.get(inner) or []:
                    if isinstance(x, dict):
                        flat.append(dict(x, _group=label) if label else x)
            v = flat or v
        if len(v) > len(best):
            best = v
    return best


_TITLES = {"web": "Web", "news": "News", "memory": "From the fabric", "research": "Research sources",
           "rows": "Records"}


def records_item(cap: str, args: Dict[str, Any], result: Any, *, kind: str = "rows", query: str = "",
                 why: str = "", title: str = "") -> Optional[Dict[str, Any]]:
    """The canvas `records` item for a list answer, or None when there is nothing in it."""
    recs = records_from(cap, result, kind=kind)
    if kind == "memory" and query:
        recs = about(recs, query)                 # a fabric record shown unasked must be about the turn
    if not recs:
        return None
    title = title or (_TITLES.get(kind, "Records") + (" · " + query if query else ""))
    item = {"title": title, "source": cap, "args": args, "query": query, "kind": kind, "items": recs,
            "total": len(recs), "why": why}
    # the fabric's records open as a GRAPH (the query, the datasets that hold them, the records, the tags they
    # share) - a list of one-line chunks told nothing about how they relate; the list is one click away
    if kind == "memory":
        item["view"] = "graph"
    return item


def item_key(cap: str, args: Dict[str, Any], kind: str) -> str:
    """Keyed so enriching the same turn twice brings the item back instead of doubling it: a dashboard read has
    one item per canvas (it is the same read whatever was asked), a query read one per query."""
    if kind == "widget" and not args:
        return "enrich:%s" % cap
    h = hashlib.sha1(("%s|%s" % (cap, sorted((args or {}).items()))).encode("utf-8")).hexdigest()[:10]
    return "enrich:%s:%s" % (cap, h)
