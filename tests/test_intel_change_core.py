"""Change detection on an indicator's series: a jump is one spike, a level that
moves and stays is one change, quiet noise raises almost nothing, and stories are
pinned to a change by when they landed."""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_change_core as chg  # noqa: E402


def _noise(n, seed, sd=1.0, base=100.0):
    r = random.Random(seed)
    return [base + r.gauss(0, sd) for _ in range(n)]


def test_robust_sigma_ignores_one_outlier():
    xs = [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 50.0]
    assert chg.mad_sigma(xs) < 0.2


def test_a_step_is_one_change():
    vals = _noise(60, 1, sd=0.3) + _noise(60, 2, sd=0.3, base=110)
    found = chg.detect(list(range(120)), vals, kind="price")
    assert len(found) == 1 and 58 <= found[0]["index"] <= 62 and found[0]["direction"] == "up"


def test_a_probability_jump_uses_absolute_moves():
    vals = [0.31 + 0.005 * math.sin(i) for i in range(50)] + [0.54] * 10
    found = chg.detect(list(range(60)), vals, kind="probability")
    assert found and found[0]["index"] == 50 and found[0]["kind"] == "spike"
    assert "23 points" in chg.describe(found[0], "Fed cut by Dec", "probability")


def test_noise_raises_few_false_alarms():
    hits = sum(len(chg.detect(list(range(300)), _noise(300, s), kind="price")) for s in range(20))
    assert hits <= 20                     # under one false alarm per 300 quiet bars


def test_corroborate_window_and_order():
    t0 = 1_000_000.0
    stories = [{"t": t0 - 3600 * 10, "title": "before", "score": 5},
               {"t": t0 + 3600 * 20, "title": "after", "score": 8},
               {"t": t0 - 3600 * 100, "title": "too early", "score": 9},
               {"title": "no time"}]
    got = chg.corroborate(t0, stories)
    assert [s["title"] for s in got] == ["after", "before"] and got[1]["lead_h"] == 10.0


def test_material_needs_a_story_or_a_huge_spike():
    a = {"kind": "spike", "magnitude": 6.0, "stories": []}
    b = {"kind": "spike", "magnitude": 6.0, "stories": [{"title": "x"}]}
    c = {"kind": "spike", "magnitude": 9.0, "stories": []}
    d = {"kind": "regime", "magnitude": 0.9, "stories": [{"title": "x"}]}
    assert chg.material([a, b, c, d]) == [b, c]
    assert "around then: x" in chg.prompt_lines([dict(b, text="BTC up 8%")])


def test_a_market_that_sits_still_still_reports_its_jump():
    vals = [0.31] * 60 + [0.54] * 5
    found = chg.detect(list(range(65)), vals, kind="probability")
    assert found and found[0]["kind"] == "spike" and found[0]["index"] == 60
