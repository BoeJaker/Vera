"""The answer check sees the run's dated evidence and the grounding floor (compute-roles P1c,
10 Oct 2026). Probes 4-8 Oct: research answers invented dates and licences while the run had
read dated sources - nothing compared the two. info.seek items are kept per run
(evidence_ledger_core); the answer check shows them to the checker and runs intelligence's
grounding_rules.check (bad [n] citations, figures no source contains)."""
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import evidence_ledger_core as E  # noqa: E402
from vera.planning import planner_styles as PS  # noqa: E402

try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None
needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")

pytestmark = pytest.mark.critical

PACK = {"need": "latest", "answerable": True, "items": [
    {"n": 1, "title": "Redis returns to open source", "url": "https://ex.org/a", "published": "2025-05-01",
     "kind": "web", "text": "Redis 8 adds AGPLv3 as an option on 1 May 2025."},
    {"n": 2, "title": "Valkey forks Redis", "url": "https://ex.org/b", "published": "2024-03-28",
     "kind": "web", "text": "The Linux Foundation announced Valkey on 28 March 2024 under BSD-3-Clause."}]}


def test_ledger_keeps_pack_items_once_and_bounded():
    E.forget("s-led")
    assert E.add("s-led", PACK) == 2
    assert E.add("s-led", {"result": PACK}) == 0          # wrapped, and the same urls: nothing new
    its = E.items("s-led")
    assert [i["n"] for i in its] == [1, 2] and its[1]["published"] == "2024-03-28"
    assert E.add("s-led", {"items": "nope"}) == 0 and E.add("", PACK) == 0
    blk = E.block(its)
    assert blk.startswith("[1] (2025-05-01) Redis returns to open source - https://ex.org/a")
    many = {"items": [{"n": i, "title": "t%d" % i, "url": "u%d" % i, "text": "x"} for i in range(80)]}
    E.add("s-led", many)
    assert len(E.items("s-led")) == E.MAX_ITEMS
    E.forget("s-led")


def test_grounding_block_shows_evidence_and_floor_findings():
    g = {"ok": False, "bad_citations": [7], "uncited_numbers": ["2023"],
         "unsupported": ["Valkey was forked in September 2023 [7]."]}
    t = PS.grounding_block("[1] (2025-05-01) x", g, "Cite [n] with dates.")
    assert "GROUNDING RULES:\nCite [n] with dates." in t
    assert "THE RUN'S DATED EVIDENCE (cite as [n]):\n[1] (2025-05-01) x" in t
    assert "citations to no source: [7]" in t and "figures/dates no source contains: 2023" in t
    assert "in: Valkey was forked in September 2023 [7]." in t
    assert "GROUNDING CHECK" not in PS.grounding_block("[1] x", {"ok": True})
    p = PS.answer_check_prompt("g", [], "ans", [], grounding_text=t)
    assert p.index("GROUNDING CHECK") < p.index("THE FINAL ANSWER TO CHECK")


@needs_app
def test_the_answer_check_runs_the_floor_on_the_runs_evidence(monkeypatch):
    seen = {}

    async def fake_gen(prompt, **kw):
        seen["prompt"] = prompt
        return "OK"

    async def fake_load(path, session_id="", repo=""):
        return ""

    monkeypatch.setattr(M, "_plan_styles", PS)
    monkeypatch.setattr(M, "_evidence_core", E)
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    E.forget("s-ac")
    E.add("s-ac", PACK)
    answer = "Valkey was forked from Redis in September 2023 [2], and Redis 8 added AGPLv3 [1]."
    out = asyncio.run(M._v6_answer_check("report on Valkey", [], [], answer, [], "s-ac"))
    assert out["evidence_items"] == 2
    if M._grounding_rules is not None:
        assert out["grounding"]["ok"] is False and "2023" in out["grounding"]["uncited_numbers"]
        assert "GROUNDING CHECK" in seen["prompt"] and "2023" in seen["prompt"]
    assert "THE RUN'S DATED EVIDENCE" in seen["prompt"] and "(2024-03-28) Valkey forks Redis" in seen["prompt"]
    E.forget("s-ac")


def test_both_tool_call_sites_record_info_seek():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert 'if entry_ok and tool == "info.seek":' in src
    assert 'if entry_ok and hop_tool == "info.seek":' in src
    assert src.count("_evidence_core.add(sid, invoke.get(\"result\"))") == 2
