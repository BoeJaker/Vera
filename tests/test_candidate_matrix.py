"""The candidate matrix (four-minds FM-2; owner, 17 Oct: "introduce and test them in parallel"): every application x
every candidate model, scored together on one rotating nightly schedule."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import applications_core as APPS  # noqa: E402
from vera.minds import judge_core as JC  # noqa: E402
from vera.minds import matrix_core as MX  # noqa: E402

pytestmark = pytest.mark.critical


def test_the_applications_join_every_judge_and_the_loop_six_stay_apart():
    assert len(JC.LOOP_QUESTIONS) == 6 and all(q.startswith("loop.") for q in JC.LOOP_QUESTIONS)
    for q in APPS.QUESTIONS:
        assert q in JC.QUESTIONS and q in JC.CARDS
        assert set(JC.CARDS[q]) == set(JC.QUESTIONS[q]["labels"].values())       # a card per label
    assert set(APPS.PENDING).isdisjoint(APPS.QUESTIONS)


def test_an_intel_item_and_a_crawl_page_become_the_question_text():
    st = {"topic_name": "UK interest rates", "title": "BoE holds at 4%", "snippet": "The Bank of England ...",
          "source": "reuters.com", "url": "https://x"}
    m = JC.user_message("intel.signal_relevant", st)
    assert m.startswith("Topic: UK interest rates\nTitle: BoE holds at 4%") and m.endswith("Answer with one word.")
    assert JC.user_message("intel.page_genre", st).endswith("Answer with one letter.")
    assert "Topic:" not in JC.user_message("intel.page_genre", st)
    assert JC.user_message("intel.signal_relevant", {"topic_name": "x"}) is None
    pg = {"topic": "rust async", "title": "Tokio guide", "excerpt": "...", "url": "https://t"}
    assert JC.user_message("discovery.page_on_topic", pg).startswith("Topic: rust async")
    # the loop's own builder still answers the loop's questions
    assert JC.user_message("loop.tier", {"goal": "build a site"}).startswith("Goal: build a site")


def _row(name, params, quant="Q4_K_M", ab=("completion",), family="qwen3"):
    return {"id": f"llm.generate:{name}", "family": "ollama", "model": name,
            "facts": {"family": family, "params": params, "quant": quant, "abilities": list(ab)}}


def test_the_llm_candidates_come_from_the_registry():
    rows = [_row("qwen3:0.6b", "751.63M"), _row("qwen3:4b", "4.0B"), _row("mistral:7b", "7.2B", family="llama"),
            _row("mistral:latest", "7.2B", family="llama"), _row("gemma3:27b", "27.4B"),
            _row("nomic-embed-text:latest", "137M", "F16", ("embedding",)),
            _row("big-f16:9b", "8.95B", "F16"), _row("tiny-f16", "1.24B", "F16"),
            {"id": "decide.shadow.score", "family": "ollama"}]
    got = MX.llm_candidates(rows)
    assert got == ["qwen3:0.6b", "tiny-f16", "qwen3:4b", "mistral:7b"]
    assert MX.llm_candidates(rows, exclude=["qwen3:4b"]) == ["qwen3:0.6b", "tiny-f16", "mistral:7b"]
    assert MX.params_b("494.03M") == pytest.approx(0.49403) and MX.params_b("x") is None


def test_the_rotation_scores_never_scored_pairs_first_then_the_oldest():
    pairs = ["a|m1", "b|m1", "a|m2", "b|m2"]
    last = {"a|m1": "2026-10-16", "b|m1": "2026-10-10"}
    assert MX.rotate(pairs, last, 3) == ["a|m2", "b|m2", "b|m1"]
    assert MX.rotate(pairs, {}, 10) == pairs and MX.rotate(pairs, {}, 0) == []


def test_votes_follow_the_judge_convention():
    binary, choice = JC.QUESTIONS["intel.signal_relevant"], JC.QUESTIONS["intel.page_genre"]
    assert MX.vote(binary, {"relevant": 0.2, "not_relevant": 0.6}) == ("not_relevant", 0.25)
    assert MX.vote(choice, {"news": 3.0, "repo": 1.0}) == ("news", 0.75)
    assert MX.vote(binary, {}) == (None, None)
    sm = MX.softmax({"a": -10.7, "b": -11.2})
    assert sm["a"] > sm["b"] and sum(sm.values()) == pytest.approx(1.0)


def test_the_report_names_the_untested_pairs():
    shadow = {"live": {"intel.signal_relevant": {"n": 0}},
              "questions": {"intel.signal_relevant": {"llm:qwen3:0.6b": {"n": 40, "agreement": 0.7,
                                                                         "vs_outcomes": {"n": 0}}}}}
    rep = MX.report(shadow, ["llm:qwen3:0.6b", "knn:minilm", "zeroshot:deberta-base"], APPS.PENDING)
    app = rep["applications"]["intel.signal_relevant"]
    assert app["untested"] == ["knn:minilm", "zeroshot:deberta-base"] and app["candidates"][0]["agreement"] == 0.7
    assert rep["pairs_untested"] == 2 and "memory.seek" in rep["pending"]


def test_the_judge_scores_rule_made_decisions_outside_the_loop_and_runs_the_nlp_models():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert "AND ($5 OR COALESCE(l.row->>'decider', 'llm') <> 'rule')" in src
    assert "question_id not in JC.LOOP_QUESTIONS)" in src
    assert '"decide.shadow.nlp", serves=_SERVES_NLP' in src and '"decide.matrix.report"' in src
    assert "await matrix_models(), cap)" in src and "jobs, chosen = await _rotate(r, jobs)" in src


def test_material_change_is_an_application_and_entity_resolve_says_why_not():
    st = {"topic": "aiops", "prev": "Growth robust", "now": "Vendors overclaim", "changed": "[]", "comps": "0"}
    m = JC.user_message("intel.material_change", st)
    assert "Previous headline: Growth robust\nNew headline: Vendors overclaim" in m
    assert JC.user_message("intel.material_change", {"prev": "x"}) is None
    assert "Wikidata ids" in APPS.PENDING["intel.entity_resolve"] and "intel.material_change" not in APPS.PENDING


def test_a_trained_heads_output_becomes_a_label():
    b, c = JC.QUESTIONS["loop.step_done"], JC.QUESTIONS["loop.tier"]
    assert MX.head_vote(b, [0.83]) == ("end", 0.83) and MX.head_vote(b, 0.2) == ("continue", 0.2)
    assert MX.head_vote(c, [0.1, 0.6, 0.2, 0.1]) == ("simple", 0.6)
    assert MX.head_vote(c, [0.9]) == (None, None) and MX.head_vote(b, ["x"]) == (None, None)


def test_bound_heads_join_the_matrix_by_registration():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert 'startswith("ml.onnx.model.")' in src and '"decide.shadow.heads", serves=_SERVES_HEADS' in src
    assert '[f"onnx:{h[\'slug\']}" for h in heads()]' in src
