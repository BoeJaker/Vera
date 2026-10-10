"""The info-seeking eval's retrieval scoring (tools/info-eval/score.py; plan SK-4.1). Pure."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "info-eval"))
import score  # noqa: E402

NOW = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc)


def test_the_question_set_is_well_formed():
    d = json.loads((ROOT / "tools" / "info-eval" / "questions.json").read_text(encoding="utf-8"))
    qs = d["questions"]
    assert len(qs) == 70 and len({q["id"] for q in qs}) == 70
    counts = {k: sum(1 for q in qs if q["kind"] == k) for k in ("latest", "fact", "overview", "memory", "nosearch")}
    assert counts == {"latest": 20, "fact": 20, "overview": 10, "memory": 10, "nosearch": 10}
    assert all(q.get("expect_any") for q in qs if q["kind"] in ("latest", "fact", "overview"))
    assert all(q.get("fresh_days") for q in qs if q["kind"] == "latest")


def test_score_pack_latest():
    q = {"id": "L01", "kind": "latest", "q": "x", "expect_any": ["AI Act"], "fresh_days": 7}
    pack = {"need": "latest", "elapsed_ms": 8300, "gaps": [], "items": [
        {"title": "EU says AI Act can manage rogue AI", "published": "2026-10-08", "read": False, "text": ""},
        {"title": "Summary", "published": "", "read": True, "text": "the AI Act applies from..."}]}
    r = score.score_pack(q, pack, NOW)
    assert r["ok"] and r["read"] == 1 and r["dated"] == 1 and r["on_target"] and r["on_target_read"]
    assert r["fresh"] and r["newest_age_days"] == 2 and r["need_ok"]
    stale = score.score_pack(q, dict(pack, items=[{"title": "AI Act", "published": "2026-08-01"}]), NOW)
    assert not stale["fresh"]


def test_score_pack_error_and_off_target():
    q = {"id": "F01", "kind": "fact", "q": "x", "expect_any": ["Bank Rate"]}
    r = score.score_pack(q, {"error": "timeout"}, NOW)
    assert not r["ok"] and not r["on_target"] and r["items"] == 0
    off = score.score_pack(q, {"items": [{"title": "Bank of America", "read": True, "text": "banking"}]}, NOW)
    assert off["ok"] and not off["on_target"]


def test_summary_and_markdown():
    rows = [score.score_pack({"id": f"F{i}", "kind": "fact", "q": "q", "expect_any": ["x"]},
                             {"elapsed_ms": ms, "items": [{"title": "x", "read": True, "published": "2026-10-01"}]}, NOW)
            for i, ms in enumerate([1000, 2000, 9000])]
    s = score.summarise(rows)["fact"]
    assert s["n"] == 3 and s["on_target_pct"] == 100.0 and s["p50_ms"] == 2000 and s["p95_ms"] == 9000
    md = score.markdown({"fact": s}, rows, {"started": "t", "target": "x", "gap_s": 20})
    assert "| fact | 3 | 100.0 | 100.0 | 100.0 | - |" in md
