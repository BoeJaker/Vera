"""Intelligence - the pure rules (no app, no I/O).

A TOPIC is something complex the user wants followed over time: what it is,
why it matters, the questions still open, the searches that feed it and how
deep to go. A SIGNAL is one thing that turned up for a topic (a page, a repo,
a note change, a dream finding). A SNAPSHOT is what Vera believed about a
topic at one moment - the timeline is the run of snapshots and signals, so
the UI can stand at any day and see the topic as it looked then.

A REPORT is a document model (sections of headline / digest / signals /
images / diagram / code / timeline / actions) rendered three ways from the
same model: a self-contained HTML page, markdown (chat, notebook, email
fallback) and a Telegram message with buttons.

Everything here is deterministic so it can be tested without Vera running;
intel_capabilities.py does the I/O.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:                                         # the installable app's head + worker registration (same package)
    from . import intel_app_core as _app
except ImportError:                          # loaded from its file, outside a package
    import importlib.util as _ilu
    import os as _os
    _spec = _ilu.spec_from_file_location("intel_app_core", _os.path.join(_os.path.dirname(__file__), "intel_app_core.py"))
    _app = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_app)

# ── vocabulary ───────────────────────────────────────────────────────────────

DEPTHS = ("scan", "standard", "deep")
CADENCES = ("nightly", "twice-daily", "weekly", "manual")
STATUSES = ("active", "paused", "archived")
SIGNAL_KINDS = ("page", "repo", "paper", "video", "note", "dream", "research", "manual")

#: per depth: queries swept per run, signals enriched per run, reader digests kept
DEPTH_BUDGET = {
    "scan":     {"queries": 2, "enrich": 2, "digests": 1, "results": 5},
    "standard": {"queries": 4, "enrich": 4, "digests": 3, "results": 8},
    "deep":     {"queries": 6, "enrich": 8, "digests": 5, "results": 10},
}

#: how long a topic may go without a sweep before it is due again
CADENCE_HOURS = {"nightly": 20, "twice-daily": 10, "weekly": 160, "manual": 10 ** 6}

#: titles that mean the fetch got an interstitial or an error, never the article
_JUNK_TITLES = re.compile(
    r"^(just a moment|attention required|access denied|403 forbidden|404|page not found|"
    r"not found|error|are you a robot|verify you are human|subscribe to continue|sign in)\b",
    re.I)

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|igshid$|si$)", re.I)

_REPO_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def slug(text: str, n: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (s[:n].rstrip("-")) or "topic"


def _question_str(q: Any) -> str:
    """A question kept as text: the model sometimes writes {question, answer}, and older
    topics hold that stringified ("{'question': ...}") - never search or store the dict."""
    if isinstance(q, dict):
        return str(q.get("question") or q.get("text") or "").strip()
    s = str(q or "").strip()
    if s.startswith("{") and "question" in s:
        try:
            import ast as _ast
            d = _ast.literal_eval(s)
            if isinstance(d, dict):
                return str(d.get("question") or d.get("text") or "").strip()
        except (ValueError, SyntaxError):
            pass
    return s


def _clean_list(v: Any, n: int = 24) -> List[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n;]+", v)]
    out: List[str] = []
    for x in v or []:
        t = _question_str(x)
        if t and t not in out:
            out.append(t)
    return out[:n]


# ── topics ───────────────────────────────────────────────────────────────────

def normalise_topic(raw: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A topic record from user input, keeping what an update did not mention."""
    base = dict(existing or {})
    raw = dict(raw or {})
    name = str(raw.get("name") or base.get("name") or "").strip()
    if not name:
        raise ValueError("a topic needs a name")
    tid = str(base.get("id") or slug(raw.get("id") or name)).strip()
    depth = str(raw.get("depth") or base.get("depth") or "standard")
    cadence = str(raw.get("cadence") or base.get("cadence") or "nightly")
    status = str(raw.get("status") or base.get("status") or "active")
    out = {
        "id": tid,
        "name": name,
        "brief": str(raw.get("brief", base.get("brief", "")) or "").strip(),
        "why": str(raw.get("why", base.get("why", "")) or "").strip(),
        "questions": _clean_list(raw.get("questions", base.get("questions"))),
        "queries": _clean_list(raw.get("queries", base.get("queries"))),
        "entities": _clean_list(raw.get("entities", base.get("entities"))),
        "exclude": _clean_list(raw.get("exclude", base.get("exclude"))),
        "sites": _clean_list(raw.get("sites", base.get("sites"))),
        "parent": str(raw.get("parent", base.get("parent", "")) or "").strip(),
        "depth": depth if depth in DEPTHS else "standard",
        "cadence": cadence if cadence in CADENCES else "nightly",
        "status": status if status in STATUSES else "active",
        "deliver": _clean_list(raw.get("deliver", base.get("deliver", ["html"]))) or ["html"],
        "min_score": _num(raw.get("min_score", base.get("min_score", 5)), 5),
        "dream_loop": bool(raw.get("dream_loop", base.get("dream_loop", False))),
        "discovery": bool(raw.get("discovery", base.get("discovery", True))),
        "vendors": bool(raw.get("vendors", base.get("vendors", False))),   # vendor pages are wanted here
        "mute_sites": _clean_list(raw.get("mute_sites", base.get("mute_sites")), 60),
        "kind": str(raw.get("kind", base.get("kind", "")) or "").strip().lower(),   # "" = worked out from the words
        "labels": _clean_list(raw.get("labels", base.get("labels")), 12),           # extra entity types to tag
        "color": str(raw.get("color", base.get("color", "")) or "").strip(),
        "created": base.get("created") or now_iso(),
        "updated": now_iso(),
        "last_sweep": base.get("last_sweep", ""),
        "last_snapshot": base.get("last_snapshot", ""),
        "query_cursor": int(base.get("query_cursor", 0) or 0),
        "origin": str(raw.get("origin", base.get("origin", "")) or ""),
    }
    # fields this function does not own (the fabric source subscriptions, anything added later) ride along from the
    # stored record: a save from the panel's form must never drop them
    return {**{k: v for k, v in base.items() if k not in out}, **out}


def _num(v: Any, d: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def topic_queries(topic: Dict[str, Any], now: Optional[datetime] = None) -> List[str]:
    """Every search that feeds a topic: the user's own first, then ones anchored on
    the topic itself (its name, with "news", this month, "analysis"), then at most two
    of its open questions, then the entities and sites it follows.

    The open questions are written by the model about whatever it last read, so they
    are narrow; when four of them followed one name search, three of every four
    searches went to them and the night fetched off-topic pages (10 Oct 2026)."""
    out: List[str] = []
    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q or "").strip()
        if q and q.lower() not in [x.lower() for x in out]:
            out.append(q)
    for q in topic.get("queries") or []:
        add(q)
    name = topic.get("name") or ""
    month = (now or datetime.now(timezone.utc)).strftime("%B %Y")
    add(f"{name} latest news")
    add(f"{name} news {month}")
    add(f"{name} analysis")
    for q in (topic.get("questions") or [])[:2]:
        add(_question_str(q))
    for e in (topic.get("entities") or [])[:4]:
        add(f"{e} {name}" if e.lower() not in name.lower() else e)
    for s in (topic.get("sites") or [])[:3]:
        add(f"site:{s} {name}")
    return out


def rotate_queries(topic: Dict[str, Any], count: int) -> Tuple[List[str], int]:
    """The next `count` queries in rotation, and the cursor to store for next time,
    so a topic with many angles is covered over several runs instead of the same
    first few every night."""
    qs = topic_queries(topic)
    if not qs:
        return [], 0
    cur = int(topic.get("query_cursor", 0) or 0) % len(qs)
    picked = [qs[(cur + i) % len(qs)] for i in range(min(count, len(qs)))]
    return picked, (cur + len(picked)) % len(qs)


def is_due(topic: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    if topic.get("status") != "active":
        return False
    hours = CADENCE_HOURS.get(topic.get("cadence") or "nightly", 20)
    last = parse_ts(topic.get("last_sweep"))
    if last is None:
        return topic.get("cadence") != "manual"
    return ((now or datetime.now(timezone.utc)) - last) >= timedelta(hours=hours)


def keywords(topic: Dict[str, Any]) -> List[str]:
    words: List[str] = []
    text = " ".join([topic.get("name", ""), topic.get("brief", ""),
                     " ".join(topic.get("entities") or []), " ".join(topic.get("questions") or [])])
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#-]{2,}", text):
        lw = w.lower().strip(".")
        if lw not in _STOP and lw not in words:
            words.append(lw)
    return words[:40]


_STOP = set("the and for with from that this what which into about over under than then their there these those "
            "are was were has have had not but how why who when where will would could should does did its our your "
            "latest news new more most some any all can may might also just like".split())


# ── signals ──────────────────────────────────────────────────────────────────

def canonical_url(url: str) -> str:
    """One spelling per page: no tracking parameters, no fragment, no trailing slash,
    lower-case host, no `www.`."""
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    if not p.scheme or not p.netloc:
        return str(url or "").strip()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), host, path, q, ""))


def signal_id(topic_id: str, url: str = "", title: str = "") -> str:
    key = canonical_url(url) if url else re.sub(r"\W+", " ", (title or "").lower()).strip()
    return hashlib.sha1(f"{topic_id}|{key}".encode("utf-8")).hexdigest()[:16]


