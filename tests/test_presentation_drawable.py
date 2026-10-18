"""DA-10b: what reaches the desk only gets better. Offline, from the source (no registry, no network):

- every capability that declares how its answer is drawn names a form the widget element draws (a form missing from
  the element would put its answers on the desk as raw data), and a kind of the contract's vocabulary;
- the number of capabilities that declare it never goes down - the floor in tests/fixtures/presentation_floor.json is
  raised when more are declared (DA-01e), never lowered.

The live half - each declaration read against a REAL answer and drawn in a real page - is tools/presentation_probe.py
(DA-10a), run on a Vera, read-only.
"""
import json
import os
import re

import pytest

from presentation_declarations import declared_in_source   # tests/ - as test_capability_presentation reads it
from vera.capability_contract_core import PRESENTATION_KINDS

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FLOOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "presentation_floor.json")


def _drawn_forms() -> set:
    src = open(os.path.join(ROOT, "vera", "widgets", "widget_element.js"), encoding="utf-8").read()
    dotted = set(re.findall(r"\bR\.([A-Za-z_]\w*)\s*=", src))
    quoted = set(re.findall(r"\bR\['([\w@-]+)'\]\s*=", src))
    return dotted | quoted


def test_every_declared_form_is_one_the_widget_draws():
    forms = _drawn_forms()
    assert len(forms) > 100, "the widget element's forms were not found - the pattern no longer reads it"
    missing = {n: d.get("form") for n, d in declared_in_source().items() if isinstance(d, dict) and d.get("form") and d["form"] not in forms}
    assert not missing, "declared forms the widget does not draw: %s" % missing


def test_every_declared_kind_is_in_the_vocabulary():
    bad = {n: d.get("kind") for n, d in declared_in_source().items() if not isinstance(d, dict) or d.get("kind") not in PRESENTATION_KINDS}
    assert not bad, "kinds outside the contract's vocabulary: %s" % bad


def test_declarations_never_go_down():
    floor = json.load(open(FLOOR, encoding="utf-8"))["declared"]
    now = len(declared_in_source())
    assert now >= floor, "%d capabilities declare how they are drawn - fewer than the floor %d" % (now, floor)
