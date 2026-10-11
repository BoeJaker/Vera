"""Gather-then-write steps end at their author call (per-run author_gather, compute-roles 11 Oct 2026):
census prose-only spent 380-1,148 s re-reading its file and counting words when its step also planned
web.research. prose.author now reports `words`."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import author_done_core as A  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical

RES = {"ok": True, "path": "race_condition_explainer.md", "words": 203}
TEXT = "Write a 200-word explainer of a race condition for a junior developer"


def test_gather_caps_only_count_when_allowed():
    caps = ["web.research", "prose.author"]
    assert A.step_is_answered(caps, TEXT, "prose.author", RES)[0] is False
    ok, why = A.step_is_answered(caps, TEXT, "prose.author", RES, allow_gather=True)
    assert ok and "IS this step's deliverable" in why
    # a cap that is neither authoring nor gathering still keeps the step open
    assert A.step_is_answered(caps + ["exec.bash.run"], TEXT, "prose.author", RES, allow_gather=True)[0] is False
    # a sequencing seam still keeps it open
    assert A.step_is_answered(caps, TEXT + ", then publish it", "prose.author", RES, allow_gather=True)[0] is False


def test_wired_per_run_and_prose_reports_words():
    assert 'v6_author_gather = bool(body.get("author_gather", False))' in SRC
    assert "author_gather=v6_author_gather," in SRC
    assert SRC.count('step["_author_gather"] = True') == 2
    assert 'allow_gather=bool(step.get("_author_gather")))' in SRC
    assert '"words": len(content.split()),' in SRC
