"""Intel caps called from chat (or any caller that leaves the arguments out) must not lose data or reach the
printer: info-seeking plan SK-0.2 / SK-0.3 (shared-planning/intelligence/PLAN-INFO-SEEKING.md)."""
from test_intel_capabilities import _load, _R, _run   # noqa: F401  (the intel test harness)


def test_saving_a_topic_keeps_its_fabric_sources(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="UK commercial property", brief="offices and logistics")["topic"]
    subs = [{"id": "intel-src-1", "kind": "feed", "value": "https://example.org/rss", "dataset_id": "intel_src_1"}]
    mod.store().patch_topic(t["id"], {"sources": subs, "some_later_field": 7})
    # the panel's Edit form sends the fields it owns and nothing else
    t2 = _R(orch, "intel.topic.save", id=t["id"], name="UK commercial property", brief="offices, logistics, retail")
    kept = mod.store().get_topic(t["id"])
    assert kept["brief"] == "offices, logistics, retail"
    assert kept["sources"] == subs and kept["some_later_field"] == 7
    assert t2["topic"]["sources"] == subs


def test_normalise_topic_never_takes_unknown_keys_from_the_input(tmp_path):
    mod, _ = _load(tmp_path)
    t = mod.core.normalise_topic({"name": "x", "sources": [{"id": "injected"}]})
    assert "sources" not in t                       # only the stored record carries fields the form does not own


def test_a_thirteenth_indicator_is_refused_not_silently_dropped(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="Numbers")["topic"]
    for i in range(mod.MAX_INDICATORS):
        assert _R(orch, "intel.indicator.add", topic_id=t["id"], kind="count", dataset_id=f"ds.{i}")["ok"]
    r = _R(orch, "intel.indicator.add", topic_id=t["id"], kind="count", dataset_id="ds.extra")
    assert "error" in r and "remove one" in r["error"]
    ids = [i["dataset_id"] for i in _R(orch, "intel.indicator.list", topic_id=t["id"])["indicators"]]
    assert len(ids) == mod.MAX_INDICATORS and "ds.extra" not in ids
    # re-adding one it already watches is not a thirteenth
    assert _R(orch, "intel.indicator.add", topic_id=t["id"], kind="count", dataset_id="ds.0")["ok"]


def test_an_ad_hoc_report_leaves_its_signals_for_the_brief(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    before = {s["id"] for s in mod.store().signals(t["id"], unreported=True)}
    assert before
    _R(orch, "intel.report.build", topic_ids=[t["id"]])                       # chat asks for a report
    assert {s["id"] for s in mod.store().signals(t["id"], unreported=True)} == before
    _R(orch, "intel.report.build", topic_ids=[t["id"]], mark_reported=True)   # the panel's builder, as before
    after = {s["id"] for s in mod.store().signals(t["id"], unreported=True)}
    assert after < before                           # the signals it reported are marked (the weak one stays)


def test_a_bare_deliver_is_html_only_and_refresh_queues(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.sweep", topic_id=t["id"])
    rid = _R(orch, "intel.report.build", topic_ids=[t["id"]])["id"]
    res = _R(orch, "intel.deliver", report_id=rid)["results"]
    assert [r["channel"] for r in res] == ["html"]
    assert not [c for c in orch.CALLS if c[0] in ("print.text", "tg.send_rich", "tg.notify")]
    import inspect
    assert inspect.signature(mod.cap_refresh).parameters["queue"].default is True


def test_lookup_finds_a_tracked_topic_by_name_and_answers_compactly(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    _R(orch, "intel.topic.save", name="UK house prices")
    _R(orch, "intel.sweep", topic_id=t["id"])
    r = _R(orch, "intel.lookup", query="what's new on AI property valuation?")
    assert r["tracked"] and r["topic"]["id"] == t["id"] and r["matches"][0]["id"] == t["id"]
    assert r["signals"] and all(set(s) == {"title", "url", "site", "published", "seen", "score", "summary"}
                                for s in r["signals"])
    assert "vec" not in str(r) and len(str(r)) < 6000
    by_id = _R(orch, "intel.lookup", topic_id=t["id"])
    assert by_id["tracked"] and by_id["topic"]["id"] == t["id"]


def test_lookup_says_when_a_subject_is_not_tracked(tmp_path):
    mod, orch = _load(tmp_path)
    _R(orch, "intel.topic.save", name="AI property valuation")
    r = _R(orch, "intel.lookup", query="bitcoin price")
    assert r["tracked"] is False and "info.seek" in r["message"] and "intel.topic.save" in r["message"]


def test_match_topics_weights_rare_words_and_phrases():
    from vera.intel import intel_core as core
    T = [{"id": "uk-news-and-current-affairs", "name": "UK news and current affairs"},
         {"id": "uk-commercial-real-estate-market", "name": "UK commercial real estate market"},
         {"id": "uk-residential-property-market-and-house-prices", "name": "UK residential property market and house prices"},
         {"id": "mcp-and-llm-tooling", "name": "MCP and LLM tooling"}]
    assert core.match_topics("updates on MCP", T)[0][1]["id"] == "mcp-and-llm-tooling"
    assert core.match_topics("latest on UK house prices", T)[0][1]["id"].startswith("uk-residential")
    assert core.match_topics("uk news", T)[0][1]["id"] == "uk-news-and-current-affairs"
    assert core.match_topics("bitcoin price", T) == []                 # one shared generic word is not a match
    assert core.match_topics("mcp-and-llm-tooling", T)[0][0] == 1.0


def test_a_new_topic_gets_its_first_sweep_now_not_tonight(tmp_path):
    # info-seeking plan SK-3.6: "track X" used to collect nothing until the 23:00 batch
    mod, orch = _load(tmp_path)
    mod.store().set_kv("config", dict(mod.store().get_kv("config", {}) or {}, first_sweep=True))
    r = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])
    assert r["created"] and "first sweep" in r["message"] and "first_sweep" in r
    assert ("submit", {"mode": "first", "key": f"first:{r['topic']['id']}",
                       "payload": {"topic_id": r["topic"]["id"]}}) in orch.CALLS
    t = mod.store().get_topic(r["topic"]["id"])                       # the harness runs the job inline
    assert t.get("last_sweep") and mod.store().signals(t["id"])
    again = _R(orch, "intel.topic.save", id=r["topic"]["id"], name="AI property valuation", brief="b")
    assert "first_sweep" not in again                                  # only on creation
    assert _run(mod._intel_job({"payload": {"mode": "first", "topic_id": r["topic"]["id"]}}, None))["skipped"]
    assert _run(mod._intel_job({"payload": {"mode": "first", "topic_id": "nope"}}, None))["skipped"]
