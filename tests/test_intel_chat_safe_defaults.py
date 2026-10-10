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
