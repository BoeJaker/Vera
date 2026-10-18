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

    async def fabric_browse(dataset_id, limit=200, offset=0, lite=False, trace_id=None):
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
        page = data[offset:offset + limit]
        return {"records": [{"id": d.get("_id", "fabric-row"), "data": d} for d in page],
                "total": len(data), "has_more": offset + limit < len(data)}

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

    async def no_network(params):                 # tests never reach Wikidata; a test that needs it fakes it
        raise mod._Throttled("no network in tests")
    mod._wd = no_network
    mod.WD_SPACING_S = 0.0
    # these tests drive sweeps themselves; a new topic's immediate first sweep (SK-3.6) is tested on its own
    mod.store().set_kv("config", dict(mod.store().get_kv("config", {}) or {}, first_sweep=False))
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
                 "intel.save", "intel.files.run", "intel.advise", "intel.session.status", "intel.notes.list",
                 "intel.models", "intel.effects", "intel.records", "intel.replay.evaluate"):
        assert name in orch.CAPABILITY_REGISTRY, name
    assert orch.UI and orch.UI[0][0][0] == "intel" and "<iframe" in orch.UI[0][1]["html"]
    fn, interval, kw = orch.SCHEDULED[0]
    assert kw.get("singleton") and kw.get("skip_in_sandbox")
    roles = orch.PROFILES["intel"]["roles"]
    assert roles["assess"]["model"] == "qwen3.6:35b-a3b" and roles["assess"]["pin"] == "cpu-247" and roles["assess"]["deny_gpu"]
    assert roles["enrich"]["deny_gpu"] and roles["enrich"]["model"] == "qwen3.6:35b-a3b" and roles["notes"]["pin"] == "cpu-247"


def test_replay_evaluation_is_offline_aggregate_only(tmp_path):
    _mod, orch = _load(tmp_path)
    before = list(orch.CALLS)
    result = _R(orch, "intel.replay.evaluate")
    assert result["ok"] is True
    assert result["accuracy"] == 1.0
    assert result["coverage"] == 1.0
    assert result["deterministic"] is True
    assert result["failed_case_ids"] == []
    assert "actual" not in result and "expected" not in result
    assert orch.CALLS == before


def test_model_inventory_joins_intel_consumers_without_executing_models(tmp_path):
    mod, orch = _load(tmp_path)

    async def callable_models(**kw):
        assert kw == {"include_llms": False, "measure": False}
        return {"models": [{"id": "nlp.ner:gliner", "cap": "nlp.ner",
                            "selector": {"task": "gliner"}, "model": "gliner/model",
                            "package_id": "pkg:gliner", "present_on": ["cpu-246"],
                            "loaded_on": ["cpu-246"]}]}

    orch.CAPABILITY_REGISTRY["model.callable"] = {"func": callable_models}
    orch.OLLAMA_INSTANCES = {"cpu-247": {"models": ["qwen3.6:35b-a3b"],
                                         "running": [{"name": "qwen3.6:35b-a3b"}]}}
    orch.MODEL_TAGS = {}
    orch._effective_role_profiles = lambda: {"intel": {"name": "intel", "roles": {
        "assess": {"model": "qwen3.6:35b-a3b", "pin": "cpu-247",
                   "job_type": "idle_intel", "deny_gpu": True}}}}
    result = _R(orch, "intel.models")
    assert result["schema"] == "vera.intel-model-consumers/v1"
    assert next(row for row in result["nlp"] if row["consumer_id"] == "intel.enrich.entities")["status"] == "verified"
    assert result["roles"][0]["present_on"] == ["cpu-247"]
    assert orch.PROMPTS == []


def test_effect_inventory_is_read_only_and_reports_attribution_gap(tmp_path):
    _mod, orch = _load(tmp_path)
    before = list(orch.CALLS)
    result = _R(orch, "intel.effects")
    assert result["schema"] == "vera.intel-effect-inventory/v1"
    assert result["readiness"]["intel_attributable"] is False
    assert "intel_attribution_missing" in result["gaps"]
    assert result["claims"] == {"executes": False, "probes": False,
                                "records_evidence": False, "changes_enforcement": False,
                                "retains_payload": False}
    assert orch.CALLS == before


def test_intel_call_context_attributes_downstream_effect_observations(tmp_path):
    mod, orch = _load(tmp_path)

    async def probe():
        return {"source": mod.effect_context.current_source()}

    orch.CAPABILITY_REGISTRY["test.effect.probe"] = {"func": probe, "raw": probe}
    assert _run(mod._call("test.effect.probe")) == {"source": "intel"}
    assert mod.effect_context.current_source() == ""


