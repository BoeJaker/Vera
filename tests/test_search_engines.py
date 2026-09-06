"""One search implementation, and it can reach past page one.

Two things were wrong before this module, both found by auditing the web caps
against the research subsystem (2026-09-06):

1. DRIFT. researcher_api and web_capabilities each defined the same three
   engines, and their fallback orders disagreed - brave -> searxng -> ddg
   against searxng -> brave -> ddg. web_capabilities prefers the researcher's
   engines when that module imports and falls back to its own otherwise, so the
   SAME cap preferred a different engine depending on which path it took.

2. NO PAGINATION. Neither ever asked for a second page. SearXNG accepts
   `pageno` and it works (measured against the live instance: page 1 -> 10
   results, page 3 -> 14); Brave accepts `offset`. Both callers requested page
   one and sliced to `limit`, so asking for 30 results returned about 10 and
   `result_count` was a ceiling rather than a request.

The no-regression property is explicit below and pinned in both directions: a
limit that fits on one page must produce exactly the request that was sent
before pagination existed.

Pure: no network, no SearXNG, no mock client.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.web import search_engines as SE                  # noqa: E402


# ── page 1 must not change ──────────────────────────────────────────────────
def test_the_first_page_carries_no_pageno():
    """PAGINATION adds nothing to the common call: `pageno` is omitted entirely
    for page 1, so introducing paging did not perturb existing searches.

    The request is NOT byte-identical to the old one, and deliberately so - the
    `language` default changed from "en" to "auto" because "en" returned zero
    results (see test_no_specific_locale_is_sent_to_searxng). Those are the only
    two differences, and both are asserted here."""
    p = SE.searxng_params("webgpu", safesearch=0, pageno=1)
    assert p == {"q": "webgpu", "format": "json",
                 "language": SE.DEFAULT_LANGUAGE, "safesearch": 0}
    assert "pageno" not in p


def test_no_specific_locale_is_sent_to_searxng():
    """The bug that made SearXNG look useless.

    Both implementations hardcoded language="en". Measured against the live
    instance with its engines healthy, three consecutive rounds, same query:
    omitted/auto/all returned 10 results each, while en/en-US/en-GB returned
    ZERO - SearXNG filters engines by declared language support and none of the
    configured engines advertise those locales, so naming one excluded all of
    them and the search came back empty with HTTP 200. Every Vera search then
    fell through to DuckDuckGo.
    """
    assert SE.DEFAULT_LANGUAGE in ("auto", "all")
    assert SE.searxng_params("q")["language"] == SE.DEFAULT_LANGUAGE


def test_a_specific_locale_is_never_the_default():
    for bad in ("en", "en-US", "en-GB"):
        assert SE.searxng_params("q")["language"] != bad


def test_a_caller_may_still_force_a_language():
    """Not a ban - a default. An instance whose engines DO advertise a locale
    should still be able to ask for it."""
    assert SE.searxng_params("q", language="fr")["language"] == "fr"


def test_a_limit_that_fits_one_page_asks_for_one_page():
    assert SE.page_plan(8) == [1]
    assert SE.page_plan(10) == [1]


def test_brave_sends_no_offset_on_the_first_page():
    assert SE.brave_offset(1) == 0


# ── pagination ──────────────────────────────────────────────────────────────
def test_a_bigger_limit_asks_for_more_pages():
    assert SE.page_plan(30) == [1, 2, 3]
    assert SE.page_plan(11) == [1, 2]


def test_pageno_is_sent_beyond_the_first_page():
    assert SE.searxng_params("q", 0, 3)["pageno"] == 3


def test_brave_pages_by_offset():
    assert SE.brave_offset(2) == 10
    assert SE.brave_offset(3, per_page=20) == 40


def test_pages_are_capped_so_one_query_cannot_hammer_the_engines():
    """`limiter: false` locally means SearXNG will not stop us; the upstream
    engines will, and three of them were already suspended on this instance."""
    assert SE.page_plan(10_000) == list(range(1, SE.MAX_PAGES + 1))


def test_a_nonsense_limit_still_asks_for_one_page():
    for bad in (0, -5, None, "x"):
        assert SE.page_plan(bad) == [1]


# ── merging pages ───────────────────────────────────────────────────────────
def _r(u):
    return {"url": u, "title": u, "snippet": ""}


def test_merging_preserves_order_and_truncates_to_the_limit():
    got = SE.merge_pages([[_r("a"), _r("b")], [_r("c"), _r("d")]], 3)
    assert [x["url"] for x in got] == ["a", "b", "c"]


def test_duplicates_across_pages_are_dropped():
    """Later pages legitimately repeat earlier hits. Without this, asking for
    more pages returns more DUPLICATES rather than more reach."""
    got = SE.merge_pages([[_r("http://x/1"), _r("http://x/2")],
                          [_r("http://x/2"), _r("http://x/3")]], 10)
    assert [x["url"] for x in got] == ["http://x/1", "http://x/2", "http://x/3"]


def test_the_same_page_with_a_trailing_slash_is_one_result():
    got = SE.merge_pages([[_r("http://x/a")], [_r("http://x/a/")]], 10)
    assert len(got) == 1


def test_a_redirect_wrapped_duplicate_is_recognised():
    """The same destination arriving raw from one engine and wrapped from
    another is one result, not two."""
    wrapped = _r("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa")
    got = SE.merge_pages([[_r("https://x.com/a")], [wrapped]], 10)
    assert len(got) == 1


def test_merging_survives_junk_entries():
    got = SE.merge_pages([[None, "nope", _r("http://x/1")]], 5)
    assert [x["url"] for x in got] == ["http://x/1"]


def test_enough_stops_the_walk_once_the_limit_is_met():
    assert SE.enough(10, 10) is True
    assert SE.enough(9, 10) is False
    assert SE.enough(5, 0) is False


# ── one engine order ────────────────────────────────────────────────────────
def test_auto_leads_with_the_configured_default():
    """This is the drift fix: research led with its configured engine, the local
    fallback always led with searxng."""
    assert SE.engine_order("auto", "brave")[0] == "brave"
    assert SE.engine_order("auto", "searxng")[0] == "searxng"


def test_auto_still_covers_every_engine():
    assert sorted(SE.engine_order("auto", "brave")) == sorted(SE.KNOWN_ENGINES)


def test_the_local_default_order_is_unchanged():
    """What web_capabilities' local path did before: searxng, brave, ddg."""
    assert SE.engine_order("auto", "searxng") == ["searxng", "brave", "ddg"]


