"""Each worldview training pins its corpus as a DatasetSnapshot (four-minds FM-8.3)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric.dataset_provider import DatasetSnapshot  # noqa: E402
from vera.worldview import worldview_corpus_core as WC  # noqa: E402

pytestmark = pytest.mark.critical


def test_the_same_corpus_gives_the_same_snapshot_whatever_the_order():
    records = {"b": {"dataset_id": "web.x"}, "a": {"dataset_id": "cve_nvd"}, "c": {}}
    r1 = WC.snapshot_records(["b", "a", "c", "a"], records)
    assert r1 == [{"id": "a", "dataset_id": "cve_nvd"}, {"id": "b", "dataset_id": "web.x"}, {"id": "c", "dataset_id": ""}]
    s1, _ = DatasetSnapshot.create(dataset_id=WC.SNAPSHOT_DATASET, created_at="2026-10-11T03:15:00+00:00",
                                   records=r1, schema=WC.SNAPSHOT_SCHEMA, provenance={"curated": True})
    s2, _ = DatasetSnapshot.create(dataset_id=WC.SNAPSHOT_DATASET, created_at="2026-10-11T03:15:00+00:00",
                                   records=WC.snapshot_records(["c", "b", "a"], records), schema=WC.SNAPSHOT_SCHEMA,
                                   provenance={"curated": True})
    assert s1.snapshot_id == s2.snapshot_id and s1.record_count == 3


def test_training_pins_its_corpus_off_the_repo():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    pin = src[src.index("async def _pin_corpus("):src.index("async def _curated_corpus_ids(")]
    assert "DatasetSnapshot.create(" in pin and 'state_dir("datasets", "worldview")' in pin and "run_in_executor" in pin
    body = src[src.index("async def cap_worldview_train("):]
    assert body.index("MODEL.corpus = _wv_corpus.mix(") < body.index("await _pin_corpus(graph")
