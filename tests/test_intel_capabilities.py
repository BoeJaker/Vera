"""Intelligence capabilities end to end against a stand-in orchestration (no app,
no network, no model): topics are swept and triaged by a fake NLP tier, read and
tagged, summarised behind the model gate, assessed by the MoE role, alerted on,
batched at night into a brief, delivered with buttons; the drop folders, the
advisor, the command, sessions, config, the n8n import and the retire list."""
import asyncio
import contextvars
import importlib.util
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestration_stub import stubbed_orchestration  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _vec(text):
    """A toy embedding: stories about the same thing get the same vector."""
    t = text.lower()
    return [1.0 if "pilot" in t else 0.0, 1.0 if "repo" in t or "github" in t else 0.0,
            1.0 if "weather" in t else 0.0, 0.1]


def _load(tmp_path, *, nlp=True):
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}
    orch.EVENTS, orch.UI, orch.SCHEDULED, orch.PROMPTS, orch.SENT, orch.CALLS = [], [], [], [], [], []
    orch.PROFILES = {}
    orch.BACKGROUND_LLM = contextvars.ContextVar("bg", default="")
    orch.LONG_HORIZON_CPU_MODEL, orch.LONG_HORIZON_CPU_NODE = "qwen3.6:35b-a3b", "cpu-247"
    orch.LONG_HORIZON_CPU_OPTIONS = {"num_ctx": 16384, "keep_alive": "2h"}

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
        orch.PROMPTS.append((prompt, dict(kw, bg=orch.BACKGROUND_LLM.get())))
        if "running assessment" in prompt:
            n = sum(1 for p, _ in orch.PROMPTS if "running assessment" in p)
            head = "Pilots are going live" if n == 1 else "Three UK firms now run production AI valuations"
            return json.dumps({"headline": head, "state": "Firms run pilots.", "changed": ["Acme went live", "b", "c"],
                               "confidence": "medium" if n == 1 else "high", "new_questions": ["Who is next?"],
                               "watch": ["Acme"], "diagram": "flowchart LR\n A-->B"})
        if "top of today's intelligence brief" in prompt:
            return json.dumps({"top_line": ["Acme went live (https://news.example/acme-pilot)", "b", "c"],
                               "follow_ups": ["How accurate are AI valuations?"], "competitor_watch": "Savills moved."})
        if "NEW NOTE" in prompt:
            return json.dumps({"title": "GPU plan", "summary": "Buy a GPU.", "search_terms": ["v100 resale price"],
                               "action_items": ["price a GPU"]})
        if "suggestions" in prompt:
            return json.dumps({"suggestions": [{"title": "Watch the GPU", "category": "new", "rationale": "r",
                                                "steps": [{"name": "gate", "cap": "ollama.gate.status", "args": {}}]}]})
        return ('Thinking... {"summary": "Acme runs an AI valuation pilot.", "so_what": "First UK pilot.", '
                '"relevance": 8, "follow_up": ["How accurate is it?"]}')

    def register_routing_profile(name, **kw):
        orch.PROFILES[name] = kw
        return kw

    orch.APP = _App(); orch.capability = capability; orch.emit_event = emit_event
    orch.enum_schema = lambda **kw: {}
    orch.register_ui = lambda *a, **k: orch.UI.append((a, k))
    orch.schedule = lambda fn, interval, **k: orch.SCHEDULED.append((fn, interval, k))
    orch.ollama_generate = ollama_generate
    orch.register_routing_profile = register_routing_profile
    reg = orch.CAPABILITY_REGISTRY

    def rec(name):
        async def f(**kw):
            orch.CALLS.append((name, kw))
            return {"ok": True}
        return f

    async def web_search(query, limit=8, dataset_id="", discover="", trace_id=None):
        return {"results": [
            {"url": "https://news.example/acme-pilot?utm_source=x", "title": "Acme launches AI valuation pilot in UK property",
             "snippet": "Acme property valuation pilot with Savills watching"},
            {"url": "https://other.example/acme", "title": "Acme pilot: a property valuation first", "snippet": "the same pilot"},
            {"url": "https://github.com/acme/valuer", "title": "acme/valuer: AI valuation agent repo", "snippet": "code"},
            {"url": "https://x.example/a", "title": "Just a moment..."},
        ]}

    async def reader_page(url, max_chars=60000, trace_id=None):
        return {"ok": True, "title": "t", "markdown": "A long enough paragraph about the Acme pilot and its valuation "
                "results. Savills is watching closely. It launches in 2027.", "image": "https://news.example/i.png",
                "minutes": 2}

    async def nlp_embed(texts=None, trace_id=None):
        return {"embeddings": [_vec(t) for t in texts]}

    async def nlp_rerank(query="", documents=None, top_k=5, trace_id=None):
        return {"ranked": [{"index": i, "score": 4.0 if ("pilot" in d.lower() or "boe" in d.lower()) else -6.0} for i, d in enumerate(documents)]}

    async def nlp_ner(text="", task="ner", labels=None, threshold=0.4, trace_id=None):
        orch.CALLS.append(("nlp.ner", {"labels": list(labels or [])}))
        return {"entities": [{"word": "Acme", "entity": "company", "score": 0.9},
                             {"word": "Savills", "entity": "property agency or consultancy", "score": 0.8}]}

    async def nlp_qa(question="", context="", trace_id=None):
        return {"answer": "2027", "score": 0.7}

    async def nlp_classify(text="", task="classify", trace_id=None):
        return {"top": "positive"}

    async def tg_send_rich(text="", buttons=None, photo_url="", parse_mode="HTML", chat_id="", trace_id=None):
        orch.SENT.append({"text": text, "buttons": json.loads(buttons or "[]"), "photo": photo_url})
        return {"ok": True}

    async def research_run(query, mode="single", output_mode="report", trace_id=None):
        orch.CALLS.append(("research.run", {"query": query}))
        return {"job_id": "j1", "status": "queued"}

    async def research_status(job_id="", trace_id=None):
        return {"status": "done", "result": {"report": "Research found the answer: about 3% error."}}

    files = {"/n": [{"name": "plan.md", "type": "file", "size": 30}],
             "/p": [{"name": "todo.txt", "type": "file", "size": 5}]}
    contents = {"/n/plan.md": "Plan: buy a GPU for the lab", "/p/todo.txt": "print me"}

    async def fs_list(ssh_host_id="", path="", trace_id=None):
        return {"entries": files.get(path, [])}

    async def fs_read(ssh_host_id="", path="", max_bytes=0, trace_id=None):
        return {"ok": True, "text": contents.get(path, "")}

    async def n8n_list(limit=100, trace_id=None):
        return {"workflows": [{"id": "1", "name": "intel-night", "active": True}, {"id": "2", "name": "notify", "active": True},
                              {"id": "3", "name": "files-night", "active": False}]}

    async def n8n_create(name="", nodes=None, connections=None, settings=None, activate=False, trace_id=None):
        orch.CALLS.append(("n8n.workflow.create", {"name": name, "activate": activate}))
        return {"id": "d1"}

    async def fabric_browse(dataset_id, limit=200, offset=0, trace_id=None):
        data = {
            "vera.interests": [{"topic": "UK commercial real estate market", "active": True, "note": "sector", "created": "1"}],
            "vera.monitors": [{"topic": "UK commercial real estate market", "active": True, "created": "2"},
                              {"topic": "Data centre demand", "active": True, "created": "2"}],
            "intel.sources": [{"id": "s1", "topic": "ai_real_estate", "query": "AI valuation pilots", "added": "1"},
                              {"id": "s2", "topic": "ai_real_estate", "query": "", "added": "2"},
                              {"id": "s3", "topic": "notes", "query": "v100 resale", "added": "1"}],
            "intel.enrichments": [{"url": "https://a.example/x", "title": "Valuation model released", "topic": "ai_real_estate",
                                   "summary": "S", "relevance": 9, "enriched": True, "enriched_at": "2026-10-03T00:00:00Z",
                                   "competitor": "Savills"}],
            "intel.notes": [{"path": "/n/plan.md", "norm": "Plan: buy a GPU for the lab", "summary": "old", "updated": "1"}],
            "files.seen": [{"path": "/p/todo.txt", "hash": "1a6f417ab11736", "at": "1"}],   # n8n cyrb53 of "print me"
        }.get(dataset_id, [])
        return {"records": [{"data": d} for d in data], "has_more": False}

    async def markets_lookup(query="", exchange="binance", limit=12, trace_id=None):
        orch.CALLS.append(("markets.lookup", {"query": query}))
        if "savills" in query.lower():
            return {"results": [{"provider": "yahoo", "symbol": "SVS.L", "name": "Savills plc", "asset_class": "stock"}]}
        if "acme" in query.lower():                      # a name that only half-matches a listing
            return {"results": [{"provider": "yahoo", "symbol": "ACMR", "name": "ACM Research Inc", "asset_class": "stock"}]}
        return {"results": [{"provider": "yahoo", "symbol": "BTC-USD", "name": "Bitcoin USD", "asset_class": "crypto"}]}

    async def markets_bars(dataset_id="", limit=5000, start="", end="", trace_id=None):
        import math
        import time as _t
        now = int(_t.time()) // 86400 * 86400
        ts = [now - (119 - i) * 86400 for i in range(120)]
        cs = [100 + 0.6 * math.sin(i * 1.7) + 0.3 * math.cos(i * 0.9) for i in range(118)]
        cs += [cs[-1] * 1.14, cs[-1] * 1.15]                 # a 14% jump yesterday, and it holds
        orch.CALLS.append(("markets.bars", {"dataset_id": dataset_id}))
        if "stale" in dataset_id:                             # a dataset whose newest bar is years old
            return {"dataset_id": dataset_id, "count": 120, "t": [t - 3000 * 86400 for t in ts], "c": cs}
        order = list(range(60, 120)) + list(range(60))        # insert order is not bar order (RF-82)
        return {"dataset_id": dataset_id, "count": 121, "t": [583743600000] + [ts[i] for i in order],
                "c": [1.0] + [cs[i] for i in order]}

    async def macro_fetch(id="", ids=None, track=True, trace_id=None):
        return {"ok": True, "datasets": [f"mkt.macro.{id.lower()}.1d"]}

    reg["markets.lookup"] = {"func": markets_lookup, "raw": markets_lookup}
    reg["markets.bars"] = {"func": markets_bars, "raw": markets_bars}
    reg["markets.macro.fetch"] = {"func": macro_fetch, "raw": macro_fetch}
    reg["markets.asset.add"] = {"func": rec("markets.asset.add"), "raw": rec("markets.asset.add")}
    for n, f in (("web.search", web_search), ("reader.page", reader_page), ("tg.send_rich", tg_send_rich),
                 ("research.run", research_run), ("research.job.status", research_status), ("fabric.browse", fabric_browse),
                 ("fs.list", fs_list), ("fs.read", fs_read), ("n8n.workflow.list", n8n_list),
                 ("n8n.workflow.create", n8n_create), ("tg.notify", rec("tg.notify")), ("print.text", rec("print.text")),
                 ("ha.notify", rec("ha.notify")), ("background.cap.enqueue", rec("background.cap.enqueue")),
                 ("n8n.workflow.activate", rec("n8n.workflow.activate")), ("integration.list", rec("integration.list")),
                 ("report.html", rec("report.html"))):
        reg[n] = {"func": f, "raw": f}
    if nlp:
        for n, f in (("nlp.embed", nlp_embed), ("nlp.rerank", nlp_rerank), ("nlp.ner", nlp_ner), ("nlp.qa", nlp_qa),
                     ("nlp.classify", nlp_classify)):
            reg[n] = {"func": f, "raw": f}

    spec = importlib.util.spec_from_file_location("intel_under_test", os.path.join(ROOT, "vera", "intel", "intel_capabilities.py"))
    with stubbed_orchestration(orch):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    from vera.intel.intel_store import IntelStore
    mod._STORE = IntelStore(tmp_path / "intel.db")
    mod.store().set_kv("config", {"competitors": ["Savills"], "files_host": "h",
                                  "drop_folders": [{"kind": "notes", "path": "/n"}, {"kind": "print", "path": "/p"}]})

    async def run_inline(mode, title, *, key, payload=None, priority=None):      # the queue, run at once
        orch.CALLS.append(("submit", {"mode": mode, "key": key, "payload": payload}))
        return await mod._intel_job({"payload": dict(payload or {}, mode=mode)}, None)
    mod._submit = run_inline
    return mod, orch


