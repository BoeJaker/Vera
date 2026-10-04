"""Intelligence capabilities end to end against a stand-in orchestration: a topic is
made, swept (web.search faked), read and assessed (reader + model faked), snapshotted,
reported, delivered to Telegram with buttons, an offered action is run once, the
nightly job stops when the idle queue says so, a dream finding lands on the right
topic, and the n8n state imports. No app, no network, no model."""
import asyncio
import importlib.util
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestration_stub import stubbed_orchestration  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(tmp_path):
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}
    orch.EVENTS, orch.UI, orch.SCHEDULED, orch.PROMPTS, orch.SENT = [], [], [], [], []

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "raw": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, *a, **k): return lambda fn: fn
        def post(self, *a, **k): return lambda fn: fn

    async def emit_event(ev):
        orch.EVENTS.append(ev)

    async def ollama_generate(prompt, **kw):
        orch.PROMPTS.append((prompt, kw))
        if "running assessment" in prompt:
            return ('Thinking... {"headline": "Pilots are going live", "state": "Two firms run pilots.", '
                    '"changed": ["Acme went live"], "confidence": "medium", "new_questions": ["Who is next?"], '
                    '"watch": ["Acme"], "diagram": "flowchart LR\\n A-->B"}')
        return ('{"summary": "Acme runs an AI valuation pilot.", "so_what": "First UK pilot.", "relevance": 8, '
                '"entities": ["Acme", "RICS"], "tags": ["pilot"], "follow_up": ["How accurate is it?"]}')

    orch.APP = _App(); orch.capability = capability; orch.emit_event = emit_event
    orch.enum_schema = lambda **kw: {}
    orch.register_ui = lambda *a, **k: orch.UI.append((a, k))
    orch.schedule = lambda fn, interval, **k: orch.SCHEDULED.append((fn, interval, k))
    orch.ollama_generate = ollama_generate

    reg = orch.CAPABILITY_REGISTRY

    async def web_search(query, limit=8, dataset_id="", discover="", trace_id=None):
        return {"results": [
            {"url": "https://news.example/acme-pilot?utm_source=x", "title": "Acme launches AI valuation pilot in UK property", "snippet": "Acme property valuation pilot"},
            {"url": "https://github.com/acme/valuer", "title": "acme/valuer: AI valuation agent", "snippet": "valuation agent code"},
            {"url": "https://x.example/a", "title": "Just a moment..."},
        ]}

    async def reader_page(url, max_chars=60000, trace_id=None):
        return {"ok": True, "title": "t", "markdown": "A long enough paragraph about the Acme pilot and its valuation results.",
                "image": "https://news.example/i.png", "minutes": 2}

    async def tg_send_rich(text="", buttons=None, photo_url="", parse_mode="HTML", chat_id="", trace_id=None):
        orch.SENT.append({"text": text, "buttons": json.loads(buttons or "[]"), "photo": photo_url})
        return {"ok": True}

    async def research_run(query, mode="single", output_mode="report", trace_id=None):
        return {"job_id": "j1", "status": "queued", "query": query}

    async def fabric_browse(dataset_id, limit=200, offset=0, trace_id=None):
        data = {
            "vera.interests": [{"topic": "UK commercial real estate market", "active": True, "note": "sector", "created": "1"}],
            "intel.sources": [{"id": "s1", "topic": "ai_ml", "query": "open source LLM release", "added": "1"},
                              {"id": "s2", "topic": "ai_ml", "query": "", "added": "2"}],
            "intel.enrichments": [{"url": "https://a.example/x", "title": "Open model released", "topic": "ai_ml",
                                   "summary": "S", "relevance": 9, "enriched_at": "2026-10-03T00:00:00Z"}],
        }.get(dataset_id, [])
        return {"records": [{"data": d} for d in data], "has_more": False}

    for n, f in (("web.search", web_search), ("reader.page", reader_page), ("tg.send_rich", tg_send_rich),
                 ("research.run", research_run), ("fabric.browse", fabric_browse)):
        reg[n] = {"func": f, "raw": f}

    spec = importlib.util.spec_from_file_location("intel_under_test", os.path.join(ROOT, "vera", "intel", "intel_capabilities.py"))
    with stubbed_orchestration(orch):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    from vera.intel.intel_store import IntelStore
    mod._STORE = IntelStore(tmp_path / "intel.db")
    return mod, orch


def _run(coro):
    return asyncio.run(coro)


def test_registers_caps_panel_and_tick(tmp_path):
    mod, orch = _load(tmp_path)
    for name in ("intel.topic.save", "intel.sweep", "intel.timeline.get", "intel.report.build", "intel.deliver",
                 "intel.action.run", "intel.brief", "intel.ingest_dream", "intel.import.n8n", "intel.n8n.retire",
                 "intel.panel.html", "intel.summary", "intel.entities.get", "intel.asof.get"):
        assert name in orch.CAPABILITY_REGISTRY, name
    assert orch.UI and orch.UI[0][0][0] == "intel" and "<iframe" in orch.UI[0][1]["html"]
    fn, interval, kw = orch.SCHEDULED[0]
    assert kw.get("singleton") and kw.get("skip_in_sandbox")


