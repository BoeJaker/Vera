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


def test_gap_rows():
    g = sc.gap_rows([{"query": "lab space cambridge", "n": 3, "last": NOW, "callers": ["info.seek"]}])
    assert g == [{"query": "lab space cambridge", "times": 3, "last": NOW.isoformat(), "callers": ["info.seek"]}]


def test_info_seek_records_gaps_and_the_caps_exist():
    src = (ROOT / "vera" / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    assert 'record_gap(subject, caller="info.seek")' in src
    caps = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    assert '@capability("fabric.map.search"' in caps and '@capability("fabric.map.gaps"' in caps
    assert "purpose = 'gap'" in caps and 'purpose="gap"' in caps
