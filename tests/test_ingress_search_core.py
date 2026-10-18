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
