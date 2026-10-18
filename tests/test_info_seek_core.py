"""info.seek's pure decisions (info-seeking plan SK-3.1). The owner, 2026-10-10: "latest updates on a topic" reached
for research.run (a report job of minutes) or got answered from memory with guesses. Pure: no network."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.web import info_seek_core as C                   # noqa: E402

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("q,need", [
    ("what's the latest on the EU AI Act?", "latest"), ("any news about n8n", "latest"),
    ("give me updates on UK house prices", "latest"), ("who won the race today", "latest"),
    ("what is the Bank of England base rate", "fact"), ("tell me about Copilot Studio agents", "fact"),
    ("explain how HNSW indexes work", "overview"), ("compare LangGraph and CrewAI", "overview"),
    ("write me a report on RAG rerankers", "deep"), ("do a deep dive into MCP security", "deep"),
])
def test_needs(q, need):
    assert C.classify_need(q)["need"] == need, q


@pytest.mark.parametrize("q,subject", [
    ("what's the latest on the EU AI Act?", "EU AI Act"), ("any news about n8n", "n8n"),
    ("give me updates on UK house prices", "UK house prices"), ("updates on MCP right now", "MCP"),
    ("latest AI/ML updates", "AI/ML"), ("write me a report on RAG rerankers", "RAG rerankers"),
    ("tell me about Copilot Studio agents", "Copilot Studio agents"), ("the news", "the news"),
])
def test_subject(q, subject):
    assert C.subject_of(q) == subject


def test_latest_queries_are_anchored_on_the_subject_and_dated():
    qs = C.latest_queries("EU AI Act", "what's the latest on the EU AI Act?", NOW)
    assert qs == ["EU AI Act latest news", "EU AI Act news October 2026", "what's the latest on the EU AI Act?"]
    assert C.latest_queries("MCP", "MCP", NOW) == ["MCP latest news", "MCP news October 2026"]


def test_merge_drops_the_same_page_and_the_same_headline():
    a = [{"url": "https://www.example.com/a/?utm_source=x", "title": "Rates held at 4 per cent by the Bank"}]
    b = [{"url": "https://example.com/a", "title": "other"},
         {"url": "https://mirror.example.org/a", "title": "Rates held at 4 per cent by the Bank"},
         {"url": "https://example.com/b", "title": "B"}]
    m = C.merge_hits([a, b])
    assert [x["url"] for x in m] == ["https://www.example.com/a/?utm_source=x", "https://example.com/b"]


@pytest.mark.parametrize("s,day", [
    ("2026-09-17", "2026-09-17"), ("2026-09-17T10:00:00Z", "2026-09-17"), ("Thu, 17 Sep 2026 10:00:00 +0000", "2026-09-17"),
    ("17 September 2026", "2026-09-17"), ("Sep 17, 2026", "2026-09-17"), ("posted 2026-09-17 by x", "2026-09-17"),
])
def test_dates(s, day):
    assert C.parse_date(s).strftime("%Y-%m-%d") == day


def test_no_date_is_none():
    assert C.parse_date("") is None and C.parse_date("yesterday") is None


def test_latest_order_newest_since_first_then_undated_then_old_and_future_is_undated():
    items = [{"t": "old", "published": "2026-08-01"}, {"t": "undated"}, {"t": "new1", "published": "2026-10-01"},
             {"t": "new2", "published": "2026-10-09"}, {"t": "future", "published": "2027-01-01"}]
    out = [x["t"] for x in C.order_latest(items, "2026-09-26", NOW)]
    assert out == ["new2", "new1", "undated", "future", "old"]


def test_per_site_keeps_depth_across_sources():
    items = [{"url": f"https://a.com/{i}"} for i in range(4)] + [{"url": "https://b.com/1"}]
    assert [x["url"] for x in C.per_site(items, 2)] == ["https://a.com/0", "https://a.com/1", "https://b.com/1"]


def test_excerpt_cuts_at_a_sentence():
    t = "First sentence here. Second sentence is longer than that. Third."
    assert C.excerpt(t, 40) == "First sentence here."
    assert C.excerpt("short", 40) == "short"


def test_pack_numbers_dates_and_says_what_is_missing():
    items = [{"title": "A", "url": "https://a.com/x", "published": "2026-10-08", "text": "body"},
             {"title": "B", "url": "https://b.com/y", "snippet": "snip", "kind": "intel"}]
    p = C.build_pack("latest", "q", "S", items, since="2026-09-26", checked=["web: 3"], now=NOW, elapsed_ms=5)
    assert [r["n"] for r in p["items"]] == [1, 2]
    assert p["items"][0]["published"] == "2026-10-08" and p["items"][0]["age_days"] == 2.8 and p["items"][0]["read"]
    assert p["items"][1]["kind"] == "intel" and p["items"][1]["text"] == "snip" and p["items"][1]["published"] == ""
    assert p["gaps"] == [] and "never fill a gap" in p["guide"]
    stale = C.build_pack("latest", "q", "S", [{"title": "old", "url": "https://o.com", "published": "2026-01-01"}],
                         since="2026-09-26", now=NOW)
    assert stale["gaps"] == ["no source found is dated on or after 2026-09-26"]
    assert C.build_pack("fact", "q", "S", [], now=NOW)["gaps"] == ["nothing was found"]


# ── dates the results state about themselves; titles; DDG; SearXNG news params ──

from vera.web import search_engines as SE                  # noqa: E402


@pytest.mark.parametrize("text,day", [
    ("14 hours ago | Watford Observer on MSN", "2026-10-10"), ("3 days ago - Reuters", "2026-10-07"),
    ("2 weeks ago", "2026-09-26"), ("yesterday", "2026-10-09"), ("Sep 17, 2026 - The bank held rates", "2026-09-17"),
    ("17 September 2026 ... text", "2026-09-17"),
])
def test_date_from_text(text, day):
    assert C.date_from_text(text, NOW).strftime("%Y-%m-%d") == day


def test_an_event_date_inside_the_text_is_not_the_page_date():
    assert C.date_from_text("What changed on 2 August 2026 for providers", NOW) is None


def test_fill_dates_uses_the_byline_then_the_snippet_and_keeps_a_real_date():
    items = [{"age_text": "5 hours ago | BBC"}, {"snippet": "Oct 1, 2026 - rates"}, {"published": "2026-09-01"}, {}]
    assert C.fill_dates(items, NOW) == 2
    assert [i.get("published") for i in items] == ["2026-10-10", "2026-10-01", "2026-09-01", None]


def test_pack_titles_are_unescaped():
    p = C.build_pack("fact", "q", "s", [{"title": "What&#x27;s Delayed &amp; What Applies", "url": "https://x.eu"}], now=NOW)
    assert p["items"][0]["title"] == "What's Delayed & What Applies"


def test_searxng_news_params_only_when_asked():
    assert SE.searxng_params("x") == {"q": "x", "format": "json", "language": SE.DEFAULT_LANGUAGE, "safesearch": 0}
    p = SE.searxng_params("x", categories="news", time_range="week")
    assert p["categories"] == "news" and p["time_range"] == "week"
    assert "time_range" not in SE.searxng_params("x", time_range="fortnight")


DDG = '''<div class="result"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.eu%2Fx&amp;rut=1">The <b>EU AI Act</b> in 2026</a></h2>
<a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.eu%2Fx">a.eu/x</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A running guide to where the <b>EU AI Act</b> stands &amp; what&#x27;s next.</a></div>
<div class="result"><h2 class="result__title"><a rel="nofollow" class="result__a" href="https://b.eu/y">No snippet here</a></h2></div>
<div class="result"><h2 class="result__title"><a class="result__a" rel="nofollow" href="https://c.eu/z">Third</a></h2>
<a class="result__snippet" href="https://c.eu/z">Third's own snippet</a></div>'''


def test_ddg_snippets_keep_the_bolded_words_and_stay_with_their_result():
    r = SE.parse_ddg_html(DDG)
    assert [x["title"] for x in r] == ["The EU AI Act in 2026", "No snippet here", "Third"]
    assert r[0]["snippet"] == "A running guide to where the EU AI Act stands & what's next."
    assert r[0]["raw_url"] == "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.eu%2Fx&rut=1"
    assert r[1]["snippet"] == "" and r[2]["snippet"] == "Third's own snippet"   # never the neighbour's


def test_the_researcher_ddg_uses_the_shared_parser():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "research" / "researcher_api.py").read_text(encoding="utf-8")
    body = src[src.index("async def search_ddg"):src.index("async def search_ddg") + 2500]
    assert "_engines.parse_ddg_html(r.text)" in body and "result__snippet" not in body


def test_fact_and_overview_queries_lead_with_the_subjects_keywords():
    # prod 10 Oct: the question form returned bank home pages, the keyword form the Bank's rate page and the BBC
    assert C.fact_queries("Bank of England base rate", "what is the Bank of England base rate?") == [
        "Bank of England base rate", "what is the Bank of England base rate"]
    assert C.fact_queries("x", "x?") == ["x"]
    assert C.overview_queries("RAG rerankers", "compare RAG rerankers", NOW) == [
        "RAG rerankers", "RAG rerankers explained overview 2026", "compare RAG rerankers"]


def test_the_cap_reads_the_newest_first_and_ranks_facts_against_the_question():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    body = src[src.index("async def cap_info_seek"):]
    latest = body[body.index('if need == "latest":'):body.index("    else:\n        # fact")]
    assert latest.index("items = _seek.order_latest(items, since, now)") < latest.index("got = await _seek_read(")
    other = body[body.index("    else:\n        # fact"):body.index("intel = await intel_task")]
    assert "_seek.fact_queries(subject, question)" in other and "await _seek_rerank(question, hits)" in other
    assert "web.research" not in other                    # one question-shaped search was the bank-homepage failure


def test_searches_are_spaced_not_sent_in_one_burst():
    # 2026-10-10: four searches at once hit every upstream engine four times in the same instant; brave and
    # duckduckgo answered this IP with "too many requests" and CAPTCHAs
    import asyncio, time
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _seek_searches"):src.index("def _report_seller")]
    starts = []

    async def fake_call(name, timeout, **kw):
        starts.append((kw["query"], time.monotonic()))
        return {"results": [kw["query"]]}
    ns = {"asyncio": asyncio, "List": list, "Dict": dict, "Any": object, "SEEK_SPACING_S": 0.05, "_seek_call": fake_call}
    exec(fn, ns)
    t0 = time.monotonic()
    out = asyncio.run(ns["_seek_searches"](["a", "b", "c"], first_slot=1))
    assert [r["results"] for r in out] == [["a"], ["b"], ["c"]]          # results in query order
    gaps = [round(t - t0, 2) for _, t in sorted(starts, key=lambda x: x[1])]
    assert gaps[0] >= 0.045 and gaps[1] - gaps[0] >= 0.04 and gaps[2] - gaps[1] >= 0.04
    assert "res = await _seek_searches(queries, first_slot=1)" in src and "res = await _seek_searches(queries)\n" in src
    assert 'os.getenv("VERA_SEEK_SPACING_S", "0.5")' in src


def _fn_src(path, start, end):
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / path).read_text(encoding="utf-8")
    return src[src.index(start):src.index(end)], src


def test_a_challenge_page_is_not_an_article_and_is_never_cached():
    # 2026-10-10: MSE's Cloudflare "Just a moment..." came back from reader.page as a read article, cached for a week
    from vera.web import web_client as wc
    body, src = _fn_src("browser_capabilities.py", "def _is_challenge", "async def reader_read")
    ns = {"_wc": wc}
    exec(body, ns)
    ch = ns["_is_challenge"]
    assert ch({"title": "Just a moment...", "markdown": "Enable JavaScript and cookies to continue"})
    assert ch({"title": "", "markdown": "Checking your browser before accessing the site."})
    assert not ch({"title": "Bank Rate held at 4%", "markdown": "The MPC voted... " * 40})
    assert not ch({"title": "Why access denied errors happen", "markdown": "access denied " + "long article " * 400})
    rd = src[src.index("async def reader_read"):]
    assert 'page_html, title = ("" if fp.get("block_reason") else fp.get("html") or "")' in rd
    assert "if rec and _is_challenge(rec):" in rd and rd.index("if rec and _is_challenge(rec):") < rd.index("await _rc.put(")
    assert 'and not _is_challenge(hit):' in rd                      # a challenge already cached is read again


def test_seek_read_reads_the_next_result_when_one_is_blocked():
    import asyncio
    body, _ = _fn_src("web_capabilities.py", "async def _seek_read", "async def _seek_rerank")
    calls = []

    async def fake_call(name, timeout, **kw):
        calls.append(kw["url"])
        if "blocked" in kw["url"]:
            return {"ok": False, "error": "blocked: the page is an anti-bot challenge, not the article"}
        return {"ok": True, "markdown": "Article text. " * 20, "title": "T", "published": "2026-10-09"}
    ns = {"asyncio": asyncio, "List": list, "Dict": dict, "Any": object, "_seek_call": fake_call, "_seek": C}
    exec(body, ns)
    items = [{"url": "https://a/1"}, {"url": "https://blocked/2"}, {"url": "https://c/3"}, {"url": "https://d/4"},
             {"url": "https://intel/5", "kind": "intel"}]
    got = asyncio.run(ns["_seek_read"](items, 3, 200))
    assert got == 3 and calls == ["https://a/1", "https://blocked/2", "https://c/3", "https://d/4"]
    assert items[1]["unreadable"].startswith("blocked") and not items[1].get("text")
    assert items[3]["text"] and items[3]["published"] == "2026-10-09" and "text" not in items[4]


def test_deep_without_a_background_job_for_loops():
    # loops block research.* job caps (compute-roles, 10 Oct): need=deep with start_report=false is the overview only
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    body = src[src.index("async def cap_info_seek"):]
    assert "start_report: bool = True" in body
    i, j = body.index('if need == "deep" and not start_report:'), body.index('elif need == "deep":')
    assert i < j and "research.run" not in body[i:j] and "research.run" in body[j:j + 300]


# baseline run 10 Oct (tools/info-eval): L19 "updates on the ... white paper" was taken as a report request and
# started research.run; F04 "When does ... apply?" searched the question and got dictionary pages for "does"
@pytest.mark.parametrize("q,need", [
    ("Any updates on the UK AI regulation white paper or AI bill?", "latest"),
    ("what does the research report say about rates", "fact"),
    ("write a white paper on AI in valuation", "deep"), ("give me a detailed report on n8n", "deep"),
])
def test_a_named_document_is_not_a_report_request(q, need):
    assert C.classify_need(q)["need"] == need


def test_wh_questions_search_their_keywords_first():
    assert C.keywords_of("When does the EU AI Act's high-risk obligation apply?") == "EU AI Act's high-risk obligation apply"
    q = "When does the EU AI Act's high-risk obligation apply?"
    assert C.fact_queries(C.subject_of(q), q)[0] == "EU AI Act's high-risk obligation apply"
    assert C.fact_queries("Bank of England base rate", "What is the Bank of England base rate?")[0] == "Bank of England base rate"


def test_no_control_characters_in_the_core():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "info_seek_core.py").read_text(encoding="utf-8")
    assert not any(ord(ch) < 9 for ch in src)          # a patch once wrote \b as a backspace byte


@pytest.mark.parametrize("text,day", [
    ("# High-level summary\nUpdated on 31 August 2026\nThe AI Act...", "2026-08-31"),
    ("Title\nPublished Time: 2026-09-17T10:00:00\n...", "2026-09-17"),
    ("By X | Posted on Oct 3, 2026 | 4 min read", "2026-10-03"),
    ("Last updated: 1st October 2026", "2026-10-01"),
])
def test_a_labelled_page_date(text, day):
    assert C.labelled_date(text).strftime("%Y-%m-%d") == day


@pytest.mark.parametrize("text", [
    "Ollama is a tool... Initial release July 8, 2023",          # an event, not the page's date
    "Consultation closes 31 October 2026",                        # a deadline
    "x" * 1600 + " Updated on 1 May 2026",                        # too far down to be the page's own line
])
def test_a_bare_or_distant_date_is_not_the_page_date(text):
    assert C.labelled_date(text) is None


def test_since_your_last_look():
    assert C.subject_key("EU AI Act!") == "eu-ai-act"
    s = C.since_for("2026-10-01T09:00:00+00:00", NOW)
    assert s == {"since": "2026-10-01", "why": "since you last looked on 2026-10-01"}
    assert C.since_for("2026-10-10T09:00:00+00:00", NOW)["since"] == "2026-09-26"      # same day: default window
    assert C.since_for("2026-08-01", NOW)["since"] == "2026-09-26"                      # over a month: default
    assert C.since_for("", NOW) == {"since": "2026-09-26", "why": "the last 14 days"}


def test_latest_records_and_reads_the_last_look():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    body = src[src.index("async def cap_info_seek"):]
    latest = body[body.index('if need == "latest":'):body.index("    else:\n        # fact")]
    assert "_seek.since_for(await _last_seek(subject), now)" in latest
    assert latest.index("await _last_seek(subject)") < latest.index("await _last_seek(subject, now.isoformat(")
    helper = src[src.index("async def _last_seek"):src.index("def _report_seller")]
    assert "except Exception" in helper and "ex=_LAST_SEEK_TTL" in helper


# caps.search (plan SK-1.2): measured on prod 11 Oct - "latest news about MCP" -> mcp.register_server first,
# "what is the bank of england base rate" -> evolve.delegate.rate, "find out about the EU AI Act" -> operator caps
@pytest.mark.parametrize("q,want", [
    ("latest news about MCP", True), ("what is the bank of england base rate", True),
    ("find out about the EU AI Act", True), ("who won the f1 race", True), ("latest updates on UK house prices", True),
    ("find tools for sending email", False), ("what caps handle image generation", False),
    ("what is the census status", False), ("send an email to bob", False), ("create a dashboard panel", False),
])
def test_info_intent(q, want):
    assert C.info_intent(q) is want


def test_boost_puts_the_information_caps_first():
    res = [("mcp.register_server", 8.7), ("mcp.tools", 8.6), ("web.research", 6.0), ("x", 1.0)]
    out = C.boost_info(res, {"info.seek", "web.research", "intel.lookup", "mcp.tools"}, 5)
    assert [n for n, _ in out] == ["info.seek", "web.research", "intel.lookup", "mcp.register_server", "mcp.tools"]
    assert out[0][1] > out[1][1] > out[2][1] > 8.7
    assert [n for n, _ in C.boost_info(res, {"web.research"}, 3)] == ["web.research", "mcp.register_server", "mcp.tools"]


def test_caps_search_applies_the_boost_only_without_filters():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vera" / "dag" / "dag_store.py").read_text(encoding="utf-8")
    body = src[src.index("async def caps_search"):src.index("def caps_list_categories")]
    assert "if not category and not tags:" in body and "_isc.boost_info(results, CAPABILITY_REGISTRY.keys(), top_k)" in body


def test_feed_hits_match_the_feeds_vera_follows():
    # plan SK-3.5: an untracked subject still gets what the followed feeds carried (live 11 Oct: the Guardian property
    # feed had "UK house prices flatline..." for "UK house prices")
    import asyncio
    from pathlib import Path
    from vera.intel import intel_sources_core as isrc
    src = (Path(__file__).resolve().parents[1] / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _feed_hits"):src.index("async def _seek_read")]
    recs = [{"data": {"title": "UK house prices flatline as rising mortgage costs weigh", "link": "https://g.co/a",
                      "published": "Wed, 07 Oct 2026 07:56:02 GMT"}, "created_at": "2026-10-07T08:00:00Z"},
            {"data": {"title": "UK house price growth halves", "link": "https://g.co/b"}, "created_at": "x"},
            {"data": {"title": "Office rents rise in London", "link": "https://g.co/c"}, "created_at": "x"}]

    async def call(name, timeout, **kw):
        if name == "fabric.sources":
            return {"sources": [{"id": "intel-src-1", "label": "Guardian: property", "dataset_id": "d1"},
                                {"id": "other-src", "dataset_id": "d2"}]}
        assert kw["dataset_id"] == "d1"                      # only the feeds intel added
        return {"records": recs}
    ns = {"asyncio": asyncio, "List": list, "Dict": dict, "Any": object, "_seek_call": call, "_seek": C}
    exec(fn, ns)
    hits = asyncio.run(ns["_feed_hits"]("UK house prices"))
    assert [h["url"] for h in hits] == ["https://g.co/a", "https://g.co/b"]
    assert hits[0]["engine"] == "feed: Guardian: property" and hits[0]["published"].startswith("2026-10-07")
    assert "feed_task = asyncio.create_task(_feed_hits(subject))" in src



def test_known_items_from_the_page_index():
    """web-ingress WI-10b: what the fabric already read joins the evidence, one per site, never a duplicate of a
    live result, never a near-empty page."""
    body = "Lab space vacancy in Cambridge fell to 2.1 per cent as demand from life science firms continued. " * 4
    pages = [{"url": "https://www.bidwells.co.uk/research/lab", "title": "Lab market", "full_text": body, "synced_at": "2026-10-01"},
             {"url": "https://bidwells.co.uk/research/other", "title": "Same site", "full_text": body},
             {"url": "https://news.example/a?utm_source=x", "title": "Already live", "full_text": body},
             {"url": "https://thin.example/", "title": "Thin", "full_text": "too short"},
             {"url": "https://savills.co.uk/lab", "title": "Savills lab", "full_text": body}]
    live = [{"url": "https://news.example/a", "title": "live"}]
    k = C.known_items(pages, live, chars=120)
    assert [x["url"] for x in k] == ["https://www.bidwells.co.uk/research/lab", "https://savills.co.uk/lab"]
    assert all(x["kind"] == "known" and len(x["text"]) <= 120 and x["snippet"] for x in k)
    assert k[0]["seen"] == "2026-10-01"
    assert C.known_items([], live) == [] and len(C.known_items(pages, [], limit=1)) == 1


def test_info_seek_asks_the_page_index_bounded():
    import ast
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "web", "web_capabilities.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_info_seek")
    d = ast.dump(fn)
    assert "discovery.recall_index.search" in d and "known_items" in d
    assert '"discovery.recall_index.search", 3' in src


def test_known_pages_must_be_about_the_question():
    """web-ingress WI-14a: the index ORs words, so 'zubrowka coumarin limits 2026' ranked a paper list and an ONS
    bulletin (prod, 18 Oct). A known page needs half the subject's words; years do not count."""
    assert C.cover_terms("what are the zubrowka coumarin limits in 2026") == ["zubro", "couma", "limit"]
    assert C.coverage("zubrowka coumarin limits 2026", "Paper list 2026: limits of LLMs") == 1 / 3
    assert C.coverage("Cambridge lab space", "lab space vacancy in Cambridge") == 1.0 and C.coverage("in 2026", "x") == 1.0
    body = "Lab space vacancy in Cambridge fell to 2.1 per cent as demand from life science firms continued. " * 4
    other = "Private rent and house prices rose 5 per cent in the year to 2026, the ONS said today in its bulletin. " * 4
    pages = [{"url": "https://ons.example/rent", "title": "Rents", "full_text": other},
             {"url": "https://bidwells.example/lab", "title": "Lab market", "full_text": body}]
    k = C.known_items(pages, [], query="Cambridge lab space vacancy 2026")
    assert [x["url"] for x in k] == ["https://bidwells.example/lab"]
    assert len(C.known_items(pages, [])) == 2
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "web", "web_capabilities.py"), encoding="utf-8").read()
    assert "chars=chars, query=subject)" in src


def test_a_known_page_that_is_also_a_live_result_is_not_a_gap():
    """prod 18 Oct: after the gap fill crawled the zubrowka pages, the next seek found one of them live as well;
    known_items rightly left it out of the evidence, and info.seek recorded the gap again. The gap test now asks
    the index alone."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "web", "web_capabilities.py"), encoding="utf-8").read()
    i = src.index("_seek.known_items(known.get(\"pages\") or [], [], chars=80, query=subject, limit=1)")
    assert i < src.index('record_gap(subject, caller="info.seek")')
    body = "Zubrowka bison grass vodka: coumarin limits in the EU are 2 mg per litre for spirit drinks. " * 4
    pages = [{"url": "https://monkeybrad.example/tatanka", "title": "Tatanka", "full_text": body}]
    live = [{"url": "https://monkeybrad.example/tatanka"}]
    assert C.known_items(pages, live, query="zubrowka coumarin limits 2026") == []
    assert len(C.known_items(pages, [], query="zubrowka coumarin limits 2026", limit=1)) == 1
