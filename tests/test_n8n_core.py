"""Tests for the n8n integration's pure logic (vera/n8n/n8n_core.py).

URL derivation matters because a wrong webhook/MCP URL fails as a 404 that
looks like a broken workflow, and the DAG exporter matters because a silent
translation loss produces an n8n workflow that runs but does the wrong thing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.n8n import n8n_core as core  # noqa: E402


# ── URLs ────────────────────────────────────────────────────────────────────
def test_join_url_avoids_double_slashes():
    assert core.join_url("https://h/", "/mcp/", "/vera/") == "https://h/mcp/vera"
    assert core.join_url("https://h", "a", "", "b") == "https://h/a/b"


def test_mcp_urls_use_the_prefixes_n8n_registers():
    urls = core.mcp_urls("https://n8n.corp.local", "vera")
    assert urls["production"] == "https://n8n.corp.local/mcp/vera"
    assert urls["test"] == "https://n8n.corp.local/mcp-test/vera"


def test_webhook_urls():
    urls = core.webhook_urls("https://n8n.corp.local/", "/ping")
    assert urls["production"] == "https://n8n.corp.local/webhook/ping"
    assert urls["test"] == "https://n8n.corp.local/webhook-test/ping"


def test_slug_and_cap_name():
    assert core.slugify("My Report 2026!") == "my_report_2026"
    assert core.slugify("") == "workflow"
    assert core.workflow_cap_name("Daily Digest") == "n8n.daily_digest"


# ── workflow inspection ─────────────────────────────────────────────────────
def _wf(nodes, name="wf", wid="1", active=True):
    return {"id": wid, "name": name, "active": active, "nodes": nodes,
            "connections": {}}


def test_extract_triggers_finds_mcp_and_webhook_with_urls():
    wf = _wf([
        {"name": "MCP", "type": core.MCP_TRIGGER_TYPE,
         "parameters": {"path": "vera"}},
        {"name": "Hook", "type": core.WEBHOOK_TRIGGER_TYPE,
         "parameters": {"path": "ping", "httpMethod": "POST"}},
        {"name": "Set", "type": "n8n-nodes-base.set", "parameters": {}},
    ])
    trg = core.extract_triggers(wf, "https://n8n.corp.local")
    kinds = {t["kind"] for t in trg}
    assert kinds == {"mcp", "webhook"}
    mcp = next(t for t in trg if t["kind"] == "mcp")
    assert mcp["urls"]["production"] == "https://n8n.corp.local/mcp/vera"
    hook = next(t for t in trg if t["kind"] == "webhook")
    assert hook["method"] == "POST"
    assert hook["urls"]["production"] == "https://n8n.corp.local/webhook/ping"


def test_extract_triggers_without_base_url_omits_urls():
    wf = _wf([{"name": "MCP", "type": core.MCP_TRIGGER_TYPE,
               "parameters": {"path": "vera"}}])
    assert "urls" not in core.extract_triggers(wf)[0]


def test_workflow_summary():
    wf = _wf([{"name": "Hook", "type": core.WEBHOOK_TRIGGER_TYPE,
               "parameters": {"path": "p"}}], name="N", wid="9")
    s = core.workflow_summary(wf, "https://h")
    assert s["id"] == "9" and s["name"] == "N" and s["active"] is True
    assert s["node_count"] == 1
    assert s["node_types"] == [core.WEBHOOK_TRIGGER_TYPE]


# ── Vera DAG → n8n ──────────────────────────────────────────────────────────
def test_dag_export_chains_sequential_nodes():
    dag = [["fabric.query", "a"], ["memory.seek", "b"]]
    out = core.vera_dag_to_n8n(dag, "wf", "https://vera:8999")
    wf = out["workflow"]
    names = [n["name"] for n in wf["nodes"]]
    assert names[0].startswith("When clicking")
    assert names[1:] == ["fabric.query", "memory.seek"]
    # trigger → first cap → second cap
    assert wf["connections"][names[0]]["main"][0][0]["node"] == "fabric.query"
    assert wf["connections"]["fabric.query"]["main"][0][0]["node"] == "memory.seek"
    assert out["notes"] == []


def test_dag_export_targets_veras_mcp_call():
    out = core.vera_dag_to_n8n([["system.ping", "p"]], "wf", "https://vera:8999")
    node = next(n for n in out["workflow"]["nodes"]
                if n["type"] == "n8n-nodes-base.httpRequest")
    assert node["parameters"]["url"] == "https://vera:8999/mcp/call"
    assert "system.ping" in node["parameters"]["jsonBody"]
    assert node["parameters"]["method"] == "POST"


def test_dag_export_fans_out_parallel_group_and_flags_no_rejoin():
    dag = [["a.one", "x"], [["b.two", "y"], ["c.three", "z"]]]
    out = core.vera_dag_to_n8n(dag, "wf", "https://v")
    conns = out["workflow"]["connections"]["a.one"]["main"][0]
    assert {c["node"] for c in conns} == {"b.two", "c.three"}
    # Vera merges branches into shared state; n8n needs an explicit Merge node,
    # and that difference must be reported rather than silently dropped.
    assert any("Merge" in n for n in out["notes"])


def test_dag_export_reports_dropped_callable_condition():
    dag = [["a.one", "x", lambda s: True]]
    out = core.vera_dag_to_n8n(dag, "wf", "https://v")
    assert any("callable condition" in n for n in out["notes"])


def test_dag_export_reports_string_condition_as_if_node():
    dag = [["a.one", "x", "CONDITION:ready"]]
    out = core.vera_dag_to_n8n(dag, "wf", "https://v")
    assert any("'ready'" in n and "IF node" in n for n in out["notes"])


def test_dag_export_deduplicates_repeated_capability_names():
    """n8n node names must be unique or connections address the wrong node."""
    out = core.vera_dag_to_n8n([["a.one", "x"], ["a.one", "y"]], "wf", "https://v")
    names = [n["name"] for n in out["workflow"]["nodes"]]
    assert len(names) == len(set(names))
    assert "a.one" in names and "a.one 2" in names


def test_dag_export_skips_unrecognised_node_with_a_note():
    out = core.vera_dag_to_n8n([["a.one", "x"], "garbage"], "wf", "https://v")
    assert any("unrecognised" in n for n in out["notes"])


def test_dag_export_without_manual_trigger():
    out = core.vera_dag_to_n8n([["a.one", "x"]], "wf", "https://v",
                               add_manual_trigger=False)
    assert all(n["type"] != "n8n-nodes-base.manualTrigger"
               for n in out["workflow"]["nodes"])


# ── n8n workflow → Vera DAG node ────────────────────────────────────────────
def test_workflow_as_dag_node_is_a_legal_two_element_node():
    wf = _wf([{"name": "MCP", "type": core.MCP_TRIGGER_TYPE,
               "parameters": {"path": "vera"}}], name="Daily Digest")
    got = core.n8n_workflow_as_dag_node(wf)
    assert got["dag_node"] == ["n8n.daily_digest", "daily_digest"]
    assert got["usable"] is True
    assert "mcp" in got["callable_via"]


def test_workflow_as_dag_node_honours_explicit_out_key():
    wf = _wf([{"name": "H", "type": core.WEBHOOK_TRIGGER_TYPE,
               "parameters": {"path": "p"}}], name="X")
    assert core.n8n_workflow_as_dag_node(wf, out_key="result")["dag_node"][1] == "result"


def test_workflow_without_trigger_is_not_usable():
    """No inbound trigger means nothing outside n8n can start it — saying
    otherwise would hand back a DAG node that always fails."""
    wf = _wf([{"name": "Set", "type": "n8n-nodes-base.set", "parameters": {}}])
    got = core.n8n_workflow_as_dag_node(wf)
    assert got["usable"] is False
    assert "no inbound trigger" in got["note"]
