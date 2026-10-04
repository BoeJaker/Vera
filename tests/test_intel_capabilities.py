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
        return {"ranked": [{"index": i, "score": 4.0 if "pilot" in d.lower() else -6.0} for i, d in enumerate(documents)]}

    async def nlp_ner(text="", task="ner", labels=None, threshold=0.4, trace_id=None):
        return {"entities": [{"word": "Acme", "score": 0.9}, {"word": "Savills", "score": 0.8}]}

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
    assert roles["enrich"]["deny_gpu"] and "model" not in roles["enrich"]


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
    assert sn2["alert"] and sum(1 for n, _ in orch.CALLS if n == "tg.notify") == 1
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
    from datetime import datetime
    sess = mod.ops.active_session([{"name": "s", "days": list(range(7)), "start": "00:00", "end": "23:59", "budget": 3}],
                                  datetime(2026, 10, 4, 12, 0))
    planned = _run(mod._plan_session(sess, datetime(2026, 10, 4, 12, 0)))
    assert planned >= 2 and any(n == "research.run" for n, _ in orch.CALLS)
    assert _run(mod._plan_session(sess, datetime(2026, 10, 4, 12, 5))) <= 3 - planned


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
