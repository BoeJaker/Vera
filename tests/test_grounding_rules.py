"""The grounding rules module (info-seeking plan SK-1.3): one statement per surface, and the deterministic check of an
answer against its evidence. Pure."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.agents import grounding_rules as G                  # noqa: E402

ITEMS = [{"n": 1, "title": "Base rate held at 3.75%", "published": "2026-09-17",
          "text": "The Bank of England held Bank Rate at 3.75% on 17 September 2026; 7 of 9 MPC members voted to hold."},
         {"n": 2, "title": "UK house prices", "published": "2026-10-01",
          "text": "Nationwide said annual house price growth was 2.4% in September, average price £271,000."}]


def test_text_per_surface():
    for s in G.SURFACES:
        t = G.text(s)
        assert t.startswith("GROUNDING") and "info.seek" in t and "Never fill a gap" in t and "[n]" in t
    assert "research.run" in G.text("chat") and "research.run" not in G.text("loop")
    assert "start_report=false" in G.text("loop")
    assert len(G.rules("aide")) == 5 and G.text("nonsense") == G.text("chat")


def test_a_grounded_answer_passes():
    a = "The Bank of England held Bank Rate at 3.75% on 17 September 2026 [1]. House prices rose 2.4% [2], to £271,000."
    r = G.check(a, ITEMS)
    assert r == {"ok": True, "bad_citations": [], "uncited_numbers": [], "unsupported": []}


def test_a_citation_to_nothing_and_an_invented_figure_are_caught():
    a = "Rates are 4.25% [3]. Growth was 2.4% [2]."
    r = G.check(a, ITEMS)
    assert not r["ok"] and r["bad_citations"] == [3] and r["uncited_numbers"] == ["4.25%"]
    assert r["unsupported"] == ["Rates are 4.25% [3]."]


def test_formatting_is_ignored_and_list_markers_and_small_numbers_are_not_figures():
    a = "1. Average price is 271000 pounds [2]\n2. Seven of 9 voted to hold [1]."
    r = G.check(a, ITEMS)
    assert r["ok"], r
    assert G.check("It was £271k in 2031.", ITEMS)["uncited_numbers"] == ["2031"]      # £271k is the £271,000
    assert G.check("It was £1.2bn.", ITEMS)["uncited_numbers"] == ["£1.2bn"]


def test_a_year_the_sources_never_mention_is_unsupported():
    r = G.check("The rule takes effect in 2027 [1].", ITEMS)
    assert r["uncited_numbers"] == ["2027"] and not r["ok"]


# loops number a run's evidence across packs (E1..En; compute-roles fix/loop-evidence-numbering, 10 Oct)
RUN = [dict(ITEMS[0], e="E1", n=1), dict(ITEMS[1], e="E2", n=1)]       # two packs: both items are their pack's [1]


def test_run_wide_ids():
    ok = G.check("Bank Rate is 3.75% [E1]; prices rose 2.4% [E2].", RUN, id_key="e")
    assert ok["ok"] and ok["bad_citations"] == []
    bad = G.check("Bank Rate is 3.75% [E1] [E7].", RUN, id_key="e")
    assert bad["bad_citations"] == ["E7"] and not bad["ok"]
    assert G.check("x [E1, E2] and [E1; E9]", RUN, id_key="e")["bad_citations"] == ["E9"]


def test_a_bare_number_in_the_id_field_answers_to_its_e_form():
    run = [dict(ITEMS[0], e=1), dict(ITEMS[1], e=2)]
    assert G.check("3.75% [E1] and 2.4% [2]", run, id_key="e")["bad_citations"] == []


def test_lists_of_pack_numbers_and_the_default_key():
    assert G.check("3.75% [1, 2]", ITEMS)["bad_citations"] == []
    assert G.check("3.75% [1, 4]", ITEMS)["bad_citations"] == [4]
    assert G.check("3.75% [E1]", ITEMS)["bad_citations"] == ["E1"]       # no E ids in a single pack
    assert G.check("3.75% [E1, E2] 2.4%", RUN, id_key="e")["uncited_numbers"] == []   # ids are not figures