def test_the_whole_cycle(tmp_path):
    mod, orch = _load(tmp_path)
    R = lambda n, **kw: _run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))
    t = R("intel.topic.save", name="AI property valuation", brief="AI valuation in UK property",
          questions="Which firms run pilots?\nHow accurate?", entities=["Acme"], depth="scan")["topic"]
    assert t["id"] == "ai-property-valuation" and t["questions"] == ["Which firms run pilots?", "How accurate?"]

    sw = R("intel.sweep", topic_id=t["id"])
    assert sw["swept"][0]["new"] == 2                     # the interstitial is junk, the repeat is a duplicate
    assert R("intel.sweep", topic_id=t["id"])["swept"][0]["new"] == 0

    en = _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    assert en["enriched"] == 2
    assert all(kw["prefer_gpu"] is False and kw["think"] is False and kw["job_type"] == "idle_intel"
               for _, kw in orch.PROMPTS)
    sigs = R("intel.signal.list", topic_id=t["id"])["rows"]
    assert {s["score"] for s in sigs} == {8.0}

    sn = _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))["snapshot"]
    assert sn["headline"] == "Pilots are going live"
    assert "Who is next?" in mod.store().get_topic(t["id"])["questions"]

    tl = R("intel.timeline.get", topic_id=t["id"], days=7)
    assert tl["days"][-1]["signals"] == 2 and tl["days"][-1]["snapshots"] == 1
    day = tl["days"][-1]["date"]
    assert R("intel.asof.get", topic_id=t["id"], when=day)["snapshot"]["id"] == sn["id"]

    rep = R("intel.report.build", topic_ids=t["id"], deliver="telegram")
    assert not rep["empty"] and rep["delivered"][0]["ok"]
    sent = orch.SENT[-1]
    assert "Pilots are going live" in sent["text"] and sent["photo"] == "https://news.example/i.png"
    assert sent["buttons"][0][0]["url"].endswith("/intel/report/view/" + rep["id"])
    page = mod.store().get_report(rep["id"], with_html=True)["_html"]
    assert "<vera-mermaid" in page and "data-token=" in page
    # the same signals are not reported twice
    assert R("intel.report.build", topic_ids=t["id"])["empty"]

    acts = R("intel.action.list", status="offered")["rows"]
    kinds = {a["kind"] for a in acts}
    assert {"repo", "research"} <= kinds
    research = next(a for a in acts if a["kind"] == "research")
    r = R("intel.action.run", token=research["token"])
    assert r["ok"] and "j1" in r["message"]
    assert R("intel.action.run", token=research["token"])["error"]          # one time only
    assert R("intel.action.run", token="forged")["error"] == "unknown action"

    assert "AI property valuation" in R("intel.brief")["text"]
    assert R("intel.summary")["topics"] == 1

    d = R("intel.ingest_dream", text="# Acme pilot\nThe AI valuation pilot at Acme expanded to property in the UK.")
    assert d["ok"] and d["topic"] == t["id"]
    assert not R("intel.ingest_dream", text="Weather is nice today")["ok"]


def test_nightly_job_yields_when_told(tmp_path):
    mod, orch = _load(tmp_path)
    _run(orch.CAPABILITY_REGISTRY["intel.topic.save"]["func"](name="Topic one"))
    calls = []

    async def busy():
        calls.append(1)
        return "someone is using the box"
    r = _run(mod._intel_job({"payload": {}}, busy))
    assert r["yielded"].startswith("pre-empted") and "using the box" in r["yielded"] and r["done"] == []  # the queue re-queues only pre-empted jobs

    async def quiet():
        return ""
    r = _run(mod._intel_job({"payload": {}}, quiet))
    assert r["ok"] and r["topics"] == ["topic-one"]


def test_n8n_import_is_dry_by_default(tmp_path):
    mod, orch = _load(tmp_path)
    R = lambda n, **kw: _run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))
    dry = R("intel.import.n8n")
    names = {t["name"] for t in dry["topics"]}
    assert {"UK commercial real estate market", "AI and machine learning"} <= names and dry["signals"] == 1
    assert mod.store().topics() == []
    R("intel.import.n8n", dry_run=False)
    ai = mod.store().get_topic("ai-and-machine-learning")
    assert ai["queries"] == ["open source LLM release"]
    assert mod.store().count_signals(topic_id="ai-and-machine-learning") == 1
    R("intel.import.n8n", dry_run=False)                   # idempotent
    assert len(mod.store().topics()) == 2 and mod.store().count_signals() == 1


def test_review_regressions(tmp_path):
    mod, orch = _load(tmp_path)
    R = lambda n, **kw: _run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))
    # the clock window wraps midnight, and a night keeps one name across it
    from datetime import datetime
    assert mod._in_window(23, 23, 7) and mod._in_window(3, 23, 7) and not mod._in_window(12, 23, 7)
    assert mod._night_of(datetime(2026, 10, 5, 2), 23) == mod._night_of(datetime(2026, 10, 4, 23), 23) == "2026-10-04"
    # a background step never brings a deleted topic back
    t = R("intel.topic.save", name="Gone soon")["topic"]
    R("intel.topic.delete", id=t["id"])
    _run(mod.sweep_topic(t))
    assert mod.store().get_topic(t["id"]) is None
    # a look-back report does not eat the next brief
    t = R("intel.topic.save", name="AI property valuation pilots", entities=["Acme"])["topic"]
    R("intel.sweep", topic_id=t["id"])
    for s in mod.store().signals(t["id"]):
        mod.store().put_signal(dict(s, score=9.0))
    assert not R("intel.report.build", topic_ids=t["id"], include_reported=True)["empty"]
    assert not R("intel.report.build", topic_ids=t["id"])["empty"]
    # a failed action can be tried again
    a = R("intel.action.list", status="offered")["rows"][0]
    mod.store().claim_action(a["token"])
    mod.store().finish_action(a["token"], "failed", {"error": "x"})
    assert mod.store().claim_action(a["token"])
