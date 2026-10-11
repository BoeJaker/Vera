"""Relevance x recency x importance (four-minds FM-7.2): per-class half-lives, a floor, temporal intent from the
question, importance; behind memory.rank, off by default."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import rank_core as RK  # noqa: E402

pytestmark = pytest.mark.critical
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def test_classes():
    assert RK.klass("web.search") == "news" and RK.klass("docs_bbc_com") == "news"
    assert RK.klass("mkt.yahoo.spy.1d") == "telemetry" and RK.klass("research.reports") == "research"
    assert RK.klass("memory.message") == "chat" and RK.klass("memory.fact") == "fact"
    assert RK.klass("cve_nvd") == "default" and RK.klass("") == "default"


def test_half_lives_floor_and_no_decay_classes():
    assert RK.time_weight(3.0, "news") == pytest.approx(0.5)
    assert RK.time_weight(60.0, "research") == pytest.approx(0.5)
    assert RK.time_weight(400.0, "news") == RK.FLOOR
    assert RK.time_weight(4000.0, "docs") == 1.0 and RK.time_weight(None, "news") == 1.0


def test_temporal_intent_steepens_or_switches_off_decay():
    assert RK.temporal_intent("what is the latest on Valkey?") == "recent"
    assert RK.temporal_intent("how did we originally set up the census?") == "historical"
    assert RK.temporal_intent("explain the census") == "none"
    assert RK.time_weight(3.0, "news", "recent") == pytest.approx(0.25)
    assert RK.time_weight(300.0, "news", "historical") == 1.0


def test_score_orders_fresh_news_over_stale_but_keeps_docs():
    fresh = RK.score(0.8, _ago(1), "web.search", now=NOW)
    stale = RK.score(1.0, _ago(30), "web.search", now=NOW)
    assert fresh > stale
    assert RK.score(1.0, _ago(900), "docs.design", now=NOW) == 1.0
    assert RK.score(1.0, _ago(1), "memory.fact", importance=0.0, now=NOW) == 0.5
    assert RK.score(1.0, "not a date", "web.search", now=NOW) == 1.0


def test_seek_uses_rank_core_only_behind_its_switch():
    src = open(os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py"), encoding="utf-8").read()
    body = src[src.index("async def cap_memory_seek("):src.index("async def cap_memory_read(")]
    assert 'ranked_mode = await _switch("memory.rank") == "on"' in body
    assert "_rk.score(rel, c[\"created_at\"], c[\"dataset_id\"], c.get(\"importance\"), intent) if ranked_mode" in body
    assert "else rel * _recency_boost(c[\"created_at\"])" in body
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["memory.rank"]
    assert f.modes == ("off", "on") and f.default == "off" and f.package == "FM-7.2"
