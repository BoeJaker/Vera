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