def test_effect_inventory_reads_only_intel_attributed_provider_evidence(tmp_path):
    _mod, orch = _load(tmp_path)
    seen = []

    async def evidence(family="", source="", limit=0):
        seen.append((family, source, limit))
        return {"totals": {"observations": 2 if family == "telegram" else 1},
                "source_filter": source}

    orch.CAPABILITY_REGISTRY["integration.effect.shadow.evidence"] = {
        "func": evidence, "raw": evidence}
    result = _R(orch, "intel.effects")
    assert seen == [
        ("telegram", "intel", 50), ("email", "intel", 50),
        ("homeassistant", "intel", 50), ("n8n", "intel", 50),
        ("repository", "intel", 50), ("printing", "intel", 50),
    ]
    assert result["readiness"]["intel_attributable"] is True
    assert result["readiness"]["attributed_observations"] == 7
    by_id = {row["id"]: row for row in result["boundaries"]}
    assert by_id["channel.phone"]["evidence_family"] == "homeassistant"
    assert by_id["channel.print"]["evidence_family"] == "printing"
    assert by_id["repo.acquire"]["evidence_family"] == "repository"
    assert by_id["n8n.proposal.create"]["evidence_family"] == "n8n"
    assert all("portable_effect_observation_missing" not in by_id[key]["gaps"]
               for key in ("channel.phone", "channel.print", "repo.acquire",
                           "n8n.proposal.create"))


def test_record_reconciliation_matches_only_real_fabric_and_delivery_evidence(tmp_path):
    mod, orch = _load(tmp_path)
    mod.store().put_signal({"id": "sig-1", "topic_id": "topic-1", "seen": "2026-10-16T00:00:00Z"})
    mod.store().put_signal({"id": "sig-2", "topic_id": "topic-1", "seen": "2026-10-16T00:01:00Z"})
    mod.store().put_report({"id": "report-1", "ts": "2026-10-16T01:00:00Z",
                            "title": "Brief", "topic_ids": ["topic-1"]}, "<p>Brief</p>")
    mod.store().put_report({"id": "report-local-only", "ts": "2026-10-16T01:01:00Z",
                            "title": "Local", "topic_ids": ["topic-1"]}, "<p>Local</p>")
    mod.store().log_delivery("report-1", "fabric", "2026-10-16T01:02:00Z", True,
                             {"channel": "fabric", "ok": True})
    mod.store().log_delivery("report-1", "memory", "2026-10-16T01:03:00Z", True,
                             {"channel": "memory", "ok": True})
    # A long unrelated tail must not hide evidence for a selected report.
    for index in range(120):
        mod.store().log_delivery("other", "html", f"2026-10-16T02:{index % 60:02d}:00Z", True,
                                 {"channel": "html", "ok": True})

    async def browse(dataset_id="", limit=200, offset=0, lite=False):
        rows = {
            "intel.signals": [
                {"id": "intel-sig-sig-1", "data": {"id": "sig-1"}},
                {"id": "intel-sig-orphan", "data": {"id": "orphan"}},
            ],
            "intel.reports": [
                {"id": "random-fabric-id", "data": {"id": "report-1"}},
            ],
        }[dataset_id]
        page = rows[offset:offset + limit]
        return {"records": page, "total": len(rows),
                "has_more": offset + limit < len(rows)}

    orch.CAPABILITY_REGISTRY["fabric.browse"] = {"func": browse, "raw": browse}
    result = _R(orch, "intel.records", limit=20)
    assert result["schema"] == "vera.intel-record-reconciliation/v1"
    assert result["native_authority"] == "intel.sqlite"
    assert result["read_only"] and not result["executes"] and not result["writes"]
    assert result["reconciliation"]["signals"]["status"] == "gaps"
    assert result["reconciliation"]["signals"]["missing_ids"] == ["sig-2"]
    assert result["reconciliation"]["signals"]["unexpected_ids"] == ["orphan"]
    reports = result["reconciliation"]["reports"]
    assert reports["status"] == "matched"
    assert reports["expected_shared"] == 1
    assert reports["fabric_delivery_successes_sampled"] == 1
    assert reports["memory"] == {
        "delivery_successes_sampled": 1,
        "projection_identity_available": False,
        "status": "delivery_evidence_only",
        "reason": "native_delivery_log_retains_no_projection_identity",
    }
    families = {row["id"]: row for row in result["families"]}
    assert families["reports"]["portable_record"]["status"] == "not_revision_backed"
    assert families["decisions_outcomes"]["shared_contract"] == "four_minds.decision_outcome"
    assert families["signals"]["native_count"] == 2
    assert families["reports"]["native_count"] == 2
    assert families["actions_deliveries"]["native_count"] == 122


def test_record_reconciliation_reports_fabric_failure_without_fabricating_missing_rows(tmp_path):
    mod, orch = _load(tmp_path)
    mod.store().put_signal({"id": "sig-1", "topic_id": "topic-1", "seen": "2026-10-16T00:00:00Z"})

    async def unavailable(**_kw):
        raise RuntimeError("fabric unavailable")

    orch.CAPABILITY_REGISTRY["fabric.browse"] = {"func": unavailable, "raw": unavailable}
    result = _R(orch, "intel.records", limit=20)
    signals = result["reconciliation"]["signals"]
    assert signals["status"] == "shared_source_unavailable"
    assert signals["missing"] == 0
    assert signals["coverage_complete"] is False
    assert "fabric unavailable" in signals["error"]


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
    assert kinds[:2] == ["sweeps", "files"] and "topic" in kinds              # phase 1 sweeps everything first
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


