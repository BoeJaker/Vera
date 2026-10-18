"""Discovery's page search index in Postgres (four-minds FM-7.1 / Q5): the query builder, the sync rows and the
search SQL are pure; discover_recall uses the index only behind its switch and keeps the LIKE path otherwise."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import page_search_core as PS  # noqa: E402

pytestmark = pytest.mark.critical


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_terms_and_the_or_query():
    assert PS.terms("What's the latest on Redis licensing, redis?") == ["what", "the", "latest", "redis", "licensing"]
    assert PS.or_tsquery("Valkey vs Redis") == "valkey | redis"
    assert PS.or_tsquery("a b !! ??") == "" and PS.or_tsquery("") == ""
    assert len(PS.terms(" ".join(f"word{i}" for i in range(20)))) == PS.MAX_TERMS


def test_no_tsquery_operator_can_be_injected():
    q = PS.or_tsquery("foo' & !bar | (baz:*) <-> qux")
    assert set(q.replace(" | ", "")) <= set("abcdefghijklmnopqrstuvwxyz0123456789")
    assert q == "foo | bar | baz | qux"


def test_sync_rows_cap_text_and_carry_the_watermark():
    big = "x" * (PS.TEXT_CAP + 10)
    rows = [(7, "page:a", "https://a", "A", "web.x", 12, big), (9, None, "https://b", "B", "web.x", 1, "t"),
            (8, "page:c", None, None, None, None, None)]
    args, top = PS.to_rows(rows)
    assert top == 9 and [a[0] for a in args] == ["page:a", "page:c"]
    assert len(args[0][5]) == PS.TEXT_CAP and args[0][6] == 7
    assert args[1] == ("page:c", "", "", "", 0, "", 8)
    assert PS.to_rows([]) == ([], 0)


def test_nul_bytes_never_reach_postgres():
    nul = chr(0)
    args, top = PS.to_rows([(3, "page:n", "https://n" + nul, "Ti" + nul + "tle", "web" + nul + ".x", 2,
                             "a" + nul + "b" + nul + "c")])
    assert top == 3 and args[0] == ("page:n", "https://n", "Title", "web.x", 2, "abc", 3)
    assert all(nul not in str(v) for v in args[0])
    assert PS.clean(("x" + nul) * 3, 2) == "xx" and PS.clean(None) == ""


def test_search_sql_ranks_and_filters():
    s = PS.search_sql(False)
    assert "ts_rank_cd(search_vector, q)" in s and "ORDER BY rank DESC, src_rowid DESC" in s and "$3" not in s
    assert "dataset_id = $3" in PS.search_sql(True)


def test_the_index_is_a_generated_weighted_tsvector_with_gin():
    ddl = "\n".join(PS.DDL)
    assert "GENERATED ALWAYS AS" in ddl and "setweight(to_tsvector('english', coalesce(title, '')), 'A')" in ddl
    assert f"USING gin(search_vector)" in ddl and PS.STATE in ddl
    assert "ON CONFLICT (id) DO UPDATE" in PS.UPSERT and "GREATEST" in PS.SET_WATERMARK


def test_page_dict_and_lag():
    d = PS.page_dict({"url": "u", "title": "t", "full_text": "x", "dataset_id": "d", "word_count": 3, "rank": 0.123456})
    assert d == {"url": "u", "title": "t", "full_text": "x", "tags": "", "dataset_id": "d", "word_count": 3,
                 "rank": 0.1235}
    assert PS.lag(100, 90) == 10 and PS.lag(5, None) == 5 and PS.lag(5, 9) == 0


def test_sync_rereads_the_watermark_row():
    # a REPLACE of the newest page keeps its rowid: the sync must re-read it, not start past it
    src = _src("vera/fabric/page_search.py")
    assert "after = max(0, wm - 1)" in src and "async with _LOCK" in src


def test_discover_recall_uses_the_index_only_behind_its_switch():
    src = _src("vera/fabric/discovery.py")
    body = src[src.index("async def discover_recall("):src.index("def _discover_recall_sync(")]
    assert "_recall_from_index(query, dataset_id, limit)" in body
    helper = src[src.index("async def _recall_from_index("):src.index("def _discover_recall_sync(")]
    assert 'if mode != "pg":' in helper and "return None" in helper and '"discovery.recall_index.search"' in helper
    sync = src[src.index("def _discover_recall_sync("):src.index("async def discover_crawl_urls(")]
    assert "if indexed is not None:" in sync and "elif query and len(pages) < limit:" in sync


def test_the_switch_and_module_are_registered():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["discovery.recall_index"]
    assert f.modes == ("off", "pg") and f.default == "off" and f.package == "FM-7.1"
    assert 'os.path.join(_here, "fabric/page_search.py")' in _src("vera/capability_orchestration.py")
