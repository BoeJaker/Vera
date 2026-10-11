"""info.seek's decisions, without the network (info-seeking plan SK-3.1, shared-planning/intelligence/
PLAN-INFO-SEEKING.md).

The owner, 2026-10-10: asked for "the latest updates on a topic" chat reached for research.run (a report job of
5-30 minutes) or answered from memory and guessed; "speed is an important factor but more important is the quality
and depth of responses and picking/providing the right tool for the job". info.seek is the one front door: it
decides what kind of need a question is, builds the searches, merges what Vera tracks with what the web says, and
returns numbered, dated evidence the answer cites. This module is the part that can be wrong without a network, so
it is pure and tested on its own.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

NEEDS = ("fact", "latest", "overview", "deep")

# a report the user wants written - not a document named in the question ("updates on the AI white paper" is latest)
_DEEP = re.compile(r"\b((write|make|produce|prepare|draft|do|give|put together)\s+(me\s+)?(a\s+|an\s+)?(full\s+|detailed\s+|"
                   r"proper\s+|comprehensive\s+|in-?depth\s+)?(report|write-?up|briefing|white ?paper|deep[- ]dive)|"
                   r"deep[- ]dive (into|on)|in[- ]depth (report|research|analysis) (on|of|into)|"
                   r"comprehensive (report|research|analysis) (on|of|into))\b", re.I)
_LATEST = re.compile(r"\b(latest|newest|news|headlines?|updates?|what'?s new|whats new|recent(ly)?|this (week|month)|"
                     r"today|tonight|developments?|just (announced|released|launched)|any (news|word)|"
                     r"happening (with|in|at))\b", re.I)
_OVERVIEW = re.compile(r"\b(overview|landscape|state of( the art)?|explain|compare|comparison|versus|vs\.?|"
                       r"pros and cons|trade-?offs?|how does .{2,60} work|background on|summary of|introduction to)\b",
                       re.I)

# the words a request wraps a subject in: "what's the latest on X?", "any news about X", "give me updates on X"
_LEAD = re.compile(r"^\s*(please\s+)?((can|could) you\s+)?((give|get|find|show|tell) (me\s+)?)?(me\s+)?"
                   r"(what'?s|whats|what is|what are|is there|are there|any)?\s*"
                   r"(the\s+)?(very\s+)?(latest|newest|recent|current)?\s*"
                   r"(news|updates?|developments?|headlines?|word|info(rmation)?|state)?\s*"
                   r"(new\s+)?(on|about|for|in|with|regarding|re:?|from|at)?\s+", re.I)
_TAIL = re.compile(r"\s*(\b(right now|today|this week|this month|lately|recently|at the moment|currently|"
                   r"these days|please|updates?|news|headlines|developments)\b|[?.!])+\s*$", re.I)
_REPORT_LEAD = re.compile(r"^\s*(please\s+)?((can|could) you\s+)?(write|make|build|produce|do|give)\s+(me\s+)?"
                          r"(a\s+|an\s+)?(full\s+|detailed\s+|deep\s+|comprehensive\s+)?"
                          r"(report|write-?up|briefing|brief|deep[- ]dive|overview|analysis)\s+(on|about|of|into)\s+",
                          re.I)
_ASK_LEAD = re.compile(r"^\s*(explain|compare|tell me about|summari[sz]e|describe)\s+(how\s+)?", re.I)

_MONTHS = ("January February March April May June July August September October November December").split()


def classify_need(text: str) -> Dict[str, str]:
    """{need, reason}: deep (a report the user wants to read later), latest (what changed), overview (the state of a
    field, a comparison, an explanation), else fact. Rules first; the cap accepts an explicit need over this."""
    t = " ".join(str(text or "").split())
    if _DEEP.search(t):
        return {"need": "deep", "reason": "asks for a report: " + _DEEP.search(t).group(0)}
    if _LATEST.search(t):
        return {"need": "latest", "reason": "asks what is new: " + _LATEST.search(t).group(0)}
    if _OVERVIEW.search(t):
        return {"need": "overview", "reason": "asks for the wider picture: " + _OVERVIEW.search(t).group(0)}
    return {"need": "fact", "reason": "a specific question"}


_INFO_ASK = re.compile(r"^\s*(what|who|when|where|which|how (much|many|does|do|is|are))\b|\b(find out|look up|tell me about|"
                       r"research|news|price of|prices?|rate|latest|update|explain)\b", re.I)
# a question about Vera itself, or a request for a tool, is not a question about the world
_INTERNAL = re.compile(r"\b(vera|census|loop ?lab|sandbox|pipeline|worktree|board item|capabilit|caps?|panel|dashboard|"
                       r"canvas|widget|tools? (for|to|that)|skill|my (notes|memory|calendar|inbox|topics?))\b", re.I)
INFO_CAPS = ("info.seek", "web.research", "intel.lookup")


def info_intent(text: str) -> bool:
    """True when `text` asks about the outside world - the latest on something, a fact, a price, "find out about" -
    rather than about Vera's own parts or for a tool. caps.search uses it to put the information caps first (plan
    SK-1.2: "latest news about MCP" ranked mcp.register_server first, "what is the base rate" evolve.delegate.rate)."""
    t = " ".join(str(text or "").split())
    return bool(t) and bool(_INFO_ASK.search(t)) and not _INTERNAL.search(t)


def boost_info(results: Sequence[Any], available: Iterable[str], top_k: int) -> List[Any]:
    """(name, score) results with the information caps first, in INFO_CAPS order, scored just above the best."""
    have = set(available)
    rows = [(str(n), float(s)) for n, s in results]
    top = max([s for _, s in rows] + [0.0])
    lead = [(n, round(top + 1.0 - 0.2 * i, 3)) for i, n in enumerate(c for c in INFO_CAPS if c in have)]
    names = {n for n, _ in lead}
    return (lead + [(n, s) for n, s in rows if n not in names])[:max(1, int(top_k))]


def subject_of(text: str) -> str:
    """The subject inside a request: "what's the latest on the EU AI Act?" -> "the EU AI Act" -> "EU AI Act"."""
    s = " ".join(str(text or "").split())
    s = _ASK_LEAD.sub("", _REPORT_LEAD.sub("", s, count=1), count=1)
    for _ in range(2):
        s2 = _TAIL.sub("", _LEAD.sub("", s, count=1)).strip()
        if not s2 or s2 == s:
            break
        s = s2
    s = re.sub(r"^(the|a|an)\s+", "", s, flags=re.I).strip()
    if len(re.sub(r"\b(the|a|an|me|it|this|that)\b", "", s, flags=re.I).strip()) < 2:
        return " ".join(str(text or "").split())          # nothing left that names a subject: keep the words
    return s


def month_label(now: datetime) -> str:
    return f"{_MONTHS[now.month - 1]} {now.year}"


def latest_queries(subject: str, question: str = "", now: Optional[datetime] = None, limit: int = 3) -> List[str]:
    """Searches for what changed on a subject: anchored on the subject (the lesson of intel's relevance collapse:
    question-shaped searches drift), dated with the month, and the user's own words last."""
    now = now or datetime.now(timezone.utc)
    subj = subject.strip()
    out = [f"{subj} latest news", f"{subj} news {month_label(now)}"]
    q = " ".join(str(question or "").split())
    if q and q.lower().strip("?") not in (subj.lower(), out[0].lower()):
        out.append(q)
    seen, uniq = set(), []
    for x in out:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:limit]


