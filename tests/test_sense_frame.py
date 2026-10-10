"""sense.frame: one Frame per text (four-minds FM-1.1)."""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import sense_core as SC  # noqa: E402

pytestmark = pytest.mark.critical

CAPS = {"memory.seek", "web.search", "backup.status"}


def test_patterns_find_what_they_should():
    t = ("check backup.status on cpu-247 and CT148 (192.168.0.138), then open "
         "https://example.com/a?b=1 and /home/boejaker/Vera/vera/fabric/memory.py; "
         "also $BTC, ETH/USDT and SOL; session claude:4dfcd766 and notes.md")
    p = SC.patterns(t, CAPS)
    assert p["urls"] == ["https://example.com/a?b=1"]
    assert "/home/boejaker/Vera/vera/fabric/memory.py" in p["paths"] and "notes.md" in p["paths"]
    assert p["tickers"] == ["BTC", "ETH/USDT", "SOL"]
    assert set(p["nodes"]) == {"cpu-247", "CT148", "192.168.0.138"}
    assert p["sessions"] == ["claude:4dfcd766"]
    assert p["caps"] == ["backup.status"]


def test_patterns_do_not_invent():
    p = SC.patterns("e.g. version 1.2.3 on 2026-10-08, it is OK and the API works, i.e. fine", CAPS)
    assert p == {"urls": [], "paths": [], "tickers": [], "nodes": [], "sessions": [], "caps": []}
    assert SC.patterns("memory.seek", None)["caps"] == []          # no registry given: no cap names claimed


def test_domains_rank_cards_by_cosine():
    cards = {"markets": [1.0, 0.0, 0.0], "estate": [0.0, 1.0, 0.0], "media": [0.6, 0.8, 0.0]}
    d = SC.domains([0.9, 0.1, 0.0], cards, top=2)
    assert [x["domain"] for x in d] == ["markets", "media"] and d[0]["score"] > d[1]["score"]
    assert SC.domains(None, cards) == [] and SC.domains([1.0, 0.0], cards) == []   # wrong dimension: skipped


def test_the_frame_record():
    pats = SC.patterns("hello", CAPS)
    fr = SC.frame("hello", "chat", pats, doms=[{"domain": "chat", "score": 0.7}], ms={"total": 9.5},
                  cards_version="abc")
    assert fr["schema"] == SC.FRAME_SCHEMA and fr["domain"] == "chat" and fr["degraded"] is False
    assert fr["text_hash"] == SC.text_hash("hello", "chat") != SC.text_hash("hello", "goal")
    assert "vector" not in fr
    deg = SC.frame("hello", "chat", pats, degraded=True)
    assert deg["degraded"] and deg["domain"] is None and deg["domains"] == []


def test_every_domain_has_a_card_and_the_cards_have_a_version():
    assert all(len(v) > 30 for v in SC.DOMAIN_CARDS.values())
    v1 = SC.card_hash(SC.DOMAIN_CARDS)
    assert v1 != SC.card_hash(dict(SC.DOMAIN_CARDS, chat="something else"))


def test_the_capability_is_registered():
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "minds/sense_capabilities.py"),' in orch


def test_frame_with_nodes_down_is_patterns_only(monkeypatch):
    M = pytest.importorskip("Vera.vera.minds.sense_capabilities", reason="needs the app")

    async def down(name, timeout, **kw):
        return {"error": "down"}
    monkeypatch.setattr(M, "_raw", down)
    monkeypatch.setattr(M, "_redis", lambda: None)
    monkeypatch.setattr(M, "_CARDS", {})
    fr = asyncio.run(M.cap_sense_frame.__wrapped__("look at cpu-247", kind="chat")
                     if hasattr(M.cap_sense_frame, "__wrapped__") else M.cap_sense_frame("look at cpu-247"))
    assert fr["degraded"] is True and fr["patterns"]["nodes"] == ["cpu-247"] and fr["domains"] == []


def test_frame_with_a_vector_names_the_domain(monkeypatch):
    M = pytest.importorskip("Vera.vera.minds.sense_capabilities", reason="needs the app")
    names = list(M.SC.DOMAIN_CARDS)

    async def fake(name, timeout, **kw):
        if name == "nlp.embed":
            texts = kw["texts"]
            if len(texts) == len(names):                       # the cards: one-hot per domain
                return {"embeddings": [[1.0 if i == j else 0.0 for i in range(len(names))] for j in range(len(names))]}
            return {"embeddings": [[1.0 if n == "markets" else 0.05 for n in names]]}
        return {"error": "x"}
    monkeypatch.setattr(M, "_raw", fake)
    monkeypatch.setattr(M, "_redis", lambda: None)
    monkeypatch.setattr(M, "_CARDS", {})
    fn = getattr(M.cap_sense_frame, "__wrapped__", M.cap_sense_frame)
    fr = asyncio.run(fn("what is BTC doing today", kind="chat", vector=True))
    assert fr["degraded"] is False and fr["domain"] == "markets" and fr["patterns"]["tickers"] == ["BTC"]
    assert isinstance(fr["vector"], list) and len(fr["vector"]) == len(names)
