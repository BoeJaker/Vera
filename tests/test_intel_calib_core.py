"""The per-topic learned relevance curve: labels from outcomes, an honest fit,
held-out calibration, and no use until it is calibrated."""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_calib_core as c  # noqa: E402


def _gen(n, seed=7, a=0.9, b=-4.5):
    r = random.Random(seed)
    out = []
    for _ in range(n):
        x = r.uniform(0, 10)
        out.append((x, 1 if r.random() < 1 / (1 + math.exp(-(a * x + b))) else 0))
    return out


def test_labels_from_outcomes():
    assert c.label_of(["link.opened"]) == 1 and c.label_of(["user.feedback.useful"]) == 1
    assert c.label_of(["link.opened", "user.feedback.not_useful"]) == 0       # an explicit rating wins
    assert c.label_of(["user.feedback.mute_site"]) == 0 and c.label_of([]) is None


def test_not_enough_ratings_is_not_a_curve():
    res = c.calibrate(_gen(20))
    assert not res["usable"] and "needs 30" in res["why"]


def test_a_real_curve_is_found_and_calibrated():
    res = c.calibrate(_gen(400))
    assert res["usable"] and res["ece"] <= 0.05 and 4.5 <= res["score_at_half"] <= 6.0
    assert c.p_useful(res, 9) > 0.8 and c.p_useful(res, 1) < 0.2 and c.p_useful(None, 5) is None


def test_noise_is_not_trusted():
    r = random.Random(3)
    res = c.calibrate([(r.uniform(0, 10), r.randint(0, 1)) for _ in range(300)])
    assert not res["usable"]
