"""Delivery as one system: every vector x every style, the canvas a vector, and a model that picks.

Owner, 2026-10-02: "could the output options be turned into a proper system that can deliver a
selection of styles via any of the available delivery methods and expose this to the chat/agentic
loops - add a configuration option to the ui that lets the llm pick a delivery method - also id
like for html delivery to be able to go to the canvas instead of automatically opening a new tab".

Pure tests (lowercase import) cover the channel registry, the canvas builders and the pick parser;
the app tests drive report.canvas / output.deliver_auto / output.redeliver with the caps they call
stubbed; the chat page and the canvas element are read as source for the wiring.
"""
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera import delivery as D  # noqa: E402

try:
    from Vera.vera.render import render_capabilities as R
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
except Exception:                                    # pragma: no cover
    R = None

needs_app = pytest.mark.skipif(R is None, reason="app module not importable here")
CHAT = (ROOT / "vera" / "chat" / "chat_panel.html").read_text(encoding="utf-8")
CANVAS = (ROOT / "vera" / "canvas" / "canvas_element.js").read_text(encoding="utf-8")


# ── the registry ────────────────────────────────────────────────────────────

def test_the_canvas_is_a_vector_twice_over():
    ids = {c["id"]: c for c in D.list_channels()}
    assert ids["canvas"]["cap"] == "canvas.add" and ids["canvas_html"]["cap"] == "report.canvas"
    assert ids["canvas"]["target_field"] == "session_id" and ids["canvas_html"]["needs_target"]


def test_a_canvas_delivery_lands_on_the_session_canvas_or_a_named_one():
    a = D.build_args("canvas", "# Plan\n- one", {"label": "Plan", "target": "sess-1"})
    assert a["session_id"] == "sess-1" and a["kind"] == "markdown" and a["content"]["md"].startswith("# Plan")
    b = D.build_args("canvas", "x" * 50, {"target": "cv_abc"})
    assert b["id"] == "cv_abc" and "session_id" not in b
    h = D.build_args("canvas_html", "# Report", {"label": "R", "target": "sess-1", "illustrate": 2})
    assert h["session_id"] == "sess-1" and h["content"] == "# Report" and h["illustrate"] == 2 and h["mode"] == "auto"


def test_the_models_menu_names_every_vector_but_the_chat_itself():
    txt = D.describe_for_model(None, [{"id": "short", "label": "Short"}])
    assert "[[deliver:" in txt and "- canvas " in txt and "- canvas_html " in txt and "- podcast " in txt
    assert "- chat " not in txt and "short (Short)" in txt
    assert D.describe_for_model([]) == ""


# ── the pick ────────────────────────────────────────────────────────────────

ALLOWED = ["canvas", "canvas_html", "html", "podcast"]


def test_a_pick_is_one_of_the_offered_vectors_or_none():
    assert D.parse_pick('{"channel":"canvas","style":"short","why":"a plan"}', ALLOWED, ["short"]) == \
        {"channel": "canvas", "style": "short", "why": "a plan"}
    assert D.parse_pick('Sure! {"channel": "email"}', ALLOWED)["channel"] == "none"     # never invents a vector
    assert D.parse_pick('{"channel":"podcast","style":"haiku"}', ALLOWED, ["short"])["style"] == ""
    assert D.parse_pick('{"channel":"none","style":"short"}', ALLOWED, ["short"])["style"] == ""
    assert D.parse_pick("I'd say podcast", ALLOWED)["channel"] == "podcast"                # bare id, last resort
    assert D.parse_pick("", ALLOWED)["channel"] == "none"


def test_the_pick_prompt_carries_the_request_the_answer_and_the_menu():
    p = D.pick_prompt("THE-ANSWER", "THE-ASK", [{"id": "canvas", "label": "Canvas"}], [{"id": "short"}])
    assert "THE-ANSWER" in p and "THE-ASK" in p and "- canvas:" in p and "short" in p and '"channel"' in p


# ── the caps, with what they call stubbed ───────────────────────────────────

def _stub(monkeypatch, name, fn):
    monkeypatch.setitem(CAPABILITY_REGISTRY, name, {"func": fn})


@needs_app
def test_an_html_report_lands_on_the_canvas_not_in_a_tab(monkeypatch):
    seen = {}

    async def report(**kw):
        seen["report"] = kw
        return {"ok": True, "url": "/render/report/r1.html", "artifact_id": "art1", "title": "T", "download_url": "/d"}

    async def add(**kw):
        seen["add"] = kw
        return {"ok": True, "id": "cv_session_s1", "resolved": "added"}
    _stub(monkeypatch, "report.html", report)
    _stub(monkeypatch, "canvas.add", add)
    out = asyncio.run(R.report_canvas(content="# Hi", title="T", session_id="s1"))
    assert out["ok"] and out["url"] == "/render/report/r1.html" and out["canvas"] == "cv_session_s1"
    a = seen["add"]
    assert a["kind"] == "html" and a["session_id"] == "s1" and a["key"] == "report:art1"
    assert a["content"]["url"] == "/render/report/r1.html" and a["size"] == "xl"


