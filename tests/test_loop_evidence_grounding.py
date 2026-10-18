"""The answer check sees the run's dated evidence and the grounding floor (compute-roles P1c,
10 Oct 2026). Probes 4-8 Oct: research answers invented dates and licences while the run had
read dated sources - nothing compared the two. info.seek items are kept per run
(evidence_ledger_core); the answer check shows them to the checker and runs intelligence's
grounding_rules.check (bad [n] citations, figures no source contains). Review vera-03: packs
number from [1], so citations are checked only for a single pack; every item is kept for the
check; each pack's need/since/gaps reach the checker."""
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

PACK = {"need": "latest", "since": "2025-01-01", "answerable": True, "items": [
    {"n": 1, "title": "Redis returns to open source", "url": "https://ex.org/a", "published": "2025-05-01",
     "kind": "web", "text": "Redis 8 adds AGPLv3 as an option on 1 May 2025."},
    {"n": 2, "title": "Valkey forks Redis", "url": "https://ex.org/b", "published": "2024-03-28",
     "kind": "web", "text": "The Linux Foundation announced Valkey on 28 March 2024 under BSD-3-Clause."}]}
PACK2 = {"need": "fact", "answerable": False, "gaps": ["no source dated on or after 2026-09-01"], "items": [
    {"n": 1, "title": "Valkey 8 released", "url": "https://ex.org/c", "published": "2024-09-16",
     "text": "Valkey 8.0 shipped on 16 September 2024."}]}


def test_ledger_numbers_run_wide_keeps_pack_n_and_every_item():
    E.forget("s-led")
    assert E.add("s-led", PACK, step=1) == 2
    assert E.add("s-led", {"result": PACK}) == 0           # wrapped, and the same urls: nothing new
    assert E.add("s-led", PACK2, step=3) == 1
    its = E.items("s-led")
    assert [(i["e"], i["pack"], i["pack_n"]) for i in its] == [(1, 1, 1), (2, 1, 2), (3, 3, 1)]
    ps = E.packs("s-led")
    assert ps[-1]["gaps"] == ["no source dated on or after 2026-09-01"] and ps[-1]["answerable"] is False
    blk = E.block(its, packs_info=ps)
    assert blk.startswith("E1 (pack 1 [1]) (2025-05-01) Redis returns to open source - https://ex.org/a")
    assert "NOT answerable; gaps: no source dated on or after 2026-09-01" in blk
    assert all(i["n"] is None for i in E.check_items("s-led"))     # several packs: [n] ambiguous
    E.forget("s-led")
    E.add("s-one", PACK)
    assert [i["n"] for i in E.check_items("s-one")] == [1, 2]     # one pack: its own [n]
    many = {"items": [{"n": i, "title": "t%d" % i, "url": "u%d" % i, "text": "x"} for i in range(80)]}
    E.add("s-one", many)
    assert len(E.items("s-one")) == 82                              # every item kept for the check
    assert len(E.block(E.items("s-one"))) <= E.BLOCK_CHARS          # only the block is cut
    E.forget("s-one")
    assert E.add("", PACK) == 0 and E.add("s-x", {"items": "nope"}) == 0


def test_grounding_block_shows_evidence_and_floor_findings():
    g = {"ok": False, "bad_citations": [7], "uncited_numbers": ["2023"],
         "unsupported": ["Valkey was forked in September 2023 [7]."]}
    t = PS.grounding_block("E1 (pack 1 [1]) (2025-05-01) x", g, "Cite [n] with dates.")
    assert "GROUNDING RULES:\nCite [n] with dates." in t
    assert "THE RUN'S DATED EVIDENCE (cite as [n]):\nE1 (pack 1 [1]) (2025-05-01) x" in t
    assert "citations to no source: [7]" in t and "figures/dates no source contains: 2023" in t
    assert "in: Valkey was forked in September 2023 [7]." in t
    assert "GROUNDING CHECK" not in PS.grounding_block("E1 x", {"ok": True})
    p = PS.answer_check_prompt("g", [], "ans", [], grounding_text=t)
    assert p.index("GROUNDING CHECK") < p.index("THE FINAL ANSWER TO CHECK")