def _run(coro):
    return asyncio.run(coro)


def _R(orch, n, **kw):
    return _run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))


def test_registers_caps_panel_tick_and_profile(tmp_path):
    mod, orch = _load(tmp_path)
    for name in ("intel.topic.save", "intel.sweep", "intel.timeline.get", "intel.report.build", "intel.deliver",
                 "intel.action.run", "intel.brief", "intel.ingest_dream", "intel.import.n8n", "intel.n8n.retire",
                 "intel.panel.html", "intel.summary", "intel.entities.get", "intel.asof.get", "intel.command",
                 "intel.save", "intel.files.run", "intel.advise", "intel.session.status", "intel.notes.list"):
        assert name in orch.CAPABILITY_REGISTRY, name
    assert orch.UI and orch.UI[0][0][0] == "intel" and "<iframe" in orch.UI[0][1]["html"]
    fn, interval, kw = orch.SCHEDULED[0]
    assert kw.get("singleton") and kw.get("skip_in_sandbox")
    roles = orch.PROFILES["intel"]["roles"]
    assert roles["assess"]["model"] == "qwen3.6:35b-a3b" and roles["assess"]["pin"] == "cpu-247" and roles["assess"]["deny_gpu"]
    assert roles["enrich"]["deny_gpu"] and roles["enrich"]["model"] == "qwen3.6:35b-a3b" and roles["notes"]["pin"] == "cpu-247"