def test_the_night_refits_the_calibration_curves(tmp_path):
    # the curves were only ever fitted on request, so the learned gate never saw a rating given after 6 Oct
    mod, orch = _load(tmp_path)
    _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])
    assert not mod.store().get_kv("calibration")
    _run(mod._start_night("2026-10-04"))
    rec = mod.store().get_kv("calibration")
    assert rec and rec.get("at") and "labelled" in rec


class _FakeSvc:
    """The idle queue as intel.night.run sees it: jobs run when drained, never on their own."""
    def __init__(self, mod):
        self.mod, self.jobs, self.reserved, self.released, self.drains = mod, [], None, [], 0

    async def submit(self, kind, title="", *, dedupe_key="", payload=None, total=None, priority=None):
        self.jobs.append({"id": f"{kind}:{len(self.jobs)}", "kind": kind, "state": "waiting", "payload": payload})
        return {"queued": True, "id": self.jobs[-1]["id"]}

    async def load_jobs(self):
        return [dict(j) for j in self.jobs]

    async def report_progress(self, *a, **k):
        pass

    async def submit_mode(self, mode, title, *, key, payload=None, priority=None):
        """intel's _submit, queueing (the harness's default runs jobs at once)."""
        return await self.submit("intel", title, dedupe_key=f"intel:{key}", payload=dict(payload or {}, mode=mode),
                                 total=1, priority=priority)

    def reserve(self, kinds, until, by=""):
        self.reserved = (tuple(kinds), until, by)

    def release_reservation(self, by=""):
        self.released.append(by)

    async def drain_once(self, busy, probe, now=None):
        self.drains += 1
        assert self.reserved and busy == ""                      # drained only inside the reserved window
        w = [j for j in self.jobs if j["state"] == "waiting"]
        if not w:
            return {"action": "idle"}
        await self.mod._intel_job(w[0], probe)
        self.jobs.remove(w[0])
        return {"action": "started", "kind": w[0]["kind"]}


def test_night_run_reserves_the_queue_and_drains_the_batch_to_the_brief(tmp_path, monkeypatch):
    # 12-17 Oct: the night batch waited six days for 600 s of quiet; in a scheduled window it runs regardless
    mod, orch = _load(tmp_path)
    _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])
    fake = _FakeSvc(mod)
    monkeypatch.setattr(mod, "_svc", lambda: fake)
    monkeypatch.setattr(mod, "_submit", fake.submit_mode)
    monkeypatch.setattr(mod, "_NIGHT_RUN_POLL_S", 0.0)
    r = _R(orch, "intel.night.run", minutes=30)
    assert r["ok"] and r["reason"] == "brief written" and r["ran"] >= 3 and r["remaining"] == 0, r
    assert fake.reserved[0] == ("intel",) and fake.reserved[2] == "intel.night.run"
    assert fake.released == ["intel.night.run"]                  # always, even when the loop ends early
    night = mod.store().get_kv("night")
    assert night["pending"] == [] and night["brief"] not in ("", "-")
    assert any(e.get("type", "").endswith("night_run") or e.get("kind") == "night_run" or "night_run" in str(e)
               for e in orch.EVENTS)
    # a second call the same night does not start the batch again
    r2 = _R(orch, "intel.night.run", minutes=30)
    assert r2["ok"] and r2["ran"] == 0 and r2["reason"] == "brief written"


def test_night_run_without_an_idle_queue_runs_inline(tmp_path, monkeypatch):
    mod, orch = _load(tmp_path)
    _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])
    monkeypatch.setattr(mod, "_svc", lambda: None)
    r = _R(orch, "intel.night.run")
    assert r["ok"] and "inline" in r["reason"]
    assert mod.store().get_kv("night")["brief"] not in ("", "-")


def test_night_schedule_is_an_exclusive_cap_entry_and_idempotent(tmp_path):
    mod, orch = _load(tmp_path)
    calls = []

    async def upsert(schedule=None, trace_id=None):
        calls.append(schedule)
        return {"ok": True, "schedule": dict(schedule, id="sched1"), "next": {"start": "x", "end": "y"}}

    orch.CAPABILITY_REGISTRY["evolve.schedule.upsert"] = {"func": upsert, "raw": upsert, "meta": {}}
    r = _R(orch, "intel.night.schedule", start="00:10", end="02:40", minutes=120)
    assert r["ok"] and r["schedule"]["id"] == "sched1"
    rec = calls[0]
    assert rec["kind"] == "cap" and rec["target"] == {"name": "intel.night.run", "arguments": {"minutes": 120}}
    assert rec["exclusive"] is True and rec["repeat"] == "once_per_window" and rec["days"] == [0, 1, 2, 3, 4, 5, 6]
    assert rec["start"] == "00:10" and rec["end"] == "02:40" and "id" not in rec
    _R(orch, "intel.night.schedule", days="0,2,4", enabled=False)
    assert calls[1]["id"] == "sched1" and calls[1]["days"] == [0, 2, 4] and calls[1]["enabled"] is False


