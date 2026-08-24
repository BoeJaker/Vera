import copy
import json
from pathlib import Path

import pytest

from vera.evaluation_corpus_core import (
    canonical_fingerprint, load_corpus, score_case, validate_corpus,
)


pytestmark = pytest.mark.critical
CORPUS = Path(__file__).parents[1] / "evaluations" / "frozen-corpus-v1.json"


def test_frozen_corpus_is_valid_and_covers_required_domains():
    report = validate_corpus(load_corpus(CORPUS))
    assert report["ok"] is True
    assert report["lanes"] == {"deterministic": 7, "queued_live": 4}
    assert {"run", "workflow", "tool-selection", "memory", "fabric",
            "generation", "operator", "research", "load"} <= set(report["domains"])


def test_fingerprint_is_order_stable_and_excludes_embedded_fingerprint():
    corpus = load_corpus(CORPUS)
    reordered = json.loads(json.dumps(corpus, sort_keys=True))
    reordered["fingerprint"] = "stale-derived-value"
    assert canonical_fingerprint(corpus) == canonical_fingerprint(reordered)


def test_validation_rejects_duplicate_ids_and_unbudgeted_live_case():
    corpus = load_corpus(CORPUS)
    bad = copy.deepcopy(corpus)
    bad["cases"][1]["id"] = bad["cases"][0]["id"]
    bad["cases"][-1].pop("budget")
    codes = {issue["code"] for issue in validate_corpus(bad)["issues"]}
    assert {"case.id_duplicate", "case.live_budget_required"} <= codes


def test_score_case_is_deterministic_and_fails_missing_paths_closed():
    case = {"id": "resolver", "expected": {
        "authorized": False, "rank.selected": "safe.cap", "executed": False}}
    result = score_case(case, {"authorized": False, "executed": False,
                               "rank": {"selected": "safe.cap"}})
    assert result["ok"] is True and result["score"] == 1.0
    missing = score_case(case, {"authorized": False})
    assert missing["ok"] is False and missing["score"] == pytest.approx(1 / 3, abs=0.0001)
