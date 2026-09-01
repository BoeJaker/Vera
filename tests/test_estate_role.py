"""Only the estate's owner may sweep it; a metrics write must not resurrect a
dead worker; and a container pins the image it was created with.

All three come from one incident. A foundry-provisioned Vera worker in a Proxmox
container on 192.168.0.141 ran the hourly estate sweep for a day against the
shared coordinator Redis. It survived prod restarts and the user stopping every
Loop Lab sandbox, because it was never on this host.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import estate_role as er              # noqa: E402
from vera.workers import worker_registry_hygiene as wrh  # noqa: E402
from vera.workers import image_drift as idf            # noqa: E402

pytestmark = pytest.mark.critical


# ══ 1. only the estate's owner may sweep it ═════════════════════════════════

def test_an_instance_that_cannot_see_the_estate_may_not_sweep_it():
    """The 192.168.0.141 case. There, .loop-lab-worktrees does not exist, so
    EVERY descriptor probes ABSENT and every sandbox looks reapable."""
    ok, why = er.may_sweep_estate(estate_present=False)
    assert ok is False
    assert "cannot see the sandbox estate" in why


def test_possession_is_checked_before_any_flag():
    """A flag only stops instances that carry it, and the offender predated
    every flag we could add - so absence of the estate must deny on its own,
    with no help from is_worker/is_dev_sandbox."""
    ok, _ = er.may_sweep_estate(estate_present=False, is_worker=False,
                                is_dev_sandbox=False)
    assert ok is False


def test_a_worker_on_the_estate_host_is_still_denied():
    """It would pass the possession test, so the flag has to matter too."""
    ok, why = er.may_sweep_estate(estate_present=True, is_worker=True)
    assert ok is False and "task runner" in why


def test_a_dev_sandbox_is_denied():
    ok, why = er.may_sweep_estate(estate_present=True, is_dev_sandbox=True)
    assert ok is False and "guest" in why


def test_the_estate_owner_may_sweep():
    assert er.may_sweep_estate(estate_present=True) == (True, "")


def test_only_estate_jobs_are_gated():
    """Gating everything would stop a worker doing its actual job."""
    assert er.decide("agent_rag_refresh", estate_present=False)["run"] is True
    assert er.decide("evolve.scaffolding.sweep", estate_present=False)["run"] is False


def test_the_destructive_ambient_jobs_are_all_listed():
    for j in ("evolve.scaffolding.sweep", "evolve.sandbox.idle_sweep",
              "evolve.mainline_mirror.refresh"):
        assert er.is_estate_job(j), j


def test_the_refusal_says_why():
    d = er.decide("evolve.scaffolding.sweep", estate_present=False)
    assert "evolve.scaffolding.sweep" in d["reason"] and "ABSENT" in d["reason"]


def test_an_unreadable_root_does_not_read_as_absent(tmp_path, monkeypatch):
    """Absence is what licenses deletion, so "I could not look" must never be
    reported as "it is gone" - the same asymmetry as the worktree probe."""
    def boom(_):
        raise OSError("permission denied")
    monkeypatch.setattr(er.os.path, "isdir", boom)
    assert er.estate_is_present("/some/root") is True


def test_estate_presence_is_a_real_filesystem_check(tmp_path):
    assert er.estate_is_present(str(tmp_path)) is False
    (tmp_path / er.ESTATE_DIR).mkdir()
    assert er.estate_is_present(str(tmp_path)) is True
    assert er.estate_is_present("") is False


# ══ 2. ghosts in the worker registry ════════════════════════════════════════

GHOST = {"status": "idle", "cpu_pct": "3.1", "ram_pct": "40.0", "disk_pct": "12"}
LIVE = {"id": "worker-c6f22631", "host": "LLM", "pid": "13215",
        "started": "2026-09-01T17:49:38Z", "status": "idle", "cpu_pct": "3.1"}


def test_metrics_must_not_create_a_missing_key():
    """hset CREATES a missing key. That is what turned an expired registration
    into a permanent, identity-less ghost."""
    assert wrh.may_write_metrics(key_exists=False) is False
    assert wrh.may_write_metrics(key_exists=True) is True


def test_a_key_with_no_identity_and_no_expiry_is_a_ghost():
    assert wrh.is_ghost(GHOST, wrh.NO_EXPIRY) is True


def test_a_live_worker_is_not_a_ghost():
    assert wrh.is_ghost(LIVE, 117) is False


def test_identity_alone_clears_it_even_with_no_expiry():
    """A registration mid-write can briefly lack a TTL; it is not a ghost."""
    assert wrh.is_ghost(LIVE, wrh.NO_EXPIRY) is False


def test_a_ttl_alone_clears_it_even_with_no_identity():
    """Something is maintaining it, so it will clean itself up."""
    assert wrh.is_ghost(GHOST, 117) is False


def test_classify_separates_the_observed_registry():
    """The exact three keys seen on 2026-09-01 with one instance running."""
    plan = wrh.classify([
        {"id": "worker-c6f22631", "record": LIVE, "ttl": 117},
        {"id": "worker-4f5f38d6", "record": GHOST, "ttl": -1},
        {"id": "worker-b6c3cead", "record": GHOST, "ttl": -1},
    ])
    assert plan["live"] == ["worker-c6f22631"]
    assert plan["ghost_ids"] == ["worker-4f5f38d6", "worker-b6c3cead"]
    assert "2 GHOST(S)" in wrh.describe(plan)


def test_describe_is_quiet_when_healthy():
    plan = wrh.classify([{"id": "w", "record": LIVE, "ttl": 117}])
    assert "no ghosts" in wrh.describe(plan)


def test_registry_junk_does_not_raise():
    assert wrh.classify(None)["ghosts"] == []
    assert wrh.classify(["nope", None, {}])["ghosts"] == []
    assert wrh.is_ghost(None, -1) is False


# ══ 3. container image drift ════════════════════════════════════════════════

def _c(name, tag, image_id):
    return {"Names": ["/" + name], "Image": tag, "ImageID": image_id}


IMAGES = {"vera:latest": "sha256:NEW", "code-server:latest": "sha256:CS"}


def test_a_container_on_a_superseded_image_is_drifted():
    plan = idf.classify([_c("vera-worker-1", "vera:latest", "sha256:OLD")], IMAGES)
    assert [r["name"] for r in plan["drifted"]] == ["vera-worker-1"]


def test_a_container_on_the_current_image_is_not():
    plan = idf.classify([_c("vera-worker-1", "vera:latest", "sha256:NEW")], IMAGES)
    assert plan["drifted"] == [] and plan["current"] == ["vera-worker-1"]


def test_loop_lab_sandboxes_are_excluded_on_purpose():
    """They are pinned to a branch; rolling them forward would destroy exactly
    what they were spawned to test."""
    plan = idf.classify([_c("vera-dev-feat-x", "vera:latest", "sha256:OLD")], IMAGES)
    assert plan["drifted"] == []
    assert plan["skipped"][0]["name"] == "vera-dev-feat-x"


def test_session_sandboxes_are_excluded_too():
    plan = idf.classify([_c("vera-sbx-abc", "vera:latest", "sha256:OLD")], IMAGES)
    assert plan["drifted"] == []


def test_infrastructure_is_not_judged_against_the_vera_build():
    for name in ("vera-registry", "vera-garage", "vera-openbao"):
        plan = idf.classify([_c(name, "vera:latest", "sha256:OLD")], IMAGES)
        assert plan["drifted"] == [], name


def test_an_unresolvable_tag_decides_nothing():
    """Acting on a tag we cannot resolve would recreate a container for no
    reason."""
    plan = idf.classify([_c("vera-worker-1", "mystery:tag", "sha256:OLD")], IMAGES)
    assert plan["drifted"] == [] and plan["unknown"][0]["name"] == "vera-worker-1"


def test_the_note_says_a_restart_will_not_fix_it():
    """The trap that let this run for weeks: restart reuses the same image."""
    plan = idf.classify([_c("vera-worker-1", "vera:latest", "sha256:OLD")], IMAGES)
    note = idf.describe(plan)
    assert "RECREATED" in note and "restart will NOT" in note


def test_drift_junk_does_not_raise():
    assert idf.classify(None, None)["drifted"] == []
    assert idf.classify(["nope", None], {})["drifted"] == []


# ══ call sites ══════════════════════════════════════════════════════════════

def _read(*parts):
    with open(os.path.join(os.path.dirname(__file__), "..", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_scheduler_consults_the_estate_gate():
    src = _read("vera", "capability_orchestration.py")
    assert "_estate_role.is_estate_job(task[\"name\"])" in src
    assert "estate_present=_ESTATE_PRESENT" in src


def test_the_metrics_loop_checks_the_key_exists():
    src = _read("vera", "workers", "workers.py")
    body = src[src.index("for wid in list(WORKER_REGISTRY.keys()):"):]
    body = body[:body.index("meta = WORKER_META.get(wid)")]
    # The call APPEARING is not enough - it has to be the live condition, and
    # it has to skip. `if False and not _wrh.may_write_metrics(...)` still
    # contains the call and guards nothing.
    assert "if _wrh is not None and not _wrh.may_write_metrics(" in body
    guard = body[body.index("if _wrh is not None and not _wrh.may_write_metrics("):]
    assert "continue" in guard[:300], "the guard must skip the write"
    assert body.index("may_write_metrics") < body.index("await r.hset"), \
        "the guard must precede the metrics hset"


def test_the_ttl_is_refreshed_on_any_write_not_just_ssh_entries():
    """It used to sit inside `if meta:`, which is what made ghosts permanent."""
    src = _read("vera", "workers", "workers.py")
    body = src[src.index("for wid in list(WORKER_REGISTRY.keys()):"):]
    before_meta = body[:body.index("meta = WORKER_META.get(wid)")]
    assert 'await r.expire(f"vera:workers:{wid}", 120)' in before_meta


def test_workers_module_uses_absolute_imports_only():
    """It is a loader entry point - a relative import there has no parent
    package and unregistered a whole subsystem once already."""
    src = _read("vera", "workers", "workers.py")
    assert "from Vera.vera.workers import worker_registry_hygiene" in src
    assert "from .worker_registry_hygiene" not in src