def test_topics_and_sweeps_touch_the_web_map(tmp_path):
    # web-ingress WI-10: the intel topic is the canonical topic id; its sweep's signals become members
    mod, orch = _load(tmp_path)
    orch.CAPABILITY_REGISTRY["fabric.map.topic.touch"] = {"func": orch.CAPABILITY_REGISTRY["tg.notify"]["func"], "raw": None}
    calls = []

    async def touch(**kw):
        calls.append(kw)
        return {"topic_key": kw.get("topic_id"), "members_added": len(kw.get("urls") or [])}
    orch.CAPABILITY_REGISTRY["fabric.map.topic.touch"] = {"func": touch, "raw": touch}

    async def go():
        r = await orch.CAPABILITY_REGISTRY["intel.topic.save"]["func"](name="AI property valuation", entities=["Acme"],
                                                                       sites=["news.example"], queries=["acme pilot"])
        await asyncio.sleep(0); await asyncio.sleep(0)
        t = mod.store().get_topic(r["topic"]["id"])
        await mod.sweep_topic(t)
        await asyncio.sleep(0); await asyncio.sleep(0)
        return t

    t = _run(go())
    assert calls and calls[0]["topic_id"] == t["id"] and calls[0]["source"] == "intel.topic.save"
    assert calls[0]["seeds"] == ["news.example"] and "acme pilot" in calls[0]["queries"] and calls[0]["entities"] == ["Acme"]
    sweep = [c for c in calls if c["source"] == "intel.sweep"]
    assert sweep and sweep[0]["role"] == "source" and any(u.startswith("https://news.example/") for u in sweep[0]["urls"])
    assert mod._touch_topic_map(t) is None or True                      # never raises without a running loop either


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


def test_intel_job_wrapper_projects_the_actual_native_invocation(tmp_path, monkeypatch):
    mod, _orch = _load(tmp_path)
    from vera.execution.run_journal import MemoryRunJournal
    from vera.execution.run_projection import ShadowRunRegistry

    registry = ShadowRunRegistry(max_runs=20, journal=MemoryRunJournal())
    monkeypatch.setattr(mod.intel_run, "SHADOW_RUNS", registry)

    result = _run(mod._intel_job({
        "id": "intel:native-job",
        "preempts": "2",
        "payload": {
            "mode": "topic",
            "topic_id": "private-topic",
            "until": "2000-01-01T00:00:00Z",
        },
    }, None))

    assert result["skipped"] == "its session ended"
    observed = registry.get(registry.list()[0]["id"])
    assert observed["run"]["status"] == "skipped"
    assert observed["run"]["attempt"] == 3
    assert observed["run"]["workflow_id"] == "intel.idle.topic"
    assert "private-topic" not in str(observed)
    assert "2000-01-01" not in str(observed)

    registry = ShadowRunRegistry(max_runs=20, journal=MemoryRunJournal())
    monkeypatch.setattr(mod.intel_run, "SHADOW_RUNS", registry)
    result = _run(mod._intel_job({
        "id": "intel:malformed-attempt", "preempts": "not-a-number",
        "payload": {"mode": "unknown-mode"},
    }, None))
    assert result["ok"] is False
    assert registry.get(registry.list()[0]["id"])["run"]["attempt"] == 1


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
    assert not _R(orch, "intel.report.build", topic_ids=t["id"])["empty"]          # ad hoc: marks nothing
    assert not _R(orch, "intel.report.build", topic_ids=t["id"], mark_reported=True)["empty"]
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


