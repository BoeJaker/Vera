"""Provenance stamping — the change-traceability guard from
documentation/specs/dev-lifecycle-and-repo-hygiene.md §5.1. Pure/deterministic:
tests the shape of get_provenance() and the setdefault + never-raise semantics of
event_stamp (which runs on every emitted event, so it MUST be cheap and safe).
"""

from vera import provenance as prov


def test_get_provenance_shape_and_types():
    p = prov.get_provenance()
    for k in ("git_sha", "git_sha_short", "branch", "dirty",
              "instance", "pid", "started_at"):
        assert k in p, f"missing provenance field: {k}"
    assert isinstance(p["dirty"], bool)
    assert isinstance(p["pid"], int)
    assert isinstance(p["git_sha_short"], str) and len(p["git_sha_short"]) <= 10
    # Cached — a second call returns the same boot-time snapshot.
    assert prov.get_provenance()["git_sha"] == p["git_sha"]


def test_event_stamp_adds_compact_fields():
    ev = {"type": "x"}
    prov.event_stamp(ev)
    assert "ver" in ev and "br" in ev and "dirty" in ev
    assert isinstance(ev["dirty"], bool)
    # ver is the short sha (or empty if git is unavailable — never a crash).
    assert ev["ver"] == prov.get_provenance()["git_sha_short"]


def test_event_stamp_never_overwrites_existing():
    ev = {"type": "x", "ver": "CUSTOM", "br": "mybranch", "dirty": True}
    prov.event_stamp(ev)
    assert ev["ver"] == "CUSTOM" and ev["br"] == "mybranch" and ev["dirty"] is True


def test_event_stamp_is_quiet_and_returns_none():
    # It runs on EVERY event — it must never raise and must return None.
    assert prov.event_stamp({}) is None
    assert prov.event_stamp({"ts": "now"}) is None


# ── resolution order: env → git → persisted file (the container fallback) ──

def test_from_env_reads_injected_vars():
    import os
    keys = ("VERA_GIT_SHA", "VERA_GIT_BRANCH", "VERA_GIT_DIRTY")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["VERA_GIT_SHA"] = "abcdef1234567890"
        os.environ["VERA_GIT_BRANCH"] = "feat/x"
        os.environ["VERA_GIT_DIRTY"] = "true"
        c = prov._from_env()
        assert c["git_sha"] == "abcdef1234567890"
        assert c["git_sha_short"] == "abcdef1234"
        assert c["branch"] == "feat/x"
        assert c["dirty"] is True
        assert c["source"] == "env"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_from_env_empty_without_sha():
    import os
    saved = os.environ.pop("VERA_GIT_SHA", None)
    try:
        assert prov._from_env() == {}
    finally:
        if saved is not None:
            os.environ["VERA_GIT_SHA"] = saved


def test_from_file_reads_persisted(tmp_path):
    # This is the path a git-less container actually takes: a host-side bring-up
    # persisted the JSON, the container reads it.
    import json
    f = tmp_path / ".vera-provenance.json"
    f.write_text(json.dumps({"git_sha": "deadbeefcafe0000",
                             "branch": "loop-lab/z", "dirty": False}))
    saved = prov._PROV_FILE
    try:
        prov._PROV_FILE = str(f)
        c = prov._from_file()
        assert c["git_sha"] == "deadbeefcafe0000"
        assert c["git_sha_short"] == "deadbeefca"
        assert c["branch"] == "loop-lab/z"
        assert c["source"] == "file"
    finally:
        prov._PROV_FILE = saved


def test_from_file_absent_returns_empty(tmp_path):
    saved = prov._PROV_FILE
    try:
        prov._PROV_FILE = str(tmp_path / "does-not-exist.json")
        assert prov._from_file() == {}
    finally:
        prov._PROV_FILE = saved


def test_required_identity_can_refresh_an_early_unresolved_cache(monkeypatch):
    attempts = iter(({}, {
        "git_sha": "abcdef1234567890", "git_sha_short": "abcdef1234",
        "branch": "main", "dirty": False, "source": "git",
    }))
    monkeypatch.setattr(prov, "_CACHE", {})
    monkeypatch.setattr(prov, "_from_env", lambda: {})
    monkeypatch.setattr(prov, "_from_git", lambda: next(attempts))
    monkeypatch.setattr(prov, "_from_file", lambda: {})
    monkeypatch.setattr(prov, "_persist", lambda core: None)

    assert prov.get_provenance()["source"] == "none"
    # Ordinary reads remain cheap and keep the boot result.
    assert prov.get_provenance()["source"] == "none"
    refreshed = prov.get_provenance(refresh_unresolved=True)
    assert refreshed["git_sha"] == "abcdef1234567890"
    assert refreshed["source"] == "git"
    # Once resolved, even a refresh request preserves the process identity.
    assert prov.get_provenance(refresh_unresolved=True) == refreshed


def test_supported_import_names_share_one_boot_identity(monkeypatch):
    """A late alias import cannot claim a worktree commit loaded after boot."""
    import builtins
    import importlib.util

    shared = {}
    monkeypatch.setattr(builtins, prov._PROCESS_CACHE_KEY, shared, raising=False)
    monkeypatch.setattr(prov, "_CACHE", shared)
    monkeypatch.setattr(prov, "_from_env", lambda: {})
    monkeypatch.setattr(prov, "_from_git", lambda: {
        "git_sha": "boot1234567890", "git_sha_short": "boot123456",
        "branch": "bleeding-edge", "dirty": False, "source": "git",
    })
    monkeypatch.setattr(prov, "_persist", lambda core: None)

    boot = prov.get_provenance()
    spec = importlib.util.spec_from_file_location(
        "vera_test_alternate_provenance", prov.__file__)
    assert spec is not None and spec.loader is not None
    alternate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alternate)
    monkeypatch.setattr(alternate, "_from_env", lambda: {})
    monkeypatch.setattr(alternate, "_from_git", lambda: {
        "git_sha": "later0987654321", "git_sha_short": "later09876",
        "branch": "moved-worktree", "dirty": True, "source": "git",
    })

    assert alternate._CACHE is shared
    assert alternate.get_provenance(refresh_unresolved=True) == boot
    assert alternate.get_provenance()["git_sha"] == "boot1234567890"
