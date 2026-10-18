"""FM-5.2 second pass (owner, 18 Oct 2026: "the labelling could do with a second pass with full / better test
data"): a claim is checked against the best windows of everything the run read, the same runs are harvested again
into fresh items, and a direct-entailment NLI judge (evidence entails claim) joins the matrix."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import applications_core as AP  # noqa: E402
from vera.minds import claims_core as CL  # noqa: E402
from vera.minds import matrix_core as MX  # noqa: E402

pytestmark = pytest.mark.critical

FILLER = " ".join(f"word{i}" for i in range(400))          # a long page: the settling sentence is far down it
PAGE = (FILLER + " Apple shipped WebGPU in Safari 26 on macOS Tahoe, iOS 26 and iPadOS 26 in September 2025. "
        + FILLER)
OTHER = "Release notes: Safari 26 brings WebGPU to macOS Tahoe and iOS 26. Firefox 141 enabled WebGPU on Windows."
EVENTS = [
    {"type": "agent_loop_v5.tool_done", "tool": "web.research", "ok": True,
     "preview": json.dumps({"result": {"items": [{"title": "WebGPU status 2026", "url": "https://a.example/webgpu",
                                                  "text": PAGE},
                                                 {"title": "Safari 26 notes", "url": "https://b.example/safari",
                                                  "text": OTHER}]}})},
    {"type": "agent_loop_v6.done",
     "summary": "WebGPU report. Safari 26 shipped WebGPU on macOS Tahoe and iOS 26 in September 2025."},
]


def test_the_best_windows_of_everything_read_not_one_window():
    ps = CL.passages(EVENTS)
    claim = "Safari 26 shipped WebGPU on macOS Tahoe and iOS 26 in September 2025."
    top = CL.top_passages(claim, ps)
    assert 2 <= len(top) <= CL.EVIDENCE_K
    assert {p["url"] for p, _ in top} == {"https://a.example/webgpu", "https://b.example/safari"}
    same = [p for p, _ in top if p["url"] == "https://a.example/webgpu"]
    ats = sorted(p["at"] for p in same)
    assert all(b - a >= CL.WINDOW for a, b in zip(ats, ats[1:]))      # never two overlapping windows of one text
    assert [s for _, s in top] == sorted((s for _, s in top), reverse=True)


def test_a_pair_carries_the_joined_evidence_and_its_version():
    (pair,) = [p for p in CL.pairs(EVENTS) if "September 2025" in p["claim"]]
    ev = pair["evidence"]
    assert "[WebGPU status 2026]" in ev and "[Safari 26 notes]" in ev
    assert "Apple shipped WebGPU in Safari 26" in ev
    assert len(ev) <= CL.EVIDENCE_CHARS and pair["sources"] == 2 and pair["version"] == CL.EVIDENCE_VERSION
    assert CL.baseline(pair) in CL.LABELS                         # the rule still reads the best window's overlap


def test_new_items_do_not_collide_with_the_first_harvest():
    assert CL.claim_ref("s1", "a claim") != __import__("hashlib").sha1(b"s1|a claim").hexdigest()[:16]
    src = open(os.path.join(ROOT, "vera", "minds", "claims.py"), encoding="utf-8").read()
    assert 'DONE_KEY = "vera:claims:harvested:" + CL.EVIDENCE_VERSION' in src
    assert '"evidence": p.get("version")' in src


def test_the_judges_see_the_longer_evidence():
    ev = "x " * 900
    msg = AP.user_message("output.claim_supported", {"claim": "Safari 26 ships WebGPU.", "evidence": ev})
    assert len(msg) > 1500 and AP.EVIDENCE_CHARS == CL.EVIDENCE_CHARS


def test_the_direct_pair_is_evidence_then_claim():
    st = {"claim": "Safari 26 ships WebGPU.", "evidence": "Safari 26 brings WebGPU to macOS Tahoe."}
    assert AP.direct_pair("output.claim_supported", st) == {"premise": st["evidence"], "hypothesis": st["claim"]}
    assert AP.direct_pair("output.claim_supported", {"claim": "x"}) is None
    assert AP.direct_pair("loop.tier", st) is None
    d = AP.DIRECT["output.claim_supported"]
    assert (d["yes"], d["no"]) == ("supported", "unsupported")


def test_nli_direct_is_a_matrix_candidate_bound_to_the_direct_questions():
    assert MX.NLP_FAMILY["nli-direct:deberta-large"] == "nli_direct" and "nli-direct:deberta-large" in MX.NLP_IMPLS
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert '"nli_direct": "nli-direct:deberta-large"' in src
    assert 'questions=list(APPS.DIRECT),\n    selector={"family": "nli_direct"}' in src
    assert 'for q in (APPS.DIRECT if direct else JC.CARDS)' in src
    body = src[src.index("async def _direct_probs("):]
    assert '"premise": pair["premise"], "hypothesis": pair["hypothesis"]' in body[:700]
    assert 'return {d["yes"]: e, d["no"]: 1.0 - e}' in body[:700]