def test_the_whole_cycle(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", brief="AI valuation in UK property",
           questions="When does it launch?\nHow accurate?", entities=["Acme"], depth="deep")["topic"]
    sw = _R(orch, "intel.sweep", topic_id=t["id"])["swept"][0]
    assert sw["new"] == 2 and sw["joined"] == 1 and sw["nlp"]          # the second pilot story joins the first
    pilot = next(s for s in mod.store().signals(t["id"]) if "pilot" in s["title"].lower())
    repo = next(s for s in mod.store().signals(t["id"]) if "repo" in s["title"].lower())
    assert pilot["score"] > repo["score"] and pilot["score"] >= 5                # the reranker leads
    assert "https://other.example/acme" in (mod.store().get_signal(pilot["id"]).get("also") or [])

    en = _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    assert en["enriched"] == 1 and en["nlp_only"] == 1                        # the repo is below the gate: NLP only
    p2 = mod.store().get_signal(pilot["id"])
    assert p2["entities"] == ["Acme", "Savills"] and p2["evidence"][0]["answer"] == "2027"
    assert p2["entity_types"]["Savills"] == "property agency or consultancy"
    assert "planning authority" in [c for c in orch.CALLS if c[0] == "nlp.ner"][-1][1]["labels"]   # a property topic
    assert p2["competitors"] == ["Savills"] and p2["stance"] == "positive" and p2["summary"].startswith("Acme runs")
    r2 = mod.store().get_signal(repo["id"])
    assert r2["nlp_only"] and r2["enriched"] and r2["summary"]
    model_calls = [kw for _, kw in orch.PROMPTS]
    assert all(kw["prefer_gpu"] is False and kw["think"] is False and kw["job_type"] == "idle_intel" for kw in model_calls)
    assert model_calls[0]["role"] == "enrich" and model_calls[0]["profile"] == "intel"
    assert model_calls[0]["bg"] == "intel:enrich" and "Bidwells" in model_calls[0]["system"]

    sn = _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    assert sn["snapshot"]["headline"] == "Pilots are going live" and sn["alert"] == ""     # a first assessment is not news
    assert orch.PROMPTS[-1][1]["role"] == "assess"
    assert "2027" in orch.PROMPTS[-1][0]                                       # extractive answers ride along
    # a second assessment that moves: one alert, once
    mod.store().put_signal(dict(mod.store().get_signal(pilot["id"]), seen="2099-01-01T00:00:00Z"))
    sn2 = _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    assert sn2.get("alert"), sn2
    assert sum(1 for n, _ in orch.CALLS if n == "tg.notify") == 1
    assert "Who is next?" in [w["query"] for w in mod.store().get_kv("research_wanted")]   # deep: queued for a session

    tl = _R(orch, "intel.timeline.get", topic_id=t["id"], days=7)
    assert tl["days"][-1]["signals"] >= 1


