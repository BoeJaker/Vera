import copy
from pathlib import Path

import pytest

from vera.execution.dag_workflow_execution import (
    prepare_dag_execution,
    prepare_plain_dag_execution,
    prepare_streamed_dag_execution,
    workflow_execution_metadata,
)
from vera.execution.run_projection import ShadowRunRegistry
import vera.execution.run_shadow as run_shadow


pytestmark = pytest.mark.critical


def test_supported_plain_graph_is_exactly_materialized_through_ir():
    graph = [
        ["alpha.read", "read"],
        [["beta.left", "left"], ["beta.right", "right"]],
        ["gamma.write", "saved", "CONDITION:read"],
    ]
    before = copy.deepcopy(graph)
    result = prepare_plain_dag_execution(graph)

    assert graph == before
    assert result["authoritative"] is True
    assert result["mode"] == "workflow_ir_materialized"
    assert result["graph"] == graph
    assert result["graph"] is not graph
    assert result["workflow_hash"].startswith("sha256:")
    assert result["executes"] is False


def test_native_maps_remain_exact_despite_non_blocking_adapter_gaps():
    graph = [["alpha", "out", None, {"source": "input"}, {"value": "target"}]]
    result = prepare_plain_dag_execution(graph)

    assert result["authoritative"] is True
    assert result["graph"] == graph
    assert result["gaps"]
    assert not any(gap["blocking"] for gap in result["gaps"])


def test_callable_condition_stays_on_explicit_native_compatibility_path_without_invocation():
    invoked = []

    def condition(_state):
        invoked.append(True)
        return True

    graph = [["alpha", "out", condition]]
    result = prepare_plain_dag_execution(graph)

    assert result["authoritative"] is False
    assert result["mode"] == "native_compatibility"
    assert result["graph"] is graph
    assert invoked == []
    assert result["reason"] in {"non_canonical_native_graph", "workflow_import_blocked"}


def test_malformed_json_safe_graph_stays_native_with_blocking_gap_evidence():
    graph = [42]
    result = prepare_plain_dag_execution(graph)

    assert result["authoritative"] is False
    assert result["reason"] == "workflow_import_blocked"
    assert result["gaps"] and result["gaps"][0]["blocking"] is True


def test_non_list_graph_is_rejected_before_any_adapter_work():
    with pytest.raises(TypeError, match="graph must be an array"):
        prepare_plain_dag_execution({})


def test_supervised_execution_materializes_definition_but_keeps_native_control():
    graph = [["alpha", "out"]]
    result = prepare_dag_execution(graph, supervised=True)

    assert result["mode"] == "workflow_ir_materialized"
    assert result["control_mode"] == "native_supervised"
    assert result["authoritative"] is True
    assert result["graph"] == graph
    assert result["graph"] is not graph
    assert result["workflow_hash"].startswith("sha256:")


def test_supervised_callable_definition_stays_explicitly_native_compatible():
    def condition(_state):
        return True

    graph = [["alpha", "out", condition]]
    result = prepare_dag_execution(graph, supervised=True)

    assert result["mode"] == "native_compatibility"
    assert result["control_mode"] == "native_supervised"
    assert result["authoritative"] is False
    assert result["graph"] is graph


def test_public_provenance_excludes_the_materialized_graph():
    result = prepare_dag_execution([["alpha", "out"]])
    metadata = workflow_execution_metadata(result)

    assert "graph" not in metadata
    assert metadata["workflow_hash"].startswith("sha256:")
    assert metadata["mode"] == "workflow_ir_materialized"


def test_streamed_preparation_preserves_default_payload_and_opt_in_provenance():
    graph = [["alpha", "out"]]
    default = prepare_streamed_dag_execution(graph)
    visible = prepare_streamed_dag_execution(graph, include_workflow_ir=True)

    assert default["graph"] == graph
    assert default["graph"] is not graph
    assert default["workflow_ir"] is None
    assert visible["workflow_ir"]["workflow_hash"].startswith("sha256:")
    assert "graph" not in visible["workflow_ir"]


@pytest.mark.asyncio
async def test_run_shadow_uses_workflow_definition_hash_as_identity(monkeypatch):
    registry = ShadowRunRegistry()
    monkeypatch.setattr(run_shadow, "SHADOW_RUNS", registry)
    events = []

    async def emit(event):
        events.append(event)

    async def executor(graph, state, trace_id, observer):
        return {**state, "done": True}

    result = await run_shadow.execute_dag_with_run_shadow(
        executor=executor, graph=[], state={}, trace_id="trace-1", emit=emit,
        workflow_id="sha256:definition", session_id="session-1",
    )

    assert result == {"done": True}
    assert events[0]["run"]["workflow_id"] == "sha256:definition"
    assert events[-1]["run"]["workflow_id"] == "sha256:definition"


def test_dag_ui_distinguishes_ir_authority_from_native_modes():
    panel = (Path(__file__).parents[1] / "vera/capability_orchestration.html").read_text(
        encoding="utf-8")

    assert "Workflow IR '+String(wir.workflow_hash" in panel
    assert "native compatibility" in panel
    assert "Supervised done · Workflow IR " in panel
    assert "native compatibility" in panel
    assert "native supervision" in panel