_WH = re.compile(r"^\s*(who|what|when|where|which|why|how|is|are|was|were|does|do|did|can|could|should|will|would|has|have)\b",
                 re.I)
_KW_STOP = set("who what when where which why how is are was were does do did can could should will would has have the "
               "a an of to in on for at by with from about under into is it its this that these those me you your my i "
               "tell please there any".split())


def keywords_of(text: str) -> str:
    """The content words of a question, in order and as written: "When does the EU AI Act's high-risk obligation
    apply?" -> "EU AI Act's high-risk obligation apply". A question-shaped search drifts (prod, 10 Oct: this one
    returned dictionary pages for "does")."""
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.+/-]*", str(text or "")) if w.lower() not in _KW_STOP]
    return " ".join(words).strip(" .?") or " ".join(str(text or "").split())


def fact_queries(subject: str, question: str = "", limit: int = 2) -> List[str]:
    """The subject's keywords first - measured on prod 10 Oct: "what is the Bank of England base rate" returned bank
    home pages (Bank of America, Barclays, HSBC), "Bank of England base rate" the Bank's rate page, MSE and the BBC -
    then the question itself for what the keywords miss."""
    out = []
    if _WH.match(subject or ""):            # the subject is still the question: search its keywords instead
        subject = keywords_of(subject)
    for x in (subject, question):
        x = " ".join(str(x or "").split()).strip(" ?")
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:limit]


