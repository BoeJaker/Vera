"""search_engines.py -- one definition of how Vera searches the web.

THE DUPLICATION THIS ENDS. Two independent implementations of the same three
engines existed, and they had drifted:

    researcher_api.py   search_searxng / search_brave / search_ddg
                        _clean_search_url        order: brave -> searxng -> ddg
    web_capabilities.py _search_searxng / _search_brave / _search_ddg
                        _decode_redirect         order: searxng -> brave -> ddg

web_capabilities prefers the researcher's engines when that module imports and
falls back to its own copies otherwise - so the SAME cap picked a different
engine depending on which path it took. Both orders are now decided by
``engine_order`` here.

THE PAGINATION GAP. Neither implementation ever asked for a second page.
SearXNG takes ``pageno`` and it works (measured 2026-09-06 against the live
instance: page 1 -> 10 results, page 3 -> 14), Brave takes ``offset``, but both
callers requested page one and sliced to ``limit``. So a caller asking for 30
results silently received about 10, and ``result_count`` was a ceiling rather
than a request.

WHAT IS AND IS NOT HERE. Only the decisions - which engines, in what order, how
many pages, how to merge them, how to unwrap a redirect. The HTTP stays with the
caps, so everything below is pure and can be tested without a network, a
SearXNG, or a mock client.

PAGE ONE IS UNCHANGED ON THE WIRE. ``searxng_params`` omits ``pageno`` entirely
for the first page, so the common case (limit <= one page) sends exactly the
request it sent before this module existed. That is deliberate: the point is to
add reach, not to perturb every existing search.
"""

from __future__ import annotations

import html as _html
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

#: Results a single SearXNG page returns. Not a setting - an observation of what
#: the instance actually yields, used to work out how many pages a limit needs.
DEFAULT_PER_PAGE = 10

#: Ceiling on pages per query. "Unlimited within reason": the local instance has
#: `limiter: false` so it does not throttle us, but every page is a fan-out to
#: the upstream engines, and those DO rate-limit - brave, startpage and wikidata
#: were already suspended on this instance before any of this work started.
MAX_PAGES = 5

#: Engines in their stable tie-break order, used to complete a chain.
KNOWN_ENGINES = ("searxng", "brave", "ddg")

#: SearXNG's `language` parameter. "auto" matches the instance's own
#: `default_lang` and, crucially, is not a SPECIFIC locale.
#:
#: Both implementations hardcoded "en", and on this instance that returns
#: NOTHING. Measured 2026-09-06, three consecutive rounds with the upstream
#: engines healthy, same query:
#:
#:     (omitted) -> 10      auto  -> 10      all   -> 10
#:     en        ->  0      en-US ->  0      en-GB ->  0
#:
#: SearXNG filters engines by declared language support, and the engines
#: configured here do not advertise the en locales - so naming one excluded all
#: of them and the search returned an empty list with HTTP 200. That is why
#: every Vera search silently fell through to DuckDuckGo and SearXNG appeared
#: useless: it was being asked a question none of its engines would answer.
DEFAULT_LANGUAGE = "auto"

#: The last-resort engine. Both implementations already fell back to it, and
#: keeping that is what makes this consolidation non-regressive: no call that
#: used to reach ddg stops reaching it.
LAST_RESORT = "ddg"


def decode_redirect(url: str) -> str:
    """Unwrap a search engine's redirect wrapper to the real destination.

    Keeps web_capabilities' rules exactly - including requiring "google." in the
    host before treating ``/url?`` as a Google redirect, which the researcher's
    looser version did not do and which would rewrite any URL containing that
    substring. Bing's ``/ck/`` wrapper is added; it was in the researcher's copy
    only, so this is the union rather than a narrowing.
    """
    url = _html.unescape(url or "")
    parsed = urlparse(url)
    if parsed.netloc in ("duckduckgo.com", "www.duckduckgo.com") and parsed.path.startswith("/l"):
        target = parse_qs(parsed.query).get("uddg", parse_qs(parsed.query).get("u", [""]))[0]
        if target:
            return unquote(target)
    if "/url?" in url and "google." in parsed.netloc:
        qs = parse_qs(parsed.query)
        target = qs.get("q", qs.get("url", [""]))[0]
        if target:
            return unquote(target)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target:
            return unquote(target.lstrip("a1"))
    return url


