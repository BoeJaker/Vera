"""Intel security fixes from the 2026-10-10 review (D9): repository paths, settings that redirect links or read
folders, and one-time action tokens in the activity timeline."""
from test_intel_capabilities import _load, _R, _run   # noqa: F401  (the intel test harness)


def test_repo_urls_cannot_climb_out_of_the_repo_folder(tmp_path):
    mod, orch = _load(tmp_path)
    for bad in ("https://github.com/../..", "https://github.com/./x", "https://github.com/owner/..",
                "https://gitlab.com/.hidden/repo", "https://github.com/owner/...git"):
        assert mod.core.repo_of(bad) is None, bad
    assert mod.core.repo_of("https://github.com/langchain-ai/langgraph")["repo"] == "langgraph"
    assert "error" in _R(orch, "intel.repo.get", url="https://github.com/../..")


def test_settings_that_redirect_links_or_read_folders_are_checked(tmp_path):
    mod, orch = _load(tmp_path)
    ok = _R(orch, "intel.config.set", public_base="https://llm.int:8999/")
    assert ok["ok"] and mod.config()["public_base"] == "https://llm.int:8999" and "rejected" not in ok
    for bad in ("https://evil.example.com", "https://llm.int:8999/x?y=1", "javascript:alert(1)",
                "https://user@llm.int"):
        r = _R(orch, "intel.config.set", public_base=bad)
        assert r["rejected"] and mod.config()["public_base"] == "https://llm.int:8999", bad
    assert _R(orch, "intel.config.set", public_base="http://myhome.duckdns.org:8999")["ok"]
    root = mod.DROP_ROOT
    good = {"kind": "notes", "path": root + "/notes/"}
    r = _R(orch, "intel.config.set", settings={"drop_folders": [
        good, {"kind": "inbox", "path": "/etc"}, {"kind": "print", "path": root + "/../../etc"},
        {"kind": "podcast", "path": "relative/path"}]})
    assert [f["path"] for f in mod.config()["drop_folders"]] == [root + "/notes"]
    assert r["rejected"] and "drop_folders" in r["rejected"][0]
    _R(orch, "intel.config.set", settings={"persona": "x" * 10000})
    assert len(mod.config()["persona"]) == mod.PERSONA_MAX


def test_the_activity_timeline_never_carries_a_one_time_token(tmp_path):
    from vera.intel import intel_activity_core as act
    ref = act.action_ref("secret-token-123")
    assert ref.startswith("act-") and "secret" not in ref and ref == act.action_ref("secret-token-123")
    evs = act.events(actions=[{"token": "secret-token-123", "status": "done", "label": "Research further",
                               "created": "2026-10-09T08:00:00Z", "topic_id": "t"}])
    assert evs and all("secret-token-123" not in str(e) for e in evs)


