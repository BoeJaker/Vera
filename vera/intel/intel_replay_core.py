"""Deterministic, non-executing replay for Intel's pure decision seams."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .intel_core import (
    apply_enrichment,
    build_report,
    channel_args,
    heuristic_score,
    make_snapshot,
    normalise_signal,
    novelty,
)
from .intel_ops_core import (
    blend_score,
    genre_factor,
    genre_of,
    ner_entities,
    qa_answer,
    staleness_factor,
    stance,
)


CORPUS_SCHEMA = "vera.intel-replay-corpus/v1"
REPORT_SCHEMA = "vera.intel-replay-report/v1"
POLICY = {
    "synthetic_only": True,
    "network_calls": False,
    "model_calls": False,
    "capability_execution": False,
    "writes": False,
}


def load_corpus(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("corpus_root_must_be_object")
    return value


def canonical_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if corpus.get("schema") != CORPUS_SCHEMA:
        issues.append({"code": "schema.invalid", "path": "schema"})
    policy = corpus.get("policy")
    if not isinstance(policy, Mapping) or any(policy.get(k) is not v for k, v in POLICY.items()):
        issues.append({"code": "policy.nonexecuting_required", "path": "policy"})
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        cases = []
        issues.append({"code": "cases.invalid", "path": "cases"})
    seen: set[str] = set()
    for index, case in enumerate(cases):
        path = f"cases.{index}"
        if not isinstance(case, Mapping):
            issues.append({"code": "case.invalid", "path": path})
            continue
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            issues.append({"code": "case.id_required", "path": f"{path}.id"})
        elif case_id in seen:
            issues.append({"code": "case.id_duplicate", "path": f"{path}.id"})
        seen.add(case_id)
        for field in ("now", "topic", "candidates", "snapshot_output", "expected"):
            expected_type = list if field == "candidates" else Mapping
            if field == "now":
                if not isinstance(case.get(field), str):
                    issues.append({"code": f"case.{field}_invalid", "path": f"{path}.{field}"})
            elif not isinstance(case.get(field), expected_type):
                issues.append({"code": f"case.{field}_invalid", "path": f"{path}.{field}"})
    return {"schema": REPORT_SCHEMA, "ok": not issues, "issues": issues,
            "case_count": len(cases), "fingerprint": canonical_fingerprint(corpus)}


def _replay_case(case: Mapping[str, Any]) -> dict[str, Any]:
    topic = dict(case["topic"])
    now = datetime.fromisoformat(str(case["now"]).replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    recent_titles = list(case.get("recent_titles") or [])
    retained: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen_ids: set[str] = set()

    for raw_candidate in case["candidates"]:
        candidate = dict(raw_candidate)
        opaque_id = str(candidate.pop("fixture_id", ""))
        model = candidate.pop("model", {}) if isinstance(candidate.get("model"), Mapping) else {}
        signal = normalise_signal(topic["id"], candidate, source="fixture", query="frozen")
        if not signal or signal["id"] in seen_ids:
            dropped.append(opaque_id)
            continue
        seen_ids.add(signal["id"])
        nov = novelty(signal, recent_titles)
        heuristic = heuristic_score(signal, topic, now=now, novelty_v=nov)
        genre, _ = genre_of(model.get("genre") or {})
        score = blend_score(relevance=model.get("relevance"), heuristic=heuristic, novelty=nov)
        score = round(score * genre_factor(genre, bool(topic.get("vendors_ok"))) *
                      staleness_factor(signal, now.year), 1)
        if score < float(topic.get("minimum_score", 4.0)):
            dropped.append(opaque_id)
            continue
        parsed = dict(model.get("enrichment") or {})
        parsed["entities"] = ner_entities(model.get("ner") or {}) or parsed.get("entities", [])
        answer = qa_answer(str(model.get("question") or ""), model.get("qa") or {})
        if answer:
            parsed["answers"] = [answer["question"]]
        signal = apply_enrichment(signal, parsed)
        signal.update({"score": score, "genre": genre, "stance": stance(model.get("stance") or {}),
                       "fixture_id": opaque_id})
        retained.append(signal)

    retained.sort(key=lambda row: (-float(row.get("score") or 0), row["fixture_id"]))
    snapshot = make_snapshot(topic, dict(case["snapshot_output"]), retained,
                             ts=str(case["now"]), first=bool(case.get("first_snapshot", False)))
    report = build_report([topic], {topic["id"]: {"signals": retained, "snapshot": snapshot}},
                          title="Frozen Intel replay", ts=str(case["now"]))
    plan = channel_args("email", report, report_url="/intel/reports/frozen", target="fixture@example.invalid")
    return {
        "retained": [row["fixture_id"] for row in retained],
        "dropped": sorted(dropped),
        "scores": {row["fixture_id"]: row["score"] for row in retained},
        "snapshot": {"changed": snapshot["changed"], "confidence": snapshot["confidence"],
                     "signal_count": snapshot["signal_count"]},
        "report": {"topics": report["counts"]["topics"], "signals": report["counts"]["signals"],
                   "section_kinds": [row["kind"] for row in report["sections"]]},
        "delivery_plan": {"channel": "email", "planned": bool(plan),
                          "has_target": bool(plan and plan.get("to")), "executed": False},
    }


def evaluate_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    validation = validate_corpus(corpus)
    if not validation["ok"]:
        return {**validation, "accuracy": 0.0, "coverage": 0.0,
                "deterministic": False, "failed_case_ids": []}
    outcomes: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        actual = _replay_case(case)
        expected = case["expected"]
        ok = actual == expected
        outcomes.append({"id": case["id"], "ok": ok, "actual": actual})
    repeated = [_replay_case(case) for case in corpus["cases"]]
    deterministic = repeated == [row["actual"] for row in outcomes]
    total = len(outcomes)
    passed = sum(int(row["ok"]) for row in outcomes)
    return {**validation, "ok": passed == total and deterministic,
            "accuracy": round(passed / total, 4) if total else 0.0,
            "coverage": 1.0 if total else 0.0, "deterministic": deterministic,
            "failed_case_ids": [row["id"] for row in outcomes if not row["ok"]],
            "case_ids": [row["id"] for row in outcomes]}