def overview_queries(subject: str, question: str = "", now: Optional[datetime] = None, limit: int = 3) -> List[str]:
    """Several angles for a wider picture: the subject, an explainer, and the question."""
    now = now or datetime.now(timezone.utc)
    out = []
    for x in (subject, f"{subject} explained overview {now.year}", question):
        x = " ".join(str(x or "").split()).strip(" ?")
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:limit]


_TRACK = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|igshid$|si$)", re.I)


def canon_url(url: str) -> str:
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACK.match(k)])
    return urlunsplit((p.scheme.lower() or "https", host, (p.path or "").rstrip("/"), q, ""))


def site_of(url: str) -> str:
    try:
        h = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _title_key(t: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()[:90]


def merge_hits(lists: Iterable[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Search results from several queries as one list, first-seen order kept; the same page (one URL spelling, or
    the same headline on another URL) once."""
    out, urls, titles = [], set(), set()
    for lst in lists:
        for h in lst or []:
            u = canon_url(h.get("url") or "")
            tk = _title_key(h.get("title"))
            if not u or u in urls or (len(tk) > 24 and tk in titles):
                continue
            urls.add(u)
            if tk:
                titles.add(tk)
            out.append(dict(h))
    return out


def parse_date(s: Any) -> Optional[datetime]:
    """A page or feed date in the usual spellings (ISO, RFC 822, "17 September 2026", "Sep 17, 2026"), as UTC."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    t = str(s).strip()
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(t)
        if d:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(t[:30].strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


_AGO = re.compile(r"\b(\d{1,3})\s*(min(?:ute)?s?|h(?:ou)?rs?|hours?|days?|d|weeks?|w|months?|mo|years?|y)\s+ago\b", re.I)
_LEAD_DATE = re.compile(r"^\s*([A-Z][a-z]{2,8}\.? \d{1,2}, 20\d\d|\d{1,2} [A-Z][a-z]{2,8}\.? 20\d\d|20\d\d-\d\d-\d\d)\b")


def date_from_text(text: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    """A date a result states about itself in its snippet or byline: "14 hours ago | Watford Observer",
    "yesterday", or a leading "Sep 17, 2026 - ..." (how search engines prefix a snippet). Not a date somewhere
    in the text: "what changed on 2 August 2026" is an event, not when the page was written."""
    now = now or datetime.now(timezone.utc)
    t = str(text or "")
    m = _AGO.search(t[:80])
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        days = (n / 1440.0 if u.startswith("min") else n / 24.0 if u.startswith("h") else
                n * 7.0 if u.startswith("w") else n * 30.0 if u.startswith("mo") else
                n * 365.0 if u.startswith("y") else float(n))
        return now - timedelta(days=days)
    if re.match(r"^\s*yesterday\b", t, re.I):
        return now - timedelta(days=1)
    if re.match(r"^\s*today\b", t, re.I):
        return now
    m = _LEAD_DATE.match(t)
    return parse_date(m.group(1).replace(".", "")) if m else None


_LABELLED = re.compile(r"(?i)\b(?:published(?: time| on| at)?|updated(?: on| at)?|posted(?: on)?|last (?:updated|modified|"
                       r"reviewed)(?: on)?|date published)\s*[:\-|]?\s*"
                       r"(\d{1,2}(?:st|nd|rd|th)? [A-Za-z]{3,9}\.? 20\d\d|[A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?,? 20\d\d|"
                       r"20\d\d-\d\d-\d\d)")


def labelled_date(text: Any, chars: int = 1500) -> Optional[datetime]:
    """A date the page labels as its own near the top - "Updated on 31 August 2026", "Published Time: 2026-09-17".
    A bare date there is often an event (a release date, a consultation deadline), so only a labelled one counts
    (sampled on prod 11 Oct: 2 of 40 undated pages had one; 6 more had a bare date, mostly events)."""
    m = _LABELLED.search(str(text or "")[:chars])
    if not m:
        return None
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", m.group(1)).replace(".", "")
    return parse_date(raw)


def fill_dates(items: Sequence[Dict[str, Any]], now: Optional[datetime] = None) -> int:
    """Give undated items the date their own byline or snippet states; returns how many got one."""
    n = 0
    for it in items:
        if it.get("published") and parse_date(it.get("published")):
            continue
        d = date_from_text(it.get("age_text"), now) or date_from_text(it.get("snippet"), now)
        if d:
            it["published"] = d.strftime("%Y-%m-%d")
            n += 1
    return n


def subject_key(subject: str) -> str:
    """One spelling per subject for remembering when it was last looked up."""
    return re.sub(r"[^a-z0-9]+", "-", str(subject or "").lower()).strip("-")[:80]


def since_for(last_seek: Any, now: Optional[datetime] = None, default_days: int = 14) -> Dict[str, str]:
    """What counts as new for a 'latest' question: since the user last asked about this subject when that was 2-30
    days ago (plan SK-2.5: "what's new" means new to them); otherwise the default window - asked again the same day,
    a since of an hour ago would find nothing, and after a month the default is the better frame."""
    now = now or datetime.now(timezone.utc)
    d = parse_date(last_seek)
    if d and timedelta(days=2) <= now - d <= timedelta(days=30):
        return {"since": d.strftime("%Y-%m-%d"), "why": f"since you last looked on {d.strftime('%Y-%m-%d')}"}
    return {"since": default_since(now, default_days), "why": f"the last {default_days} days"}


def default_since(now: Optional[datetime] = None, days: int = 14) -> str:
    return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%d")


def order_latest(items: Sequence[Dict[str, Any]], since: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Newest first among what is dated on or after `since`; then undated items in the order relevance gave them
    (they may be new - the page just did not say); then older dated items, newest first. A date in the future (a
    page's clock or an event date) counts as undated."""
    now = now or datetime.now(timezone.utc)
    cut = parse_date(since) or (now - timedelta(days=14))
    fresh, undated, old = [], [], []
    for i, it in enumerate(items):
        d = parse_date(it.get("published"))
        if d and d > now + timedelta(days=1):
            d = None
        if d is None:
            undated.append((i, it))
        elif d >= cut:
            fresh.append((d, i, it))
        else:
            old.append((d, i, it))
    fresh.sort(key=lambda x: (-x[0].timestamp(), x[1]))
    old.sort(key=lambda x: (-x[0].timestamp(), x[1]))
    return [it for _, _, it in fresh] + [it for _, it in undated] + [it for _, _, it in old]


def per_site(items: Sequence[Dict[str, Any]], cap: int = 2) -> List[Dict[str, Any]]:
    """At most `cap` items from one site, order kept: depth means several sources, not one site's pages."""
    n: Dict[str, int] = {}
    out = []
    for it in items:
        s = site_of(it.get("url") or "") or "?"
        if n.get(s, 0) >= cap:
            continue
        n[s] = n.get(s, 0) + 1
        out.append(it)
    return out


def excerpt(text: Any, chars: int) -> str:
    """The lead of a page, cut at a sentence or word boundary."""
    t = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()
    if len(t) <= chars:
        return t
    cut = t[:chars]
    i = max(cut.rfind(". "), cut.rfind(".\n"))
    if i > chars * 0.4:
        return cut[:i + 1]
    j = cut.rfind(" ")
    return (cut[:j] if j > chars * 0.6 else cut).rstrip() + " ..."


GUIDE = ("Answer from these items only. Cite them as [n] with their date (published where given, else 'undated'); "
         "for 'latest' questions lead with the newest. Say plainly what was looked for and not found (see gaps) - "
         "never fill a gap from memory or a guess. End with the sources you used as '[n] title - url (date)'.")


def build_pack(need: str, question: str, subject: str, items: Sequence[Dict[str, Any]], *, since: str = "",
               checked: Sequence[str] = (), gaps: Sequence[str] = (), now: Optional[datetime] = None,
               elapsed_ms: int = 0, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The evidence pack: numbered items with their dates and ages, what was checked, what is missing."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for it in items:
        d = parse_date(it.get("published"))
        if d and d > now + timedelta(days=1):
            d = None
        rows.append({
            "n": len(rows) + 1,
            "title": _html.unescape(str(it.get("title") or it.get("url") or ""))[:180],
            "url": it.get("url") or "",
            "site": site_of(it.get("url") or ""),
            "published": d.strftime("%Y-%m-%d") if d else "",
            "age_days": round((now - d).total_seconds() / 86400.0, 1) if d else None,
            "kind": it.get("kind") or "web",
            "read": bool(it.get("text")),
            "text": it.get("text") or it.get("snippet") or "",
        })
    g = list(gaps)
    if need == "latest" and since:
        cut = parse_date(since)
        if cut and not any(r["published"] and parse_date(r["published"]) >= cut for r in rows):
            g.append(f"no source found is dated on or after {since}")
    if not rows:
        g.append("nothing was found")
    out = {"need": need, "question": question, "subject": subject, "since": since, "answerable": bool(rows),
           "items": rows, "checked": list(checked), "gaps": g, "guide": GUIDE, "elapsed_ms": int(elapsed_ms)}
    if extra:
        out.update(extra)
    return out