def test_corpus_export(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    mod.store().put_topic(mod.core.normalise_topic({"name": "From your notes", "id": mod.ops.NOTES_TOPIC}))
    mod.store().put_signal(mod.core.normalise_signal(mod.ops.NOTES_TOPIC, {"url": "https://x.example/n", "title": "a note"}))
    out = _R(orch, "intel.corpus.export", max_share=1.0)
    assert out["schema"] == "vera.intel-corpus/v1" and out["records"]
    assert all(r["topic_id"] == t["id"] for r in out["records"]) and out["stats"]["private_dropped"] == 1
    assert out["sequences"]["stories"][t["id"]] and all("vec" not in r for r in out["records"])
    assert any("vec" in r for r in _R(orch, "intel.corpus.export", max_share=1.0, vectors=True)["records"])


def test_geocoder_caches_rate_limits_and_tags_signals(tmp_path):
    mod, orch = _load(tmp_path)
    calls = []

    async def http_json(url, params=None, timeout=20.0, headers=None):
        calls.append((url, dict(params or {}), dict(headers or {})))
        if "nominatim" in url:
            if params["q"] == "Atlantis":
                return []
            return [{"lat": "53.79", "lon": "-1.54", "name": params["q"], "addresstype": "city",
                     "boundingbox": ["53.6", "53.9", "-1.7", "-1.3"],
                     "address": {"country": "United Kingdom", "country_code": "gb", "state": "England"}}]
        return []
    mod._http_json = http_json
    mod.geo.MIN_INTERVAL_S = 0.0
    r = _R(orch, "geo.lookup", name="Leeds")
    assert r["ok"] and r["place"]["cc"] == "gb" and r["place"]["bbox"] == [53.6, 53.9, -1.7, -1.3]
    assert "vera-intel" in calls[0][2]["User-Agent"] and "@" not in calls[0][2]["User-Agent"]
    _R(orch, "geo.lookup", name="leeds ")
    assert len(calls) == 1                                                      # cached
    assert not _R(orch, "geo.lookup", name="Atlantis")["ok"]
    _R(orch, "geo.lookup", name="Atlantis")
    assert len(calls) == 2                                                      # a miss is cached too

    async def nlp_ner(text="", task="ner", labels=None, threshold=0.4, trace_id=None):
        return {"entities": [{"word": "Acme", "entity": "company", "score": 0.9},
                             {"word": "Leeds", "entity": "location", "score": 0.8}]}
    orch.CAPABILITY_REGISTRY["nlp.ner"] = {"func": nlp_ner, "raw": nlp_ner}
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    _run(mod.enrich_topic(mod.store().get_topic(t["id"])))
    tagged = [s for s in mod.store().signals(t["id"]) if s.get("places")]
    assert tagged and tagged[0]["places"][0]["name"] == "Leeds" and tagged[0]["places"][0]["cc"] == "gb"
    assert len(calls) == 2                                                      # Leeds came from the cache
    pts = _R(orch, "intel.places.get", topic_id=t["id"])["points"]
    assert pts[0]["name"] == "Leeds" and pts[0]["size"] == len(tagged) and pts[0]["lit"] and pts[0]["detail"]


def test_a_cut_short_night_says_so_in_the_morning_brief(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    mod.store().set_kv("night", {"id": "n1", "stats": {"topics": 3}, "pending": ["x", "y"]})
    doc = _run(mod.build_report([], period_hours=24, note=mod.ops.night_note(mod.store().get_kv("night"))))
    assert doc["note"].startswith("Overnight: 1 of 3 topics refreshed")
    assert "Overnight: 1 of 3" in mod.core.render_html(doc) and "Overnight: 1 of 3" in mod.core.render_markdown(doc)
    assert "Overnight: 1 of 3" in mod.core.render_telegram(doc)["text"]


def test_the_night_sweeps_every_topic_before_any_model_work(tmp_path):
    mod, orch = _load(tmp_path)
    a = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    b = _R(orch, "intel.topic.save", name="Second topic pilot", entities=["Acme"])["topic"]
    order = []
    real_sweep = mod.sweep_topic

    async def sweep(t, depth=""):
        order.append(("sweep", t["id"]))
        return await real_sweep(t, depth=depth)
    mod.sweep_topic = sweep
    real_enrich = mod.enrich_topic

    async def enrich(t, should_continue=None):
        order.append(("enrich", t["id"]))
        return await real_enrich(t, should_continue=should_continue)
    mod.enrich_topic = enrich
    submitted = []
    run_inline = mod._submit

    async def submit(mode, title, *, key, payload=None, priority=None):
        submitted.append((mode, priority))
        return await run_inline(mode, title, key=key, payload=payload, priority=priority)
    mod._submit = submit
    _run(mod._start_night("2026-10-05"))
    sweeps = [x for x in order if x[0] == "sweep"]
    assert sorted(i for _, i in sweeps) == sorted([a["id"], b["id"]])          # each swept exactly once
    first_enrich = next(i for i, x in enumerate(order) if x[0] == "enrich")
    assert first_enrich > max(i for i, x in enumerate(order) if x[0] == "sweep")  # all sweeps before any model work
    assert submitted[0] == ("sweeps", 25) and ("brief", 20) in submitted
    n = mod.store().get_kv("night")
    assert sorted(n["swept"]) == sorted([a["id"], b["id"]]) and n["stats"]["new"] >= 1 and not n["pending"]


def test_report_page_is_rendered_from_the_record_not_stored_html(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    doc = _run(mod.build_report([t["id"]], period_hours=24))
    st = mod.store()
    st.put_report(st.get_report(doc["id"]), "<html>stale broken page</html>")     # what old reports hold
    page = _run(mod._serve_report(doc["id"]))
    assert b"stale broken page" not in page.body and b"<main>" in page.body


def test_a_report_is_read_aloud_with_chapters(tmp_path):
    import io
    import wave
    mod, orch = _load(tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    mod._audio_dir = lambda: audio_dir
    said = []

    async def tts(text, voice="", speed=1.0):
        said.append(text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(1000)
            w.writeframes(b"\x01\x00" * 2000)                       # two seconds a piece
        return buf.getvalue()
    mod._tts = tts
    mod.shutil.which = lambda name: None                              # no ffmpeg: keep the WAV
    mod._mp3_with_soundfile = lambda w, m: False
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    doc = _run(mod.build_report([t["id"]], period_hours=24))
    assert ("submit", {"mode": "speak", "key": f"speak:{doc['id']}", "payload": {"report_id": doc["id"]}}) in \
        [(n, {k: kw[k] for k in ("mode", "key", "payload")}) for n, kw in orch.CALLS if n == "submit" and kw["mode"] == "speak"]
    got = mod.store().get_report(doc["id"])["audio"]                  # the queue ran it inline
    assert got["format"] == "wav" and got["seconds"] >= 4 and got["chapters"][0]["id"] == "intro"
    assert got["chapters"][1]["id"] == t["id"] and got["chapters"][1]["start"] > 0
    assert (audio_dir / f"{doc['id']}.wav").exists() and said
    page = _run(mod._serve_report(doc["id"])).body.decode()
    assert f'/intel/report/audio/{doc["id"]}' in page and 'class="listen"' in page
    assert _run(mod._serve_audio(doc["id"])).status_code == 200
    assert _R(orch, "intel.report.speak", id="nope").get("error")


def test_app_routes(tmp_path):
    import json as _json
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    doc = _run(mod.build_report([t["id"]], period_hours=24))
    rows = _json.loads(_run(mod._serve_app_list()).body)["rows"]
    assert rows and rows[0]["id"] == doc["id"]
    assert b"/intel/app/list" in _run(mod._serve_app()).body
    assert _json.loads(_run(mod._serve_manifest()).body)["scope"] == "/intel/"
    sw = _run(mod._serve_sw())
    assert sw.headers["Service-Worker-Allowed"] == "/intel/" and b"caches" in sw.body
    assert _run(mod._serve_icon_png(77)).status_code == 404
    assert _run(mod._serve_icon_svg()).body.startswith(b"<svg")


def test_the_narration_follows_the_brief_to_telegram_once(tmp_path):
    import io
    import wave
    mod, orch = _load(tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    mod._audio_dir = lambda: audio_dir
    sent = []

    async def send_audio(path="", title="", performer="", caption="", chat_id="", trace_id=None):
        sent.append({"path": path, "title": title, "caption": caption})
        return {"ok": True}
    orch.CAPABILITY_REGISTRY["tg.send_audio"] = {"func": send_audio, "raw": send_audio}

    async def tts(text, voice="", speed=0.0):
        assert speed == 0.0 and voice == ""                       # no override: the node's configured voice
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(1000)
            w.writeframes(b"\x01\x00" * 1000)
        return buf.getvalue()
    mod._tts = tts
    mod.shutil.which = lambda name: None
    mod._mp3_with_soundfile = lambda w, m: False
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    doc = _run(mod.build_report([t["id"]], period_hours=24))           # narrated inline by the fake queue
    assert not sent                                                     # not delivered to Telegram yet
    _run(mod.deliver_report(doc["id"], ["telegram"]))
    assert len(sent) == 1 and sent[0]["path"].endswith(f"{doc['id']}.wav") and "min" in sent[0]["caption"]
    _run(mod.deliver_report(doc["id"], ["telegram"]))
    _run(mod.speak_report(doc["id"]))
    assert len(sent) == 1                                               # once, whichever came second
    assert _R(orch, "intel.config.set", settings={"speak_voice": "bf_emma", "telegram_audio": False, "geocode": False})["changed"] \
        == ["geocode", "speak_voice", "telegram_audio"]


def test_ratings_build_a_topic_curve_that_only_watches_until_switched_on(tmp_path):
    import random as _r
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    st = mod.store()
    rng = _r.Random(5)
    for i in range(300):                         # 300 rated signals: high scores useful, low not
        sid = f"s{i:03d}"
        score = rng.uniform(0, 10)
        st.put_signal({"id": sid, "topic_id": t["id"], "title": f"t{i}", "url": f"https://x.example/{i}", "score": score,
                       "seen": "2026-10-05T00:00:00Z"})
        mod._decide("intel.signal_relevant", "relevant", state={"s": sid}, ref_kind="signal", ref_id=sid,
                    detail={"score": score})
        useful = rng.random() < 1 / (1 + 2.718 ** (-(1.0 * score - 5)))
        mod._outcome("signal", sid, "user.feedback.useful" if useful else "user.feedback.not_useful")
    s = _R(orch, "intel.calibration.status", refit=True)
    cur = s["topics"][t["id"]]
    assert s["labelled"] == 300 and cur["usable"] and not s["on"]
    # in shadow: the decision carries p, the gate still uses the score threshold
    _R(orch, "intel.sweep", topic_id=t["id"])
    d = [x for x in st.decisions(question_id="intel.signal_relevant", limit=50) if x["decider"] == "rule" and x["detail"].get("p_shadow") is not None]
    assert d and d[0]["p"] is not None
    _R(orch, "intel.config.set", settings={"calibrated_gate": True})
    assert _R(orch, "intel.calibration.status")["on"]


def test_entities_get_stable_identities_and_the_resolver_backs_off(tmp_path):
    mod, orch = _load(tmp_path)
    mod.WD_SPACING_S = 0.0
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Savills"])["topic"]
    st = mod.store()
    for i in range(3):
        st.put_signal({"id": f"s{i}", "topic_id": t["id"], "title": f"Savills news {i}", "url": f"https://www.savills.co.uk/{i}",
                       "site": "savills.co.uk", "score": 7, "seen": "2026-10-05T00:00:00Z",
                       "entities": ["Savills"], "entity_types": {"Savills": "property agency or consultancy"}})
    calls = []

    async def wd(params):
        calls.append(params["action"])
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q10458056", "label": "Savills", "description": "real estate services provider"},
                               {"id": "Q7628329", "label": "Savills North America", "description": ""}]}
        return {"entities": {"Q10458056": {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q891723"}}}}],
                                                       "P856": [{"mainsnak": {"datavalue": {"value": "https://www.savills.com"}}}]}},
                             "Q7628329": {"claims": {}}}}
    mod._wd = wd
    n = _run(mod._resolve_topic_entities(st.get_topic(t["id"])))
    rec = st.get_kv(f"wd:savills|org|{t['id']}")
    assert n == 1 and rec["qid"] == "Q10458056" and rec["website"] == "https://www.savills.com"
    assert any(d["question_id"] == "intel.entity_resolve" and d["answer"] == "resolved" for d in st.decisions(limit=50))
    node = next(x for x in _R(orch, "intel.entities.get", topic_id=t["id"])["nodes"] if x["id"] == "Savills")
    assert node["qid"] == "Q10458056" and node["url"].endswith("Q10458056")
    before = len(calls)
    _run(mod._resolve_topic_entities(st.get_topic(t["id"])))
    assert len(calls) == before                                   # cached: no second lookup

    async def refuse(params):
        raise mod._Throttled("429")
    mod._wd = refuse
    assert "refused" in _R(orch, "intel.entity.resolve", name="CBRE", topic_id=t["id"], force=True)["error"]


def test_attention_indicator_from_a_name(tmp_path):
    import time as _t
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Savills"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    mod.store().put_signal({"id": "sv1", "topic_id": t["id"], "title": "Savills research", "site": "savills.co.uk",
                            "url": "https://www.savills.co.uk/research", "score": 6, "seen": "2026-10-05T00:00:00Z"})

    async def wd(params):
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q10458056", "label": "Savills", "description": "real estate services provider"}]}
        return {"entities": {"Q10458056": {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q891723"}}}}],
                                                      "P856": [{"mainsnak": {"datavalue": {"value": "https://www.savills.com"}}}]},
                                           "sitelinks": {"enwiki": {"title": "Savills"}}}}}
    mod._wd = wd
    today = int(_t.time()) // 86400 * 86400
    views = [60 + (i * 7) % 9 for i in range(90)]
    views[-2] = 500                                                    # yesterday: in the news

    async def http_json(url, params=None, timeout=20.0, headers=None):
        assert "pageviews/per-article/en.wikipedia" in url and "/Savills/" in url
        return {"items": [{"timestamp": _t.strftime("%Y%m%d00", _t.gmtime(today - (89 - i) * 86400)), "views": v}
                          for i, v in enumerate(views)]}
    mod._http_json = http_json
    add = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="attention", query="Savills")
    assert add["ok"] and add["indicator"]["dataset_id"] == "wiki:en:Savills" and add["indicator"]["label"] == "Savills"
    ch = _R(orch, "intel.indicator.check", topic_id=t["id"])["changes"]
    assert len(ch) == 1 and "drew" in ch[0]["text"] and ch[0]["stories"]      # the sweep's stories around it
    a = mod.core.make_action("watch", topic_id=t["id"], target="wiki:en:Knight_Frank", query="Knight Frank")
    assert mod.core.action_args(a) == {"topic_id": t["id"], "kind": "attention", "wiki_title": "Knight_Frank", "label": "Knight Frank"}


