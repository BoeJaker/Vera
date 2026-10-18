"""History series for the Loop Lab Tests timeline (vera-44's ask, 18 Oct 2026)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import series_core as S  # noqa: E402

pytestmark = pytest.mark.critical

REPORT = {"pairs_scored": 77, "pairs_untested": 152, "candidates": ["a", "b", "c"],
          "rotation": {"llm_pairs_total": 168, "queued_ever": 48, "budget_per_night": 48},
          "applications": {
              "loop.tier": {"live": {"n": 380, "accuracy": 0.847},
                            "candidates": [{"candidate": "knn:minilm", "outcomes_n": 299, "accuracy": 0.876, "n": 405}]},
              "output.claim_supported": {"live": {"n": 120, "accuracy": 0.75},
                                         "candidates": [{"candidate": "nli-direct:deberta-large", "outcomes_n": 97,
                                                         "accuracy": 0.742, "n": 114},
                                                        {"candidate": "llm:qwen3:0.6b", "outcomes_n": 20,
                                                         "accuracy": 0.9, "n": 40}]}}}


def test_matrix_row_counts_pairs_that_beat_live():
    r = S.matrix_row(REPORT)
    assert (r["pairs_scored"], r["pairs_untested"], r["applications"], r["candidates"]) == (77, 152, 2, 3)
    assert r["pairs_beating_live"] == 1 and r["budget_per_night"] == 48
    assert S.matrix_row({"error": "x"}) is None


def test_claims_row_ranks_only_judges_over_the_floor():
    r = S.claims_row(REPORT)
    assert r["live_accuracy"] == 0.75 and r["best_judge"] == "nli-direct:deberta-large"   # 0.6b has 20 outcomes
    assert r["beats_live"] is False and r["judges"]["llm:qwen3:0.6b"]["outcomes_n"] == 20
    assert S.claims_row({"applications": {}}) is None


def test_worldview_row_keeps_the_spread():
    h = {"status": "degraded", "metrics": {"median_population": 1.0, "share_below_min": 0.833, "concepts_below_min": 80,
                                           "largest_share": 0.19, "records_per_concept": 23.22, "num_concepts": 96}}
    r = S.worldview_row(h)
    assert r["median_population"] == 1.0 and r["share_below_min"] == 0.833 and r["status"] == "degraded"
    assert S.worldview_row({}) is None


def test_the_caps_exist_with_the_timeline_shape():
    src = open(os.path.join(ROOT, "vera", "minds", "tests_monitor.py"), encoding="utf-8").read()
    for cap in ('"decide.series.snapshot"', '"decide.matrix.history"', '"decide.claims.history"',
                '"worldview.spread.history"'):
        assert cap in src
    assert "ORDER BY ts DESC" in src and 'dict(d, ts=r["ts"].isoformat())' in src