def test_a_named_engine_leads_but_still_falls_back_to_ddg():
    """Both implementations already fell through to ddg on at least one path.
    Keeping that is what stops this consolidation taking results away from a
    caller who named an engine that happens to be down."""
    assert SE.engine_order("searxng") == ["searxng", "ddg"]
    assert SE.engine_order("brave") == ["brave", "ddg"]


def test_asking_for_ddg_does_not_list_it_twice():
    assert SE.engine_order("ddg") == ["ddg"]


def test_an_unknown_engine_falls_back_to_the_full_chain():
    assert SE.engine_order("altavista", "searxng") == ["searxng", "brave", "ddg"]
    assert SE.engine_order("", "searxng") == ["searxng", "brave", "ddg"]


# ── redirect unwrapping (the union of the two old copies) ───────────────────
def test_a_duckduckgo_wrapper_is_unwrapped():
    assert SE.decode_redirect(
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa&rut=z") == "https://x.com/a"


def test_a_bing_wrapper_is_unwrapped():
    """Present in the researcher's copy only - this is the union, not a
    narrowing."""
    assert SE.decode_redirect(
        "https://www.bing.com/ck/a?u=a1https%3A%2F%2Fx.com%2Fb") == "https://x.com/b"


def test_google_url_is_only_unwrapped_on_a_google_host():
    """web_capabilities required "google." in the host; the researcher's looser
    version rewrote ANY url containing "/url?". Keeping the stricter rule."""
    assert SE.decode_redirect(
        "https://www.google.com/url?q=https%3A%2F%2Fx.com%2Fc") == "https://x.com/c"
    other = "https://example.com/url?q=https%3A%2F%2Fevil.com"
    assert SE.decode_redirect(other) == other


def test_an_ordinary_url_is_returned_unchanged():
    assert SE.decode_redirect("https://x.com/a?b=1") == "https://x.com/a?b=1"
    assert SE.decode_redirect("") == ""


def test_html_entities_are_decoded():
    assert SE.decode_redirect("https://x.com/a?b=1&amp;c=2") == "https://x.com/a?b=1&c=2"
