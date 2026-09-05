"""
n8n_core.py — pure logic for the n8n integration
================================================

URL derivation and DAG/workflow translation, kept free of app and network
imports so it is unit-testable without booting Vera
(`tests/test_n8n_core.py` imports it as `vera.n8n.n8n_core`).

The interop between Vera DAGs and n8n workflows is deliberately **asymmetric**,
because the two models are not equivalent and pretending otherwise would
produce a converter that silently lies:

  Vera DAG → n8n workflow   Faithful for the subset a Vera DAG actually is: an
                            ordered list of capability calls with parallel
                            groups. Each capability becomes an HTTP Request node
                            calling Vera's own `/mcp/call`, so the exported
                            workflow really runs the same capabilities.

  n8n workflow → Vera DAG   NOT a node-by-node translation. An n8n workflow can
                            contain hundreds of node types with no Vera
                            equivalent, so translating node-for-node would either
                            drop logic or invent it. Instead a workflow is
                            surfaced as ONE Vera capability (`n8n.<slug>`), which
                            is itself a legal DAG node. Composition happens at
                            the workflow boundary, where the semantics are real.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# n8n registers trigger webhooks under a prefix derived from the node's
# `nodeType`. MCP Server Trigger declares nodeType 'mcp' with isFullPath, so its
# live URL is <base>/mcp/<path> and its editor-test URL <base>/mcp-test/<path>.
MCP_PROD_PREFIX = "mcp"
MCP_TEST_PREFIX = "mcp-test"
WEBHOOK_PROD_PREFIX = "webhook"
WEBHOOK_TEST_PREFIX = "webhook-test"

MCP_TRIGGER_TYPE = "@n8n/n8n-nodes-langchain.mcpTrigger"
WEBHOOK_TRIGGER_TYPE = "n8n-nodes-base.webhook"


def slugify(value: str, fallback: str = "workflow") -> str:
    out = _SLUG_RE.sub("_", (value or "").strip().lower()).strip("_")
    return out or fallback


def workflow_cap_name(workflow_name: str, prefix: str = "n8n") -> str:
    """Vera capability name for an n8n workflow exposed as a tool."""
    return f"{prefix}.{slugify(workflow_name)}"


def join_url(base: str, *parts: str) -> str:
    """Join without the double slashes that make n8n 404 a valid path."""
    out = (base or "").rstrip("/")
    for p in parts:
        p = (p or "").strip("/")
        if p:
            out += "/" + p
    return out


def mcp_urls(base_url: str, path: str) -> Dict[str, str]:
    """Production and test URLs for an MCP Server Trigger at `path`.

    Trigger v2 serves both MCP transports on the same URL (streamable-HTTP on
    POST, SSE on GET), so one URL is all a client needs.
    """
    return {
        "production": join_url(base_url, MCP_PROD_PREFIX, path),
        "test": join_url(base_url, MCP_TEST_PREFIX, path),
    }


def webhook_urls(base_url: str, path: str) -> Dict[str, str]:
    return {
        "production": join_url(base_url, WEBHOOK_PROD_PREFIX, path),
        "test": join_url(base_url, WEBHOOK_TEST_PREFIX, path),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Workflow inspection
# ═════════════════════════════════════════════════════════════════════════════

def extract_triggers(workflow: Dict[str, Any],
                     base_url: str = "") -> List[Dict[str, Any]]:
    """Every inbound entry point a workflow exposes, with its live URLs.

    This is what makes a workflow *callable* from Vera: without resolving the
    trigger path there is no address to send anything to.
    """
    out: List[Dict[str, Any]] = []
    for node in workflow.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        ntype = node.get("type") or ""
        params = node.get("parameters") or {}
        path = params.get("path") or ""
        if ntype == MCP_TRIGGER_TYPE:
            entry = {"kind": "mcp", "node": node.get("name", ""), "path": path}
            if base_url and path:
                entry["urls"] = mcp_urls(base_url, path)
            out.append(entry)
        elif ntype == WEBHOOK_TRIGGER_TYPE:
            entry = {"kind": "webhook", "node": node.get("name", ""),
                     "path": path,
                     "method": params.get("httpMethod") or "GET"}
            if base_url and path:
                entry["urls"] = webhook_urls(base_url, path)
            out.append(entry)
        elif ntype.endswith(".executeWorkflowTrigger"):
            out.append({"kind": "sub_workflow", "node": node.get("name", "")})
    return out


def workflow_summary(workflow: Dict[str, Any],
                     base_url: str = "") -> Dict[str, Any]:
    nodes = workflow.get("nodes") or []
    return {
        "id": workflow.get("id", ""),
        "name": workflow.get("name", ""),
        "active": bool(workflow.get("active")),
        "node_count": len(nodes),
        "node_types": sorted({n.get("type", "") for n in nodes
                              if isinstance(n, dict)}),
        "triggers": extract_triggers(workflow, base_url),
        "tags": [t.get("name", "") if isinstance(t, dict) else str(t)
                 for t in (workflow.get("tags") or [])],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Vera DAG → n8n workflow
# ═════════════════════════════════════════════════════════════════════════════

def _http_node(name: str, cap: str, vera_url: str, out_key: str,
               position: Tuple[int, int], node_id: str) -> Dict[str, Any]:
    """An HTTP Request node that invokes one Vera capability via /mcp/call."""
    return {
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [position[0], position[1]],
        "parameters": {
            "method": "POST",
            "url": join_url(vera_url, "mcp", "call"),
            "sendBody": True,
            "specifyBody": "json",
            # Arguments come from the incoming item, mirroring how a Vera DAG
            # feeds a node from shared state.
            "jsonBody": (
                '={{ JSON.stringify({ name: "%s", '
                'arguments: $json.arguments || $json || {}, '
                'caller_kind: "n8n" }) }}' % cap
            ),
            "options": {"response": {"response": {"neverError": True}}},
        },
        "notes": f"Vera capability {cap} → state key '{out_key or '(none)'}'",
    }


def vera_dag_to_n8n(dag: List[Any], name: str, vera_url: str,
                    *, add_manual_trigger: bool = True) -> Dict[str, Any]:
    """Translate a Vera DAG into an importable n8n workflow.

    Handles the two node shapes `run_graph` accepts: `[cap, out_key, cond?]`
    and a list-of-lists parallel group. Returns the workflow plus `notes`
    describing anything that could not be represented exactly, so the caller
    can surface the lossiness instead of discovering it at runtime.
    """
    nodes: List[Dict[str, Any]] = []
    connections: Dict[str, Any] = {}
    notes: List[str] = []
    counter = 0

    def nid() -> str:
        nonlocal counter
        counter += 1
        return f"vera-node-{counter:04d}"

    def connect(src: str, dst: str) -> None:
        connections.setdefault(src, {"main": [[]]})
        connections[src]["main"][0].append(
            {"node": dst, "type": "main", "index": 0})

    x, y0 = 0, 0
    prev: List[str] = []

    if add_manual_trigger:
        trig = {"id": nid(), "name": "When clicking 'Execute workflow'",
                "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1,
                "position": [x, y0], "parameters": {}}
        nodes.append(trig)
        prev = [trig["name"]]
        x += 220

    used_names: set = {n["name"] for n in nodes}

    def unique(base: str) -> str:
        cand, i = base, 1
        while cand in used_names:
            i += 1
            cand = f"{base} {i}"
        used_names.add(cand)
        return cand

    for step in dag:
        # Parallel group: a list whose first element is itself a list.
        if isinstance(step, list) and step and isinstance(step[0], list):
            heads: List[str] = []
            for bi, branch in enumerate(step):
                if not (isinstance(branch, list) and branch):
                    continue
                cap = branch[0] if isinstance(branch[0], str) else str(branch[0])
                out_key = branch[1] if len(branch) > 1 else ""
                nm = unique(cap)
                nodes.append(_http_node(nm, cap, vera_url, out_key,
                                        (x, y0 + bi * 140), nid()))
                for p in prev:
                    connect(p, nm)
                heads.append(nm)
            if len(heads) > 1:
                notes.append(
                    "Parallel group of %d branches fans out but does not "
                    "re-join: Vera merges branch results back into shared "
                    "state automatically, whereas n8n needs an explicit Merge "
                    "node. Add one if downstream nodes need all branches."
                    % len(heads))
            prev = heads or prev
            x += 220
            continue

        if not (isinstance(step, list) and step):
            notes.append(f"Skipped unrecognised DAG node: {step!r}")
            continue

        cap = step[0] if isinstance(step[0], str) else str(step[0])
        out_key = step[1] if len(step) > 1 else ""
        cond = step[2] if len(step) > 2 else None

        nm = unique(cap)
        nodes.append(_http_node(nm, cap, vera_url, out_key, (x, y0), nid()))
        for p in prev:
            connect(p, nm)
        prev = [nm]
        x += 220

        if cond is not None:
            if callable(cond):
                notes.append(
                    f"Node '{cap}' had a Python callable condition, which has "
                    "no n8n equivalent and was dropped — add an IF node.")
            elif isinstance(cond, str) and cond.startswith("CONDITION:"):
                notes.append(
                    f"Node '{cap}' runs only when state key "
                    f"'{cond.split(':', 1)[1]}' is truthy — represent this with "
                    "an IF node before it.")

    return {
        "workflow": {
            "name": name,
            "nodes": nodes,
            "connections": connections,
            "settings": {"executionOrder": "v1"},
        },
        "notes": notes,
        "node_count": len(nodes),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  n8n workflow → Vera DAG
# ═════════════════════════════════════════════════════════════════════════════

def n8n_workflow_as_dag_node(workflow: Dict[str, Any], out_key: str = "",
                             prefix: str = "n8n") -> Dict[str, Any]:
    """Describe an n8n workflow as the Vera DAG node that invokes it.

    Composition happens at the workflow boundary — see this module's docstring
    for why a node-by-node translation would be dishonest.
    """
    name = workflow.get("name") or workflow.get("id") or "workflow"
    cap = workflow_cap_name(name, prefix)
    key = out_key or slugify(name)
    triggers = extract_triggers(workflow)
    callable_via = [t["kind"] for t in triggers] or ["none"]
    return {
        "dag_node": [cap, key],
        "capability": cap,
        "output_key": key,
        "workflow_id": workflow.get("id", ""),
        "workflow_name": name,
        "callable_via": callable_via,
        "usable": bool(triggers),
        "note": (
            "Use this node inside any Vera DAG once the workflow is registered "
            "as a capability (n8n.mcp.connect for MCP tools, or "
            "n8n.workflow.register for a webhook-triggered workflow)."
            if triggers else
            "This workflow has no inbound trigger, so nothing outside n8n can "
            "start it — add an MCP Server Trigger or a Webhook node first."
        ),
    }