def _nowww(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _site(url: str) -> str:
    try:
        return _nowww(urlsplit(url).netloc.lower()) if url else ""
    except ValueError:
        return ""


def repo_of(url: str) -> Optional[Dict[str, str]]:
    """{host, owner, repo, clone} when a url is a code repository's page."""
    try:
        p = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = _nowww(p.netloc.lower())
    if host not in _REPO_HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2 or parts[0] in ("topics", "orgs", "search", "marketplace", "explore", "features", "about"):
        return None
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
        return None
    # "." / ".." (or any dot-led name) would walk the clone directory out of the repos folder - review 2026-10-10:
    # https://github.com/../.. resolved the clone target to the intel state dir, then `git reset --hard` ran there
    if owner.startswith(".") or repo.startswith(".") or not repo.strip("."):
        return None
    return {"host": host, "owner": owner, "repo": repo, "clone": f"https://{host}/{owner}/{repo}.git",
            "url": f"https://{host}/{owner}/{repo}"}


def signal_kind(url: str, title: str = "") -> str:
    u = (url or "").lower()
    if repo_of(url):
        return "repo"
    if "arxiv.org" in u or u.endswith(".pdf") or "doi.org" in u or "/abs/" in u:
        return "paper"
    if "youtube.com" in u or "youtu.be" in u or "vimeo.com" in u:
        return "video"
    return "page"


def is_junk(title: str, snippet: str = "") -> bool:
    t = (title or "").strip()
    if not t:
        return True
    return bool(_JUNK_TITLES.match(t))


def normalise_signal(topic_id: str, raw: Dict[str, Any], *, source: str = "web",
                     query: str = "") -> Optional[Dict[str, Any]]:
    """A search hit / note / dream finding as a signal, or None when it is junk."""
    url = str(raw.get("url") or raw.get("link") or "").strip()
    if url and not re.match(r"(?i)^https?://", url):
        url = ""                      # only web links survive - never javascript:, data:, file:
    title = re.sub(r"\s+", " ", _html.unescape(str(raw.get("title") or raw.get("name") or ""))).strip()
    snippet = re.sub(r"\s+", " ", _html.unescape(str(raw.get("snippet") or raw.get("description")
                                                     or raw.get("summary") or raw.get("text") or ""))).strip()
    if not title and snippet:
        title = snippet[:90]
    if is_junk(title, snippet):
        return None
    kind = str(raw.get("kind") or "") or (signal_kind(url, title) if url else "note")
    return {
        "id": signal_id(topic_id, url, title),
        "topic_id": topic_id,
        "url": canonical_url(url) if url else "",
        "title": title[:300],
        "snippet": snippet[:1200],
        "kind": kind if kind in SIGNAL_KINDS else "page",
        "source": source,
        "query": query,
        "seen": str(raw.get("seen") or now_iso()),
        "published": str(raw.get("published") or raw.get("date") or ""),
        "image": str(raw.get("image") or ""),
        "site": _site(url),
        "score": None,
        "enriched": False,
    }


def bigrams(s: str) -> set:
    s = re.sub(r"\s+", " ", (s or "").lower()).strip()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def dice(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return 2 * len(A & B) / (len(A) + len(B))


def novelty(signal: Dict[str, Any], recent_titles: Sequence[str]) -> float:
    """1.0 = nothing like it seen; 0 = the same story again under another url."""
    best = max((dice(signal.get("title", ""), t) for t in recent_titles), default=0.0)
    return round(1.0 - best, 3)


def heuristic_score(signal: Dict[str, Any], topic: Dict[str, Any], *,
                    now: Optional[datetime] = None, novelty_v: float = 1.0) -> float:
    """0-10 before any model has read the page: keyword overlap, recency, novelty,
    and a nudge for kinds that tend to carry substance (papers, repos)."""
    kws = keywords(topic)
    text = (signal.get("title", "") + " " + signal.get("snippet", "")).lower()
    hits = sum(1 for k in kws if k in text)
    overlap = min(1.0, hits / max(3, min(8, len(kws) or 1)))
    if any(x.lower() in text for x in topic.get("exclude") or []):
        return 0.0
    rec = 0.6
    pub = parse_ts(signal.get("published"))
    if pub:
        age_d = ((now or datetime.now(timezone.utc)) - pub).total_seconds() / 86400
        rec = 1.0 if age_d < 2 else 0.8 if age_d < 7 else 0.5 if age_d < 30 else 0.2
    bonus = {"paper": 0.6, "repo": 0.5, "dream": 0.4, "research": 0.6}.get(signal.get("kind", ""), 0.0)
    s = 10 * (0.55 * overlap + 0.2 * rec + 0.25 * novelty_v) + bonus
    return round(max(0.0, min(10.0, s)), 1)


# ── notes: only real edits reach a model ────────────────────────────────────

def note_hash(text: str) -> str:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    return hashlib.sha1("\n".join(x for x in lines if x).encode("utf-8")).hexdigest()


def note_delta(old: str, new: str) -> Dict[str, Any]:
    """Added/removed lines between two versions of a note; `cosmetic` when every
    added line has a near-twin among the removed ones (a typo or a reword)."""
    norm = lambda t: [re.sub(r"\s+", " ", ln).strip() for ln in (t or "").splitlines() if ln.strip()]
    a, b = norm(old), norm(new)
    sa, sb = set(a), set(b)
    added = [x for x in b if x not in sa]
    removed = [x for x in a if x not in sb]
    cosmetic = bool(added) and all(any(dice(x, r) >= 0.8 for r in removed) for x in added)
    return {"added": added, "removed": removed, "cosmetic": cosmetic or (not added and not removed)}


# ── model replies ────────────────────────────────────────────────────────────

def parse_json_loose(text: str) -> Optional[Any]:
    """The outermost JSON object in a reply that may think aloud around it."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", str(text))
    start = t.find("{")
    while start != -1:
        depth = 0
        in_s = esc = False
        for i in range(start, len(t)):
            c = t[i]
            if in_s:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_s = False
                continue
            if c == '"':
                in_s = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except ValueError:
                        break
        start = t.find("{", start + 1)
    return None


def clean_prose(text: str) -> str:
    t = str(text or "").strip()
    t = re.sub(r"(?is)^<think>.*?</think>\s*", "", t)
    t = re.sub(r"(?is)^thinking process:.*?\n\s*\n", "", t)
    return t.strip()


#: page text given to the model once the NLP tier has triaged a source. Measured on prod
#: (2026-10-04, MoE on cpu-247): ~10k-char prompts took 100-120 s, a 1.5k one 34 s.
TRIAGED_TEXT_CHARS = 4500


FRESHNESS_RULE = ("Facts older than about six months are background, not news: when the source's facts are "
                   "old, say when they date from (\"In May 2024, ...\") and score relevance down.")


def _dates(signal: Dict[str, Any]) -> str:
    pub = str(signal.get("published") or "")[:10] or "unknown"
    return f"TODAY: {datetime.now(timezone.utc).date().isoformat()}\nSOURCE PUBLISHED: {pub}\n"


def enrich_prompt(topic: Dict[str, Any], signal: Dict[str, Any], page_text: str, words: int = 220,
                  *, triaged: bool = False) -> str:
    qs = "\n".join(f"- {q}" for q in (topic.get("questions") or [])[:6]) or "- (none recorded)"
    if triaged:
        # relevance, entities and answers already came from the NLP tier - the model only writes
        return (
            f"You are following the topic \"{topic.get('name')}\" ({topic.get('brief') or topic.get('name')}).\n"
            f"Why it matters to the reader: {topic.get('why') or 'not stated'}\n"
            f"Read this source and answer in JSON only. Your whole answer is about {max(120, words - 80)} words.\n"
            f"{FRESHNESS_RULE}\n{_dates(signal)}"
            f"SOURCE TITLE: {signal.get('title')}\nSOURCE URL: {signal.get('url')}\n"
            f"SOURCE TEXT:\n{(page_text or signal.get('snippet') or '')[:TRIAGED_TEXT_CHARS]}\n\n"
            "JSON keys:\n"
            "  summary   - 2-4 plain sentences on what the source actually says (facts, numbers, names)\n"
            "  so_what   - 1-2 sentences on why it matters for this topic and reader\n"
            "  relevance - integer 0-10 (10 = changes the picture; 0 = off-topic or old news)\n"
            "  code      - a short code/config snippet from the source if it carries one, else \"\"\n"
            "  code_lang - its language, else \"\"\n"
            "  follow_up - list of 0-2 research questions worth chasing next\n")
    return (
        f"You are an intelligence analyst following the topic \"{topic.get('name')}\".\n"
        f"What the topic is: {topic.get('brief') or topic.get('name')}\n"
        f"Why it matters to the reader: {topic.get('why') or 'not stated'}\n"
        f"Open questions:\n{qs}\n\n"
        f"Read this source and answer in JSON only. Your whole answer is about {words} words.\n"
        f"{FRESHNESS_RULE}\n{_dates(signal)}"
        f"SOURCE TITLE: {signal.get('title')}\nSOURCE URL: {signal.get('url')}\n"
        f"SOURCE TEXT:\n{(page_text or signal.get('snippet') or '')[:9000]}\n\n"
        "JSON keys:\n"
        "  summary   - 2-4 plain sentences on what the source actually says (facts, numbers, names)\n"
        "  so_what   - 1-2 sentences on why it matters for this topic and reader\n"
        "  relevance - integer 0-10 (10 = changes the picture; 0 = off-topic)\n"
        "  answers   - list of the open questions this helps answer (verbatim), may be empty\n"
        "  entities  - list of organisations, people, products named\n"
        "  tags      - 2-5 short lowercase tags\n"
        "  code      - a short code/config snippet from the source if it carries one, else \"\"\n"
        "  code_lang - its language, else \"\"\n"
        "  follow_up - list of 0-2 research questions worth chasing next\n"
    )


def apply_enrichment(signal: Dict[str, Any], parsed: Dict[str, Any], *, page: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    s = dict(signal)
    p = parsed or {}
    rel = p.get("relevance")
    try:
        rel = max(0, min(10, int(float(rel))))
    except (TypeError, ValueError):
        rel = None
    s.update({
        "summary": clean_prose(str(p.get("summary") or ""))[:1500],
        "so_what": clean_prose(str(p.get("so_what") or ""))[:800],
        "answers": _clean_list(p.get("answers"), 8) or list(signal.get("answers") or []),
        "entities": _clean_list(p.get("entities"), 16) or list(signal.get("entities") or []),
        "tags": [t.lower() for t in _clean_list(p.get("tags"), 6)] or list(signal.get("tags") or []),
        "code": str(p.get("code") or "")[:4000],
        "code_lang": str(p.get("code_lang") or "")[:20],
        "follow_up": _clean_list(p.get("follow_up"), 3),
        "enriched": True,
        "enriched_at": now_iso(),
    })
    if rel is not None:
        s["score"] = float(rel)
    if page:
        s["digest"] = reader_digest(page)
        if page.get("image") and not s.get("image"):
            s["image"] = str(page["image"])
        if page.get("published") and not s.get("published"):
            s["published"] = str(page["published"])
        if page.get("byline"):
            s["byline"] = str(page["byline"])[:120]
        if page.get("minutes"):
            s["minutes"] = page.get("minutes")
    return s


def reader_digest(page: Dict[str, Any], max_chars: int = 2400) -> Dict[str, Any]:
    """The reader-mode page cut to a readable digest: its first paragraphs, whole,
    up to max_chars - never a sentence chopped mid-way."""
    md = str(page.get("markdown") or "")
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)          # images go in the figure, not the text
    paras = [p.strip() for p in re.split(r"\n\s*\n", md) if p.strip()]
    paras = [p for p in paras if len(p) > 60 and not p.startswith(("|", "<"))]
    out, n = [], 0
    for p in paras:
        if n + len(p) > max_chars and out:
            break
        out.append(p)
        n += len(p)
    return {"title": str(page.get("title") or ""), "site": str(page.get("site") or ""),
            "byline": str(page.get("byline") or ""), "minutes": page.get("minutes") or 0,
            "words": page.get("words") or 0, "text": "\n\n".join(out)[: max_chars + 400]}


def snapshot_prompt(topic: Dict[str, Any], previous: Optional[Dict[str, Any]],
                    signals: Sequence[Dict[str, Any]], words: int = 380, *, market: str = "",
                    levels: str = "", drift: str = "") -> str:
    prev = (previous or {}).get("state") or "(no earlier assessment - this is the first)"
    lines = []
    for s in signals[:14]:
        when = f", published {str(s['published'])[:10]}" if s.get("published") else ""
        lines.append(f"- [{s.get('score') if s.get('score') is not None else '?'}/10] {s.get('title')}"
                     f" ({s.get('site') or s.get('source')}{when}): {s.get('summary') or s.get('snippet') or ''}"[:600])
    qs = "\n".join(f"- {q}" for q in (topic.get("questions") or [])[:8]) or "- (none)"
    return (
        f"You keep a running assessment of the topic \"{topic.get('name')}\" for one reader.\n"
        f"Topic: {topic.get('brief') or topic.get('name')}\nWhy it matters: {topic.get('why') or 'not stated'}\n"
        f"Open questions:\n{qs}\n\nPREVIOUS ASSESSMENT:\n{prev}\n\nNEW EVIDENCE SINCE THEN:\n"
        + ("\n".join(lines) or "- nothing new") +
        (f"\n\nTHE NUMBERS THIS TOPIC WATCHES, NOW:\n{levels}\n(cite them where they bear on the picture)"
         if levels else "") +
        (f"\n\nWHAT VERA'S WORLDVIEW NOTICED: {drift}\n(say whether the evidence bears this out; it is a hint, not a fact)"
         if drift else "") +
        (f"\n\nTHE NUMBERS THIS TOPIC WATCHES MOVED:\n{market}\n"
         "(say whether the evidence explains each move; a move nothing explains is itself worth a question)"
         if market else "") +
        f"\n\nToday is {datetime.now(timezone.utc).date().isoformat()}. Give the date of any figure you cite; "
        "evidence older than about six months is background, never the current picture.\n"
        f"Write the updated assessment. Answer in JSON only. Your whole answer is about {words} words.\n"
        "JSON keys:\n"
        "  headline - one sentence: the single most important thing now\n"
        "  state    - the current picture in 3-6 sentences (what is true now, with numbers and names)\n"
        "  changed  - list of short bullets: what is DIFFERENT from the previous assessment (empty if nothing)\n"
        "  confidence - low | medium | high\n"
        "  answered - list of open questions now answered, each as {question, answer}\n"
        "  new_questions - list of 0-3 questions the evidence raises\n"
        "  watch    - list of 0-4 entities/things to watch next\n"
        "  diagram  - a small mermaid 'flowchart LR' (max 10 nodes) of how the main actors relate, or \"\"\n"
    )


def make_snapshot(topic: Dict[str, Any], parsed: Dict[str, Any], signals: Sequence[Dict[str, Any]],
                  *, ts: str = "", first: bool = False) -> Dict[str, Any]:
    """An assessment record. A first assessment has nothing to differ from, so its
    'what changed' is empty whatever the model wrote ('previous was null...')."""
    p = parsed or {}
    conf = str(p.get("confidence") or "medium").lower()
    answered = []
    for a in p.get("answered") or []:
        if isinstance(a, dict) and a.get("question"):
            answered.append({"question": str(a["question"])[:300], "answer": str(a.get("answer") or "")[:600]})
    diagram = sanitize_mermaid(str(p.get("diagram") or ""))
    ts = ts or now_iso()
    return {
        "id": hashlib.sha1(f"{topic.get('id')}|{ts}".encode()).hexdigest()[:16],
        "topic_id": topic.get("id"),
        "ts": ts,
        "headline": clean_prose(str(p.get("headline") or ""))[:400],
        "state": clean_prose(str(p.get("state") or ""))[:3000],
        "changed": [] if first else _clean_list(p.get("changed"), 10),
        "confidence": conf if conf in ("low", "medium", "high") else "medium",
        "answered": answered[:8],
        # the model sometimes writes questions as {question, answer} objects: keep the question
        "new_questions": _clean_list([(q.get("question") if isinstance(q, dict) else q)
                                      for q in (p.get("new_questions") or []) if q], 4),
        "watch": _clean_list(p.get("watch"), 6),
        "diagram": diagram,
        "signal_ids": [s.get("id") for s in signals][:40],
        "signal_count": len(signals),
    }


def sanitize_mermaid(src: str) -> str:
    """Only a plain flowchart survives: no click handlers, no html labels, no init
    directives - a model wrote it and the page renders it."""
    s = re.sub(r"```(?:mermaid)?", "", src or "").strip()
    if not s:
        return ""
    first = s.splitlines()[0].strip().lower()
    if not (first.startswith("flowchart") or first.startswith("graph")):
        return ""
    bad = re.compile(r"(?i)\bclick\b|%%\{|<script|javascript:|href|callback|<\/?[a-z]")
    lines = [ln for ln in s.splitlines() if not bad.search(ln)]
    return "\n".join(lines[:40])


def entity_diagram(topic: Dict[str, Any], signals: Sequence[Dict[str, Any]], limit: int = 9) -> str:
    """A flowchart of the topic and the entities its signals name most, weighted by count."""
    counts: Dict[str, int] = {}
    for s in signals:
        for e in s.get("entities") or []:
            k = str(e).strip()
            if k and len(k) < 60:
                counts[k] = counts.get(k, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    if not top:
        return ""
    def lab(x: str) -> str:
        return re.sub(r'["\[\]{}()<>|]', " ", x).strip()[:40]
    lines = ["flowchart LR", f'  T(["{lab(topic.get("name", "topic"))}"])']
    for i, (e, n) in enumerate(top):
        lines.append(f'  E{i}["{lab(e)}"]')
        lines.append(f"  T -->|{n}| E{i}")
    return "\n".join(lines)


# ── timeline ─────────────────────────────────────────────────────────────────

def timeline(signals: Sequence[Dict[str, Any]], snapshots: Sequence[Dict[str, Any]],
             reports: Sequence[Dict[str, Any]] = (), *, days: int = 90,
             now: Optional[datetime] = None, changes: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Day buckets over a window (counts, best score, the best signal's title) plus
    the point events - signals, snapshots, reports - newest first."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=max(1, days) - 1)).date()
    buckets: Dict[str, Dict[str, Any]] = {}
    for i in range(max(1, days)):
        d = (start + timedelta(days=i)).isoformat()
        buckets[d] = {"date": d, "signals": 0, "snapshots": 0, "reports": 0, "changes": 0, "top": None, "top_score": -1}
    events: List[Dict[str, Any]] = []
    for s in signals:
        t = parse_ts(s.get("seen"))
        if not t:
            continue
        d = t.date().isoformat()
        sc = s.get("score") if s.get("score") is not None else -1
        if d in buckets:
            b = buckets[d]
            b["signals"] += 1
            if sc > b["top_score"]:
                b["top_score"], b["top"] = sc, s.get("title")
        events.append({"kind": "signal", "ts": s.get("seen"), "title": s.get("title"), "id": s.get("id"),
                       "score": s.get("score"), "url": s.get("url"), "topic_id": s.get("topic_id"),
                       "sub": s.get("kind")})
    for sn in snapshots:
        t = parse_ts(sn.get("ts"))
        if not t:
            continue
        d = t.date().isoformat()
        if d in buckets:
            buckets[d]["snapshots"] += 1
        events.append({"kind": "snapshot", "ts": sn.get("ts"), "title": sn.get("headline") or "assessment",
                       "id": sn.get("id"), "topic_id": sn.get("topic_id"), "changed": len(sn.get("changed") or []),
                       "confidence": sn.get("confidence")})
    for r in reports:
        t = parse_ts(r.get("ts"))
        if not t:
            continue
        d = t.date().isoformat()
        if d in buckets:
            buckets[d]["reports"] += 1
        events.append({"kind": "report", "ts": r.get("ts"), "title": r.get("title"), "id": r.get("id"),
                       "url": r.get("url")})
    for c in changes:
        try:
            t = datetime.fromtimestamp(float(c.get("t")), timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        d = t.date().isoformat()
        if d in buckets:
            buckets[d]["changes"] += 1
        events.append({"kind": "change", "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": c.get("text"),
                       "id": f"{c.get('indicator')}:{c.get('t')}", "topic_id": c.get("topic_id"),
                       "sub": c.get("kind"), "direction": c.get("direction"),
                       "stories": len(c.get("stories") or [])})
    events.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    for b in buckets.values():
        if b["top_score"] < 0:
            b["top_score"] = None
    return {"days": list(buckets.values()), "events": events,
            "from": start.isoformat(), "to": now.date().isoformat()}


def as_of(snapshots: Sequence[Dict[str, Any]], when: str) -> Optional[Dict[str, Any]]:
    """The assessment in force at `when` - the latest snapshot at or before it."""
    t = parse_ts(when)
    if not t:
        return None
    best = None
    for s in snapshots:
        st = parse_ts(s.get("ts"))
        if st and st <= t and (best is None or st > parse_ts(best.get("ts"))):
            best = s
    return best


# ── actions: what a report can offer to do next ─────────────────────────────

#: an action kind -> the capability it runs and how its args are made. Only
#: these run from a report button; anything else is refused.
ACTIONS: Dict[str, Dict[str, Any]] = {
    "research":  {"label": "Research further", "cap": "research.run", "glyph": "⌕"},
    "discover":  {"label": "Map the topic", "cap": "fabric.discover.topic", "glyph": "◎"},
    "repo":      {"label": "Get repo", "cap": "intel.repo.get", "glyph": "⬇"},
    "read":      {"label": "Read in full", "cap": "reader.page", "glyph": "≡"},
    "track":     {"label": "Track as a topic", "cap": "intel.topic.save", "glyph": "✚"},
    "sweep":     {"label": "Sweep now", "cap": "intel.sweep", "glyph": "↻"},
    "watch":     {"label": "Watch this number", "cap": "intel.indicator.add", "glyph": "∿"},
}

# What a watch action may point at: a listing (price:<provider>:<symbol>) or a
# prediction market (pm:<source>:<id>) - nothing else reaches intel.indicator.add.
_WATCH_TARGET = re.compile(r"^(price:[a-z0-9_]{2,20}:[A-Za-z0-9.\-=^]{1,24}|pm:(polymarket|manifold):[A-Za-z0-9_\-]{1,80}"
                           r"|wiki:en:[A-Za-z0-9_().,'&\-]{1,120})$")


def make_action(kind: str, *, label: str = "", topic_id: str = "", target: str = "",
                query: str = "", ttl_days: int = 14) -> Optional[Dict[str, Any]]:
    """An action record with an unguessable one-time token; None for an unknown kind
    or one missing what it needs."""
    spec = ACTIONS.get(kind)
    if not spec:
        return None
    if kind == "repo" and not repo_of(target):
        return None
    if kind == "read" and not str(target).startswith(("http://", "https://")):
        return None
    if kind in ("research", "discover", "track") and not (query or target):
        return None
    if kind == "watch" and not (topic_id and _WATCH_TARGET.match(str(target or ""))):
        return None
    exp = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": secrets.token_urlsafe(18), "kind": kind, "label": label or spec["label"],
            "glyph": spec["glyph"], "cap": spec["cap"], "topic_id": topic_id,
            "target": str(target or "")[:500], "query": str(query or "")[:500],
            "status": "offered", "created": now_iso(), "expires": exp}


def action_args(action: Dict[str, Any]) -> Dict[str, Any]:
    """The capability arguments an action runs with - built HERE from the stored
    record, never taken from the request that pressed the button."""
    k, q, t = action.get("kind"), action.get("query") or "", action.get("target") or ""
    if k == "research":
        return {"query": q or t, "mode": "single", "output_mode": "report"}
    if k == "discover":
        return {"topic": q or t}
    if k == "repo":
        return {"url": t, "topic_id": action.get("topic_id", "")}
    if k == "read":
        return {"url": t}
    if k == "track":
        return {"name": q or t, "parent": action.get("topic_id", ""), "origin": "report-action"}
    if k == "sweep":
        return {"topic_id": action.get("topic_id", "")}
    if k == "watch" and _WATCH_TARGET.match(t):
        kind, a, b = t.split(":", 2)
        if kind == "price":
            return {"topic_id": action.get("topic_id", ""), "kind": "price", "provider": a, "symbol": b, "label": q[:60]}
        if kind == "wiki":
            return {"topic_id": action.get("topic_id", ""), "kind": "attention", "wiki_title": b, "label": q[:60]}
        return {"topic_id": action.get("topic_id", ""), "kind": "probability", "market_source": a, "market_id": b}
    return {}


def action_usable(action: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Tuple[bool, str]:
    if not action:
        return False, "unknown action"
    if action.get("status") in ("running", "done", "expired"):
        return False, f"already {action.get('status')}"
    exp = parse_ts(action.get("expires"))
    if exp and (now or datetime.now(timezone.utc)) > exp:
        return False, "expired"
    if action.get("kind") not in ACTIONS:
        return False, "kind not allowed"
    return True, ""


def actions_for(topic: Dict[str, Any], signals: Sequence[Dict[str, Any]], snapshot: Optional[Dict[str, Any]],
                limit: int = 8) -> List[Dict[str, Any]]:
    """What a report offers: repos to fetch, follow-up research, new questions,
    the strongest pages to read whole, entities worth their own topic."""
    out: List[Dict[str, Any]] = []
    seen = set()
    def add(a: Optional[Dict[str, Any]]) -> None:
        if a and (a["kind"], a["target"], a["query"]) not in seen and len(out) < limit:
            seen.add((a["kind"], a["target"], a["query"]))
            out.append(a)
    tid = topic.get("id", "")
    for s in signals:
        r = repo_of(s.get("url", ""))
        if r:
            add(make_action("repo", label=f"Get repo {r['owner']}/{r['repo']}", topic_id=tid, target=r["url"]))
    for q in (snapshot or {}).get("new_questions") or []:
        add(make_action("research", label=f"Research: {q[:70]}", topic_id=tid, query=q))
    for s in signals:
        for q in s.get("follow_up") or []:
            add(make_action("research", label=f"Research: {q[:70]}", topic_id=tid, query=q))
    for w in (snapshot or {}).get("watch") or []:
        add(make_action("track", label=f"Track {w[:50]}", topic_id=tid, query=w))
    add(make_action("discover", label=f"Map {topic.get('name', '')[:50]}", topic_id=tid, query=topic.get("name", "")))
    return out


# ── reports: one model, three renderings ────────────────────────────────────

def build_report(topics: Sequence[Dict[str, Any]], per_topic: Dict[str, Dict[str, Any]], *,
                 title: str = "", period_hours: int = 24, ts: str = "") -> Dict[str, Any]:
    """per_topic[tid] = {signals:[...], snapshot:{...}|None, previous:{...}|None, actions:[...], images:[...]}"""
    ts = ts or now_iso()
    sections: List[Dict[str, Any]] = []
    head_lines = []
    for t in topics:
        d = per_topic.get(t["id"]) or {}
        sn = d.get("snapshot") or {}
        if sn.get("headline"):
            head_lines.append({"topic": t["name"], "topic_id": t["id"], "text": sn["headline"],
                               "confidence": sn.get("confidence", "")})
    if head_lines:
        sections.append({"kind": "headline", "items": head_lines})
    for t in topics:
        d = per_topic.get(t["id"]) or {}
        sigs = sorted(d.get("signals") or [], key=lambda s: -(s.get("score") or 0))
        sn = d.get("snapshot") or {}
        block: Dict[str, Any] = {"kind": "topic", "topic": {k: t.get(k) for k in ("id", "name", "brief", "color")},
                                 "parts": []}
        if sn.get("state") or sn.get("changed"):
            block["parts"].append({"kind": "assessment", "state": sn.get("state", ""), "changed": sn.get("changed") or [],
                                   "confidence": sn.get("confidence", ""), "answered": sn.get("answered") or [],
                                   "new_questions": sn.get("new_questions") or []})
        if d.get("moves"):
            block["parts"].append({"kind": "moves", "items": [
                {"text": c.get("text"), "kind": c.get("kind"), "direction": c.get("direction"),
                 "stories": [{"title": s.get("title"), "url": s.get("url")} for s in (c.get("stories") or [])[:3]]}
                for c in list(d["moves"])[-6:]]})
        imgs = [s for s in sigs if s.get("image")][:4] + list(d.get("images") or [])
        if imgs:
            block["parts"].append({"kind": "images", "items": [
                {"url": i.get("image") or i.get("url"), "caption": i.get("title") or i.get("caption") or "",
                 "href": i.get("url") if i.get("image") else ""} for i in imgs[:6]]})
        if sigs:
            block["parts"].append({"kind": "signals", "items": [_sig_view(s) for s in sigs[:12]]})
        digests = [s for s in sigs if (s.get("digest") or {}).get("text")][:DEPTH_BUDGET.get(t.get("depth", "standard"), {}).get("digests", 3)]
        if digests:
            block["parts"].append({"kind": "digests", "items": [
                {"title": s.get("title"), "url": s.get("url"), "site": s.get("site"),
                 "byline": (s.get("digest") or {}).get("byline", ""), "minutes": (s.get("digest") or {}).get("minutes", 0),
                 "summary": s.get("summary", ""), "text": s["digest"]["text"]} for s in digests]})
        diagram = sn.get("diagram") or entity_diagram(t, sigs)
        if diagram:
            block["parts"].append({"kind": "diagram", "mermaid": diagram,
                                   "caption": "How the main actors relate" if sn.get("diagram") else "Who the sources name most"})
        codes = [s for s in sigs if s.get("code")][:3]
        if codes:
            block["parts"].append({"kind": "code", "items": [
                {"lang": s.get("code_lang") or "", "code": s["code"], "from": s.get("title"), "url": s.get("url")}
                for s in codes]})
        acts = d.get("actions") or []
        if acts:
            block["parts"].append({"kind": "actions", "items": [
                {k: a.get(k) for k in ("token", "kind", "label", "glyph", "target", "query")} for a in acts]})
        if block["parts"]:
            sections.append(block)
    n_sig = sum(len((per_topic.get(t["id"]) or {}).get("signals") or []) for t in topics)
    names = ", ".join(t["name"] for t in topics[:3]) + (" +" + str(len(topics) - 3) if len(topics) > 3 else "")
    return {
        "id": hashlib.sha1(f"report|{ts}|{','.join(t['id'] for t in topics)}".encode()).hexdigest()[:12],
        "title": title or (f"Intel brief - {names}" if topics else f"Intel brief - {ts[:10]}"),
        "ts": ts, "period_hours": period_hours,
        "topic_ids": [t["id"] for t in topics],
        "counts": {"topics": len(topics), "signals": n_sig},
        "sections": sections,
    }


def _sig_view(s: Dict[str, Any]) -> Dict[str, Any]:
    v = {k: s.get(k) for k in ("id", "title", "url", "site", "kind", "score", "summary", "so_what",
                               "snippet", "published", "tags", "answers")}
    if s.get("id") and s.get("url"):
        v["go"] = f"/intel/go/{s['id']}"          # opens are counted as outcomes, then redirected
    return v


_E = _html.escape


def _when(ts: Any, tz: str = "Europe/London") -> str:
    """'Mon 5 Oct 2026, 10:50' in the reader's time zone (the raw value if it will not parse)."""
    d = parse_ts(ts)
    if not d:
        return str(ts or "")
    try:
        from zoneinfo import ZoneInfo
        d = d.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        pass
    return f"{d:%a} {d.day} {d:%b %Y, %H:%M}"


def _md_inline(text: str) -> str:
    t = _E(text or "")
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)

    def _bare(m: "re.Match[str]") -> str:  # a bare URL becomes a short link: the site, not 80 characters of path
        url = m.group(2)
        host = re.sub(r"^www\.", "", urlsplit(_html.unescape(url)).netloc or "") or "link"
        return f'{m.group(1)}<a href="{url}" target="_blank" rel="noopener">{host} ↗</a>'
    t = re.sub(r'(^|[\s(])(https?://[^\s)<"]+)', _bare, t)
    return t


def _safe_href(u: str) -> str:
    u = str(u or "").strip()
    return _E(u) if u.startswith(("http://", "https://", "/")) else "#"


REPORT_CSS = """
:root{--bg:#0f1115;--card:#171a21;--bd:#2a2f3a;--fg:#e6e8ee;--dim:#9aa3b2;--acc:#7aa2f7;--ok:#9ece6a;--warn:#e0af68;--bad:#f7768e;--code:#11141a}
@media (prefers-color-scheme:light){:root{--bg:#f7f7f9;--card:#fff;--bd:#dde1e8;--fg:#1d2330;--dim:#5b6475;--acc:#3b63c9;--code:#f0f2f6}}
:root{--s1:var(--bg);--s2:var(--card);--t1:var(--fg);--t2:var(--dim);--t3:var(--dim);--ac:var(--acc);--ac2:var(--ok);--ac3:var(--warn);--bd2:var(--bd);--err:var(--bad)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:1.7rem;margin:0 0 4px}h2{font-size:1.25rem;margin:2.2em 0 .6em;padding-bottom:6px;border-bottom:1px solid var(--bd)}
h3{font-size:1.02rem;margin:1.4em 0 .5em;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
a{color:var(--acc)}.meta{color:var(--dim);font-size:.85rem}
.toc{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}.toc a{padding:3px 10px;border:1px solid var(--bd);border-radius:999px;text-decoration:none;font-size:.85rem}
.head{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 18px;margin:18px 0}
.head li{margin:.35em 0}.conf{font-size:.72rem;padding:1px 7px;border-radius:999px;border:1px solid var(--bd);color:var(--dim);margin-left:6px}
.state{background:var(--card);border-left:3px solid var(--acc);padding:10px 16px;border-radius:0 10px 10px 0}
.chg li::marker{content:"\\25B8  ";color:var(--warn)}
.sig{display:grid;grid-template-columns:42px 1fr;gap:12px;padding:12px 0;border-bottom:1px solid var(--bd)}
.score{font-weight:700;font-size:1.05rem;text-align:center;border-radius:8px;padding:4px 0;background:var(--code)}
.s-hi{color:var(--ok)}.s-md{color:var(--warn)}.s-lo{color:var(--dim)}
.sig .t{font-weight:600}.sig .why{color:var(--dim);font-style:italic}.tags span{font-size:.72rem;color:var(--dim);margin-right:8px}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.imgs figure{margin:0;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.imgs img{width:100%;height:130px;object-fit:cover;display:block}.imgs figcaption{font-size:.78rem;color:var(--dim);padding:6px 8px}
details.dg{background:var(--card);border:1px solid var(--bd);border-radius:10px;margin:10px 0;padding:0 16px}
details.dg summary{cursor:pointer;padding:12px 0;font-weight:600}details.dg .rd{font:16px/1.75 Georgia,"Iowan Old Style",serif;max-width:68ch;padding-bottom:14px}
.code{position:relative;margin:12px 0}.code pre{background:var(--code);border:1px solid var(--bd);border-radius:8px;padding:12px 14px;overflow-x:auto;font:13px/1.5 ui-monospace,Consolas,monospace}
.code .lang{position:absolute;top:6px;right:10px;font-size:.7rem;color:var(--dim)}.code .from{font-size:.8rem;color:var(--dim)}
.k{color:#bb9af7}.s{color:#9ece6a}.c{color:#6b7385;font-style:italic}.n{color:#ff9e64}
vera-mermaid{display:block;margin:12px 0}.mm{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px}
.acts{display:flex;flex-wrap:wrap;gap:8px}.act{border:1px solid var(--acc);background:transparent;color:var(--acc);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit;font-size:.88rem}
.act:hover{background:var(--acc);color:var(--bg)}.act[disabled]{opacity:.55;cursor:default}.act .r{display:block;font-size:.75rem;color:var(--dim)}
.foot{margin-top:46px;padding-top:12px;border-top:1px solid var(--bd);color:var(--dim);font-size:.8rem}
@media print{.acts,.toc{display:none}details.dg{display:block}details.dg .rd{display:block}}
.fbr{display:flex;gap:4px;margin-top:4px}.fbr button{border:1px solid rgba(127,127,127,.35);background:none;border-radius:6px;cursor:pointer;font-size:12px;padding:1px 6px}.fbr button:disabled{opacity:.6;cursor:default}
.player{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:8px;margin:10px 0;padding:8px 10px;background:var(--card);border:1px solid var(--bd);border-radius:12px;box-shadow:0 4px 14px rgba(0,0,0,.12)}.player .pp{width:40px;height:40px;border-radius:50%;border:0;background:var(--acc);color:#fff;font-size:15px;cursor:pointer;flex:0 0 auto}.player input[type=range]{flex:1;min-width:60px;accent-color:var(--acc)}.player .tm{font-size:.8rem;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}.player select{background:none;color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:2px}h2 .listen{margin-left:8px;font-size:.75rem;vertical-align:middle;border:1px solid var(--bd);background:none;color:var(--acc);border-radius:999px;padding:2px 10px;cursor:pointer}
main{overflow-wrap:anywhere}.mm{max-width:100%;overflow-x:auto}.back{position:sticky;top:0;z-index:6;padding:8px 14px;background:var(--bg);border-bottom:1px solid var(--bd)}.back a{text-decoration:none;font-size:.9rem}img,svg,video{max-width:100%;height:auto}pre{overflow-x:auto}table{display:block;overflow-x:auto}.player{top:38px}@media (max-width:640px){body{font-size:16px}main{padding:14px 12px 48px}h1{font-size:1.35rem}h2{font-size:1.12rem;margin-top:1.6em}.toc{overflow-x:auto;flex-wrap:nowrap;padding-bottom:4px}.toc a{white-space:nowrap}.player{gap:6px;padding:6px 8px}.player .tm{font-size:.72rem}.player select{display:none}button.act{width:100%;text-align:left}.fbr button{padding:4px 10px;font-size:15px}}
@media print{.back,.player,.fbr,h2 .listen{display:none}}
"""

_KW = r"\b(def|class|return|import|from|if|else|elif|for|while|in|async|await|function|const|let|var|new|try|except|catch|fn|pub|struct|impl|use|package|func|go|yield|with|as|lambda|true|false|null|None|True|False)\b"


_TOK = re.compile(r"(?P<c>(?:#|//|--).*$)|(?P<s>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')|(?P<k>" + _KW + r")|(?P<n>\b\d+(?:\.\d+)?\b)")


def highlight(code: str, lang: str = "") -> str:
    """A small, safe highlighter (comments, strings, keywords, numbers): the raw
    line is split into tokens first and each piece escaped on its own, so markup
    is never re-scanned."""
    out = []
    for line in (code or "").splitlines():
        parts, pos = [], 0
        for m in _TOK.finditer(line):
            parts.append(_E(line[pos:m.start()]))
            parts.append(f'<span class="{m.lastgroup}">{_E(m.group(0))}</span>')
            pos = m.end()
        parts.append(_E(line[pos:]))
        out.append("".join(parts))
    return "\n".join(out)


def _score_cls(v: Any) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "s-lo"
    return "s-hi" if v >= 7 else "s-md" if v >= 4 else "s-lo"


# The <vera-mermaid> element renders itself when it connects: it takes its text,
# clears it and draws into its shadow root. (A boot script that "helped" by
# calling render() on elements without a light-DOM <svg> re-rendered every one of
# them with the now-empty text - every diagram in every report came out blank.)
# The renderer is inlined, so a report opened as a file, from an email or offline
# still draws its diagrams; the served copy is the fallback.
_MERMAID_JS: List[str] = []


def _mermaid_script() -> str:
    if not _MERMAID_JS:
        try:
            from pathlib import Path
            js = (Path(__file__).resolve().parents[1] / "render" / "vera_mermaid.js").read_text(encoding="utf-8")
            # only a literal "</script" could end the inline block early; nothing else is touched
            # (escaping every "</" broke a regex literal in the renderer)
            _MERMAID_JS.append("<script>" + re.sub(r"</(script)", r"<\\/\1", js, flags=re.I) + "</script>")
        except OSError:
            _MERMAID_JS.append('<script src="/ui/elements/vera_mermaid.js"></script>')
    # a phone has no room for a left-to-right chart: turn it top-down before the element reads its text
    narrow = ('<script>if(window.matchMedia&&matchMedia("(max-width: 640px)").matches)'
              'document.querySelectorAll("vera-mermaid").forEach(function(e){'
              r'e.textContent=e.textContent.replace(/^(\s*(?:flowchart|graph))\s+(LR|RL)\b/,"$1 TD");});</script>')
    return narrow + _MERMAID_JS[0]


def _has_diagram(doc: Dict[str, Any]) -> bool:
    return any(p.get("kind") == "diagram" for sec in doc.get("sections") or [] for p in sec.get("parts") or [])

# The page's own buttons post the one-time token back; the server looks the
# action up and builds the call itself, so the page can never choose a capability.
_ACTION_JS = """<script>(function(){
document.addEventListener('click',function(ev){var f=ev.target.closest('button.fbk[data-sid]');if(f){
var row=f.parentNode;row.querySelectorAll('button').forEach(function(x){x.disabled=true;});
fetch('/intel/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signal_id:f.dataset.sid,verdict:f.dataset.v})})
.then(function(x){return x.json();}).then(function(j){var c=j&&(j.content||j);f.textContent=c&&c.error?'\\u2717':'\\u2713';f.title=(c&&c.error)||'thanks - this teaches the gates';})
.catch(function(){f.textContent='\\u2717';row.querySelectorAll('button').forEach(function(x){x.disabled=false;});});return;}
var b=ev.target.closest('button.act[data-token]');if(!b)return;
b.disabled=true;var r=b.querySelector('.r');if(r)r.textContent='starting\\u2026';
fetch('/intel/action/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:b.dataset.token})})
.then(function(x){return x.json();}).then(function(j){var c=j&&(j.content||j);if(r)r.textContent=c.error?('\\u2717 '+c.error):('\\u2713 '+(c.message||'started'));if(c.error)b.disabled=false;})
.catch(function(e){if(r)r.textContent='\\u2717 '+e;b.disabled=false;});});})();</script>"""


def render_html(doc: Dict[str, Any], *, base_url: str = "") -> str:
    """The report as one self-contained page (themed, printable, diagrams via
    <vera-mermaid>, reader-mode digests that fold, highlighted code, action buttons)."""
    parts: List[str] = []
    title = doc.get("title") or "Intel brief"
    topics = [s for s in doc.get("sections") or [] if s.get("kind") == "topic"]
    parts.append(f'<header><h1>{_E(title)}</h1><div class="meta">{_E(_when(doc.get("ts", "")))} · '
                 f'{doc.get("counts", {}).get("topics", 0)} topics · {doc.get("counts", {}).get("signals", 0)} signals'
                 f' · last {int(doc.get("period_hours") or 24)}h</div></header>')
    if doc.get("note"):
        parts.append(f'<div class="meta" style="margin:6px 0 10px">{_E(doc["note"])}</div>')
    audio = doc.get("audio") or {}
    if audio.get("url"):
        parts.append(audio_player(audio))
    starts = {c.get("id"): c.get("start") for c in audio.get("chapters") or []}
    if doc.get("top_line"):
        parts.append('<section class="head"><h3 style="margin-top:0">Top line</h3><ul>'
                     + "".join(f"<li>{_md_inline(x)}</li>" for x in doc["top_line"]) + "</ul></section>")
    if len(topics) > 1:
        parts.append('<nav class="toc">' + "".join(
            f'<a href="#t-{_E(s["topic"]["id"])}">{_E(s["topic"]["name"])}</a>' for s in topics) + "</nav>")
    for sec in doc.get("sections") or []:
        if sec.get("kind") == "headline":
            parts.append('<section class="head"><h3 style="margin-top:0">Top line</h3><ul>' + "".join(
                f'<li><b>{_E(h["topic"])}</b> — {_md_inline(h["text"])}'
                + (f'<span class="conf">{_E(h["confidence"])} confidence</span>' if h.get("confidence") else "")
                + "</li>" for h in sec["items"]) + "</ul></section>")
        elif sec.get("kind") == "topic":
            t = sec["topic"]
            lb = (f' <button class="listen" data-start="{float(starts[t["id"]]):.1f}" title="listen to this topic">'
                  f'\u25b6 Listen</button>') if starts.get(t["id"]) is not None else ""
            parts.append(f'<section id="t-{_E(t["id"])}"><h2>{_E(t["name"])}{lb}</h2>')
            if t.get("brief"):
                parts.append(f'<div class="meta">{_E(t["brief"])}</div>')
            for p in sec.get("parts") or []:
                parts.append(_render_part(p))
            parts.append("</section>")
    if doc.get("competitors") or doc.get("competitor_note"):
        parts.append('<section id="competitors"><h2>Competitor watch</h2>'
                     + (f'<div class="state">{_md_inline(doc["competitor_note"])}</div>' if doc.get("competitor_note") else "")
                     + "".join(f'<div class="sig"><div class="score s-md">\u25c6</div><div><div class="t">'
                               f'<a href="{_safe_href(c.get("url"))}" target="_blank" rel="noopener">{_E(c.get("title") or "")}</a></div>'
                               f'<div class="meta">{_E(c.get("firm") or "")} \u00b7 {_E(c.get("topic") or "")}</div></div></div>'
                               for c in doc.get("competitors") or []) + "</section>")
    if doc.get("notes"):
        parts.append('<section id="notes"><h2>From your notes</h2><ul>'
                     + "".join(f"<li>{_md_inline(n)}</li>" for n in doc["notes"]) + "</ul></section>")
    if doc.get("follow_up_actions") or doc.get("follow_ups"):
        acts = doc.get("follow_up_actions") or []
        parts.append('<section id="followups"><h2>Suggested follow-ups</h2>'
                     + ('<div class="acts">' + "".join(
                         f'<button class="act" data-token="{_E(a["token"])}">{_E(a.get("glyph") or "")} {_E(a.get("label") or "")}'
                         f'<span class="r"></span></button>' for a in acts if a.get("token")) + "</div>" if acts else
                        "<ul>" + "".join(f"<li>{_E(q)}</li>" for q in doc.get("follow_ups") or []) + "</ul>")
                     + "</section>")
    parts.append(f'<div class="foot">Vera intelligence \u00b7 report {_E(doc.get("id", ""))}'
                 + (f' · <a href="{_safe_href(base_url + "/intel/panel")}">open in Vera</a>' if base_url is not None else "")
                 + "</div>")
    # every report page is part of the installable app: its manifest, a way back to
    # the list, and the worker that keeps it readable offline
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            + _app.HEAD +
            f"<title>{_E(title)}</title><style>{REPORT_CSS}</style></head><body>"
            '<nav class="back"><a href="/intel/app">‹ Briefs</a></nav><main>'
            + "".join(parts) + "</main>" + (_mermaid_script() if _has_diagram(doc) else "") + _ACTION_JS
            + (_PLAYER_JS if audio.get("url") else "") + _app.REGISTER
            + "</body></html>")