def test_night_batch_brief_and_delivery(tmp_path):
    mod, orch = _load(tmp_path)
    _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])
    _run(mod._start_night("2026-10-04"))
    night = mod.store().get_kv("night")
    assert night["pending"] == [] and night["brief"] not in ("", "-")
    kinds = [c[1]["mode"] for c in orch.CALLS if c[0] == "submit"]
    assert kinds[0] == "files" and "topic" in kinds
    assert any(kw.get("role") == "brief" for _, kw in orch.PROMPTS)           # the MoE wrote the top line, at night
    doc = mod.store().get_report(night["brief"], with_html=True)
    assert doc["top_line"][0].startswith("Acme went live") and doc["follow_up_actions"]
    assert doc["competitors"] and doc["competitors"][0]["firm"] == "Savills"
    assert doc["notes"] and "NEW note" in doc["notes"][0]
    assert "Top line" in doc["_html"] and "Competitor watch" in doc["_html"] and "From your notes" in doc["_html"]
    assert mod.store().get_kv("pending_brief")["id"] == night["brief"]
    assert any(n == "tg.notify" and "Intel night" in kw["text"] for n, kw in orch.CALLS)
    # morning: delivered to the n8n fan-out, never generated
    n_prompts = len(orch.PROMPTS)
    res = _run(mod.deliver_report(night["brief"], ["telegram", "phone", "print", "podcast"]))["results"]
    assert all(r["ok"] for r in res) and len(orch.PROMPTS) == n_prompts
    phone = next(kw for n, kw in orch.CALLS if n == "ha.notify")
    assert phone["target"] == "mobile_app_smasnug" and phone["data"]["url"].endswith(night["brief"])
    printed = [kw for n, kw in orch.CALLS if n == "print.text"]
    assert printed[-1]["printer_id"] == "prn_d332ecadad" and len(printed[-1]["text"]) <= 620
    pod = next(kw for n, kw in orch.CALLS if n == "background.cap.enqueue" and kw.get("name") == "podcast.generate")
    assert pod["arguments"]["show_name"] == "Vera Signal"
    assert orch.SENT[-1]["text"].count("▸") == 3                          # the top line once, not the headlines too


def test_drop_folders(tmp_path):
    mod, orch = _load(tmp_path)
    r = _run(mod.run_drop_folders(None))
    assert r["notes"] == 1 and r["printed"] == 1
    nt = mod.store().get_topic("from-your-notes")
    assert "v100 resale price" in nt["queries"]
    assert any(kw.get("role") == "notes" for _, kw in orch.PROMPTS)
    n = len(orch.PROMPTS)
    r = _run(mod.run_drop_folders(None))                                       # nothing changed: nothing read, nothing printed
    assert r["notes"] == 0 and r["printed"] == 0 and len(orch.PROMPTS) == n
    rows = _R(orch, "intel.notes.list")["rows"]
    assert rows[0]["title"] == "GPU plan" and "norm" not in rows[0]


def test_advisor_command_save_and_sessions(tmp_path):
    mod, orch = _load(tmp_path)
    r = _run(mod.run_advisor())
    assert r["ok"] and r["proposals"] == 1
    made = [kw for n, kw in orch.CALLS if n == "n8n.workflow.create"]
    assert made and made[0]["activate"] is False and made[0]["name"].startswith("draft-")
    assert _R(orch, "intel.command", text="add GPU market :: v100 prices")["ok"]
    assert "gpu-market" in _R(orch, "intel.command", text="list")["text"]
    assert "paused" in _R(orch, "intel.command", text="remove gpu-market")["text"]
    assert "commands" in _R(orch, "intel.command", text="dance")["text"]
    s = _R(orch, "intel.save", url="https://news.example/read-me")
    assert s["ok"] and mod.store().get_topic("saved") and s["signal"]["entities"] == ["Acme", "Savills"]
    assert _R(orch, "intel.save", url="javascript:alert(1)")["error"]
    # a session plans research and topic work with its end time attached
    mod.store().set_kv("research_wanted", [{"topic_id": "x", "query": "Who is next?"}])
    _R(orch, "intel.topic.save", name="Topic one")
    from datetime import datetime, timedelta
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)   # today: queued work past its
    sess = mod.ops.active_session([{"name": "s", "days": list(range(7)), "start": "00:00", "end": "23:59", "budget": 3}],
                                  noon)                                       # window's end is dropped by the clock
    planned = _run(mod._plan_session(sess, noon))
    assert planned >= 2 and any(n == "research.run" for n, _ in orch.CALLS)
    assert _run(mod._plan_session(sess, noon + timedelta(minutes=5))) <= 3 - planned


