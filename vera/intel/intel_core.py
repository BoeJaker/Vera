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


def _clean_list(v: Any, n: int = 24) -> List[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n;]+", v)]
    out: List[str] = []
    for x in v or []:
        t = str(x or "").strip()
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
        "color": str(raw.get("color", base.get("color", "")) or "").strip(),
        "created": base.get("created") or now_iso(),
        "updated": now_iso(),
        "last_sweep": base.get("last_sweep", ""),
        "last_snapshot": base.get("last_snapshot", ""),
        "query_cursor": int(base.get("query_cursor", 0) or 0),
        "origin": str(raw.get("origin", base.get("origin", "")) or ""),
    }
    return out


def _num(v: Any, d: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def topic_queries(topic: Dict[str, Any]) -> List[str]:
    """Every search that feeds a topic: the user's own first, then ones made from
    its name, its open questions and the entities it follows."""
    out: List[str] = []
    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q or "").strip()
        if q and q.lower() not in [x.lower() for x in out]:
            out.append(q)
    for q in topic.get("queries") or []:
        add(q)
    name = topic.get("name") or ""
    add(f"{name} latest news")
    for q in (topic.get("questions") or [])[:4]:
        add(q)
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
    title = re.sub(r"\s+", " ", str(raw.get("title") or raw.get("name") or "")).strip()
    snippet = re.sub(r"\s+", " ", str(raw.get("snippet") or raw.get("description")
                                       or raw.get("summary") or raw.get("text") or "")).strip()
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
        "site": (_nowww(urlsplit(url).netloc.lower()) if url else ""),
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


