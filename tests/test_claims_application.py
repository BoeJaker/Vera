"""FM-5.2 as an application of the candidate matrix: a run's checkable claims paired with the evidence it read,
recorded for every judge to answer - no method chosen up front."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import claims_core as CL  # noqa: E402
from vera.minds import judge_core as JC  # noqa: E402
from vera.minds import ledger_core as LC  # noqa: E402

pytestmark = pytest.mark.critical

DELIVERABLE = ("### WebGPU Browser Support Summary (October 2026)\n\n"
               "As of October 17, 2026, **WebGPU** is supported in Chrome 113 and later on Windows and macOS. "
               "Safari 26 ships it on macOS Tahoe. It is a nice technology. Firefox enabled it in version 141.\n"
               "- Overall the web is better.")
EVIDENCE = {"type": "agent_loop_v5.tool_done", "tool": "web.research", "ok": True,
            "preview": json.dumps({"result": {"items": [
                {"title": "WebGPU status", "url": "https://caniuse/webgpu",
                 "text": "WebGPU shipped in Chrome 113 on Windows, macOS and ChromeOS. Firefox 141 enabled WebGPU "
                         "on Windows."},
                {"title": "Safari 26", "url": "https://webkit.org/blog",
                 "text": "Safari 26 brings WebGPU to macOS Tahoe, iOS 26 and iPadOS 26."}]}})}
DONE = {"type": "agent_loop_v6.done", "summary": DELIVERABLE}


def test_only_checkable_sentences_are_claims():
    cs = CL.claims(DELIVERABLE)
    assert any("Chrome 113" in c for c in cs) and any("Safari 26" in c for c in cs)
    assert any("Firefox enabled it in version 141" in c for c in cs)
    assert not any("nice technology" in c for c in cs) and not any("web is better" in c for c in cs)
    assert len(CL.claims(DELIVERABLE * 10)) == len(cs)               # deduplicated


def test_each_claim_is_paired_with_the_passage_that_shares_most_words():
    ps = CL.pairs([EVIDENCE, DONE])
    safari = next(p for p in ps if "Safari 26" in p["claim"])
    assert "macOS Tahoe" in safari["evidence"] and safari["url"] == "https://webkit.org/blog"
    assert safari["overlap"] >= CL.OVERLAP_SUPPORTED and CL.baseline(safari) == "supported"
    assert CL.pairs([DONE]) == []                                    # no evidence read, nothing to check against
    assert CL.deliverable_of([EVIDENCE, DONE]) == DELIVERABLE


def test_a_clipped_preview_still_yields_items():
    clipped = '{"result": {"items": [{"title": "A", "url": "u1", "text": "Chrome 113 shipped WebGPU'
    items = CL.evidence_items(clipped)
    assert items and items[0]["url"] == "u1" and "Chrome 113" in items[0]["text"]


def test_the_claim_is_an_application_every_judge_answers():
    assert CL.QUESTION in JC.QUESTIONS and CL.QUESTION in JC.CARDS
    assert set(JC.QUESTIONS[CL.QUESTION]["labels"].values()) == set(CL.LABELS)
    msg = JC.user_message(CL.QUESTION, {"claim": "Safari 26 ships WebGPU", "evidence": "Safari 26 brings WebGPU",
                                        "title": "Safari 26", "url": "https://webkit.org"})
    assert msg.startswith("Claim: Safari 26 ships WebGPU\n\nEvidence (Safari 26 | https://webkit.org): ")
    assert JC.user_message(CL.QUESTION, {"claim": "x"}) is None


def test_the_recorded_decision_is_a_valid_ledger_row():
    p = CL.pairs([EVIDENCE, DONE])[0]
    row = LC.make_decision(CL.QUESTION, CL.baseline(p), origin="background", surface="loop", qtype="binary",
                           labels=list(CL.LABELS), state={"claim": p["claim"], "url": p["url"]},
                           state_ref={"kind": "claim", "id": CL.claim_ref("s1", p["claim"])}, decider="rule",
                           implementation=CL.RULE, mode="rule", run_id="s1", session_id="s1",
                           detail={"overlap": p["overlap"]})
    assert LC.check_decision(row) == []
    assert CL.claim_ref("s1", "a") != CL.claim_ref("s2", "a")


# -- the picker, from the first night's 57 harvested claims (18 Oct): 22 were not claims about the world ----------
@pytest.mark.parametrize("s", [
    "Current Browser Support Status (as of late 2026)",                              # a heading
    "Report: The 2024 Redis Licensing Change and the Launch of Valkey",
    "SSPLv1 (Server Side Public License v1)",
    "Here is a 200-word explainer on race conditions, tailored for a junior developer:",   # the run on itself
    "Consequently, no standalone summary document exists to cite specific version numbers (e.g., \"Chrome 120+\") "
    "or detailed feature matrix tables directly from this run's artifacts.",
    "A 4-week roadmap for a Personal Finance Tracker has been defined with the following milestones:",
    "Thread-Safe Libraries : Prefer pre-tested, thread-safe APIs over custom implementations.",   # advice
    "Use Locks/Mutexes : Ensure only one thread accesses shared resources at a time.",
    "Milestone 3 (Week 3): Transaction History & Filtering.",                         # a plan step
])
def test_headings_self_reference_advice_and_plan_steps_are_not_claims(s):
    assert not CL.is_claim(s)
    assert CL.claims(s) == []


@pytest.mark.parametrize("s", [
    "Apple Safari: Early access / Beta stage.",
    "In June 2024, the company introduced a new dual-license model:",
    "Redis Inc., the company behind the open-source project, announced on March 21, 2024 , that it was changing "
    "the license for its Redis OSS version.",
    "The latest stable Python 3 release version is Python 3.15.0 , released on October 9, 2026.",
    "Qdrant : Apache License 2.0 - Permissive, allowing commercial use and modification without copyleft restrictions.",
    "Google Chrome / Microsoft Edge : Both browsers have full WebGPU support enabled by default across all current "
    "versions (Chromium 120+).",
])
def test_statements_about_the_world_stay_claims(s):
    assert CL.is_claim(s)
    assert CL.claims(s) == [s]


# -- picker round 2 (18 Oct pass 2: 15 of 104 still not claims; these 5 are the run's own steps and source lists) ----
@pytest.mark.parametrize("s", [
    "Based on the execution of prose.author in Step 2, here are the changes for EU AI Act high-risk obligations since "
    "August 2026:",
    "Data synthesized from live research including the WebGPU GitHub Wiki , Abratabia (browser feature database), and "
    "official release notes for Chrome 113+, Firefox 145+, Safari 26, and Edge 113+.",
    "web.research : Live aggregation of browser engine release notes and API status reports confirming stabilization "
    "timelines for Chrome, Firefox, Safari, and Edge as of Q4 2026.",
    "1. meow-sci/brutal-browser-poc : Confirmed Chrome v155 (Oct 06) and Firefox v158 (Oct 13) status with WebGPU "
    "enabled by default.",
    "2. gpuweb/gpuweb Wiki Implementation Status (Edited Oct 2, 2026): Verified engine-level readiness across Chromium, "
    "Gecko, and WebKit engines for stable deployment.",
])
def test_the_runs_own_steps_and_source_lists_are_not_claims(s):
    assert not CL.is_claim(s)


@pytest.mark.parametrize("s", [
    "Chrome & Edge : Stable support has been available since version 113.",
    "As of October 2026, the Bank of England's Bank Rate stands at 3.75% .",
    "Firefox : Stable builds for Windows and macOS have included WebGPU since versions 141 and 145, respectively.",
    "Valkey aims to be \"Redis-compatible\" but with an Apache 2.0 / BSD-style license that allows unrestricted use of "
    "new features.",
])
def test_real_claims_from_pass_2_are_still_claims(s):
    assert CL.is_claim(s)
