"""memory.seek's result cache (four-minds FM-7.1): keyed by every shaping argument and the fabric generation, behind
the switch memory.seek_cache (off by default); a hit still counts for the retrieval eval set."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import seek_cache_core as SC  # noqa: E402

pytestmark = pytest.mark.critical


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_the_key_normalises_the_query_and_keeps_every_argument():
    base = SC.key("Redis  licence", "", "", 8, 4000, True, 7)
    assert base.startswith(SC.PREFIX) and base == SC.key("redis licence", "", "", 8, 4000, True, "7")
    for other in (SC.key("redis licence", "cve", "", 8, 4000, True, 7),
                  SC.key("redis licence", "", "7d", 8, 4000, True, 7),
                  SC.key("redis licence", "", "", 9, 4000, True, 7),
                  SC.key("redis licence", "", "", 8, 2000, True, 7),
                  SC.key("redis licence", "", "", 8, 4000, False, 7)):
        assert other != base


def test_an_ingest_changes_every_key():
    assert SC.key("q", "", "", 8, 4000, True, 7) != SC.key("q", "", "", 8, 4000, True, 8)
    assert SC.key("q", "", "", 8, 4000, True, None) == SC.key("q", "", "", 8, 4000, True, 0)
    assert SC.TTL_S == 600


def test_seek_reads_the_cache_first_and_stores_both_answers():
    src = _src("vera/fabric/memory_retrieval.py")
    body = src[src.index("async def cap_memory_seek("):src.index("async def cap_memory_read(")]
    assert body.index("await _seek_cache_key(") < body.index('_rtc.Envelope("memory.seek")')
    assert "return dict(hit, cached=True)" in body and "_reval.on_seek(query" in body
    assert body.count("await _seek_cache_put(ck, ") == 2
    helper = src[src.index("async def _seek_cache_key("):src.index("async def _seek_cache_get(")]
    assert '!= "on"' in helper and "return None" in helper and "_scc.GEN_KEY" in helper


def test_every_ingest_bumps_the_generation():
    src = _src("vera/fabric/data_fabric.py")
    i = src.index('await emit_event({"type": "fabric.ingested"')
    tail = src[i:i + 900]
    assert "if ingested:" in tail and 'await _r.incr("vera:fabric:generation")' in tail
    assert SC.GEN_KEY == "vera:fabric:generation"


def test_the_switch_is_registered_off():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["memory.seek_cache"]
    assert f.modes == ("off", "on") and f.default == "off" and f.package == "FM-7.1"


def test_seek_returns_the_evidence_per_pick():
    src = _src("vera/fabric/memory_retrieval.py")
    body = src[src.index("async def cap_memory_seek("):src.index("async def cap_memory_read(")]
    for field in ('"vector_score": round(float((vec_sims or {}).get(c["id"], 0.0)), 3)',
                  '"keyword": c["id"] in (text_ids or set())', '"origin": c.get("origin") or "fabric"'):
        assert field in body