def engine_order(engine: str = "auto", configured_default: str = "searxng") -> List[str]:
    """Which engines to try, in order.

    ``auto`` starts from the operator's configured default (research's
    web_cfg.engine) and then completes the chain, so the two dispatch paths can
    no longer disagree about which engine leads.

    A NAMED engine is honoured first and still falls through to ddg. Both
    implementations already did that on at least one path, and preserving it is
    what keeps this change from taking results away from a caller who named an
    engine that happens to be down.
    """
    eng = (engine or "auto").strip().lower()
    default = (configured_default or "searxng").strip().lower()
    if eng in ("", "auto"):
        head = default if default in KNOWN_ENGINES else "searxng"
        return [head] + [e for e in KNOWN_ENGINES if e != head]
    if eng not in KNOWN_ENGINES:
        head = default if default in KNOWN_ENGINES else "searxng"
        return [head] + [e for e in KNOWN_ENGINES if e != head]
    return [eng] if eng == LAST_RESORT else [eng, LAST_RESORT]


def page_plan(limit: int, per_page: int = DEFAULT_PER_PAGE,
              max_pages: int = MAX_PAGES) -> List[int]:
    """The 1-based page numbers needed to satisfy ``limit``.

    ``[1]`` whenever one page suffices - the overwhelming majority of calls -
    so nothing about them changes.
    """
    try:
        want = int(limit)
    except (TypeError, ValueError):
        want = 0
    if want <= 0:
        return [1]
    size = max(1, int(per_page or DEFAULT_PER_PAGE))
    pages = (want + size - 1) // size
    return list(range(1, max(1, min(int(max_pages or MAX_PAGES), pages)) + 1))


def searxng_params(query: str, safesearch: Any = 0, pageno: int = 1,
                   language: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    """Query parameters for one SearXNG page.

    ``pageno`` is OMITTED for page 1 so the first request is byte-identical to
    the one sent before pagination existed; SearXNG defaults to page 1 anyway.
    """
    params: Dict[str, Any] = {"q": query, "format": "json",
                              "language": language, "safesearch": safesearch}
    if int(pageno or 1) > 1:
        params["pageno"] = int(pageno)
    return params


def brave_offset(pageno: int = 1, per_page: int = DEFAULT_PER_PAGE) -> int:
    """Brave paginates by result OFFSET, not page number."""
    return max(0, (max(1, int(pageno or 1)) - 1) * max(1, int(per_page or DEFAULT_PER_PAGE)))


def _dedupe_key(result: Dict[str, Any]) -> str:
    """Identity of a result for de-duplication: its destination URL.

    Compared after redirect unwrapping and without a trailing slash, because the
    same page arriving from two engines or two pages is one result.
    """
    u = str((result or {}).get("url") or "").strip()
    return decode_redirect(u).rstrip("/").lower()


def merge_pages(pages: Iterable[Sequence[Dict[str, Any]]], limit: int) -> List[Dict[str, Any]]:
    """Flatten paged results into one list: order preserved, duplicates dropped,
    truncated to ``limit``.

    Later pages legitimately repeat earlier hits, so without the de-duplication
    asking for more pages would return more DUPLICATES rather than more reach.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    try:
        want = int(limit)
    except (TypeError, ValueError):
        want = 0
    for page in (pages or []):
        for item in (page or []):
            if not isinstance(item, dict):
                continue
            key = _dedupe_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if want > 0 and len(out) >= want:
                return out
    return out


def enough(collected: int, limit: int) -> bool:
    """True when there is no point fetching another page."""
    try:
        want = int(limit)
    except (TypeError, ValueError):
        return False
    return want > 0 and int(collected) >= want
