"""vera-44's data-side asks for Loop Lab S2 Tests (18 Oct 2026)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import matrix_core as MX  # noqa: E402

pytestmark = pytest.mark.critical


def test_the_matrix_report_carries_flat_cells_with_the_live_column():
    shadow = {"live": {"loop.tier": {"n": 380, "accuracy": 0.847}},
              "questions": {"loop.tier": {"knn:minilm": {"n": 405, "agreement": 0.889, "ms_p50": None,
                                                         "vs_outcomes": {"n": 299, "accuracy": 0.876}}}}}
    out = MX.report(shadow, ["knn:minilm", "card:minilm"], {})
    assert out["cells"] == [
        {"application": "loop.tier", "candidate": "live", "accuracy": 0.847, "n": 380, "agreement": None,
         "outcomes_n": 380, "ms_p50": None, "live": True},
        {"application": "loop.tier", "candidate": "knn:minilm", "accuracy": 0.876, "n": 405, "agreement": 0.889,
         "outcomes_n": 299, "ms_p50": None, "live": False}]
    assert out["applications"]["loop.tier"]["untested"] == ["card:minilm"]        # the nested form is unchanged


def test_the_chat_corpus_headline_is_objects_and_says_broken():
    src = open(os.path.join(ROOT, "vera", "minds", "tests_monitor.py"), encoding="utf-8").read()
    body = src[src.index("async def _h_chat_corpus("):src.index("async def _h_claims(")]
    assert "str(d.get(k))" not in body
    assert '"broken": bool(cases) and status.get("error", 0) == cases' in body
