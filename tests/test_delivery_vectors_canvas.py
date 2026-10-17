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
import json
import pathlib
import re
import shutil
import subprocess
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


def test_an_explicit_ask_decides_without_a_model():
    """Measured 2026-10-02 on qwen3.5:2b: the plainest asks went to 'none' - so the request is read first."""
    allowed = ["canvas", "canvas_html", "html", "podcast", "email", "telegram", "notebook", "memory"]
    asks = {"plan my weekend and keep it somewhere I can see": "canvas",
            "summarise the news so I can listen to it on my drive": "podcast",
            "email me the report": "email",                       # an email, not a report
            "ping it to my phone": "telegram",
            "write it up as a proper document": "canvas_html",
            "put it on the canvas": "canvas",
            "remember that the router is on .1": "memory",
            "what is 12 times 9?": "",
            "how does TLS work, briefly": "none",              # asked short: the chat alone
            "briefly, email me the gist": "email"}              # a named place still wins
    for q, want in asks.items():
        assert D.explicit_ask(q, allowed) == want, q
    assert D.explicit_ask("email me the report", ["canvas"]) == ""        # not offered: not chosen


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
    out = asyncio.run(R.output_deliver_auto(content="# Findings\n" + "y" * 600, question="what did the survey find",
                                            session_id="s2", html_to_canvas=True))
    assert calls["gen"]["job_type"] == "delivery_pick"
    assert out["channel"] == "canvas_html" and out["delivered"] and calls["add"]["content"]["url"] == "/render/report/r2.html"


@needs_app
def test_an_explicit_ask_is_delivered_without_asking_a_model(monkeypatch):
    calls = {}

    async def gen(**kw):
        calls["gen"] = kw
        return {"text": '{"channel": "none"}'}

    async def add(**kw):
        calls["add"] = kw
        return {"ok": True, "id": "cv_session_s4"}
    _stub(monkeypatch, "llm.generate", gen)
    _stub(monkeypatch, "canvas.add", add)
    out = asyncio.run(R.output_deliver_auto(content="- pack\n- book\n- leave", question="keep it somewhere I can see",
                                            session_id="s4"))
    assert "gen" not in calls and out["channel"] == "canvas" and out["delivered"] and out["why"] == "you asked for it"


@needs_app
def test_most_answers_stay_in_the_chat(monkeypatch):
    calls = {}

    async def gen(**kw):
        calls["gen"] = kw
        return {"text": '{"channel": "none", "why": "the chat is enough"}'}
    _stub(monkeypatch, "llm.generate", gen)
    short = asyncio.run(R.output_deliver_auto(content="z" * 80, question="what is 12 times 9", session_id="s3"))
    assert short["channel"] == "none" and short["delivered"] is False and "gen" not in calls   # short: no model
    long_ = asyncio.run(R.output_deliver_auto(content="z" * 900, question="explain it", session_id="s3"))
    assert long_["ok"] and long_["channel"] == "none" and long_["delivered"] is False and "gen" in calls


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


def _chat_fn(name):
    """The source of one top-level chat function, by brace matching from its declaration."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", CHAT)
    assert m, name + " is gone from chat_panel.html"
    depth, k = 0, CHAT.index("{", m.end())
    for k in range(k, len(CHAT)):
        depth += {"{": 1, "}": -1}.get(CHAT[k], 0)
        if depth == 0:
            return CHAT[m.start():k + 1]
    raise AssertionError("unbalanced " + name)


_HTML_OPENS_HARNESS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
async function run(htmlOpens){
  const log = {calls: [], opened: [], canvas: 0};
  const els = {cfgHtmlOpen: {value: htmlOpens}, cfgDelivery: {value: 'chat'}, cfgDeliveryStyle: {value: ''}};
  const ctx = {
    BASE: 'http://vera', SID: 's1', console,
    _MSG_REGISTRY: {m1: {getContent: () => '# Title\nSome body text for the report.'}},
    document: {getElementById: id => els[id] || null},
    localStorage: {getItem: () => null, setItem: () => {}},
    window: {open: (href) => { log.opened.push(href); }},
    appendSys: () => {}, esc: s => String(s), _galButton: () => '',
    _pages: new Set(), togglePage: () => { log.canvas++; }, _canvasColumnMount: () => { log.canvas++; },
    fetch: async (url, opt) => { const b = JSON.parse(opt.body); log.calls.push(b);
      return {json: async () => ({content: {ok: true, url: '/r/1.html'}})}; },
  };
  vm.createContext(ctx);
  vm.runInContext(src + '\n;this.__f={_msgHtmlReport,_redeliverDo,_dlvReveal};', ctx);
  await ctx.__f._msgHtmlReport('m1');
  await ctx.__f._msgHtmlReport('nope', 'Given text from the kit prose row');
  await ctx.__f._redeliverDo('m1', 'html', null);
  ctx.__f._dlvReveal('html', 'http://vera/r/2.html');   // a delivery the server routed as plain 'html'
  return log;
}
(async () => { process.stdout.write(JSON.stringify({canvas: await run('canvas'), tab: await run('tab')})); })()
  .catch(e => { console.error(e); process.exit(1); });
"""


def test_an_html_report_opens_a_tab_only_when_asked():
    """Runs the chat's real report / re-deliver / reveal code: with "HTML opens" on the canvas the report
    is drawn there (report.canvas, channel canvas_html) and no tab opens; only "tab" opens one."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = "\n".join(_chat_fn(n) for n in ("_dlvSaved", "_dlvCfg", "_dlvReveal", "_msgHtmlReport", "_redeliverDo"))
    p = subprocess.run([node, "-e", _HTML_OPENS_HARNESS], input=src, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    cv, tb = out["canvas"], out["tab"]
    # canvas: both report builds go to report.canvas, the re-deliver goes as canvas_html, the column opens, no tab
    assert [c["name"] for c in cv["calls"][:2]] == ["report.canvas", "report.canvas"]
    assert cv["calls"][1]["arguments"]["content"] == "Given text from the kit prose row"
    assert cv["calls"][2]["name"] == "output.redeliver" and cv["calls"][2]["arguments"]["channel"] == "canvas_html"
    assert cv["opened"] == [] and cv["canvas"] == 3
    # tab: report.html, channel html, and a tab per delivery - only because it was asked for
    assert [c["name"] for c in tb["calls"][:2]] == ["report.html", "report.html"]
    assert tb["calls"][2]["arguments"]["channel"] == "html"
    assert tb["opened"] == ["http://vera/r/1.html"] * 3 + ["http://vera/r/2.html"] and tb["canvas"] == 0


def test_the_canvas_draws_a_delivered_report_in_a_sandboxed_frame():
    assert 'data-live="report"' in CANVAS and "kind === 'report'" in CANVAS
    assert "inner.setAttribute('sandbox', 'allow-scripts allow-popups allow-popups-to-escape-sandbox')" in CANVAS
    assert "allow-same-origin" not in CANVAS[CANVAS.index("kind === 'report'"):CANVAS.index("kind === 'report'") + 400]


def test_the_ask_is_not_named_request():
    """The capability layer drops an argument named `request` (it is FastAPI's) - live, every ask arrived empty."""
    src = (ROOT / "vera" / "render" / "render_capabilities.py").read_text(encoding="utf-8")
    sig = src[src.index("async def output_deliver_auto("):src.index("async def output_deliver_auto(") + 200]
    assert "question: str" in sig and "request" not in sig
    assert "question:String(request||'')" in CHAT