def test_config_import_retire(tmp_path):
    mod, orch = _load(tmp_path)
    r = _R(orch, "intel.config.set", settings={"sessions": [{"name": "sat", "days": [5], "start": "09:00", "end": "13:00"},
                                                            {"start": "bad"}], "bogus": 1, "llm_gate": "6.5",
                                               "brief_channels": ["telegram", "nope"]})
    assert r["config"]["sessions"][0]["budget"] == 6 and len(r["config"]["sessions"]) == 1
    assert r["config"]["llm_gate"] == 6.5 and r["config"]["brief_channels"] == ["telegram"] and "bogus" not in r["config"]
    dry = _R(orch, "intel.import.n8n")
    names = {t["name"]: t for t in dry["topics"]}
    assert names["UK commercial real estate market"]["depth"] == "deep" and "Data centre demand" in names
    assert "AI in real estate, labs & industrial" in names and dry["competitors"] == ["Savills"]
    assert mod.store().topics() == []
    _R(orch, "intel.import.n8n", dry_run=False)
    assert mod.store().get_topic("from-your-notes")["queries"] == ["v100 resale"]
    assert mod.store().get_kv("note:/n/plan.md")["summary"] == "old"
    assert "Savills" in mod.config()["competitors"]
    r = _run(mod.run_drop_folders(None))
    assert r["notes"] == 0 and r["printed"] == 0                                # files-night already handled both
    ret = _R(orch, "intel.n8n.retire")
    assert [w["name"] for w in ret["workflows"]] == ["intel-night", "files-night"] and "notify" not in str(ret["workflows"])
    assert ret["workflows"][0]["action"] == "would deactivate" and "vera-tag" in ret["missing"]
    _R(orch, "intel.n8n.retire", dry_run=False)
    assert ("n8n.workflow.activate", {"id": "1", "active": False}) in orch.CALLS


def test_without_the_nlp_tier_it_still_works(tmp_path):
    mod, orch = _load(tmp_path, nlp=False)
    t = _R(orch, "intel.topic.save", name="Acme valuation pilot", entities=["Acme"])["topic"]
    sw = _R(orch, "intel.sweep", topic_id=t["id"])["swept"][0]
    assert sw["new"] >= 2 and not sw["nlp"]
    en = _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    assert en["enriched"] + en["nlp_only"] >= 1


def test_nightly_job_yields_when_told(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="Topic one")["topic"]

    async def busy():
        return "someone is using the box"
    r = _run(mod._intel_job({"payload": {"mode": "topic", "topic_id": t["id"]}}, busy))
    assert r["yielded"].startswith("pre-empted") and "using the box" in r["yielded"]

    async def own_gate():
        return "gate held by idle_intel"
    import time as _t
    mod._OWN["llm_ended"] = _t.time()
    r = _run(mod._intel_job({"payload": {"mode": "topic", "topic_id": t["id"]}}, own_gate))
    assert r.get("ok"), r                                                      # our own hold is not a reason to stop
    r = _run(mod._intel_job({"payload": {"mode": "topic", "topic_id": t["id"], "until": "2000-01-01T00:00:00Z"}}, None))
    assert r["skipped"] == "its session ended"


