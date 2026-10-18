"""FM-W.2 (four-minds, 18 Oct 2026): the worldview stream worker saw 1552 events and encoded 0 with no reason given.
It now says why it skips each record, and retries the ones whose embedding was not written yet."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.worldview import stream_core as SC  # noqa: E402

pytestmark = pytest.mark.critical
E768 = [0.1] * 768


def test_each_record_is_encoded_or_skipped_for_a_named_reason():
    take, sk = SC.classify(["a", "b", "c", "d", "e"], [E768, None, [0.1] * 384, E768, []], {"d"}, 768)
    assert take == [0]
    assert sk == {"already_indexed": ["d"], "not_embedded": ["b", "e"], "dim_mismatch": ["c"]}


def test_not_yet_embedded_ids_are_retried_then_dropped():
    book = {}
    for _ in range(SC.RETRY_MAX):
        book = SC.retry_ids(book, ["x"], ["x"])
    assert book == {"x": SC.RETRY_MAX}
    assert SC.retry_ids(book, ["x"], ["x"]) == {}                       # given up after RETRY_MAX passes
    assert SC.retry_ids({"y": 1}, [], ["y"]) == {}                      # embedded now: leaves the book
    assert SC.retry_ids({"z": 1}, [], ["other"]) == {"z": 1}           # not asked this pass: kept


def test_the_retry_book_is_bounded():
    book = SC.retry_ids({}, [str(i) for i in range(SC.RETRY_CAP + 50)])
    assert len(book) == SC.RETRY_CAP


def test_the_worker_counts_and_retries():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    for key in ('"fabric_events":', '"skipped":', '"retry_pending":', '"retried_encoded":'):
        assert key in src
    body = src[src.index("async def _stream_encode_new_records("):src.index("async def _stream_dynamics_update(")]
    assert "_SC.classify(ids, embs, MODEL.record_concepts.keys(), MODEL.embed_dim)" in body
    assert body.count("_SC.retry_ids(") == 2
    worker = src[src.index("async def _wv_stream_worker("):src.index('"worldview.stream.start"')]
    assert worker.index("if _STREAM_RETRY:") < worker.index("# Check if dynamics update is due")
    assert 'fe[etype] = fe.get(etype, 0) + 1' in worker



# -- FM-W.3 (18 Oct): the scheduled catch-up for allow-listed datasets ------------------------------------------
def test_catchup_takes_only_new_ids_once_in_order():
    assert SC.unindexed(["a", "b", "a", "", "c", "d"], {"b", "d"}) == ["a", "c"]
    assert SC.chunks(["1", "2", "3"], 2) == [["1", "2"], ["3"]]


def test_the_catchup_capability():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    body = src[src.index('"worldview.catchup",'):src.index('"worldview.stream.status",')]
    assert 'CATCHUP_DATASETS_DEFAULT = ["web.worldview_seeds"]' in src
    assert "_SC.unindexed(listed, MODEL.record_concepts.keys())" in body
    assert "_stream_encode_new_records(ds, limit=len(chunk), record_ids=chunk)" in body
    assert "range(CATCHUP_MAX_PAGES)" in body and "run_in_executor(None, _list, ds)" in body
    assert "asyncio.to_thread(MODEL.save)" in body
