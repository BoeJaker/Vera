import copy

import pytest

from vera.elements.flow_builder_workflow import (
    GRAPH_EXTENSION,
    analyze_flow_builder_graph,
    graph_to_workflow_ir,
    semantic_diff,
    workflow_ir_to_graph,
)


pytestmark = pytest.mark.critical


def _graph():
    return {
        "nodes": [
            {"id": "load", "type": "fabric.query", "out": "records",
             "params": {"query": {"source": "value", "value": "status:open"}}},
            {"id": "write", "type": "artifact.write",
             "params": {"payload": {"source": "state", "value": "records"}}},
        ],
        "meta": {"name": "portable flow", "description": "Two tasks"},
    }


def test_portable_graph_round_trips_exactly_with_stable_ids():
    graph = _graph()
    converted = graph_to_workflow_ir(graph)
    restored = workflow_ir_to_graph(converted["workflow"])

    assert converted["ok"] is True
    assert converted["classification"] == "portable"
    assert converted["gaps"] == []
    assert converted["semantic_diff"] == []
    assert converted["workflow"]["steps"][0]["id"] == "load"
    assert restored["graph"] == graph
    assert restored["executes"] is False


def test_native_fields_are_preserved_exactly_and_reported_before_execution():
    graph = _graph()
    graph["meta"]["viewport"] = {"zoom": 1.5}
    graph["nodes"][0]["pos"] = {"x": 20, "y": 40}
    graph["nodes"][1]["condition"] = "approved"

    converted = graph_to_workflow_ir(graph)
    restored = workflow_ir_to_graph(converted["workflow"])

    assert converted["classification"] == "native_extensions"
    assert {gap["path"] for gap in converted["gaps"]} == {
        "meta.viewport", "nodes[0].pos", "nodes[1].condition"}
    assert converted["workflow"]["extensions"][GRAPH_EXTENSION] == graph
    assert restored["graph"] == graph
    assert restored["classification"] == "native_extensions"
    assert converted["executes"] is False


def test_ir_without_preserved_graph_fails_closed_when_semantics_do_not_fit():
    workflow = {
        "ir_version": "1.0",
        "steps": [{"id": "loop", "type": "loop", "max_iterations": 3,
                   "condition": {"kind": "state", "value": "again"},
                   "body": [{"id": "task", "type": "task", "task": "work"}]}],
    }
    result = workflow_ir_to_graph(workflow)

    assert result["ok"] is False
    assert result["graph"] is None
    assert result["classification"] == "unsupported"
    assert result["gaps"][0]["code"] == "unsupported_structure"
    assert result["gaps"][0]["blocking"] is True


def test_invalid_graph_does_not_return_partial_ir():
    graph = _graph()
    graph["nodes"][0]["params"]["query"] = {
        "source": "secret", "value": "plaintext"}
    result = graph_to_workflow_ir(graph)

    assert result["ok"] is False
    assert result["workflow"] is None
    assert result["classification"] == "unsupported"
    assert result["gaps"][0]["blocking"] is True


def test_semantic_diff_reports_paths_and_hashes_not_values():
    before = _graph()
    after = copy.deepcopy(before)
    after["nodes"][0]["params"]["query"]["value"] = "private changed query"
    changes = semantic_diff(before, after)

    assert changes == [{
        "path": "nodes[0].params.query.value", "kind": "value_changed",
        "before_hash": changes[0]["before_hash"],
        "after_hash": changes[0]["after_hash"],
    }]
    assert changes[0]["before_hash"].startswith("sha256:")
    assert "private changed query" not in str(changes)


def test_preserved_graph_cannot_disagree_with_visible_ir_steps():
    converted = graph_to_workflow_ir(_graph())
    workflow = copy.deepcopy(converted["workflow"])
    workflow["steps"][0]["task"] = "dangerous.replacement"
    workflow.pop("content_hash", None)
    result = workflow_ir_to_graph(workflow)

    assert result["ok"] is False
    assert result["classification"] == "inconsistent"
    assert result["graph"] is None
    assert result["gaps"][0]["code"] == "extension_ir_mismatch"
    assert result["semantic_diff"][0]["path"] == "steps[0].task"


def test_analysis_omits_workflow_payload_and_never_executes():
    report = analyze_flow_builder_graph(_graph())
    assert report["ok"] is True
    assert "workflow" not in report
    assert report["executes"] is False
