"""Specialist (non-LLM) models: component versions and the per-node view.

Nothing could say whether two nodes ran the same NLP server, or whether a node
was behind the host (2026-09-28): a deployed component had no version at all.
The version is now the content of what a deploy ships, written beside it, and
the server re-checks its own files so a hand edit cannot pass for a version.

Imported lowercase with the worktree on sys.path so the worktree copy is tested."""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.provisioning import components_core as cc  # noqa: E402
from vera.catalog import specialist_core as sc  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ── versions ──────────────────────────────────────────────────────────────────
def test_version_is_the_content_not_the_order():
    a = cc.component_version("nlp_server", [("a.py", b"1"), ("b.py", b"2")])
    b = cc.component_version("nlp_server", [("b.py", b"2"), ("a.py", b"1")])
    assert a == b and a["version"].startswith("code-") and len(a["version"]) == 17
    c = cc.component_version("nlp_server", [("a.py", b"1"), ("b.py", b"3")])
    assert c["version"] != a["version"]
    # a rename is a different version too
    d = cc.component_version("nlp_server", [("a.py", b"1"), ("c.py", b"2")])
    assert d["version"] != a["version"]


def test_compare_names_the_state_and_the_files():
    host = cc.component_version("x", [("a.py", b"1"), ("b.py", b"2")])
    assert cc.compare_versions(host, dict(host))["state"] == "current"
    node = cc.component_version("x", [("a.py", b"1"), ("b.py", b"old")])
    got = cc.compare_versions(host, node)
    assert got == {"state": "behind", "changed": ["b.py"]}
    assert cc.compare_versions(host, {})["state"] == "unversioned"
    assert cc.compare_versions(host, None)["state"] == "absent"


def test_version_record_round_trips_through_the_ssh_read():
    rec = cc.component_version("nlp_server", [("a.py", b"1")])
    assert cc.parse_version(cc.version_json(rec).decode()) == rec
    assert cc.parse_version("") == {} and cc.parse_version("not json") == {}
    assert cc.parse_version(json.dumps({"version": "x"})) == {}      # wrong schema
    cmd = cc.version_lookup_cmd("nlp_server")
    for d in cc.EDGE_DIR_CANDIDATES:                  # same dirs the deploy probes
        assert d in cmd
    assert "nlp_server.version.json" in cmd


def _server_record(tmp_path, edit=None, with_version=True):
    """Run nlp_server.component_record() in a subprocess, from a deploy-shaped dir."""
    d = tmp_path / "edge"
    d.mkdir()
    shutil.copy(os.path.join(_ROOT, "edge", "nlp_server.py"), d / "nlp_server.py")
    files = [("nlp_server.py", (d / "nlp_server.py").read_bytes()), ("<deps>", b"{}")]
    if with_version:
        (d / "nlp_server.version.json").write_bytes(
            cc.version_json(cc.component_version("nlp_server", files)))
    if edit:
        with open(d / "nlp_server.py", "a", encoding="utf-8") as fh:
            fh.write(edit)
    out = subprocess.run(
        [sys.executable, "-c",
         "import json, nlp_server; print(json.dumps(nlp_server.component_record()))"],
        cwd=str(d), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]), files


def test_the_server_reports_the_version_it_was_deployed_with(tmp_path):
    rec, files = _server_record(tmp_path)
    assert rec["version"] == cc.component_version("nlp_server", files)["version"]
    assert rec["intact"] is True and rec["changed"] == []


def test_a_hand_edit_on_the_node_cannot_pass_for_the_version(tmp_path):
    rec, _ = _server_record(tmp_path, edit="\n# edited on the node\n")
    assert rec["intact"] is False and rec["changed"] == ["nlp_server.py"]


def test_a_server_deployed_before_versions_says_so(tmp_path):
    rec, _ = _server_record(tmp_path, with_version=False)
    assert rec["version"] == "" and rec["intact"] is None


