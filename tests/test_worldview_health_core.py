"""worldview.health verdicts (doc 49 §4.1) and the research source gate (RF-07)."""
import ast
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.worldview.worldview_health_core import (  # noqa: E402
    DEGRADED, OK, STALE, UNTRAINED, usable, verdict)

pytestmark = pytest.mark.critical

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _model(**kw):
    m = {"ready": True, "num_concepts": 64, "records_assigned": 6400, "transitions_observed": 3000,
         "train_steps": {"gnn": 500, "codebook": 400, "dynamics": 20000},
         "codebook": {"perplexity": 40.0}, "last_fabric_persist": (NOW - timedelta(days=1)).isoformat()}
    m.update(kw)
    return m


def test_prod_shape_of_22_sep_is_degraded_with_reasons():
    m = _model(num_concepts=512, records_assigned=2299, transitions_observed=716,
               train_steps={"gnn": 180, "codebook": 406, "dynamics": 28689},
               codebook={"perplexity": 87.5})
    v = verdict(m, {"vectors": 2299}, now=NOW)
    assert v["status"] == DEGRADED
    text = " ".join(v["reasons"])
    assert "records per concept" in text and "memorising" in text and "held-out" in text
    assert not usable(v)


def test_untrained():
    assert verdict({"ready": False}, now=NOW)["status"] == UNTRAINED
    assert verdict(_model(records_assigned=0), now=NOW)["status"] == UNTRAINED
    assert verdict(_model(train_steps={"gnn": 0, "codebook": 0, "dynamics": 0}), now=NOW)["status"] == UNTRAINED


def test_stale_checkpoint():
    v = verdict(_model(last_fabric_persist=(NOW - timedelta(days=30)).isoformat()), now=NOW)
    assert v["status"] == STALE


def test_never_ok_without_a_held_out_evaluation():
    v = verdict(_model(), {"vectors": 6400}, now=NOW)
    assert v["status"] == DEGRADED and v["reasons"] == ["no held-out next-concept evaluation against a baseline"]


def test_ok_only_when_right_sized_and_beating_the_baseline():
    v = verdict(_model(), {"vectors": 6400}, held_out={"beats_baseline": True}, now=NOW)
    assert v["status"] == OK and usable(v)
    v = verdict(_model(), {"vectors": 6400}, held_out={"beats_baseline": False}, now=NOW)
    assert v["status"] == DEGRADED


def test_metrics_are_reported():
    m = verdict(_model(), {"vectors": 3200}, now=NOW)["metrics"]
    assert m["records_per_concept"] == 100.0 and m["index_share"] == 0.5


def test_research_source_checks_health_first():
    src = open(os.path.join(ROOT, "vera", "research", "researcher_api.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "gather_worldview")
    dumped = ast.dump(fn)
    assert "worldview.health" in dumped
    assert "VERA_RESEARCH_WORLDVIEW_UNGATED" in dumped



# -- FM-W.1 (18 Oct): the distribution, not only the mean -----------------------------------------------------
def test_the_18_oct_shape_is_degraded_although_the_mean_passes():
    from vera.worldview.worldview_health_core import spread
    pops = [779, 300, 200, 150, 120, 100, 90, 80, 70, 60, 50, 40, 35, 30, 25, 22] + [1] * 78   # 2229 records
    m = _model(num_concepts=96, records_assigned=sum(pops), transitions_observed=3000,
               train_steps={"gnn": 20, "codebook": 32, "dynamics": 1065}, codebook={"perplexity": 40.0})
    good = {"beats_baseline": True}
    assert verdict(m, held_out=good, now=NOW)["status"] == OK                      # the mean alone: ok
    v = verdict(m, held_out=good, now=NOW, populations=pops)
    assert v["status"] == DEGRADED
    assert any("median concept holds" in r for r in v["reasons"])
    assert v["metrics"]["concepts_below_min"] == 80 and v["metrics"]["median_population"] == 1  # 2 unused count as 0
    assert spread([5, 30, 30]) == {"median_population": 30, "concepts_below_min": 1, "share_below_min": 0.333,
                                   "largest_population": 30, "largest_share": 0.462}


def test_one_concept_swallowing_the_records_is_degraded():
    pops = [30] * 9 + [400]
    m = _model(num_concepts=10, records_assigned=sum(pops), transitions_observed=3000,
               train_steps={"gnn": 20, "codebook": 32, "dynamics": 1065}, codebook={"perplexity": 5.0})
    v = verdict(m, held_out={"beats_baseline": True}, now=NOW, populations=pops)
    assert any("one concept holds" in r for r in v["reasons"])


def test_a_well_spread_model_stays_ok():
    pops = [40] * 64
    m = _model(num_concepts=64, records_assigned=sum(pops))
    assert verdict(m, held_out={"beats_baseline": True}, now=NOW, populations=pops)["status"] == OK


def test_health_passes_the_populations():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    body = src[src.index("async def cap_worldview_health("):]
    assert "populations=pops" in body[:900] and "Counter(MODEL.record_concepts.values())" in body[:900]