def test_a_source_with_more_new_items_than_a_page_is_read_to_the_cursor(tmp_path):
    # review D7: one page (srcs.PER_SOURCE) was all a sweep read, then the cursor jumped to the newest
    mod, orch = _load(tmp_path)
    recs = [{"created_at": "2026-10-10T%02d:%02d:00Z" % (i // 60, i % 60), "data": {"title": f"item {i}"}}
            for i in range(100)]
    recs.sort(key=lambda r: r["created_at"], reverse=True)            # the fabric returns newest first
    calls = []

    async def browse(dataset_id, limit=50, offset=0, trace_id=None):
        calls.append((offset, limit))
        return {"records": recs[offset:offset + limit], "has_more": offset + limit < len(recs)}
    orch.CAPABILITY_REGISTRY["fabric.browse"] = {"func": browse, "raw": browse}
    cursor = "2026-10-10T00:24:00Z"                                  # 75 records are newer than this
    r = _run(mod._browse_since("ds", cursor))
    newer = [x for x in r["records"] if x["created_at"] > cursor]
    assert len(newer) == 75 and not r.get("truncated") and len(calls) == 3
    calls.clear()
    assert len(_run(mod._browse_since("ds", ""))["records"]) == mod.srcs.PER_SOURCE and len(calls) == 1
    mod.SOURCE_MAX_PAGES = 2                                          # a flood beyond the cap is reported
    assert _run(mod._browse_since("ds", "2000-01-01T00:00:00Z"))["truncated"]


def test_research_from_a_report_button_comes_back_and_a_failed_status_check_is_not_fatal(tmp_path):
    # review D8: report-button research was never registered; one failed status call forgot a job for good
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation", entities=["Acme"])["topic"]
    a = mod.core.make_action("research", topic_id=t["id"], query="How accurate are AI valuations?")
    mod.store().put_action(a)
    assert _R(orch, "intel.action.run", token=a["token"])["ok"]
    assert mod.store().get_kv("research_jobs")["j1"]["topic_id"] == t["id"]

    state = {"n": 0}

    async def flaky(job_id="", trace_id=None):
        state["n"] += 1
        return {"error": "research module restarting"}
    orch.CAPABILITY_REGISTRY["research.job.status"] = {"func": flaky, "raw": flaky}
    for i in range(mod.RESEARCH_FORGET_AFTER - 1):
        _run(mod._research_signals(mod.store().get_topic(t["id"])))
        assert mod.store().get_kv("research_jobs")["j1"]["status_errors"] == i + 1
    _run(mod._research_signals(mod.store().get_topic(t["id"])))
    assert "j1" not in mod.store().get_kv("research_jobs")                # let go after repeated failures


def test_a_job_started_during_the_status_checks_is_not_overwritten(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation")["topic"]
    mod._remember_research("j-old", t["id"], "q")

    async def status_that_starts_another(job_id="", trace_id=None):
        mod._remember_research("j-new", "other-topic", "q2")             # a session starts one meanwhile
        return {"status": "running"}
    orch.CAPABILITY_REGISTRY["research.job.status"] = {"func": status_that_starts_another,
                                                       "raw": status_that_starts_another}
    _run(mod._research_signals(mod.store().get_topic(t["id"])))
    assert set(mod.store().get_kv("research_jobs")) == {"j-old", "j-new"}


def test_a_failed_alert_is_retried_not_marked_sent(tmp_path):
    mod, orch = _load(tmp_path)
    t = _R(orch, "intel.topic.save", name="AI property valuation")["topic"]
    mod.store().set_kv("config", dict(mod.store().get_kv("config", {}) or {}, alert_channels=["telegram"]))
    state = {"fail": True, "sent": 0}

    async def notify(text="", idempotency_key="", trace_id=None):
        if state["fail"]:
            return {"error": "telegram unreachable"}
        state["sent"] += 1
        return {"ok": True}
    orch.CAPABILITY_REGISTRY["tg.notify"] = {"func": notify, "raw": notify}
    snap = {"id": "s1", "headline": "Acme went live", "state": "x", "confidence": "high"}
    _run(mod._alert(mod.store().get_topic(t["id"]), snap, "material"))
    assert "s1" not in (mod.store().get_kv("alerts_sent", []) or [])
    assert mod.store().get_kv("alerts_retry")[0]["snap"] == "s1"
    assert mod.store().get_kv("alerts")[-1]["failed"] == ["telegram"]
    state["fail"] = False
    _run(mod._retry_alerts())
    assert "s1" in mod.store().get_kv("alerts_sent") and not mod.store().get_kv("alerts_retry") and state["sent"] == 1
    # one that keeps failing is let go after ALERT_RETRIES tries in all
    state["fail"] = True
    _run(mod._alert(mod.store().get_topic(t["id"]), dict(snap, id="s2"), "material"))
    for _ in range(mod.ALERT_RETRIES):
        _run(mod._retry_alerts())
    assert not mod.store().get_kv("alerts_retry")


def test_link_previews_are_redirected_but_not_counted(tmp_path):
    mod, orch = _load(tmp_path)

    class Req:
        def __init__(self, ua): self.headers = {"user-agent": ua}
    assert mod._is_preview(Req("TelegramBot (like TwitterBot)")) and mod._is_preview(Req("facebookexternalhit/1.1"))
    assert not mod._is_preview(Req("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"))
    assert not mod._is_preview(None)
    seen = []
    mod._outcome = lambda *a, **k: seen.append(a)
    resp = _run(mod._go_alert("abc123", Req("TelegramBot (like TwitterBot)")))
    assert resp.status_code == 302 and seen == []
    _run(mod._go_alert("abc123", Req("Mozilla/5.0 Safari")))
    assert seen and seen[0][2] == "alert.acted"


def test_deleting_a_topic_cleans_up_what_it_left_running(tmp_path):
    mod, orch = _load(tmp_path)
    deleted = []

    async def think_create(**kw):
        return {"ok": True}

    async def think_delete(name="", trace_id=None):
        deleted.append(("think", name)); return {"ok": True}

    async def src_delete(source_id="", trace_id=None):
        deleted.append(("source", source_id)); return {"deleted": True}
    for n, f in (("dream.think.create", think_create), ("dream.think.delete", think_delete),
                 ("fabric.sources.delete", src_delete)):
        orch.CAPABILITY_REGISTRY[n] = {"func": f, "raw": f}
    a = _R(orch, "intel.topic.save", name="Topic A", dream_loop=True)["topic"]
    b = _R(orch, "intel.topic.save", name="Topic B")["topic"]
    mod.store().patch_topic(a["id"], {"sources": [{"id": "intel-src-only-a", "dataset_id": "d1"},
                                                  {"id": "intel-src-shared", "dataset_id": "d2"}]})
    mod.store().patch_topic(b["id"], {"sources": [{"id": "intel-src-shared", "dataset_id": "d2"}]})
    assert _R(orch, "intel.indicator.add", topic_id=a["id"], kind="count", dataset_id="ds.1")["ok"]
    mod._remember_research("j9", a["id"], "q")
    mod.store().set_kv(f"wv_over:{a['id']}", ["x"])

    r = _R(orch, "intel.topic.delete", id=a["id"])
    assert r["ok"] and r["cleaned"]["think_loop"] and r["cleaned"]["indicators"]
    assert ("think", "think_intel_topic_a") in deleted
    assert r["cleaned"]["fabric_sources_removed"] == ["intel-src-only-a"]          # the shared one is kept
    assert not mod._indicators(a["id"]) and "j9" not in (mod.store().get_kv("research_jobs") or {})
    assert mod.store().get_kv(f"wv_over:{a['id']}") is None


def test_switching_the_dream_loop_off_stops_it(tmp_path):
    mod, orch = _load(tmp_path)
    deleted = []

    async def think_create(**kw):
        return {"ok": True}

    async def think_delete(name="", trace_id=None):
        deleted.append(name); return {"ok": True}
    for n, f in (("dream.think.create", think_create), ("dream.think.delete", think_delete)):
        orch.CAPABILITY_REGISTRY[n] = {"func": f, "raw": f}
    t = _R(orch, "intel.topic.save", name="Topic A", dream_loop=True)["topic"]
    _R(orch, "intel.topic.save", id=t["id"], name="Topic A", dream_loop=False)
    assert deleted == ["think_intel_topic_a"] and t["id"] not in (mod.store().get_kv("think_loops") or {})