def test_review_regressions(tmp_path):
    mod, orch = _load(tmp_path)
    from datetime import datetime
    assert mod._in_window(23, 23, 7) and mod._in_window(3, 23, 7) and not mod._in_window(12, 23, 7)
    assert mod._night_of(datetime(2026, 10, 5, 2), 23) == mod._night_of(datetime(2026, 10, 4, 23), 23) == "2026-10-04"
    t = _R(orch, "intel.topic.save", name="Gone soon")["topic"]
    _R(orch, "intel.topic.delete", id=t["id"])
    _run(mod.sweep_topic(t))
    assert mod.store().get_topic(t["id"]) is None
    t = _R(orch, "intel.topic.save", name="AI property valuation pilots", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    for s in mod.store().signals(t["id"]):
        mod.store().put_signal(dict(s, score=9.0))
    assert not _R(orch, "intel.report.build", topic_ids=t["id"], include_reported=True)["empty"]
    assert not _R(orch, "intel.report.build", topic_ids=t["id"])["empty"]
    assert _R(orch, "intel.report.build", topic_ids=t["id"])["empty"]
    a = _R(orch, "intel.action.list", status="offered")["rows"][0]
    mod.store().claim_action(a["token"])
    mod.store().finish_action(a["token"], "failed", {"error": "x"})
    assert mod.store().claim_action(a["token"])


def test_phase1_review_regressions(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    # a night unit the queue refuses (already queued) is not waited on: the brief still gets written
    async def refuse(mode, title, *, key, payload=None, priority=None):
        orch.CALLS.append(("submit", {"mode": mode, "key": key}))
        if mode == "topic":
            return {"ok": True, "queued": False, "message": "not queued: already queued"}
        return await mod._intel_job({"payload": dict(payload or {}, mode=mode)}, None)
    mod._submit = refuse
    _run(mod._start_night("2026-10-05"))
    n = mod.store().get_kv("night")
    assert n["pending"] == [] and n["brief"]
    assert any(c[1]["mode"] == "brief" for c in orch.CALLS if c[0] == "submit")          # the brief is a queue unit
    # a failing unit still clears the batch
    mod.store().set_kv("night", {"id": "N", "pending": [t["id"]], "stats": {}, "brief": ""})
    async def boom(*a, **k):
        raise RuntimeError("x")
    real = mod.sweep_topic
    mod.sweep_topic = boom
    r = _run(mod._intel_job({"payload": {"mode": "topic", "topic_id": t["id"], "batch": "N"}}, None))
    mod.sweep_topic = real
    assert r["ok"] is False and mod.store().get_kv("night")["pending"] == []
    # the night command never clobbers a running batch
    mod.store().set_kv("night", {"id": "N2", "pending": ["x"], "brief": ""})
    assert not _R(orch, "intel.command", text="night")["ok"]
    # a person on the gate always stops us; our own hold does not
    import time as _t
    mod._OWN["llm_ended"] = _t.time()
    async def person():
        return "the GPU gate is held by chat:user"
    async def ours():
        return "the GPU gate is held by intel:enrich"
    assert _run(mod._stop_reason(person)) and not _run(mod._stop_reason(ours))
    # refused opportunistic submits do not burn the day's budget
    async def refuse_all(mode, title, *, key, payload=None, priority=None):
        return {"ok": True, "queued": False}
    mod._submit = refuse_all
    _R(orch, "intel.topic.save", name="Fast topic", cadence="twice-daily")
    for _ in range(3):
        _run(mod._tick())
    assert int((mod.store().get_kv("budget") or {}).get("opportunistic", 0)) == 0


def test_an_empty_brief_is_never_stored(tmp_path):
    mod, orch = _load(tmp_path)
    r = _R(orch, "intel.report.build")
    assert r["empty"] and _R(orch, "intel.report.list")["rows"] == []


def test_vendor_pages_are_demoted_and_not_read_by_the_model(tmp_path):
    mod, orch = _load(tmp_path)
    lab = mod.ops.GENRE_LABELS

    async def zeroshot(text="", labels=None, multi_label=False, trace_id=None):
        g = "marketing" if "launches" in text.lower() else "news"     # a relevant page that is really an advert
        return {"labels": [{"label": lab[g], "score": 0.9}]}
    orch.CAPABILITY_REGISTRY["nlp.zeroshot"] = {"func": zeroshot, "raw": zeroshot}
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    sw = _R(orch, "intel.sweep", topic_id=t["id"])["swept"][0]
    assert sw["demoted"] == 1
    sigs = {s["title"]: s for s in mod.store().signals(t["id"])}
    repo = next(s for k, s in sigs.items() if "launches" in k.lower())
    assert repo["genre"] == "marketing"
    mod.store().put_signal(dict(repo, score=9.5))               # even a high score does not buy a model read
    _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    assert mod.store().get_signal(repo["id"])["nlp_only"]
    # a topic that wants vendors keeps them
    _R(orch, "intel.topic.save", id=t["id"], name=t["name"], vendors=True)
    assert mod.store().get_topic(t["id"])["vendors"] is True


def test_decisions_feedback_links_and_export(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    rows = mod.store().decisions(limit=100)
    qs = {r["question_id"] for r in rows}
    assert {"intel.signal_relevant", "intel.enrich_worth"} <= qs
    assert all(r["schema"] == "vera.decision/v0" and r["p"] is None and r["state_ref"]["kind"] == "signal" for r in rows)
    sig = next(s for s in mod.store().signals(t["id"]) if "pilot" in s["title"].lower())
    # a link open counts, then redirects to the stored url only
    resp = _run(mod._go_signal(sig["id"]))
    assert resp.status_code == 302 and resp.headers["location"] == sig["url"]
    assert _run(mod._go_signal("deadbeef")).status_code == 404
    # explicit feedback, and mute_site keeps the site out of the next sweep
    assert _R(orch, "intel.feedback", signal_id=sig["id"], verdict="useful")["outcomes"] >= 2
    repo = next(s for s in mod.store().signals(t["id"]) if "repo" in s["title"].lower())
    _R(orch, "intel.feedback", signal_id=repo["id"], verdict="mute_site")
    assert "github.com" in mod.store().get_topic(t["id"])["mute_sites"]
    stats = _R(orch, "intel.decisions.stats")
    sr = stats["questions"]["intel.signal_relevant"]
    assert sr["with_outcome"] >= 2 and sr["precision"] is not None
    assert any(k.startswith("nlp.") for day in stats["spend"].values() for k in day)
    assert any(k.startswith("model.") for day in stats["spend"].values() for k in day)
    ex = _R(orch, "intel.decisions.export")
    assert ex["decisions"] and ex["outcomes"] and "outcomes" not in ex["decisions"][0]
    assert {o["source"] for o in ex["outcomes"]} >= {"link.opened", "user.feedback.useful", "user.feedback.mute_site"}
    # a second sweep with the site muted brings no github hit back
    mod.store().delete_topic(t["id"], purge=True)
    mod.store().put_topic(dict(t, mute_sites=["github.com"]))
    _R(orch, "intel.sweep", topic_id=t["id"])
    assert not any("github" in (s.get("url") or "") for s in mod.store().signals(t["id"]))


def test_indicators_find_moves_corroborate_and_reach_the_assessment(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    add = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="price", query="bitcoin")
    assert add["ok"] and add["indicator"]["dataset_id"] == "mkt.yahoo.btc_usd.1d"
    assert ("markets.asset.add", {"provider": "yahoo", "symbol": "BTC-USD", "timeframes": ["1d"]}) in orch.CALLS
    assert _R(orch, "intel.indicator.add", topic_id=t["id"], kind="macro", query="CPIAUCSL")["indicator"]["dataset_id"]         == "mkt.macro.cpiaucsl.1d"
    assert "error" in _R(orch, "intel.indicator.add", topic_id=t["id"], kind="probability", query="x")
    # a first assessment, then a sweep brings a story the day the number jumps
    _R(orch, "intel.sweep", topic_id=t["id"])
    _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    ch = _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"]
    btc = [c for c in ch if c["label"] == "Bitcoin USD"]
    assert btc and btc[0]["direction"] == "up" and "Bitcoin USD up" in btc[0]["text"]
    assert btc[0]["stories"] and "pilot" in btc[0]["stories"][0]["title"].lower()
    assert not _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"]      # found once, not every night
    lst = _R(orch, "intel.indicator.list", topic_id=t["id"])
    assert lst["indicators"][0]["status"] == "ok" and lst["changes"]
    # the move goes into the next assessment prompt and makes it material
    mod.store().set_kv("alerts_sent", [])
    sn = _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    assert "THE NUMBERS THIS TOPIC WATCHES MOVED" in orch.PROMPTS[-1][0] and "Bitcoin USD up" in orch.PROMPTS[-1][0]
    assert "Bitcoin USD up" in sn["alert"]
    # the timeline and the report show it
    tl = _R(orch, "intel.timeline.get", topic_id=t["id"], days=30)
    assert any(e["kind"] == "change" for e in tl["events"]) and sum(d["changes"] for d in tl["days"]) >= 1
    doc = _run(mod.build_report([t["id"]], period_hours=24 * 30, only_new=False))
    html = mod.core.render_html(doc)
    assert "What moved" in html and "Bitcoin USD up" in html
    assert "**What moved**" in mod.core.render_markdown(doc)
    assert "Bitcoin USD up" in mod.core.render_telegram(doc)["text"]
    # stale data is never news
    st_ind = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="price", dataset_id="mkt.x.stale.1d", label="Old")
    assert not [c for c in _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"] if c["label"] == "Old"]
    old = next(i for i in _R(orch, "intel.indicator.list", topic_id=t["id"])["indicators"] if i["label"] == "Old")
    assert old["status"].startswith("stale") and st_ind["ok"]
    # removing stops it, and forgets its moves
    ind = lst["indicators"][0]["id"]
    assert _R(orch, "intel.indicator.remove", topic_id=t["id"], indicator_id=ind)["ok"]
    assert all(i["id"] != ind for i in _R(orch, "intel.indicator.list", topic_id=t["id"])["indicators"])
    assert not [c for c in mod.store().get_kv("ind_changes", []) if c.get("indicator") == ind]


def test_prediction_markets_search_suggest_watch_and_move(tmp_path):
    import time as _t
    mod, orch = _load(tmp_path)
    now = int(_t.time()) // 3600 * 3600
    gamma = {"events": [
        {"title": "BoE rate decision", "slug": "boe-dec", "closed": False, "markets": [
            {"id": "11", "question": "Will the BoE cut rates in December?", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.31", "0.69"]', "clobTokenIds": '["tokYes", "tokNo"]', "volumeNum": "5000",
             "active": True, "closed": False, "endDate": "2026-12-20T00:00:00Z"},
            {"id": "12", "question": "Will the BoE cut rates in August?", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0", "1"]', "clobTokenIds": '["a", "b"]', "closed": True}]}]}
    hist = {"history": [{"t": now - (199 - i) * 3600, "p": 0.31 + 0.002 * ((i * 7) % 3)} for i in range(196)]
            + [{"t": now - (199 - i) * 3600, "p": 0.54} for i in range(196, 200)]}
    seen = []

    async def http_json(url, params=None, timeout=20.0):
        seen.append((url, dict(params or {})))
        if url.endswith("/public-search"):
            return gamma
        if url.endswith("/search-markets"):
            return [{"id": "mf1", "question": "BoE cut rates in December 2026?", "outcomeType": "BINARY",
                     "probability": 0.4, "volume": 300, "closeTime": 1798761540000, "url": "https://manifold.markets/x"},
                    {"id": "mf2", "question": "Resolved one", "outcomeType": "BINARY", "isResolved": True}]
        if url.endswith("/markets/11"):
            return gamma["events"][0]["markets"][0]
        if url.endswith("/prices-history"):
            return hist
        raise AssertionError(url)
    mod._http_json = http_json
    t = _R(orch, "intel.topic.save", name="UK rates", questions="Will the BoE cut rates in December?")["topic"]
    rows = _R(orch, "intel.pm.search", query="BoE December cut")["rows"]
    assert {r["source"] for r in rows} == {"polymarket", "manifold"} and all(r["id"] not in ("12", "mf2") for r in rows)
    pmk = next(r for r in rows if r["source"] == "polymarket")
    assert pmk["token"] == "tokYes" and pmk["prob"] == 0.31 and pmk["url"] == "https://polymarket.com/event/boe-dec"
    sug = _R(orch, "intel.pm.suggest", topic_id=t["id"])["suggestions"]
    assert sug and sug[0]["question"].startswith("Will the BoE")
    # watching needs a market id; a bare query gives candidates instead
    bad = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="probability", query="BoE cut")
    assert "market_source" in bad["error"] and bad["candidates"]
    ind = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="probability", market_source="polymarket",
             market_id="11")["indicator"]
    assert ind["dataset_id"] == "pm:polymarket:tokYes" and ind["market"]["token"] == "tokYes"
    ch = _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"]
    assert ch and ch[0]["direction"] == "up" and "23 points" in ch[0]["text"]
    assert any(u.endswith("/prices-history") and p.get("market") == "tokYes" for u, p in seen)
    again = _R(orch, "intel.pm.suggest", topic_id=t["id"])["suggestions"]
    assert all(m["id"] != "11" for x in again for m in x["markets"])           # a watched market is not suggested again
    # its level reaches the next assessment; then the market resolves
    ind_now = _R(orch, "intel.indicator.list", topic_id=t["id"])["indicators"][0]
    assert ind_now["chg7"] is not None and ind_now["last"] == 0.54
    _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    assert "THE NUMBERS THIS TOPIC WATCHES, NOW" in orch.PROMPTS[-1][0] and "54%" in orch.PROMPTS[-1][0]
    gamma["events"][0]["markets"][0].update(closed=True, outcomePrices='["1", "0"]')
    res = _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"]
    assert [c["text"] for c in res] == ["Will the BoE cut rates in December? resolved YES"]
    lst = _R(orch, "intel.indicator.list", topic_id=t["id"])
    assert lst["indicators"][0]["status"] == "resolved yes" and lst["resolutions"][0]["answer"] == "yes"
    assert lst["resolutions"][0]["assessment"]["confidence"]
    assert not _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"]          # settled once


def test_indicator_suggest_from_named_companies_and_markets(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    for i, s in enumerate(mod.store().signals(t["id"])):     # Savills named in two sources
        mod.store().put_signal(dict(s, entities=["Savills", "Acme"],
                                    entity_types={"Savills": "property agency or consultancy", "Acme": "company"}))

    async def http_json(url, params=None, timeout=20.0):
        if url.endswith("/public-search"):
            return {"events": [{"title": "AI valuation", "slug": "ai-val", "markets": [
                {"id": "77", "question": "Will an AI valuation pilot go live in the UK?", "outcomes": '["Yes", "No"]',
                 "outcomePrices": '["0.6", "0.4"]', "clobTokenIds": '["t77", "n77"]', "volumeNum": "100"}]}]}
        return []
    mod._http_json = http_json
    sug = _R(orch, "intel.indicator.suggest", topic_id=t["id"])
    assert [p["symbol"] for p in sug["prices"]] == ["SVS.L"]           # Acme does not match "ACM Research"
    assert sug["prices"][0]["mentions"] >= 2 and sug["markets"][0]["id"] == "77"
    n_lookups = len([c for c in orch.CALLS if c[0] == "markets.lookup"])
    _R(orch, "intel.indicator.suggest", topic_id=t["id"])
    assert len([c for c in orch.CALLS if c[0] == "markets.lookup"]) == n_lookups      # remembered
    add = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="price", provider="yahoo", symbol="SVS.L", label="Savills plc")
    assert add["indicator"]["dataset_id"] == "mkt.yahoo.svs_l.1d" and add["indicator"]["label"] == "Savills plc"
    assert not _R(orch, "intel.indicator.suggest", topic_id=t["id"])["prices"]          # watched: not suggested again


def test_the_brief_offers_suggested_numbers_as_watch_buttons(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Savills"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])

    async def http_json(url, params=None, timeout=20.0):
        return {"events": []} if url.endswith("/public-search") else []
    mod._http_json = http_json
    _run(mod._refresh_suggestions(mod.store().get_topic(t["id"])))
    assert mod.store().get_kv(f"ind_suggest:{t['id']}")["prices"][0]["symbol"] == "SVS.L"
    doc = _run(mod.build_report([t["id"]], period_hours=24))
    acts = [a for sec in doc["sections"] if sec.get("kind") == "topic" for p in sec["parts"] if p["kind"] == "actions"
            for a in p["items"] if a["kind"] == "watch"]
    assert len(acts) == 1 and acts[0]["label"] == "Watch Savills plc"
    stored = mod.store().get_action(acts[0]["token"])
    assert mod.core.action_args(stored) == {"topic_id": t["id"], "kind": "price", "provider": "yahoo",
                                            "symbol": "SVS.L", "label": "Savills plc"}
    # pressing it watches the number; the next brief does not offer it again
    r = _R(orch, "intel.action.run", token=acts[0]["token"])
    assert r.get("ok") and not r.get("error"), r
    assert [i["dataset_id"] for i in _R(orch, "intel.indicator.list", topic_id=t["id"])["indicators"]] == ["mkt.yahoo.svs_l.1d"]
    assert not mod._watch_actions(mod.store().get_topic(t["id"]))
    # a forged target never becomes an action
    assert mod.core.make_action("watch", topic_id=t["id"], target="price:yahoo:SVS.L; rm -rf /") is None
    assert mod.core.make_action("watch", topic_id=t["id"], target="http://evil/") is None


def test_decisions_and_outcomes_are_mirrored_to_the_shared_ledger(tmp_path):
    mod, orch = _load(tmp_path)
    got = []

    async def decide_record(row=None, trace_id=None):
        got.append(row)
        return {"ok": True}
    orch.CAPABILITY_REGISTRY["decide.record"] = {"func": decide_record, "raw": decide_record}
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]

    async def flow():
        await orch.CAPABILITY_REGISTRY["intel.sweep"]["func"](topic_id=t["id"])
        sig = next(s for s in mod.store().signals(t["id"]) if "pilot" in s["title"].lower())
        await orch.CAPABILITY_REGISTRY["intel.feedback"]["func"](signal_id=sig["id"], verdict="useful")
        await asyncio.sleep(0)
        await asyncio.gather(*list(mod._LEDGER_TASKS))
    _run(flow())
    kinds = {r.get("schema") for r in got}
    assert {"vera.decision/v0", "vera.outcome/v0"} <= kinds
    assert len([r for r in got if r["schema"] == "vera.decision/v0"]) == len(mod.store().decisions(limit=500))
