"""claim_nli_core (compute-roles P3, 11 Oct 2026): the answer's checkable claims, paired with the
evidence windows that could support them, and NLI scores turned into a verdict per claim."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import claim_nli_core as C  # noqa: E402

pytestmark = pytest.mark.critical

ANSWER = """# WebGPU support

- **Chrome** shipped WebGPU in version 113 [E1].
- Safari 15 introduced native WebGPU support [E2].
Browsers are getting faster every day.
| --- | --- |
"""

ITEMS = [
    {"e": 1, "n": 1, "title": "Chrome 113 ships WebGPU", "published": "2023-04-06",
     "text": "Chrome 113 enables WebGPU by default on ChromeOS, macOS and Windows."},
    {"e": 2, "n": 2, "title": "WebGPU in Safari", "text": "Safari 26 all now ship WebGPU, as does Firefox 141."},
    {"e": 3, "n": 3, "title": "Unrelated", "text": "Bananas are yellow."},
]


def test_claims_are_fact_sentences_cleaned_with_cites_kept():
    cl = C.claims(ANSWER)
    assert [c["text"] for c in cl] == ["Chrome shipped WebGPU in version 113.",
                                       "Safari 15 introduced native WebGPU support."]
    assert cl[0]["cites"] == ["E1"] and cl[1]["cites"] == ["E2"]
    assert C.claims("") == [] and C.claims("No figures here at all, none.") == []
    assert len(C.claims("\n".join("Release %d shipped in 2024." % i for i in range(40)))) == C.MAX_CLAIMS


def test_pairs_prefer_cited_items_then_shared_words():
    cl = C.claims(ANSWER)
    prs = C.pairs(cl, ITEMS)
    first = [p for p in prs if p["claim"] == 0]
    assert first[0]["item"] == "E1" and "Chrome 113" in first[0]["premise"]
    assert all(p["item"] != "E3" for p in prs)           # shares no word with any claim
    assert len([p for p in prs if p["claim"] == 1]) <= C.MAX_PAIRS_PER_CLAIM
    uncited = C.claims("Chrome shipped WebGPU in version 113 by default.")
    assert C.pairs(uncited, ITEMS)[0]["item"] == "E1"


def test_windows_cover_long_text():
    text = " ".join("Sentence number %d is here." % i for i in range(200))
    ws = C._windows(text)
    assert len(ws) > 1 and all(len(w) <= C.WINDOW_CHARS for w in ws)
    assert "Sentence number 199" in ws[-1]


def test_verdicts_and_summary():
    cl = C.claims(ANSWER)
    prs = [{"claim": 0, "item": "E1", "premise": "x"}, {"claim": 1, "item": "E2", "premise": "y"}]
    scores = [{"entailment": 0.9, "neutral": 0.05, "contradiction": 0.05},
              {"entailment": 0.02, "neutral": 0.1, "contradiction": 0.88}]
    vs = C.verdicts(cl, prs, scores)
    assert [v["verdict"] for v in vs] == ["supported", "contradicted"] and vs[0]["by"] == "E1"
    vs = C.verdicts(cl, prs[:1], scores[:1])
    assert vs[1]["verdict"] == "unsupported" and vs[1]["entail"] is None
    s = C.summary(vs)
    assert s["claims"] == 2 and s["supported"] == 1 and s["flagged"] == [cl[1]["text"]]


def test_narration_is_not_a_claim():
    txt = ("Source Retrieval: Fetched official documentation about Redis 8 in 2024.\n"
           "A comprehensive Markdown document detailing WebGPU status as of 2026.\n"
           "Redis 7.4 moved to RSALv2 and SSPLv1 in March 2024.")
    assert [c["text"] for c in C.claims(txt)] == ["Redis 7.4 moved to RSALv2 and SSPLv1 in March 2024."]


def test_a_rankers_order_wins_over_word_overlap():
    cl = C.claims("Chrome shipped WebGPU in version 113 by default.")
    wins = C.windows(ITEMS)
    cand = C.candidates(cl[0], wins)
    assert cand and wins[cand[0]]["item"] == "E1"
    prs = C.pairs(cl, ITEMS, ranked={0: [1, 0]})
    assert [p["item"] for p in prs] == ["E2", "E1"]


def test_hint_block_asks_to_verify_not_to_delete():
    assert C.hint_block([]) == ""
    b = C.hint_block(["Firefox 157 shipped WebGPU.", " "] + ["c%d is 2024." % i for i in range(20)])
    assert b.startswith("CLAIMS NO EVIDENCE PASSAGE ENTAILS") and "VERIFY" in b
    assert b.count("\n- ") == C.HINT_MAX and "- Firefox 157 shipped WebGPU." in b


def test_wired_per_run_into_the_answer_check():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert 'v6_claim_nli = (body.get("claim_nli", "off") or "off").strip().lower()' in src
    assert "claim_nli=v6_claim_nli," in src
    assert "_ac = await _v6_answer_check(goal, flat_history, results, final, _unmet, sid,\n" \
           "                                     claim_nli=claim_nli)" in src
    i = src.index("async def _v6_answer_check(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert 'if claim_nli in ("shadow", "hint") and evidence:' in body
    assert '_claim_nli.hint_block(nli_sum["flagged"])' in body and 'claim_nli == "hint"' in body
    # the hint lands in the checker's grounding text, before the LLM call
    assert body.index("hint_block") < body.index("PSx.answer_check_prompt(")
    assert 'nli.get("raw") or nli.get("func")' in src and "_CLAIM_NLI_TIMEOUT_S" in src
