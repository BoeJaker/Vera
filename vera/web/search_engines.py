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


#: Docker's name for the machine the container runs on. Resolves to the bridge
#: gateway (172.17.0.1 here) and is the ONLY reliable way for a container to
#: reach a service published on its host.
HOST_GATEWAY = "host.docker.internal"

#: Hostnames that mean "the machine Vera is running on". Inside a container that
#: machine is NOT localhost, which is the whole problem below.
_SELF_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def in_container(marker: str = "/.dockerenv") -> bool:
    """True when this process is inside a container. Injectable for tests."""
    import os as _os
    return _os.path.exists(marker)


def host_candidates(url: str, inside: Optional[bool] = None) -> List[str]:
    """The URLs to try for a host-published service, best first.

    WHY THIS EXISTS. A sandbox container inherits the orchestrator's idea of
    where SearXNG lives, and that idea is written from the host's point of view.
    Measured 2026-09-06 on this estate:

      * the research source's fallback host is ``http://<BACKEND_HOST>:8888``,
        and BACKEND_HOST is ``llm.int`` - which does not resolve on the Linux
        host either, let alone inside a container;
      * SearXNG publishes on :8088. Nothing listens on :8888 at all;
      * from inside a container, ``host.docker.internal`` resolves to 172.17.0.1
        and reaches it.

    So a container asking for ``localhost`` gets ITSELF, and a container asking
    for a host-only name gets NXDOMAIN - and either way SearXNG silently
    contributes nothing and every search falls through to DuckDuckGo. Retrying
    the same port via the gateway is the fix, and it is a FALLBACK rather than a
    rewrite: the configured URL is always tried first, so a correctly reachable
    host is unaffected.
    """
    u = str(url or "").strip()
    if not u:
        return []
    inside = in_container() if inside is None else bool(inside)
    if not inside:
        return [u]
    try:
        p = urlparse(u)
    except Exception:
        return [u]
    host = (p.hostname or "").lower()
    if not host or host == HOST_GATEWAY:
        return [u]
    port = (":%d" % p.port) if p.port else ""
    alt = "%s://%s%s%s" % (p.scheme or "http", HOST_GATEWAY, port, p.path or "")
    # A container's own localhost can never be the host's, so the gateway leads.
    if host in _SELF_HOSTS:
        return [alt, u]
    return [u, alt]


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


async def walk_pages(fetch, limit: int, per_page: int = DEFAULT_PER_PAGE,
                     max_pages: int = MAX_PAGES) -> List[Dict[str, Any]]:
    """Walk an engine's pages until ``limit`` is met, and merge what came back.

    ``fetch(pageno)`` returns that page's raw results, or None to mean "this
    attempt failed" - which stops the walk while KEEPING the pages already
    gathered, because a failure on page 3 is not a reason to discard 1 and 2.

    Lives here rather than in either caller because both SearXNG engines need
    the identical walk, and the first version of this work paginated only
    web_capabilities' copy. That copy then stopped being the one that runs:
    once SearXNG actually returned results, the dispatcher preferred the
    RESEARCH path, so `limit=25` still came back with 10 on prod. One walk,
    both callers, and the stopping rules testable without a network.
    """
    # Walks until it HAS enough rather than to a page count derived from an
    # assumed page size. page_plan estimates pages as limit/per_page, which is
    # right for SearXNG's ~10 a page but silently truncates whenever a page
    # returns fewer: asking for 5 computed a single page, so a page yielding 2
    # ended the walk two short. The real stopping conditions are "enough",
    # "the engine gave nothing", and the page cap - none of which need to guess
    # how big a page is.
    pages: List[List[Dict[str, Any]]] = []
    for pageno in range(1, max(1, int(max_pages or MAX_PAGES)) + 1):
        items = await fetch(pageno)
        if items is None:
            break
        page = list(items)
        pages.append(page)
        # An empty page means the engine has nothing further; asking again just
        # costs a round trip and an upstream hit against a rate limit.
        if not page or enough(sum(len(p) for p in pages), limit):
            break
    return merge_pages(pages, limit)


def enough(collected: int, limit: int) -> bool:
    """True when there is no point fetching another page."""
    try:
        want = int(limit)
    except (TypeError, ValueError):
        return False
    return want > 0 and int(collected) >= want
