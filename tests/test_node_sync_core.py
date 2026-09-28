"""Node workers follow the commit the host RUNS (node_sync_core)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.provisioning import node_sync_core as ns  # noqa: E402

A = "a" * 40
B = "b" * 40


def _repo(tmp_path, packed=False):
    g = tmp_path / "repo" / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    if packed:
        (g / "packed-refs").write_text("# pack-refs with: peeled\n%s refs/heads/main\n" % A)
    else:
        (g / "refs" / "heads" / "main").write_text(A + "\n")
    return tmp_path / "repo"


def test_reads_the_checked_out_commit(tmp_path):
    assert ns.read_git_head(str(_repo(tmp_path))) == A


def test_reads_a_packed_ref(tmp_path):
    assert ns.read_git_head(str(_repo(tmp_path, packed=True))) == A


def test_reads_a_detached_head_and_fails_soft(tmp_path):
    r = _repo(tmp_path)
    (r / ".git" / "HEAD").write_text(B + "\n")
    assert ns.read_git_head(str(r)) == B
    assert ns.read_git_head(str(tmp_path / "nowhere")) == ""


def test_reads_through_a_linked_worktree(tmp_path):
    main = _repo(tmp_path)
    wt_admin = main / ".git" / "worktrees" / "wt"
    wt_admin.mkdir(parents=True)
    (wt_admin / "HEAD").write_text("ref: refs/heads/main\n")
    (wt_admin / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: %s\n" % wt_admin)
    assert ns.read_git_head(str(wt)) == A


def _entry(hid, **kw):
    e = {"host_id": hid, "nodename": "node-" + hid, "commit": B, "failures": 0, "last_attempt": 0}
    e.update(kw)
    return e


def _worker(hid, commit=B, status="idle"):
    return {"host": "node-" + hid, "role": "node-worker", "commit": commit, "status": status}


def test_nothing_happens_while_a_census_goal_runs():
    p = ns.plan(A, [_entry("1")], [_worker("1")], census_busy=True, now=1e6)
    assert p["run"] == [] and "census" in p["blocked"]


def test_unknown_host_commit_blocks_everything():
    assert ns.plan("", [_entry("1")], [], census_busy=False)["blocked"]


def test_one_stale_node_per_tick_in_a_stable_order():
    ents = [_entry("2"), _entry("1"), _entry("3")]
    wks = [_worker("1"), _worker("2"), _worker("3")]
    p = ns.plan(A, ents, wks, census_busy=False, now=1e6)
    assert p["run"] == ["1"]
    assert {s["host_id"] for s in p["skipped"]} == {"2", "3"}


def test_an_up_to_date_node_is_left_alone():
    p = ns.plan(A, [_entry("1", commit=A)], [_worker("1", commit=A)], census_busy=False, now=1e6)
    assert p["run"] == [] and p["up_to_date"] == ["1"]


def test_the_workers_own_report_wins_over_the_registry():
    # registry says A (what was shipped) but the worker reports B -> stale
    p = ns.plan(A, [_entry("1", commit=A)], [_worker("1", commit=B)], census_busy=False, now=1e6)
    assert p["run"] == ["1"]


def test_a_busy_worker_is_not_restarted():
    p = ns.plan(A, [_entry("1")], [_worker("1", status="running:llm.generate")],
                census_busy=False, now=1e6)
    assert p["run"] == [] and "llm.generate" in p["skipped"][0]["why"]


def test_a_dead_worker_is_reprovisioned_even_on_the_right_commit():
    p = ns.plan(A, [_entry("1", commit=A)], [], census_busy=False, now=1e6)
    assert p["run"] == ["1"]


def test_a_failing_node_backs_off():
    e = _entry("1", failures=2, last_attempt=1e6 - 100)
    p = ns.plan(A, [e], [], census_busy=False, now=1e6)
    assert p["run"] == [] and "backing off" in p["skipped"][0]["why"]
    p = ns.plan(A, [e], [], census_busy=False, now=1e6 - 100 + ns.backoff_for(2) + 1)
    assert p["run"] == ["1"]
    assert ns.backoff_for(0) == 0 and ns.backoff_for(99) == ns.BACKOFF_S[-1]


def test_record_after_counts_failures_and_clears_on_success():
    e = ns.record_after({}, ok=False, error="ssh refused", now=10)
    e = ns.record_after(e, ok=False, error="ssh refused", now=20)
    assert e["failures"] == 2 and e["last_error"] == "ssh refused" and e["last_attempt"] == 20
    e = ns.record_after(e, ok=True, commit=A, nodename="Ollama-B", now=30)
    assert e["failures"] == 0 and e["commit"] == A and e["nodename"] == "Ollama-B" and not e["last_error"]
