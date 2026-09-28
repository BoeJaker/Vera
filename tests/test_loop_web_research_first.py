"""web.research is the loop's front door for external facts.

Six censuses of `research-report` ("Produce a short report on the Redis
licensing change and the Valkey fork, with citations") chained web.search ->
web.fetch -> http.get across 26 calls to the wall cap, four times out of six,
while `web.research` - one call that searches and reads the top pages - was
never seeded for the goal, never co-offered with the other web caps, cut to a
2000-char preview when it was used, filtered out of read-only phases, and
never named in a prompt. These tests pin the repair. The prompt goldens
(tests/golden/*.json) pin the exact wording.

Imports the loop module, so it runs in-container and skips on a host venv.
"""
import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
    from Vera.vera.dag import loop_prompt_rules as R
    from Vera.vera.dag import loop_profiles as P
except Exception:                                    # pragma: no cover
    M = R = P = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")

CENSUS_GOAL = "Produce a short report on the Redis licensing change and the Valkey fork, with citations."
WEBGPU_GOAL = "Research the current state of WebGPU support across major browsers and write a short summary citing your sources."


def test_a_report_with_citations_is_webby_and_research():
    assert M._v5_goal_is_webby(CENSUS_GOAL)
    assert M._v5_goal_is_webby(WEBGPU_GOAL)
    assert M._v7_intent_heuristic(CENSUS_GOAL) in ("research", "mixed")
    assert M._v7_intent_heuristic(WEBGPU_GOAL) in ("research", "mixed")
    # a self-contained build stays build
    assert M._v7_intent_heuristic("Build a habit tracker web app as a single self-contained index.html") == "build"


def test_web_research_is_seeded_first_and_co_offered():
    # _v5_seed_caps_for keeps only registered caps, and which caps a test
    # container registers varies; pin the ORDER the function asks for instead.
    from Vera.vera import capability_orchestration as O
    fake = {c: {"func": None} for c in ("web.research", "web.search", "web.fetch", "http.get",
                                          "exec.bash.run", "code.author", "prose.author")}
    saved = dict(O.CAPABILITY_REGISTRY)
    O.CAPABILITY_REGISTRY.update({k: v for k, v in fake.items() if k not in O.CAPABILITY_REGISTRY})
    try:
        seeds = M._v5_seed_caps_for(CENSUS_GOAL)
        assert seeds and seeds[0] == "web.research", seeds
        assert seeds.index("web.research") < seeds.index("http.get")
        plain = M._v5_seed_caps_for("Create clock.html - a self-contained HTML page showing a live digital clock")
        assert plain and "web.research" not in plain, plain
    finally:
        O.CAPABILITY_REGISTRY.clear()
        O.CAPABILITY_REGISTRY.update(saved)
    cohort = [c for c in M._V5_CAP_COHORTS if "web.search" in c][0]
    assert cohort[0] == "web.research" and "web.fetch" in cohort and "http.get" in cohort


def test_web_research_is_read_only_and_longform():
    assert M._v5_is_read_only("web.research") and M._v5_is_read_only("web.crawl")
    assert "web.research" in M._V5_LONGFORM_EXACT
    assert M._v5_preview_budget("web.research") >= M._v5_preview_budget("web.fetch")


def test_the_routing_rule_and_profile_lead_with_web_research():
    text = R.CAP_ROUTING.text
    assert "web.research" in text
    assert text.index("web.research") < text.index("web.search")
    assert "browser.navigate" not in text
    prof = next((x for x in P.LOOP_PROFILES if x.get("id") == "research-brief"), None)
    assert prof is not None
    caps = list(prof.get("caps") or [])
    assert "web.research" in caps and caps.index("web.research") < caps.index("web.search")


def test_the_research_directive_leads_with_web_research():
    d = M._v7_intent_plan_directive("research")
    assert "web.research" in d and d.index("web.research") < d.index("web.fetch")
    assert "web.search \u2192 web.fetch" in d or "web.search → web.fetch" in d   # named only as the thing NOT to do


def test_web_fetch_reports_a_404_as_a_failure(monkeypatch):
    """A 404 page's text is not a source (census run58 cited a typo'd URL it
    had 'fetched' twice)."""
    import asyncio
    from Vera.vera.web import web_capabilities as WC

    async def fake_fetch(url, timeout=8.0, max_chars=16000):
        return {"html": "<h1>Not found</h1>", "text": "Not found", "title": "404", "status": 404}

    monkeypatch.setattr(WC._wc, "fetch_page", fake_fetch)
    res = asyncio.new_event_loop().run_until_complete(
        WC.cap_web_fetch(url="https://example.com/no-such-page", ingest_to_fabric=False))
    assert res.get("ok") is False and "404" in str(res.get("error"))
    assert res.get("status_code") == 404 and res.get("chars") == 0
