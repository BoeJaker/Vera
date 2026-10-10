"""Relevance is judged against the topic itself, and separately against its open
questions - never one query where narrow model-written questions drown the topic
(9 Oct 2026: every signal of the night fell below the model gate)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_core as core, intel_ops_core as ops  # noqa: E402

TOPIC = {"id": "t", "name": "UK residential property market and house prices", "brief": "sector",
         "entities": ["Nationwide"],
         "questions": [{"question": "How will hydrology assessment tools affect flood-risk zones?", "answer": "unknown"},
                       "{'question': 'What volume of social housing is sold in the Lake District?', 'answer': 'n/a'}",
                       "Plain open question?"]}


def test_the_topic_profile_is_the_topic_and_questions_stand_apart():
    prof = ops.profile_text(TOPIC)
    assert prof.startswith("UK residential property market and house prices") and "Nationwide" in prof
    assert "hydrology" not in prof and "Lake District" not in prof and "question" not in prof
    qp = ops.question_profile(TOPIC)
    assert "hydrology" in qp and "Lake District" in qp and "Plain open question?" in qp and "answer" not in qp
    assert ops.question_profile({"name": "x"}) == ""


def test_question_objects_never_become_searches_or_stored_dicts():
    qs = core.topic_queries(TOPIC)
    assert not any(q.startswith("{") or "'question'" in q for q in qs)
    assert "How will hydrology assessment tools affect flood-risk zones?" in qs
    t = core.normalise_topic({"name": "X", "questions": TOPIC["questions"]})
    assert t["questions"] == ["How will hydrology assessment tools affect flood-risk zones?",
                              "What volume of social housing is sold in the Lake District?", "Plain open question?"]


def test_triage_takes_the_better_of_topic_and_question_relevance(tmp_path):
    from test_intel_capabilities import _load
    mod, orch = _load(tmp_path)
    queries = []

    async def rerank(query="", documents=None, top_k=5, trace_id=None):
        queries.append(query)
        # against the topic, only the pilot story is relevant; against its questions, the repo answers one
        if "Who builds the valuer" in query:
            return {"ranked": [{"index": i, "score": 4.0 if "repo" in d.lower() else -6.0} for i, d in enumerate(documents)]}
        return {"ranked": [{"index": i, "score": 4.0 if "pilot" in d.lower() else -6.0} for i, d in enumerate(documents)]}
    orch.CAPABILITY_REGISTRY["nlp.rerank"] = {"func": rerank, "raw": rerank}
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    t = R("intel.topic.save", name="AI property valuation", brief="AI valuation in UK property",
          questions=["Who builds the valuer?"])["topic"]
    R("intel.sweep", topic_id=t["id"])
    assert len(queries) == 2 and "Who builds the valuer" not in queries[0] and queries[1] == "Who builds the valuer?"
    sigs = {s["title"]: s for s in mod.store().signals(t["id"])}
    pilot = next(v for k, v in sigs.items() if "pilot" in k.lower())
    repo = next(v for k, v in sigs.items() if "repo" in k.lower())
    assert pilot["relevance_nlp"] > 0.9 and repo["relevance_nlp"] > 0.9     # each relevant one way or the other


def test_searches_are_anchored_on_the_topic_not_its_questions():
    from datetime import datetime, timezone
    t = {"name": "UK commercial real estate market",
         "questions": ["What drives the SEGRO drop?", "Hines co-CEO structure?", "Uneven AI data in logistics?",
                       "A fourth narrow question?"]}
    qs = core.topic_queries(t, now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert qs[:3] == ["UK commercial real estate market latest news", "UK commercial real estate market news October 2026",
                      "UK commercial real estate market analysis"]
    assert sum(1 for q in qs if q in t["questions"]) == 2                    # at most two of its open questions
    first, _ = core.rotate_queries(dict(t, query_cursor=0), 4)
    assert sum(1 for q in first if q in t["questions"]) <= 1                 # a night's four searches are mostly the topic