# ── bringing nodes to the host's version ──────────────────────────────────────
def test_sync_updates_nodes_it_does_not_spread_the_component():
    plan = {p["host_id"]: p for p in cc.component_sync_plan([
        {"host_id": "a", "state": "current", "running": True},
        {"host_id": "b", "state": "behind", "changed": ["nlp_server.py"], "running": True},
        {"host_id": "c", "state": "behind", "changed": [cc.DEPS_ENTRY], "running": True},
        {"host_id": "d", "state": "unversioned", "running": True},
        {"host_id": "e", "state": "unversioned", "running": False},
        {"host_id": "f", "state": "absent", "running": False},
    ])}
    assert plan["a"]["action"] == "skip"
    # a code-only change needs no ~2 GB pip install
    assert plan["b"]["action"] == "deploy" and plan["b"]["install_deps"] is False
    assert plan["c"]["install_deps"] is True
    # nothing says what an unversioned node has installed
    assert plan["d"]["action"] == "deploy" and plan["d"]["install_deps"] is True
    # never deployed there: a sync must not put it on a new node
    assert plan["e"]["action"] == "skip" and plan["f"]["action"] == "skip"


def test_a_systemd_redeploy_restarts_the_running_unit():
    # `enable --now` leaves a running unit on the old code: the redeploy
    # "succeeds" and changes nothing
    src = open(os.path.join(_ROOT, "vera", "provisioning", "components_capabilities.py"),
               encoding="utf-8").read()
    body = src[src.index("async def _launch_systemd"):]
    body = body[:body.index("\nasync def ", 1) if "\nasync def " in body[1:] else len(body)]
    assert "systemctl restart {svc}" in body and "enable --now {svc}" not in body


# ── the per-node view ─────────────────────────────────────────────────────────
def _node(component, tasks=None):
    return {"node_id": "gpu-250", "nlp_url": "http://192.168.0.250:8771", "threads": 4,
            "component": component,
            "tasks": tasks or {"ner": {"model": "m/ner", "present": True, "loaded": True},
                               "qa": {"model": "m/qa", "present": False, "loaded": False}}}


def test_nlp_row_states():
    host = cc.component_version("nlp_server", [("a.py", b"1")])
    row = sc.nlp_node_row(_node(dict(host, intact=True)), host, cc.compare_versions)
    assert row["state"] == "current" and row["missing"] == ["qa"]
    assert row["tasks"]["ner"] == {"model": "m/ner", "present": True, "loaded": True,
                                   "package": ""}
    old = cc.component_version("nlp_server", [("a.py", b"0")])
    assert sc.nlp_node_row(_node(dict(old, intact=True)), host,
                           cc.compare_versions)["state"] == "behind"
    # modified wins even when the recorded version matches the host's
    mod = dict(host, intact=False, changed=["a.py"])
    row = sc.nlp_node_row(_node(mod), host, cc.compare_versions)
    assert row["state"] == "modified" and row["changed"] == ["a.py"]
    assert sc.nlp_node_row(_node({}), host, cc.compare_versions)["state"] == "unversioned"


def test_media_row_and_summary():
    m = sc.media_node_row("media-gpu", {"url": "http://192.168.0.250:8765", "status": "online",
                                        "has_gpu": True, "services": ["stt", "tts"],
                                        "detail": {"tts_engine": "kokoro", "cuda": True}})
    assert m["serves"] == {"tts_engine": "kokoro", "cuda": True}
    host = cc.component_version("nlp_server", [("a.py", b"1")])
    rows = [sc.nlp_node_row(_node(dict(host, intact=True)), host, cc.compare_versions),
            sc.nlp_node_row(_node({}), host, cc.compare_versions)]
    s = sc.summarize(rows, [m])
    assert s == {"nlp_nodes": 2, "nlp_current": 1, "nlp_not_current": 1,
                 "nlp_missing_models": 2, "media_nodes": 1, "media_online": 1}