@needs_app
def test_one_pack_checks_citations_and_figures(monkeypatch):
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
    answer = "Valkey was forked from Redis in September 2023 [2], and Redis 8 added AGPLv3 [9]."
    out = asyncio.run(M._v6_answer_check("report on Valkey", [], [], answer, [], "s-ac"))
    assert out["evidence_items"] == 2
    if M._grounding_rules is not None:
        assert out["grounding"]["ok"] is False and "2023" in out["grounding"]["uncited_numbers"]
        assert out["grounding"]["bad_citations"] == [9]
        assert "GROUNDING CHECK" in seen["prompt"] and "2023" in seen["prompt"]
    assert "(2024-03-28) Valkey forks Redis" in seen["prompt"]
    E.forget("s-ac")


@needs_app
def test_several_packs_drop_ambiguous_citations_but_keep_figures(monkeypatch):
    async def fake_gen(prompt, **kw):
        return "OK"

    async def fake_load(path, session_id="", repo=""):
        return ""

    monkeypatch.setattr(M, "_plan_styles", PS)
    monkeypatch.setattr(M, "_evidence_core", E)
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    E.forget("s-mp")
    E.add("s-mp", PACK)
    E.add("s-mp", PACK2)
    out = asyncio.run(M._v6_answer_check("g", [], [], "Valkey 8 shipped in 2019 [5].", [], "s-mp"))
    if M._grounding_rules is not None:
        assert out["grounding"]["bad_citations"] == [] and out["grounding"]["citations_unchecked"] == 2
        assert "2019" in out["grounding"]["uncited_numbers"] and out["grounding"]["ok"] is False
    E.forget("s-mp")


def test_both_tool_call_sites_record_info_seek():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert "if entry_ok and tool in _evidence_core.EVIDENCE_TOOLS:" in src
    assert "if entry_ok and hop_tool in _evidence_core.EVIDENCE_TOOLS:" in src
    assert src.count("_evidence_core.add(sid, invoke.get(\"result\"))") == 2
    assert "_evidence_core.check_items(sid)" in src and "packs_info=ev_packs" in src


def test_web_research_and_fetch_are_evidence_too():
    E.forget("s-web")
    wr = {"query": "q", "sources": [{"url": "https://gpuweb.example/status", "title": "Implementation Status",
                                     "snippet": "Safari 26 supports WebGPU", "text": "In macOS Tahoe 26 WebGPU is supported."}]}
    assert E.add("s-web", wr) == 1
    assert E.add("s-web", {"url": "https://ex.org/p", "title": "Page", "content": "Chrome 113 shipped WebGPU."}) == 1
    its = E.items("s-web")
    assert [i["url"] for i in its] == ["https://gpuweb.example/status", "https://ex.org/p"]
    assert its[0]["pack_n"] == 1 and "Tahoe 26" in its[0]["text"]
    assert set(E.EVIDENCE_TOOLS) == {"info.seek", "web.research", "web.fetch"}
    E.forget("s-web")


def test_writer_gets_e_numbered_sources_with_the_rule_and_guide():
    E.forget("s-wr")
    assert E.writer_sources("s-wr") == ""
    E.add("s-wr", dict(PACK, guide="cite [n] with dates, report gaps, never fill from memory"))
    E.add("s-wr", PACK2)
    w = E.writer_sources("s-wr")
    assert w.startswith(E.WRITER_RULE)
    assert "never fill from memory" in w
    assert "E3 (pack 2 [1]) (2024-09-16) Valkey 8 released" in w
    E.forget("s-wr")


@needs_app
def test_e_citations_are_checked_exactly_across_packs(monkeypatch):
    async def fake_gen(prompt, **kw):
        return "OK"

    async def fake_load(path, session_id="", repo=""):
        return ""

    monkeypatch.setattr(M, "_plan_styles", PS)
    monkeypatch.setattr(M, "_evidence_core", E)
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "_v5_load_current", fake_load)
    E.forget("s-e")
    E.add("s-e", PACK)
    E.add("s-e", PACK2)
    out = asyncio.run(M._v6_answer_check(
        "g", [], [], "Valkey 8.0 shipped on 16 September 2024 [E3]; see also [E9].", [], "s-e"))
    if M._grounding_rules is not None:
        g = out["grounding"]
        assert g.get("citations_unchecked") is None              # E ids are exact across packs
        assert [str(x) for x in g["bad_citations"]] in (["E9"], ["9"])
        assert "2024" not in (g.get("uncited_numbers") or [])
    E.forget("s-e")


def test_the_writer_hand_off_prefers_the_ledger():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert "_srcs = _evidence_core.writer_sources(sid) or _research_sources_core.sources_block(" in src
    assert 'grounding = dict(_grounding_rules.check(final, evidence, id_key="e"))' in src
