"""decide.export: the gold-labelled training set of the decision questions (four-minds FM-2.2)."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import export_core as EX  # noqa: E402

pytestmark = pytest.mark.critical

VERIFY = {"decision_id": "d1", "question_id": "loop.step_verify", "qtype": "binary", "labels": ["met", "not_met"],
          "answer": "not_met", "origin": "census", "run_id": "r1", "ts": "2026-10-09T13:00:00+00:00"}
TIER = dict(VERIFY, decision_id="d2", question_id="loop.tier", qtype="choice",
            labels=["single", "simple", "complex", "strategic"], answer="simple")


def _o(label, strength="medium", source="run.x", ts="2026-10-09T14:00:00", **detail):
    return {"label": label, "strength": strength, "source": source, "ts": ts, "detail": detail}


def test_gold_follows_the_outcome_verdict():
    assert EX.gold(VERIFY, [_o("correct", "strong", "run.retry_passed")]) == {
        "label": "not_met", "source": "run.retry_passed", "strength": "strong"}
    assert EX.gold(VERIFY, [_o("wrong")])["label"] == "met"          # two labels: the other one
    assert EX.gold(TIER, [_o("wrong")]) is None                      # more labels: unknown
    assert EX.gold(TIER, [_o("correct")])["label"] == "simple"
    assert EX.gold(VERIFY, [_o("interrupted", "weak")], min_strength="weak") is None   # not a verdict


def test_a_named_right_answer_wins_and_the_strongest_outcome_decides():
    human = _o("wrong", "strong", "human.label", right_answer="complex")
    assert EX.gold(TIER, [_o("correct"), human]) == {"label": "complex", "source": "human.label", "strength": "strong"}
    assert EX.gold(TIER, [_o("wrong", "strong", right_answer="nonsense")]) is None   # not one of the labels
    # the newer of two equally strong verdicts wins
    assert EX.gold(VERIFY, [_o("correct", ts="2026-10-09T14:00"), _o("wrong", ts="2026-10-09T15:00")])["label"] == "met"


def test_weak_outcomes_count_only_when_asked():
    weak = [_o("correct", "weak", "run.cap_families")]
    assert EX.gold(TIER, weak) is None
    assert EX.gold(TIER, weak, min_strength="weak")["label"] == "simple"


def test_whole_goals_are_held_out_together():
    a = EX.group_key("Build  a TODO app ", "r1")
    assert a == EX.group_key("build a todo app", "r2")               # same template, different runs
    assert a.startswith("goal:") and EX.group_key("", "r9") == "run:r9" and EX.group_key("", "", "d") == "decision:d"
    groups = [EX.group_key(f"goal {i}", "") for i in range(400)]
    test_share = sum(EX.split(g) == "test" for g in groups) / len(groups)
    assert 0.12 < test_share < 0.28
    assert all(EX.split(g) == EX.split(g) for g in groups[:20])       # stable
    assert EX.split(groups[0], holdout=0) == "train" and EX.split(groups[0], holdout=1) == "test"


def test_an_example_carries_the_question_and_the_redacted_state():
    e = EX.example(VERIFY, {"criterion": "file exists"}, [_o("correct", "strong")], goal="Make a file")
    assert e["question"]["options"] == ["met", "not_met"] and "success criterion" in e["question"]["instructions"]
    assert e["gold"] == "not_met" and e["weight"] == 1.0 and e["live_answer"] == "not_met"
    assert e["group"] == EX.group_key("make a file", "r1") and e["split"] in ("train", "test")
    bare = EX.example(TIER, {}, [])
    assert bare["gold"] is None and bare["weight"] == 0.0


def test_run_goals_origin_filter_counts_and_stable_bytes():
    goals = EX.run_goals([{"run_id": "r1", "state": {"criterion": "x"}}, {"run_id": "r1", "state": {"goal": "G"}},
                          {"run_id": "r1", "state": {"goal": "later"}}])
    assert goals == {"r1": "G"}
    assert EX.origin_ok("user", "non_census") and not EX.origin_ok("census", "non_census")
    assert EX.origin_ok("census", "census") and EX.origin_ok("x", "all")
    ex = [EX.example(VERIFY, {}, [_o("correct")]), EX.example(TIER, {}, [])]
    c = EX.counts(ex)
    assert c["loop.step_verify"]["gold"] == 1 and c["loop.step_verify"]["labels"] == {"not_met": 1}
    assert c["loop.tier"] == {"examples": 1, "gold": 0, "train": 0, "test": 0, "by_source": {},
                              "by_origin": {"census": 1}, "labels": {}}
    body = EX.to_jsonl(reversed(ex))
    assert body == EX.to_jsonl(ex) and [json.loads(l)["id"] for l in body.splitlines()] == ["d1", "d2"]


def test_the_snapshot_is_a_dataset_snapshot_and_the_module_loads():
    from vera.fabric.dataset_provider import DatasetSnapshot
    body = EX.to_jsonl([EX.example(VERIFY, {}, [_o("correct")])])
    snap, recs = DatasetSnapshot.create(dataset_id=EX.DATASET_ID, created_at="2026-10-10T00:00:00+00:00",
                                        records=[json.loads(l) for l in body.splitlines()],
                                        schema={"example": EX.EXAMPLE_SCHEMA}, provenance={})
    assert snap.snapshot_id.startswith("snap_") and snap.record_count == 1 and recs[0]["gold"] == "not_met"
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "minds/export.py"),' in orch
    src = open(os.path.join(ROOT, "vera", "minds", "export.py"), encoding="utf-8").read()
    assert '"decide.export"' in src and "state_dir(\"datasets\", \"decide\")" in src
    assert "Rows are never returned" in src and '"rows"' not in src