def test_worldview_hint_only_while_healthy_and_only_what_is_new(tmp_path):
    mod, orch = _load(tmp_path)
    health = {"status": "degraded"}
    drift_calls = []

    async def wv_health(trace_id=None):
        return dict(health)

    async def detect_drift(dataset_id="", trace_id=None):
        drift_calls.append(dataset_id)
        over = [{"concept": 1, "label": "valuation models", "direction": "over"}]
        if len(drift_calls) > 1:
            over.append({"concept": 2, "label": "planning reform", "direction": "over"})
        return {"drifted_concepts": over}
    orch.CAPABILITY_REGISTRY["worldview.health"] = {"func": wv_health, "raw": wv_health}
    orch.CAPABILITY_REGISTRY["worldview.detect_drift"] = {"func": detect_drift, "raw": detect_drift}
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    assert _run(mod._worldview_shift(t)) == "" and not drift_calls                  # degraded: not even asked
    health["status"] = "ok"
    mod.store().set_kv("wv_health", {})                                             # forget the cached reading
    assert _run(mod._worldview_shift(t)) == ""                                      # first look: nothing to compare
    assert _run(mod._worldview_shift(t)) == "the mix of what is being said shifted towards: planning reform"
    assert drift_calls == ["intel." + t["id"]] * 2
    _R(orch, "intel.sweep", topic_id=t["id"])
    _run(mod.snapshot_topic(mod.store().get_topic(t["id"])))
    assert "WHAT VERA'S WORLDVIEW NOTICED" not in orch.PROMPTS[-1][0]                # steady since: no hint
    assert any(d["question_id"] == "intel.worldview_drift" and d["shadow"] for d in mod.store().decisions(limit=50))


