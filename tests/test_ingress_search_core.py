"""The targeted index's query (vera/fabric/ingress_search_core.py; web-ingress WI-14a): index hits ranked with the web
map - topic members first, fresh and novel pages a little higher - and the gap rows."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.fabric import ingress_search_core as sc  # noqa: E402

NOW = datetime(2026, 10, 18, 13, 0, tzinfo=timezone.utc)


def test_rank_uses_the_text_rank_first_then_the_map():
    pages = [{"url": "https://a.example/1", "title": "A1", "full_text": "lab space " * 50, "rank": 0.8},
             {"url": "https://b.example/2", "title": "B2", "full_text": "lab space", "rank": 0.6},
             {"url": "https://c.example/3", "title": "C3", "full_text": "x", "rank": 0.5},
             {"url": "https://a.example/1", "title": "dup", "full_text": "", "rank": 0.1}]
    rows = {"https://b.example/2": {"first_seen": NOW - timedelta(days=40), "last_seen": NOW - timedelta(days=2),
                                    "fetches": 4, "changes": 1, "novelty": 1.0, "kind": "page"},
            "https://c.example/3": {"first_seen": NOW - timedelta(days=90), "last_seen": NOW - timedelta(days=60),
                                    "fetches": 1, "novelty": 0.0, "kind": "file"}}
    out = sc.rank(pages, rows, ["https://b.example/2"], now=NOW, limit=10)
    assert [r["url"] for r in out] == ["https://b.example/2", "https://a.example/1", "https://c.example/3"]
    b = out[0]
    assert b["topic_member"] and b["fetches"] == 4 and b["changes"] == 1 and b["known_since"].startswith("2026-09-08")
    assert b["score"] == round(0.75 + 0.3 + 0.1 + 0.1, 4) and out[2]["kind"] == "file" and out[1]["known_since"] == ""
    assert len(out[1]["excerpt"]) <= sc.EXCERPT and sc.rank([], {}) == []
    assert len(sc.rank(pages, rows, limit=1)) == 1


def test_gap_rows_and_which_are_open():
    g = sc.gap_rows([{"query": "lab space cambridge", "n": 3, "last": NOW, "callers": ["info.seek"]},
                     {"query": "filled", "n": 1, "last": NOW - timedelta(hours=2), "filled": NOW},
                     {"query": "asked again", "n": 2, "last": NOW, "filled": NOW - timedelta(days=1)}])
    assert g[0] == {"query": "lab space cambridge", "times": 3, "last": NOW.isoformat(), "callers": ["info.seek"],
                    "filled": "", "open": True}
    assert [x["open"] for x in g] == [True, False, True]


def test_fill_plan():
    gaps = [{"query": "Lab space  Cambridge", "open": True}, {"query": "lab space cambridge", "open": True},
            {"query": "done already", "open": False}, {"query": "zubrowka", "open": True},
            {"query": "coumarin limits vodka", "open": True}, {"query": "third one here", "open": True}]
    assert sc.fill_plan(gaps) == ["Lab space Cambridge", "coumarin limits vodka", "third one here"]
    assert sc.fill_plan(gaps, max_gaps=1) == ["Lab space Cambridge"] and sc.fill_plan([]) == []


def test_info_seek_records_gaps_and_the_caps_exist():
    src = (ROOT / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    assert 'record_gap(subject, caller="info.seek")' in src
    caps = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    assert '@capability("fabric.map.search"' in caps and '@capability("fabric.map.gaps"' in caps
    assert "purpose = 'gap'" in caps and 'purpose="gap"' in caps


def test_gap_fill_cap_crawls_without_the_llm_and_marks_the_gap():
    import ast
    src = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_map_gaps_fill")
    seg = ast.get_source_segment(src, fn)
    assert "discover_crawl_urls" in seg and "reconcile=False" in seg and 'purpose="gap_fill"' in seg
    assert "fill_plan" in seg and "budget" in seg
    assert "f.purpose = 'gap_fill'" in src


def test_the_gap_fill_may_run_from_the_schedule():
    from vera.evolve import schedule_core as SC
    assert any("fabric.map.gaps.fill".startswith(p) for p in SC.CAP_ALLOW_PREFIXES)
    assert not any("fabric.map.search".startswith(p) for p in SC.CAP_ALLOW_PREFIXES)


def test_overlap_and_the_daily_metrics():
    assert sc.overlap_at(["a", "b", "c"], ["b", "x", "a", "y"]) == 0.5 and sc.overlap_at(["a"], []) is None
    assert sc.overlap_at(list("abcdefghijkl"), list("lkjihgfedcba")) == round(8 / 10, 3)   # top 10 of each
    m = sc.daily_metrics({"index_hit": 3, "gap": 1}, [0.5, None, 0.3],
                         [{"topic_key": "t1", "newest": NOW - timedelta(days=2)},
                          {"topic_key": "t2", "newest": NOW - timedelta(days=10)},
                          {"topic_key": "t3", "newest": None}],
                         {"fetched": 40, "new": 12, "hours": 3}, 80, now=NOW)
    assert m["questions"] == 4 and m["index_answer_rate"] == 0.75 and m["live_overlap"] == 0.4
    assert m["overlap_samples"] == 2 and m["freshness_lag_days"]["topics"] == 2
    assert m["freshness_lag_days"]["worst"] == ("t2", 10.0) and m["freshness_lag_days"]["median"] == 10.0
    assert m["novel_yield_per_crawl_hour"] == 4.0 and m["pages_per_question"] == 20.0
    e = sc.daily_metrics({}, [], [], {}, 0, now=NOW)
    assert e["index_answer_rate"] is None and e["live_overlap"] is None and e["pages_per_question"] is None


def test_info_seek_counts_index_hits_and_the_metrics_cap_is_schedulable():
    src = (ROOT / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    assert 'purpose="index_hit"' in src and src.index('purpose="index_hit"') < src.index('record_gap(subject')
    caps = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    assert '@capability("fabric.map.metrics"' in caps and "web_metrics_daily" in caps
    from vera.evolve import schedule_core as SC
    assert SC.cap_allowed("fabric.map.metrics") and SC.cap_allowed("fabric.map.gaps.fill")
    assert not SC.cap_allowed("fabric.map.search")


def test_rrf_fuses_the_legs_by_page_and_the_reranker_blends_in():
    key = lambda u: u.split("#")[0]
    text = [{"url": "https://a/1", "full_text": "short"}, {"url": "https://b/2", "full_text": "b"}]
    vec = [{"url": "https://b/2", "full_text": "b text that is longer"}, {"url": "https://c/3", "full_text": "c"},
           {"url": "https://a/1#x", "full_text": "a much longer text"}]
    f = sc.fuse({"text": text, "vector": vec}, key)
    assert [r["url"].split("#")[0] for r in f] == ["https://b/2", "https://a/1", "https://c/3"]
    assert f[0]["legs"] == ["text", "vector"] and f[0]["full_text"] == "b text that is longer"
    assert f[1]["full_text"] == "a much longer text" and f[2]["legs"] == ["vector"]
    assert abs(f[0]["rank"] - (1 / 62 + 1 / 61)) < 1e-9
    r = sc.apply_rerank(f, [{"index": 2, "score": 6.0}, {"index": 0, "score": -6.0}])
    assert r[0]["url"] == "https://c/3" and r[0]["rerank"] > 0.99 and r[-1]["rerank"] is None
    assert sc.apply_rerank(f, []) == f and sc.squash(0.4) == 0.4 and sc.squash("x") == 0.0
    assert sc.record_page("p1", "web.search", "body", '{"url": "https://x/y", "title": "T"}', 0.8)["rank"] == 0.8
    assert sc.record_page("r1", "memory.chat", "hi", '{"text": "no url"}', 0.9) is None


def test_map_search_runs_the_legs_concurrently_and_never_filters_chroma():
    import ast
    src = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_vector_pages")
    seg = ast.get_source_segment(src, fn)
    assert "store.search(emb, top_k=n)" in seg and "dataset_id" not in seg and "VECTOR_TIMEOUT_S" in seg
    ms = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_map_search")
    seg = ast.get_source_segment(src, ms)
    assert "ensure_future(_text())" in seg and "sc.fuse(" in seg and '"nlp.rerank"' in seg and "RERANK_TIMEOUT_S" in seg


def test_retention_curves():
    rev = [{"bucket": "first", "n": 60}, {"bucket": "<1d", "n": 20, "unchanged": 15, "comparable": 20},
           {"bucket": "7-30d", "n": 20, "unchanged": 4, "comparable": 10}]
    rec = [{"bucket": "<1d", "n": 3}, {"bucket": "30-60d", "n": 1}]
    r = sc.retention_curves(rev, rec, {"web_fetches": 1000, "web_links": 500, "web_urls": 500},
                            {"web_fetches": 100, "web_links": 50}, 2.0)
    assert r["fetches"] == 100 and r["first_fetches"] == 60
    assert r["by_age"]["<1d"] == {"revisits": 20, "revisit_rate": 0.2, "unchanged_rate": 0.75}
    assert r["by_age"]["7-30d"]["unchanged_rate"] == 0.4 and r["by_age"][">90d"]["revisit_rate"] == 0.0
    assert r["recall_by_age"]["<1d"] == 0.75 and r["recall_by_age"]["30-60d"] == 0.25 and r["cited"] == 4
    assert r["cost"] == {"map_bytes": 2000, "bytes_per_day": 1000, "bytes_per_receipt": 10, "bytes_per_link": 10}
    e = sc.retention_curves([], [], {}, {}, 0)
    assert e["by_age"]["<1d"]["revisit_rate"] is None and e["cost"]["bytes_per_day"] is None


def test_fetch_receipts_carry_their_content_hash():
    from vera.fabric import ingress_receipts_core as rc
    assert "content_hash) VALUES" in rc.INSERT_FETCH and "$11" in rc.INSERT_FETCH
    assert any("web_fetches ADD COLUMN IF NOT EXISTS content_hash" in s for s in rc.DDL)
    caps = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    assert '@capability("fabric.map.retention"' in caps and 'm["retention"]' in caps


def test_concept_seeds_follow_vera62s_terms():
    cs = [{"idx": 1, "population": 19, "label": "Advances in Reasoning AI",
           "members_sample": [{"text": "From Soliloquy to Agora\nbody"}, {"text": "SOLAR-RL: long horizon\nx"}, {"text": "third"}]},
          {"idx": 2, "population": 18, "label": "Diverse Topic Clustering Samples", "members_sample": []},
          {"idx": 3, "population": 2, "label": "Tiny", "members_sample": []},
          {"idx": 4, "population": 424, "label": "Tech News", "members_sample": []},
          {"idx": 5, "population": 5, "label": "UK Lab Space", "members_sample": [{"text": "Cambridge vacancy"}]},
          {"idx": 6, "population": 7, "label": "Proptech", "members_sample": []}]
    seeds = sc.concept_seeds(cs, {6: NOW - timedelta(days=3), 5: NOW - timedelta(days=9)})
    assert [s["idx"] for s in seeds] == [1, 5, 6]               # never seeded first, then oldest-seeded
    assert seeds[0]["query"] == "Advances in Reasoning AI From Soliloquy to Agora SOLAR-RL: long horizon"
    assert seeds[1]["query"] == "UK Lab Space Cambridge vacancy" and len(sc.concept_seeds(cs, {}, cap=1)) == 1
    eff = sc.seed_effect([{"concept_idx": 5, "label": "UK Lab Space", "population_at": 5, "pages": 4},
                          {"concept_idx": 5, "label": "UK Lab Space", "population_at": 6, "pages": 3}], {5: 9})
    assert eff == [{"idx": 5, "label": "UK Lab Space", "population_at_first_seed": 5, "population_now": 9, "change": 4,
                    "seedings": 2, "pages": 7}]
    assert sc.seed_effect([{"concept_idx": 7, "population_at": 3}], {})[0]["change"] is None


def test_the_web_map_pane_is_read_only_and_lives_in_the_fabric_panel():
    caps = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    assert '"web-map", "Web map"' in caps and 'mode="element"' in caps and '<iframe src="/fabric/map/panel"' in caps
    page = (ROOT / "vera" / "fabric" / "web_map_panel.html").read_text(encoding="utf-8")
    for path in ("/fabric/map/stats", "/fabric/map/metrics/history", "/fabric/map/topics", "/fabric/map/gaps",
                 "/fabric/map/concept_seeds"):
        assert path in page, path
    assert "<button" not in page and "method: 'POST'" not in page and "gaps/fill" not in page     # no controls
    fab = (ROOT / "vera" / "fabric" / "fabric_panel.html").read_text(encoding="utf-8")
    assert 'id="fnav-webmap"' in fab and 'id="fsec-webmap"' in fab and "'/fabric/map/panel'" in fab
    assert fab.count("'webmap'") >= 2
