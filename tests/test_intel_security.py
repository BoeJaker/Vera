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