def enrich_prompt(topic: Dict[str, Any], signal: Dict[str, Any], page_text: str, words: int = 220) -> str:
    qs = "\n".join(f"- {q}" for q in (topic.get("questions") or [])[:6]) or "- (none recorded)"
    return (
        f"You are an intelligence analyst following the topic \"{topic.get('name')}\".\n"
        f"What the topic is: {topic.get('brief') or topic.get('name')}\n"
        f"Why it matters to the reader: {topic.get('why') or 'not stated'}\n"
        f"Open questions:\n{qs}\n\n"
        f"Read this source and answer in JSON only. Your whole answer is about {words} words.\n"
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
        "answers": _clean_list(p.get("answers"), 8),
        "entities": _clean_list(p.get("entities"), 16),
        "tags": [t.lower() for t in _clean_list(p.get("tags"), 6)],
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
                    signals: Sequence[Dict[str, Any]], words: int = 380) -> str:
    prev = (previous or {}).get("state") or "(no earlier assessment - this is the first)"
    lines = []
    for s in signals[:14]:
        lines.append(f"- [{s.get('score') if s.get('score') is not None else '?'}/10] {s.get('title')}"
                     f" ({s.get('site') or s.get('source')}): {s.get('summary') or s.get('snippet') or ''}"[:600])
    qs = "\n".join(f"- {q}" for q in (topic.get("questions") or [])[:8]) or "- (none)"
    return (
        f"You keep a running assessment of the topic \"{topic.get('name')}\" for one reader.\n"
        f"Topic: {topic.get('brief') or topic.get('name')}\nWhy it matters: {topic.get('why') or 'not stated'}\n"
        f"Open questions:\n{qs}\n\nPREVIOUS ASSESSMENT:\n{prev}\n\nNEW EVIDENCE SINCE THEN:\n"
        + ("\n".join(lines) or "- nothing new") +
        f"\n\nWrite the updated assessment. Answer in JSON only. Your whole answer is about {words} words.\n"
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
                  *, ts: str = "") -> Dict[str, Any]:
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
        "changed": _clean_list(p.get("changed"), 10),
        "confidence": conf if conf in ("low", "medium", "high") else "medium",
        "answered": answered[:8],
        "new_questions": _clean_list(p.get("new_questions"), 4),
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
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """Day buckets over a window (counts, best score, the best signal's title) plus
    the point events - signals, snapshots, reports - newest first."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=max(1, days) - 1)).date()
    buckets: Dict[str, Dict[str, Any]] = {}
    for i in range(max(1, days)):
        d = (start + timedelta(days=i)).isoformat()
        buckets[d] = {"date": d, "signals": 0, "snapshots": 0, "reports": 0, "top": None, "top_score": -1}
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
}


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
    return {}


def action_usable(action: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Tuple[bool, str]:
    if not action:
        return False, "unknown action"
    if action.get("status") in ("running", "done"):
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
        "title": title or f"Intel brief - {names}",
        "ts": ts, "period_hours": period_hours,
        "topic_ids": [t["id"] for t in topics],
        "counts": {"topics": len(topics), "signals": n_sig},
        "sections": sections,
    }


def _sig_view(s: Dict[str, Any]) -> Dict[str, Any]:
    return {k: s.get(k) for k in ("id", "title", "url", "site", "kind", "score", "summary", "so_what",
                                  "snippet", "published", "tags", "answers")}


_E = _html.escape


def _md_inline(text: str) -> str:
    t = _E(text or "")
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
    return t


def _safe_href(u: str) -> str:
    u = str(u or "").strip()
    return _E(u) if u.startswith(("http://", "https://", "/")) else "#"


REPORT_CSS = """
:root{--bg:#0f1115;--card:#171a21;--bd:#2a2f3a;--fg:#e6e8ee;--dim:#9aa3b2;--acc:#7aa2f7;--ok:#9ece6a;--warn:#e0af68;--bad:#f7768e;--code:#11141a}
@media (prefers-color-scheme:light){:root{--bg:#f7f7f9;--card:#fff;--bd:#dde1e8;--fg:#1d2330;--dim:#5b6475;--acc:#3b63c9;--code:#f0f2f6}}
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
"""

_KW = r"\b(def|class|return|import|from|if|else|elif|for|while|in|async|await|function|const|let|var|new|try|except|catch|fn|pub|struct|impl|use|package|func|go|yield|with|as|lambda|true|false|null|None|True|False)\b"


def highlight(code: str, lang: str = "") -> str:
    """A small, safe highlighter (keywords, strings, comments, numbers) on
    already-escaped text - enough to read code in a report without a library."""
    out = []
    for line in (code or "").splitlines():
        e = _E(line)
        cm = re.search(r"(#|//|--).*$", e)
        tail = ""
        if cm and not re.search(r"&quot;|&#x27;", e[:cm.start()]):
            tail = f'<span class="c">{e[cm.start():]}</span>'
            e = e[:cm.start()]
        e = re.sub(r"(&quot;.*?&quot;|&#x27;.*?&#x27;)", r'<span class="s">\1</span>', e)
        e = re.sub(_KW, r'<span class="k">\1</span>', e)
        e = re.sub(r"(?<![\w#&;])(\d+(?:\.\d+)?)\b", r'<span class="n">\1</span>', e)
        out.append(e + tail)
    return "\n".join(out)


def _score_cls(v: Any) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "s-lo"
    return "s-hi" if v >= 7 else "s-md" if v >= 4 else "s-lo"


_MERMAID_BOOT = ('<script src="/ui/elements/vera_mermaid.js"></script>'
                 '<script>(function(){function go(){document.querySelectorAll("vera-mermaid").forEach(function(el){'
                 'try{if(el.querySelector("svg"))return;el.render((el.getAttribute("code")||el.textContent||"").trim());}catch(e){}});}'
                 'if(document.readyState!=="loading")setTimeout(go,0);else document.addEventListener("DOMContentLoaded",go);})();</script>')

# The page's own buttons post the one-time token back; the server looks the
# action up and builds the call itself, so the page can never choose a capability.
_ACTION_JS = """<script>(function(){
document.addEventListener('click',function(ev){var b=ev.target.closest('button.act[data-token]');if(!b)return;
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
    parts.append(f'<header><h1>{_E(title)}</h1><div class="meta">{_E(doc.get("ts", ""))} · '
                 f'{doc.get("counts", {}).get("topics", 0)} topics · {doc.get("counts", {}).get("signals", 0)} signals'
                 f' · last {int(doc.get("period_hours") or 24)}h</div></header>')
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
            parts.append(f'<section id="t-{_E(t["id"])}"><h2>{_E(t["name"])}</h2>')
            if t.get("brief"):
                parts.append(f'<div class="meta">{_E(t["brief"])}</div>')
            for p in sec.get("parts") or []:
                parts.append(_render_part(p))
            parts.append("</section>")
    parts.append(f'<div class="foot">Vera intelligence · report {_E(doc.get("id", ""))}'
                 + (f' · <a href="{_safe_href(base_url + "/intel/panel")}">open in Vera</a>' if base_url is not None else "")
                 + "</div>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_E(title)}</title><style>{REPORT_CSS}</style></head><body><main>"
            + "".join(parts) + "</main>" + _MERMAID_BOOT + _ACTION_JS + "</body></html>")


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
                f'<div class="t"><a href="{_safe_href(s.get("url"))}" target="_blank" rel="noopener">{_E(s.get("title") or "")}</a></div>'
                f'<div class="meta">{_E(s.get("site") or "")} · {_E(s.get("kind") or "")}'
                + (f' · {_E(str(s.get("published"))[:10])}' if s.get("published") else "") + "</div>"
                + (f'<div>{_md_inline(s.get("summary") or s.get("snippet") or "")}</div>')
                + (f'<div class="why">{_md_inline(s["so_what"])}</div>' if s.get("so_what") else "")
                + ('<div class="tags">' + "".join(f"<span>#{_E(t)}</span>" for t in s.get("tags") or []) + "</div>" if s.get("tags") else "")
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
    out = [f"# {doc.get('title')}", f"_{doc.get('ts', '')} · {doc.get('counts', {}).get('signals', 0)} signals_", ""]
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
                elif p["kind"] == "diagram":
                    out += ["```mermaid", p["mermaid"], "```", ""]
                elif p["kind"] == "code":
                    for c in p["items"]:
                        out += [f"```{c.get('lang') or ''}", c["code"], "```", ""]
                elif p["kind"] == "actions":
                    out.append("**Vera can:** " + " · ".join(a["label"] for a in p["items"]))
                    out.append("")
    if report_url:
        out.append(f"[Full report]({report_url})")
    return "\n".join(out).strip() + "\n"


def render_telegram(doc: Dict[str, Any], *, report_url: str = "", action_url: str = "",
                    max_chars: int = 3600) -> Dict[str, Any]:
    """{text (Telegram HTML), buttons [[{text,url}]], photo} - the brief as one
    message; the buttons open the full report and run the top actions."""
    E = _E
    lines = [f"<b>{E(doc.get('title') or 'Intel brief')}</b>",
             f"<i>{E(str(doc.get('ts', ''))[:16].replace('T', ' '))} · {doc.get('counts', {}).get('signals', 0)} signals</i>", ""]
    photo = ""
    acts: List[Dict[str, Any]] = []
    for sec in doc.get("sections") or []:
        if sec["kind"] == "headline":
            for h in sec["items"]:
                lines.append(f"▸ <b>{E(h['topic'])}</b> — {E(h['text'])}")
            lines.append("")
        elif sec["kind"] == "topic":
            lines.append(f"<b>{E(sec['topic']['name'])}</b>")
            for p in sec.get("parts") or []:
                if p["kind"] == "assessment" and p.get("changed"):
                    lines += [f"  • {E(c)}" for c in p["changed"][:3]]
                elif p["kind"] == "signals":
                    for s in p["items"][:3]:
                        sc = "" if s.get("score") is None else f"[{int(round(s['score']))}] "
                        lines.append(f"  {sc}<a href=\"{E(s.get('url') or '')}\">{E((s.get('title') or '')[:110])}</a>")
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
    "podcast":  {"label": "Podcast", "render": "markdown", "cap": "podcast.generate"},
    "print":    {"label": "Printer", "render": "markdown", "cap": "print.text"},
}


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
        tg = render_telegram(doc, report_url=report_url, action_url=action_url)
        return {"text": tg["text"], "buttons": json.dumps(tg["buttons"]), "photo_url": tg["photo"], "parse_mode": "HTML"}
    if channel == "chat":
        return {"session_id": target, "report": md, "title": title}
    if channel == "email":
        return {"to": target, "subject": title, "body": html_doc or md, "html": bool(html_doc)}
    if channel == "canvas":
        where = {"id": target} if target.startswith("cv_") else {"session_id": target}
        return dict(where, kind="panel", key=f"panel:intel-report-{doc.get('id')}", size="l", at="now",
                    content={"panel": "intel-report", "title": title, "src": report_url})
    if channel == "notebook":
        return {"title": title, "description": md}
    if channel == "memory":
        return {"text": md[:4000], "category": "intel", "tags": "intel,report",
                "record_type": "intel_report", "importance": 0.5}
    if channel == "podcast":
        return {"topic": title[:200], "sources": [{"type": "text", "text": md, "label": title}],
                "instructions": "Turn this intelligence brief into a natural spoken episode; do not invent facts beyond it.",
                "wait": False}
    if channel == "print":
        return {"text": md[:6000], "title": title[:60]}
    return {}
