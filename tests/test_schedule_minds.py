"""Census schedules carry a validated switchboard set (CENSUS_MINDS contract v1, four-minds §4.1)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.evolve import schedule_core as core  # noqa: E402

pytestmark = pytest.mark.critical

OK = {"fix.worldview_source_gate": "off"}


def norm(t):
    return core._normalize_target("census", dict(t))


def test_no_minds_is_the_baseline():
    t = norm({"template": "default"})
    assert t["minds"] == {} and t["minds_label"] == "" and t["pair_of"] == ""


def test_valid_arm_is_kept_sorted_with_its_label_and_pair():
    t = norm({"template": "default", "minds": OK, "minds_label": "wv-ungated", "pair_of": "abc123"})
    assert t["minds"] == OK and t["minds_label"] == "wv-ungated" and t["pair_of"] == "abc123"


@pytest.mark.parametrize("bad", [
    {"nope.feature": "on"},                         # unknown id
    {"fix.worldview_source_gate": "maybe"},         # unknown mode
    ["not", "a", "dict"],                           # wrong shape
])
def test_bad_minds_refused_at_save(bad):
    with pytest.raises(ValueError):
        norm({"template": "default", "minds": bad, "minds_label": "x"})


@pytest.mark.parametrize("label", ["", "Has-Caps", "a" * 25, "style-x", "core-order", "default", "sp ace"])
def test_bad_labels_refused(label):
    with pytest.raises(ValueError):
        norm({"template": "default", "minds": OK, "minds_label": label})


def test_title_names_the_arm():
    t = norm({"template": "default", "minds": OK, "minds_label": "wv-ungated"})
    assert "minds wv-ungated" in core._default_title("census", t)