def test_narration_is_compressed_to_mp3_without_ffmpeg(tmp_path):
    import importlib.util as _u
    import io
    import wave
    if not _u.find_spec("soundfile"):
        return
    import soundfile as sf
    if "MP3" not in sf.available_formats():
        return
    mod, orch = _load(tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    mod._audio_dir = lambda: audio_dir
    mod.shutil.which = lambda name: None
    wav = audio_dir / "abc123.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x10\x00\xf0\xff" * 24000)              # one second
    out = _run(mod._to_mp3(wav))
    assert out.suffix == ".mp3" and out.exists() and not wav.exists() and out.stat().st_size < 48000
    with wave.open(str(wav), "wb") as w:                                # an old WAV narration to compact
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x10\x00\xf0\xff" * 24000)
    r = _R(orch, "intel.audio.compact")
    assert r["converted"] == 1 and r["kept_wav"] == 0


def test_decisions_send_what_they_saw_to_the_training_store(tmp_path, monkeypatch):
    # four-minds FM-13.6: decide.record gets the inputs (a signal's richer view, else the state) so intel decisions
    # can be rated in /decide/labels; the ledger row itself still carries only the hash
    mod, orch = _load(tmp_path)
    calls = []

    async def fake_call(name, /, **kw):
        calls.append((name, kw))
        return {"ok": True}

    monkeypatch.setattr(mod, "_has", lambda n: n == "decide.record")
    monkeypatch.setattr(mod, "_call", fake_call)

    async def go():
        mod._decide("intel.signal_relevant", "relevant", state={"url": "https://a"}, ref_kind="signal", ref_id="s1",
                    inputs={"url": "https://a", "snippet": "text"})
        mod._decide("intel.material_change", "alert", state={"topic": "t1"}, ref_kind="snapshot", ref_id="n1")
        await asyncio.gather(*list(mod._LEDGER_TASKS))

    _run(go())
    assert [c[0] for c in calls] == ["decide.record", "decide.record"]
    assert calls[0][1]["inputs"] == {"url": "https://a", "snippet": "text"}
    assert calls[1][1]["inputs"] == {"topic": "t1"}
    assert "snippet" not in json.dumps(calls[0][1]["row"])


