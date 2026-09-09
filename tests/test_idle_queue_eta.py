"""An estimate must come from a measurement, or not exist.

The failure this guards is specific: a made-up duration is indistinguishable
from a measured one once it is rendered, and it is then used to decide whether
an eight-hour backfill fits in a ten-minute gap. So every path that cannot
ground its answer in an observation returns None, and the panel says so.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera import idle_queue_eta as E                          # noqa: E402


EMBED = {"id": "embed.sessions:a", "kind": "embed.sessions",
         "title": "backfill", "items_remaining": 120}
DREAM = {"id": "dream:b", "kind": "dream", "title": "cycle",
         "expected_tokens": 2000}
RATES = {"embed.sessions": {"per_item_s": 4.0, "samples": 9}}


# â”€â”€ nothing measured, nothing claimed â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_no_measurement_means_no_estimate():
    assert E.estimate(EMBED, {}) is None


def test_no_item_count_means_no_estimate():
    j = dict(EMBED); j.pop("items_remaining")
    assert E.estimate(j, RATES) is None


def test_an_unknown_kind_has_no_shape_and_no_estimate():
    """A new producer must not silently inherit an estimator that does not
    describe it."""
    assert E.shape_of("something.new") == ""
    assert E.estimate({"kind": "something.new", "items_remaining": 5}, RATES) is None


def test_a_per_token_job_without_an_observed_tps_gets_nothing():
    assert E.estimate(DREAM, RATES, tps=None) is None
    assert E.estimate(DREAM, RATES, tps=0) is None


def test_rubbish_never_raises():
    for junk in (None, 42, "x", {}, {"kind": None}):
        assert E.estimate(junk, RATES) is None


# â”€â”€ measured, so it may answer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_a_per_item_estimate_is_items_times_the_measured_rate():
    e = E.estimate(EMBED, RATES)
    assert e["seconds"] == 480.0                    # 120 x 4.0
    assert "120 items" in e["basis"] and "4.00s measured" in e["basis"]


def test_a_per_token_estimate_uses_the_observed_tps():
    e = E.estimate(DREAM, RATES, tps=50.0)
    assert e["seconds"] == 40.0                     # 2000 / 50
    assert "50.0 tok/s observed" in e["basis"]


def test_a_thin_sample_is_flagged_provisional():
    """One unlucky first run must not masquerade as a measurement."""
    thin = {"embed.sessions": {"per_item_s": 4.0, "samples": 1}}
    assert E.estimate(EMBED, thin)["provisional"] is True
    assert E.estimate(EMBED, RATES)["provisional"] is False


# â”€â”€ learning from completed runs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_the_first_observation_becomes_the_rate():
    r = E.observe({}, "embed.sessions", seconds=40.0, items=10)
    assert r["embed.sessions"]["per_item_s"] == 4.0
    assert r["embed.sessions"]["samples"] == 1


def test_later_observations_blend_rather_than_replace():
    r = E.observe({"embed.sessions": {"per_item_s": 4.0, "samples": 5}},
                  "embed.sessions", seconds=80.0, items=10)   # 8.0s/item
    assert 4.0 < r["embed.sessions"]["per_item_s"] < 8.0
    assert r["embed.sessions"]["samples"] == 6


def test_a_useless_observation_teaches_nothing():
    base = {"embed.sessions": {"per_item_s": 4.0, "samples": 5}}
    for bad in ((0, 10), (-5, 10), (40, 0), ("x", 10), (40, None)):
        r = E.observe(base, "embed.sessions", seconds=bad[0], items=bad[1])
        assert r["embed.sessions"] == base["embed.sessions"], bad


def test_observe_does_not_mutate_the_callers_dict():
    base = {"embed.sessions": {"per_item_s": 4.0, "samples": 5}}
    E.observe(base, "embed.sessions", seconds=100.0, items=10)
    assert base["embed.sessions"]["per_item_s"] == 4.0


# â”€â”€ the timeline â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_jobs_are_laid_end_to_end_because_the_queue_runs_one_at_a_time():
    rows = E.timeline([EMBED, dict(EMBED, id="embed.sessions:b",
                                   items_remaining=30)], RATES)
    assert [r["starts_in_s"] for r in rows] == [0, 480]
    assert [r["ends_in_s"] for r in rows] == [480, 600]


def test_the_bar_stops_where_the_knowledge_stops():
    """A job with no estimate ends the timeline - everything after it would be
    an offset from a number nobody measured."""
    rows = E.timeline([dict(EMBED, id="known"),
                       {"id": "unknown", "kind": "narrator"},
                       dict(EMBED, id="never-reached")], RATES)
    assert [r["id"] for r in rows] == ["known", "unknown"]
    assert rows[1]["seconds"] is None
    assert "no measurement yet" in rows[1]["basis"]


def test_a_total_is_only_reported_when_the_whole_queue_is_known():
    """'at least 40 minutes' and '40 minutes' lead to different decisions."""
    known = E.timeline([EMBED], RATES)
    assert E.total_seconds(known) == 480.0
    partial = E.timeline([EMBED, {"id": "u", "kind": "narrator"}], RATES)
    assert E.total_seconds(partial) is None


def test_the_timeline_can_price_each_job_with_its_own_node_rate():
    """tps_for is a callable so a dream on a fast node and one on a slow node
    are not priced identically."""
    rows = E.timeline([DREAM], RATES, tps_for=lambda j: 100.0)
    assert rows[0]["seconds"] == 20.0


def test_an_empty_queue_is_an_empty_timeline():
    assert E.timeline([], RATES) == []
    assert E.timeline(None, RATES) == []
    assert E.total_seconds([]) == 0.0