FEEDBACK_BUTTONS = (("useful", "\U0001F44D", "useful - more like this"),
                    ("not_useful", "\U0001F44E", "not useful"),
                    ("mute_site", "\U0001F507", "never show this site again for this topic"))


def _speakable(text: str) -> str:
    """Text a voice can read: no links, markup or brackets left over."""
    t = re.sub(r"\[([^\]]+)\]\((?:https?://|/)[^)]*\)", r"\1", str(text or ""))
    t = re.sub(r"\(?https?://\S+\)?", "", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _chunks(text: str, limit: int = 600) -> List[str]:
    """Sentence-sized pieces for the speech server, none longer than `limit`."""
    out: List[str] = []
    cur = ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        while len(sent) > limit:                       # a run-on sentence: cut at a comma or a space
            cut = max(sent.rfind(", ", 0, limit), sent.rfind(" ", 0, limit))
            cut = cut if cut > limit // 3 else limit
            out.append((cur + " " + sent[:cut]).strip()) if cur else out.append(sent[:cut].strip())
            cur, sent = "", sent[cut:].lstrip(", ")
        if len(cur) + len(sent) + 1 > limit and cur:
            out.append(cur.strip())
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur.strip():
        out.append(cur.strip())
    return [c for c in out if c]


def spoken_script(doc: Dict[str, Any], *, per_topic_chars: int = 1100) -> List[Dict[str, Any]]:
    """The report as narration, in chapters: an intro (date, the night's note, the
    top line) and one chapter per topic (its headline, what changed, what moved).
    Each chapter is {id, title, chunks}."""
    when = parse_ts(doc.get("ts"))
    day = when.strftime("%A %d %B") if when else ""
    intro = [f"{doc.get('title') or 'Intel brief'}{', ' + day if day else ''}."]
    if doc.get("note"):
        intro.append(_speakable(doc["note"]))
    heads: Dict[str, str] = {}
    for sec in doc.get("sections") or []:
        if sec.get("kind") == "headline":
            for h in sec.get("items") or []:
                heads[h.get("topic_id") or ""] = _speakable(h.get("text") or "")
    if doc.get("top_line"):
        intro.append("The top line. " + " ".join(_speakable(x).rstrip(".") + "." for x in doc["top_line"]))
    elif heads:
        intro.append(f"{len(heads)} topics have news.")
    chapters = [{"id": "intro", "title": "Top line", "chunks": _chunks(" ".join(intro))}]
    for sec in doc.get("sections") or []:
        if sec.get("kind") != "topic":
            continue
        t = sec.get("topic") or {}
        words = [f"{t.get('name')}."]
        if heads.get(t.get("id") or ""):
            words.append(heads[t["id"]])
        for p in sec.get("parts") or []:
            if p.get("kind") == "assessment" and p.get("changed"):
                words.append("What changed: " + "; ".join(_speakable(c) for c in p["changed"][:4]) + ".")
            if p.get("kind") == "moves" and p.get("items"):
                words.append("What moved: " + "; ".join(_speakable(m.get("text")) for m in p["items"][:3]) + ".")
        text = " ".join(w for w in words if w)[:per_topic_chars]
        if len(words) > 1:
            chapters.append({"id": t.get("id") or "", "title": t.get("name") or "", "chunks": _chunks(text)})
    return [c for c in chapters if c["chunks"]]


def _fmt_secs(s: Any) -> str:
    s = int(float(s or 0))
    return f"{s // 60}:{s % 60:02d}"


def audio_player(audio: Dict[str, Any]) -> str:
    """A sticky player for a report's narration: play/pause, seek, time, speed;
    chapters are played from the 'Listen' buttons on each topic."""
    src = _safe_href(audio.get("url") or "")
    if not src or src == "#":
        return ""
    total = _fmt_secs(audio.get("seconds"))
    return (f'<div class="player" id="player"><audio id="au" preload="none" src="{src}"></audio>'
            f'<button class="pp" id="pp" aria-label="play">▶</button>'
            f'<input type="range" id="seek" min="0" max="{int(float(audio.get("seconds") or 0))}" value="0" step="1" aria-label="position">'
            f'<span class="tm" id="tm">0:00 / {total}</span>'
            f'<select id="rate" aria-label="speed"><option value="1">1×</option><option value="1.25">1.25×</option>'
            f'<option value="1.5">1.5×</option><option value="1.75">1.75×</option></select></div>')


_PLAYER_JS = r"""<script>(function(){var a=document.getElementById('au');if(!a)return;var pp=document.getElementById('pp'),sk=document.getElementById('seek'),tm=document.getElementById('tm'),rt=document.getElementById('rate');
function f(s){s=Math.floor(s||0);return Math.floor(s/60)+':'+('0'+s%60).slice(-2);}
function sync(){pp.textContent=a.paused?'▶':'❚❚';pp.setAttribute('aria-label',a.paused?'play':'pause');}
pp.onclick=function(){if(a.paused)a.play();else a.pause();};a.onplay=sync;a.onpause=sync;a.onended=sync;
a.ontimeupdate=function(){sk.value=a.currentTime;tm.textContent=f(a.currentTime)+' / '+f(a.duration||sk.max);};
sk.oninput=function(){a.currentTime=+sk.value;};rt.onchange=function(){a.playbackRate=+rt.value;};
document.addEventListener('click',function(e){var b=e.target.closest('button.listen[data-start]');if(!b)return;a.currentTime=+b.dataset.start;a.play();});
if('mediaSession' in navigator){try{navigator.mediaSession.metadata=new MediaMetadata({title:document.title,artist:'Vera intelligence'});
navigator.mediaSession.setActionHandler('play',function(){a.play();});navigator.mediaSession.setActionHandler('pause',function(){a.pause();});
navigator.mediaSession.setActionHandler('seekbackward',function(){a.currentTime=Math.max(0,a.currentTime-15);});
navigator.mediaSession.setActionHandler('seekforward',function(){a.currentTime=a.currentTime+15;});}catch(e){}}})();</script>"""


def _render_part(p: Dict[str, Any]) -> str:
    k = p.get("kind")
    if k == "assessment":
        h = '<h3>Assessment</h3>'
        if p.get("state"):
            h += f'<div class="state">{_md_inline(p["state"])}</div>'
        if p.get("changed"):
            h += '<h3>What changed</h3><ul class="chg">' + "".join(f"<li>{_md_inline(c)}</li>" for c in p["changed"]) + "</ul>"
        if p.get("answered"):
            h += '<h3>Questions answered</h3><ul>' + "".join(
                f'<li><b>{_E(a["question"])}</b><br>{_md_inline(a.get("answer", ""))}</li>' for a in p["answered"]) + "</ul>"
        if p.get("new_questions"):
            h += '<h3>New questions</h3><ul>' + "".join(f"<li>{_E(q)}</li>" for q in p["new_questions"]) + "</ul>"
        return h
    if k == "moves":
        rows = []
        for m in p.get("items") or []:
            arrow = "▲" if m.get("direction") == "up" else "▼"
            links = " · ".join(f'<a href="{_safe_href(s.get("url"))}" target="_blank" rel="noopener">{_E(s.get("title") or "")}</a>'
                                    for s in m.get("stories") or [])
            why = (f'<br><span class="meta">around then: {links}</span>' if links
                   else '<br><span class="meta">no story in the window explains it</span>')
            rows.append(f"<li>{arrow} {_E(m.get('text') or '')}{why}</li>")
        return '<h3>What moved</h3><ul class="chg">' + "".join(rows) + "</ul>"
    if k == "images":
        return '<h3>Images</h3><div class="imgs">' + "".join(
            f'<figure>{"<a href=" + chr(34) + _safe_href(i["href"]) + chr(34) + " target=_blank rel=noopener>" if i.get("href") else ""}'
            f'<img loading="lazy" src="{_safe_href(i["url"])}" alt="{_E(i.get("caption", ""))}">'
            f'{"</a>" if i.get("href") else ""}<figcaption>{_E(i.get("caption", ""))}</figcaption></figure>'
            for i in p.get("items") or [] if i.get("url")) + "</div>"
    if k == "signals":
        rows = []
        for s in p.get("items") or []:
            sc = s.get("score")
            rows.append(
                f'<div class="sig"><div class="score {_score_cls(sc)}">{"" if sc is None else int(round(sc))}</div><div>'
                f'<div class="t"><a href="{_safe_href(s.get("go") or s.get("url"))}" target="_blank" rel="noopener">{_E(s.get("title") or "")}</a></div>'
                f'<div class="meta">{_E(s.get("site") or "")} · {_E(s.get("kind") or "")}'
                + (f' · {_E(str(s.get("published"))[:10])}' if s.get("published") else "") + "</div>"
                + (f'<div>{_md_inline(s.get("summary") or s.get("snippet") or "")}</div>')
                + (f'<div class="why">{_md_inline(s["so_what"])}</div>' if s.get("so_what") else "")
                + ('<div class="tags">' + "".join(f"<span>#{_E(t)}</span>" for t in s.get("tags") or []) + "</div>" if s.get("tags") else "")
                + (f'<div class="fbr">' + "".join(
                    f'<button class="fbk" data-sid="{_E(s["id"])}" data-v="{v}" title="{tip}">{g}</button>'
                    for v, g, tip in FEEDBACK_BUTTONS) + "</div>" if s.get("id") else "")
                + "</div></div>")
        return "<h3>Signals</h3>" + "".join(rows)
    if k == "digests":
        return "<h3>Reader digests</h3>" + "".join(
            f'<details class="dg"><summary>{_E(d.get("title") or "")} <span class="meta">· {_E(d.get("site") or "")}'
            + (f' · {int(d["minutes"])} min read' if d.get("minutes") else "") + "</span></summary>"
            + (f'<p class="meta">{_md_inline(d["summary"])}</p>' if d.get("summary") else "")
            + '<div class="rd">' + "".join(f"<p>{_md_inline(x)}</p>" for x in (d.get("text") or "").split("\n\n") if x.strip())
            + f'<p class="meta"><a href="{_safe_href(d.get("url"))}" target="_blank" rel="noopener">Original ↗</a></p></div></details>'
            for d in p.get("items") or [])
    if k == "diagram":
        return (f'<h3>{_E(p.get("caption") or "Diagram")}</h3><div class="mm"><vera-mermaid bare>'
                f'{_E(p.get("mermaid") or "")}</vera-mermaid></div>')
    if k == "code":
        return "<h3>Code</h3>" + "".join(
            f'<div class="code"><div class="from">from <a href="{_safe_href(c.get("url"))}" target="_blank" rel="noopener">{_E(c.get("from") or "")}</a></div>'
            f'<span class="lang">{_E(c.get("lang") or "")}</span><pre><code>{highlight(c.get("code") or "", c.get("lang") or "")}</code></pre></div>'
            for c in p.get("items") or [])
    if k == "actions":
        return '<h3>Vera can</h3><div class="acts">' + "".join(
            f'<button class="act" data-token="{_E(a["token"])}" title="{_E(a.get("query") or a.get("target") or "")}">'
            f'{_E(a.get("glyph") or "")} {_E(a.get("label") or "")}<span class="r"></span></button>'
            for a in p.get("items") or [] if a.get("token")) + "</div>"
    return ""


def render_markdown(doc: Dict[str, Any], *, report_url: str = "") -> str:
    out = [f"# {doc.get('title')}", f"_{doc.get('ts', '')} \u00b7 {doc.get('counts', {}).get('signals', 0)} signals_", ""]
    if doc.get("note"):
        out += [f"_{doc['note']}_", ""]
    if doc.get("top_line"):
        out += ["## Top line"] + [f"- {x}" for x in doc["top_line"]] + [""]
    for sec in doc.get("sections") or []:
        if sec["kind"] == "headline":
            out.append("## Top line")
            out += [f"- **{h['topic']}** — {h['text']}" for h in sec["items"]]
            out.append("")
        elif sec["kind"] == "topic":
            out.append(f"## {sec['topic']['name']}")
            for p in sec.get("parts") or []:
                if p["kind"] == "assessment":
                    if p.get("state"):
                        out += [p["state"], ""]
                    if p.get("changed"):
                        out.append("**What changed**")
                        out += [f"- {c}" for c in p["changed"]]
                        out.append("")
                elif p["kind"] == "signals":
                    for s in p["items"][:8]:
                        sc = "" if s.get("score") is None else f"[{int(round(s['score']))}/10] "
                        out.append(f"- {sc}[{s.get('title')}]({s.get('url')}) — {s.get('summary') or s.get('snippet') or ''}".strip())
                    out.append("")
                elif p["kind"] == "moves":
                    out.append("**What moved**")
                    out += [f"- {m.get('text')}" + (f" (around then: {m['stories'][0].get('title')})" if m.get("stories") else "")
                            for m in p["items"]]
                    out.append("")
                elif p["kind"] == "diagram":
                    out += ["```mermaid", p["mermaid"], "```", ""]
                elif p["kind"] == "code":
                    for c in p["items"]:
                        out += [f"```{c.get('lang') or ''}", c["code"], "```", ""]
                elif p["kind"] == "actions":
                    out.append("**Vera can:** " + " · ".join(a["label"] for a in p["items"]))
                    out.append("")
    if doc.get("competitors") or doc.get("competitor_note"):
        out.append("## Competitor watch")
        if doc.get("competitor_note"):
            out += [doc["competitor_note"], ""]
        out += [f"- **{c.get('firm')}**: [{c.get('title')}]({c.get('url')})" for c in doc.get("competitors") or []]
        out.append("")
    if doc.get("notes"):
        out += ["## From your notes"] + [f"- {n}" for n in doc["notes"]] + [""]
    if doc.get("follow_ups"):
        out += ["## Suggested follow-ups"] + [f"- {q}" for q in doc["follow_ups"]] + [""]
    if report_url:
        out.append(f"[Full report]({report_url})")
    return "\n".join(out).strip() + "\n"


def render_telegram(doc: Dict[str, Any], *, report_url: str = "", action_url: str = "",
                    max_chars: int = 3600, go_base: str = "") -> Dict[str, Any]:
    """{text (Telegram HTML), buttons [[{text,url}]], photo} - the brief as one
    message; the buttons open the full report and run the top actions."""
    E = _E
    lines = [f"<b>{E(doc.get('title') or 'Intel brief')}</b>",
             f"<i>{E(str(doc.get('ts', ''))[:16].replace('T', ' '))} · {doc.get('counts', {}).get('signals', 0)} signals</i>", ""]
    photo = ""
    acts: List[Dict[str, Any]] = list(doc.get("follow_up_actions") or [])
    if doc.get("note"):
        lines += [f"<i>{E(doc['note'])}</i>", ""]
    if doc.get("top_line"):
        lines += [f"\u25b8 {E(x)}" for x in doc["top_line"]] + [""]
    for c in (doc.get("competitors") or [])[:3]:
        lines.append(f"\u25c6 <b>{E(c.get('firm') or '')}</b>: <a href=\"{E(c.get('url') or '')}\">{E((c.get('title') or '')[:100])}</a>")
    if doc.get("competitors"):
        lines.append("")
    for sec in doc.get("sections") or []:
        if sec["kind"] == "headline" and doc.get("top_line"):
            continue
        if sec["kind"] == "headline":
            for h in sec["items"]:
                lines.append(f"▸ <b>{E(h['topic'])}</b> — {E(h['text'])}")
            lines.append("")
        elif sec["kind"] == "topic":
            lines.append(f"<b>{E(sec['topic']['name'])}</b>")
            for p in sec.get("parts") or []:
                if p["kind"] == "assessment" and p.get("changed"):
                    lines += [f"  • {E(c)}" for c in p["changed"][:3]]
                if p["kind"] == "moves":
                    lines += [f"  {chr(0x25B2) if m.get('direction') == 'up' else chr(0x25BC)} {E(m.get('text') or '')}"
                              for m in p["items"][-2:]]
                elif p["kind"] == "signals":
                    for s in p["items"][:3]:
                        sc = "" if s.get("score") is None else f"[{int(round(s['score']))}] "
                        href = (go_base + s["go"]) if (go_base and s.get("go")) else (s.get("url") or "")
                        lines.append(f"  {sc}<a href=\"{E(href)}\">{E((s.get('title') or '')[:110])}</a>")
                elif p["kind"] == "images" and not photo:
                    photo = next((i["url"] for i in p["items"] if str(i.get("url", "")).startswith("http")), "")
                elif p["kind"] == "actions":
                    acts += p["items"]
            lines.append("")
    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 40].rsplit("\n", 1)[0] + "\n… (full report below)"
    buttons: List[List[Dict[str, str]]] = []
    if report_url:
        buttons.append([{"text": "\U0001f4c4 Full report", "url": report_url}])
    if action_url:
        row: List[Dict[str, str]] = []
        for a in acts[:4]:
            row.append({"text": (a.get("glyph", "") + " " + a.get("label", ""))[:40],
                        "url": f"{action_url}?token={a['token']}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    return {"text": text, "buttons": buttons, "photo": photo}


# ── delivery channels (the intel twin of vera/delivery.py) ──────────────────

#: id -> how intel sends a report there. `render` names which rendering it takes.
CHANNELS: Dict[str, Dict[str, Any]] = {
    "html":     {"label": "Report page", "render": "html", "cap": "", "always": True},
    "telegram": {"label": "Telegram", "render": "telegram", "cap": "tg.send_rich"},
    "chat":     {"label": "Chat", "render": "markdown", "cap": "chat.deliver", "needs_target": "session_id"},
    "email":    {"label": "Email", "render": "html", "cap": "mail.send", "needs_target": "to"},
    "canvas":   {"label": "Canvas", "render": "panel", "cap": "canvas.add", "needs_target": "session_id"},
    "notebook": {"label": "Notebook", "render": "markdown", "cap": "research.notebook.create"},
    "memory":   {"label": "Memory", "render": "markdown", "cap": "memory.store"},
    "fabric":   {"label": "Fabric", "render": "markdown", "cap": "", "always": True},
    "phone":    {"label": "Phone", "render": "short", "cap": "ha.notify"},
    "podcast":  {"label": "Podcast", "render": "markdown", "cap": "background.cap.enqueue"},
    "print":    {"label": "Printer", "render": "short", "cap": "print.text"},
}


def short_text(doc: Dict[str, Any], limit: int = 600) -> str:
    """The brief as a few lines for a phone notification or a till roll."""
    lines = [f"{doc.get('counts', {}).get('signals', 0)} new items"]
    if doc.get("top_line"):
        lines += [f"- {re.sub(r'[(]https?://[^)]*[)]', '', x).strip()}" for x in doc["top_line"]]
    else:
        for sec in doc.get("sections") or []:
            if sec.get("kind") == "headline":
                lines += [f"- {h['topic']}: {h['text']}" for h in sec["items"]]
            elif sec.get("kind") == "topic":
                for p in sec.get("parts") or []:
                    if p.get("kind") == "signals":
                        lines += [f"- {s.get('title')}" for s in p["items"][:2]]
    return "\n".join(lines)[:limit]


def channel_args(channel: str, doc: Dict[str, Any], *, report_url: str, action_url: str = "",
                 html_doc: str = "", target: str = "") -> Optional[Dict[str, Any]]:
    """The arguments for a channel's capability, or None when it cannot be sent
    (an unknown channel, or one that needs a target it was not given)."""
    ch = CHANNELS.get(channel)
    if not ch:
        return None
    if ch.get("needs_target") and not target:
        return None
    title = doc.get("title") or "Intel brief"
    md = render_markdown(doc, report_url=report_url)
    if channel == "telegram":
        tg = render_telegram(doc, report_url=report_url, action_url=action_url,
                             go_base=action_url.rsplit("/intel/", 1)[0] if "/intel/" in action_url else "")
        return {"text": tg["text"], "buttons": json.dumps(tg["buttons"]), "photo_url": tg["photo"], "parse_mode": "HTML"}
    if channel == "chat":
        return {"session_id": target, "report": md, "title": title}
    if channel == "email":
        return {"to": target, "subject": title, "body": html_doc or md, "html": bool(html_doc)}
    if channel == "canvas":
        where = {"id": target} if target.startswith("cv_") else {"session_id": target}
        return dict(where, kind="panel", key=f"panel:intel-report-{doc.get('id')}", size="l", at="now",
                    content={"panel": "intel-report", "title": title, "src": report_url})
    if channel == "phone":
        return {"target": target or "mobile_app_smasnug", "title": title[:80], "message": short_text(doc, 240),
                "data": {"url": report_url, "clickAction": report_url}}
    if channel == "notebook":
        return {"title": title, "description": md}
    if channel == "memory":
        return {"text": md[:4000], "category": "intel", "tags": "intel,report",
                "record_type": "intel_report", "importance": 0.5}
    if channel == "podcast":
        # TTS is GPU work: it goes through the idle queue, never straight onto the box
        return {"name": "podcast.generate", "title": f"intel podcast: {title[:60]}", "timeout_s": 3600,
                "submitted_by": "intel", "id": f"intel-podcast-{doc.get('id')}",
                "arguments": {"topic": title[:200], "style": "news", "show_name": "Vera Signal",
                              "sources": [{"type": "text", "text": md[:20000], "label": title}],
                              "instructions": ("Present this as a concise morning news briefing for the reader. Lead with "
                                               "what actually changed and why it matters. Group by theme, do not read URLs "
                                               "aloud, skip anything with no substance, and never invent facts."),
                              "wait": True}}
    if channel == "print":
        return {"text": short_text(doc, 600) + ("\nPage on Vera" if report_url else ""), "title": title[:24],
                "printer_id": target or "", "cut": True}
    return {}


# ── lookup: what tracking found on a subject, sized for a chat answer (info-seeking plan SK-3.4) ───────────────

def _words(text: Any) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9+.#-]*", str(text or "").lower()):
        w = w.strip(".-")
        if len(w) < 2 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


# words that ask for news rather than name a subject: "updates on MCP" is about MCP
_ASK_WORDS = set("update updates new recent recently development developments happening going tell me anything "
                 "whats what's is in on of to a an me please give show find out look up".split())


def match_topics(query: str, topics: Sequence[Dict[str, Any]], *, floor: float = 0.45) -> List[Tuple[float, Dict[str, Any]]]:
    """Topics a free-text subject refers to, best first, as (score 0..1, topic). Each subject word is weighted by
    how rare it is across the topics (\"uk\" or \"ai\" names half of them and says little; \"bitcoin\" names none), and
    counts in full when it is in the topic's name or id, two thirds in its entities, one third in its brief or
    questions. The subject as a phrase inside the topic's name or id adds 0.2 (\"uk news\" is the news topic, not
    every UK one). An exact id or name scores 1 outright."""
    import math
    q = str(query or "").strip()
    qs = slug(q, 80)
    qw = {w for w in _words(q) if w not in _ASK_WORDS}
    phrase = slug(" ".join(w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in _ASK_WORDS
                             and w not in ("latest", "the")), 120) if q else ""
    phrase = phrase if phrase != "topic" else ""
    fields = []
    for t in topics or []:
        nw = _words(t.get("name")) | _words(str(t.get("id", "")).replace("-", " "))
        ew = set().union(*[_words(e) for e in (t.get("entities") or [])]) if t.get("entities") else set()
        bw = _words(t.get("brief")) | _words(" ".join(_question_str(x) for x in (t.get("questions") or [])))
        fields.append((t, nw, ew, bw))
    n = max(1, len(fields))
    df = {w: sum(1 for _, nw, ew, bw in fields if w in nw or w in ew or w in bw) for w in qw}
    wt = {w: math.log(1.0 + n / (1.0 + df[w])) for w in qw}
    total = sum(wt.values()) or 1.0
    out: List[Tuple[float, float, Dict[str, Any]]] = []
    for t, nw, ew, bw in fields:
        if q and (qs == t.get("id") or q.lower() == str(t.get("name") or "").lower()):
            out.append((1.0, 9.0, t)); continue
        if not qw:
            continue
        pts = sum(wt[w] * (1.0 if w in nw else 2 / 3 if w in ew else 1 / 3 if w in bw else 0.0) for w in qw)
        sc = pts / total
        if phrase and (phrase in str(t.get("id") or "") or phrase in slug(t.get("name") or "", 120)):
            sc += 0.2
        if sc >= floor:
            out.append((round(min(1.0, sc), 3), sc, t))   # sorted on the uncapped score
    out.sort(key=lambda x: (-x[1], str(x[2].get("name") or "")))
    return [(a, t) for a, _, t in out]


def _clean_title(t: Any) -> str:
    s = str(t or "").strip()
    s = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", s, flags=re.S)
    return _html.unescape(s).strip()


def age_days(ts: Any, now: Optional[datetime] = None) -> Optional[float]:
    d = parse_ts(ts)
    if not d:
        return None
    return round(((now or datetime.now(timezone.utc)) - d).total_seconds() / 86400.0, 1)


def lookup_view(topic: Dict[str, Any], snapshot: Optional[Dict[str, Any]], signals: Sequence[Dict[str, Any]], *,
                since: str = "", limit: int = 8, now: Optional[datetime] = None) -> Dict[str, Any]:
    """A tracked topic as a chat-sized answer: the latest assessment with its age, and the strongest signals seen
    since `since` with their dates - never the stored vectors or page digests."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for s in signals:
        if not s.get("url") and not s.get("title"):
            continue
        rows.append({
            "title": _clean_title(s.get("title"))[:160],
            "url": s.get("url") or "",
            "site": s.get("site") or "",
            "published": str(s.get("published") or "")[:10],
            "seen": str(s.get("seen") or "")[:10],
            "score": s.get("score"),
            "summary": clean_prose(s.get("summary") or s.get("snippet") or "")[:300],
        })
        if len(rows) >= limit:
            break
    snap = None
    if snapshot:
        snap = {"headline": clean_prose(snapshot.get("headline") or "")[:400],
                "state": str(snapshot.get("state") or "")[:300],
                "ts": snapshot.get("ts") or "", "age_days": age_days(snapshot.get("ts"), now)}
    return {"tracked": True,
            "topic": {"id": topic.get("id"), "name": topic.get("name"), "brief": str(topic.get("brief") or "")[:200],
                      "cadence": topic.get("cadence"), "last_sweep": topic.get("last_sweep") or ""},
            "snapshot": snap, "since": since, "signals": rows}