def test_backfill_rebuilds_signal_inputs_and_writes_only_when_asked(tmp_path, monkeypatch):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    st = mod.store()
    for i in range(3):
        st.put_signal({"id": f"s{i}", "topic_id": t["id"], "title": f"t{i}", "url": f"https://x.example/{i}",
                       "score": 6, "snippet": f"body {i}", "seen": mod.core.now_iso()})
        mod._decide("intel.signal_relevant", "relevant", state={"s": i}, ref_kind="signal", ref_id=f"s{i}")
    mod._decide("intel.enrich_worth", "model", state={}, ref_kind="signal", ref_id="gone")
    written = []

    async def put_now(d, inputs):
        written.append((d["decision_id"], inputs))
        return True

    monkeypatch.setitem(sys.modules, "training_state", types.SimpleNamespace(put_now=put_now))
    dry = _R(orch, "intel.decisions.backfill_inputs")
    assert (dry["decisions"], dry["rebuilt"], dry["missing_signal"], dry["written"]) == (4, 3, 1, 0) and not written
    out = _R(orch, "intel.decisions.backfill_inputs", dry_run=False)
    assert out["written"] == 3 and len(written) == 3
    assert {w[1]["topic_name"] for w in written} == {"AI property valuation"}
    assert {w[1]["snippet"] for w in written} == {"body 0", "body 1", "body 2"}


def test_a_set_switch_overrides_intel_config_and_an_unset_one_changes_nothing(tmp_path):
    # four-minds FM-13.3: intel's modes as switchboard switches; intel's config stays the truth
    mod, orch = _load(tmp_path)
    eff = {"modes": {"intel.nlp_triage": "on", "intel.brief": "llm", "intel.auto_research": "off",
                     "intel.material_alerts": "on", "intel.calibrated_gate": "shadow"},
           "source": {s: "default" for s in mod.isw.SWITCHES}}

    async def minds_effective(minds=None, arm=None, trace_id=None):
        return eff

    orch.CAPABILITY_REGISTRY["minds.effective"] = {"func": minds_effective, "raw": minds_effective}

    async def read_twice():
        mod._SW.update(at=0.0, over={}, task=None)
        mod.config()                                   # starts the background read
        await mod._SW["task"]
        return mod.config()

    base = _run(read_twice())
    assert base["nlp"] is True and not base.get("calibration_off") and "brief_synthesize" not in base
    eff["modes"].update({"intel.nlp_triage": "off", "intel.brief": "extractive", "intel.calibrated_gate": "off"})
    eff["source"].update({"intel.nlp_triage": "global", "intel.brief": "global", "intel.calibrated_gate": "arm"})
    c = _run(read_twice())
    assert c["nlp"] is False and c["brief_synthesize"] is False
    assert c["calibrated_gate"] is False and c["calibration_off"] is True and mod._curves() == {}
    assert c["alerts"] == base["alerts"]               # unset: unchanged
    # the panel can mark what the switchboard is overriding (a toggle there would do nothing)
    assert set(_R(orch, "intel.config.get")["switch_overrides"]) == {"nlp", "brief_synthesize", "calibrated_gate",
                                                                     "calibration_off"}
