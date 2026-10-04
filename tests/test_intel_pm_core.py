"""Prediction-market parsing: the YES side and its token, closed markets dropped,
bet logs and price histories as regular hourly series."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_pm_core as pm  # noqa: E402


def test_polymarket_market_takes_the_yes_side():
    m = {"id": "1", "question": "Q?", "outcomes": '["No", "Yes"]', "outcomePrices": '["0.2", "0.8"]',
         "clobTokenIds": '["n", "y"]', "volumeNum": "10"}
    c = pm.polymarket_market(m, {"slug": "ev", "title": "Event"})
    assert c["token"] == "y" and c["prob"] == 0.8 and c["url"].endswith("/event/ev") and pm.ref(c) == "pm:polymarket:y"
    assert pm.polymarket_market(dict(m, closed=True)) is None
    assert pm.polymarket_market(dict(m, clobTokenIds="[]")) is None


def test_manifold_binary_unresolved_only():
    assert pm.manifold_market({"id": "a", "question": "Q", "outcomeType": "BINARY", "probability": 0.3})["prob"] == 0.3
    assert pm.manifold_market({"id": "a", "question": "Q", "outcomeType": "MULTIPLE_CHOICE"}) is None
    assert pm.manifold_market({"id": "a", "question": "Q", "outcomeType": "BINARY", "isResolved": True}) is None


def test_series_and_resample_carry_forward():
    bets = [{"createdTime": 7_200_500, "probAfter": 0.6}, {"createdTime": 1_000, "probAfter": 0.5},
            {"createdTime": 3_000_000, "probAfter": 0.9, "isRedemption": True}]
    pts = pm.manifold_series(bets)
    assert pts == [(1.0, 0.5), (7200.5, 0.6)]
    ts, vals = pm.resample(pts, 3600.0, until=4 * 3600)
    assert ts == [0.0, 3600.0, 7200.0, 10800.0, 14400.0] and vals == [0.5, 0.5, 0.6, 0.6, 0.6]
    ts, vals = pm.resample([(0.0, 0.1), (100 * 3600.0, 0.2)], 3600.0, max_points=10)
    assert len(ts) == 10 and vals[0] == 0.1 and vals[-1] == 0.2
    assert pm.polymarket_series({"history": [{"t": 5, "p": 0.4}, {"t": 1, "p": "0.3"}, {"t": None}]}) == [(1.0, 0.3), (5.0, 0.4)]
