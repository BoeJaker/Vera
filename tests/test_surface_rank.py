"""The canvas curator's first questions in shadow (four-minds FM-6.2/6.3): domains and surfaces from the turn's Frame
vector against cards, recorded beside surface.find's word lists; nothing on the canvas changes."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.canvas import surface_core as SC  # noqa: E402
from vera.canvas import surface_rank_core as R  # noqa: E402

pytestmark = pytest.mark.critical


def test_one_card_per_domain_and_per_surface():
    cards = R.domain_cards(SC.DOMAINS)
    assert set(cards) == {d["id"] for d in SC.DOMAINS}
    assert cards["markets"].startswith("the markets") and "bitcoin" in cards["markets"]
    assert R.surface_card({"cap": "markets.quotes", "about": "Live quotes"}) == "Live quotes (markets quotes)"
    assert R.surface_card({"panel": "business_panel", "about": ""}) == "(business panel)"
    assert R.cards_hash({"a": "x"}) == R.cards_hash({"a": "x"}) != R.cards_hash({"a": "y"})


def test_domains_need_tau_and_a_second_one_must_be_close():
    cards = {"markets": [1.0, 0.0], "estate": [0.97, 0.243], "calendar": [0.0, 1.0]}
    ranked = R.rank([1.0, 0.0], cards)
    assert [k for k, _ in ranked] == ["markets", "estate", "calendar"]
    assert R.pick_domains(ranked) == ["markets", "estate"]                 # 0.97 is within the gap of 1.0
    assert R.pick_domains(ranked, gap=0.01) == ["markets"]
    assert R.pick_domains([("markets", 0.2)]) == [] and R.pick_domains([]) == []
    assert R.rank(None, cards) == [] and R.rank([1.0, 0.0], {"bad": [1.0]}) == []


def test_surfaces_come_from_the_chosen_domains_only():
    ranked = [("s1", 0.9), ("s2", 0.8), ("s3", 0.7), ("s4", 0.6)]
    dom = {"s1": "estate", "s2": "markets", "s3": "markets", "s4": "markets"}
    assert R.pick_surfaces(ranked, dom, ["markets"], 2) == ["s2", "s3"]
    assert R.pick_surfaces(ranked, dom, [], 3) == []


def test_the_summary_counts_agreement_and_one_sided_finds():
    rows = [R.row("h1", ["markets"], ["markets"], [("markets", 0.5)], ["a", "b"], ["a", "c"]),
            R.row("h2", ["estate"], ["markets", "estate"], [("markets", 0.4)], ["x"], ["x"]),
            R.row("h3", [], ["trading_account"], [("trading_account", 0.45)], [], ["p"]),
            R.row("h4", ["calendar"], [], [], ["c"], []),
            R.row("h5", [], [], [], [], [])]
    s = R.summarize(rows)
    assert s["turns"] == 5 and s["both_found"] == 2 and s["cards_only"] == 1 and s["words_only"] == 1
    assert s["neither"] == 1 and s["top_domain_agree"] == 0.5
    assert s["surface_overlap"] == 0.75 and s["card_top_score_median"] == 0.45
    assert R.summarize([]) == {"turns": 0, "both_found": 0, "words_only": 0, "cards_only": 0, "neither": 0}
    assert "text" not in rows[0]                                            # the hash ties it to the turn, no text


def test_turn_prepare_starts_the_shadow_before_anything_returns_and_never_waits():
    src = open(os.path.join(ROOT, "vera", "canvas", "enrich_capabilities.py"), encoding="utf-8").read()
    prep = src[src.index("async def cap_turn_prepare("):src.index("    async def place(")]
    assert prep.index("_surface_rank_shadow(text, found,") < prep.index("if not surfaces:")
    assert "await _surface_rank_shadow" not in src
    rec = src[src.index("async def _surface_rank_record("):src.index("def _surface_rank_shadow(")]
    assert rec.index('!= "shadow"') < rec.index('"sense.frame"')            # off = not even a frame read
    assert "vector=True" in rec and "ltrim(src.SHADOW_KEY" in rec
    assert '"canvas.surface_rank.report"' in src


def test_the_switch_is_registered_off():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["canvas.surface_rank"]
    assert f.modes == ("off", "shadow") and f.default == "off" and f.package == "FM-6.2/6.3"