@needs_app
def test_redeliver_to_the_canvas_defaults_to_the_calling_session(monkeypatch):
    seen = {}

    async def add(**kw):
        seen.update(kw)
        return {"ok": True, "id": "cv_session_s9"}
    _stub(monkeypatch, "canvas.add", add)
    out = asyncio.run(R.output_redeliver(channel="canvas", content="# A plan\n" + "x" * 60, session_id="s9"))
    assert out["ok"] and seen["session_id"] == "s9" and seen["kind"] == "markdown"


@needs_app
def test_the_model_picks_a_vector_and_it_is_delivered(monkeypatch):
    calls = {}

    async def gen(**kw):
        calls["gen"] = kw
        return {"text": '{"channel": "html", "style": "", "why": "a document"}'}

    async def report(**kw):
        return {"ok": True, "url": "/render/report/r2.html", "artifact_id": "a2"}

    async def add(**kw):
        calls["add"] = kw
        return {"ok": True, "id": "cv_session_s2"}
    _stub(monkeypatch, "llm.generate", gen)
    _stub(monkeypatch, "report.html", report)
    _stub(monkeypatch, "canvas.add", add)
    out = asyncio.run(R.output_deliver_auto(content="# Findings\n" + "y" * 80, request="write it up",
                                            session_id="s2", html_to_canvas=True))
    assert calls["gen"]["job_type"] == "delivery_pick"
    assert out["channel"] == "canvas_html" and out["delivered"] and calls["add"]["content"]["url"] == "/render/report/r2.html"


@needs_app
def test_most_answers_stay_in_the_chat(monkeypatch):
    async def gen(**kw):
        return {"text": '{"channel": "none", "why": "the chat is enough"}'}
    _stub(monkeypatch, "llm.generate", gen)
    out = asyncio.run(R.output_deliver_auto(content="z" * 80, session_id="s3"))
    assert out["ok"] and out["channel"] == "none" and out["delivered"] is False


@needs_app
def test_the_pick_never_runs_on_the_gpu():
    from Vera.vera import capability_orchestration as O
    rule = O.DEFAULT_ROUTING_RULES["delivery_pick"]
    assert rule.get("deny_gpu") and rule.get("model") and "delivery_pick" in O.OLLAMA_JOB_TYPES


# ── the chat and the canvas ─────────────────────────────────────────────────

def test_settings_offer_the_delivery_choice_the_style_and_where_html_opens():
    for v in ('value="chat"', 'value="auto"', 'value="canvas"', 'value="canvas_html"', 'value="podcast"'):
        assert v in CHAT[CHAT.index('id="cfgDelivery"'):CHAT.index('id="cfgDelivery"') + 1500]
    assert 'id="cfgDeliveryStyle"' in CHAT and 'id="cfgHtmlOpen"' in CHAT and '<option value="canvas">On the canvas</option>' in CHAT
    assert "{id:'delivery', n:'Delivery', from:[['rpCfg','Delivery']]" in CHAT


def test_every_answer_path_goes_through_one_delivery():
    assert "_deliverAfter(_stripPanelMarkup(fullText), msg)" in CHAT          # the chat's reply
    assert "_deliverAfter(txt, ask?ask.content:'')" in CHAT                   # the answer after a capability
    assert "_deliverAfter(String(finalText), goal)" in CHAT                    # the agent loop's final answer
    assert "name:'output.deliver_auto'" in CHAT


def test_an_html_report_opens_a_tab_only_when_asked():
    i = CHAT.index("async function _msgHtmlReport(mid){"); body = CHAT[i:i + 2500]
    assert "where==='canvas'?'report.canvas':'report.html'" in body and "window.open" not in body
    j = CHAT.index("async function _redeliverDo(mid, channel, btn){"); rd = CHAT[j:j + 2500]
    assert "if(channel==='html'&&_dlvCfg().html==='canvas') channel='canvas_html';" in rd and "window.open" not in rd
    k = CHAT.index("function _dlvReveal(channel, href){"); rv = CHAT[k:k + 600]
    assert "_dlvCfg().html==='tab'" in rv and "window.open" in rv


def test_the_canvas_draws_a_delivered_report_in_a_sandboxed_frame():
    assert 'data-live="report"' in CANVAS and "kind === 'report'" in CANVAS
    assert "inner.setAttribute('sandbox', 'allow-scripts allow-popups allow-popups-to-escape-sandbox')" in CANVAS
    assert "allow-same-origin" not in CANVAS[CANVAS.index("kind === 'report'"):CANVAS.index("kind === 'report'") + 400]
