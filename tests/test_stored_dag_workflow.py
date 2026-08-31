import copy
from pathlib import Path

import pytest

from vera.dag.stored_dag_workflow import inspect_stored_dag_workflow


pytestmark = pytest.mark.critical


def _record():
    return {
        "id": "dag-1", "name": "Example", "archived": False,
        "updated_at": "2026-08-30T12:00:00Z", "content_hash": "",
        "initial_state": {"source": "artifact://input"},
        "dag": [["alpha.read", "value"], ["beta.write", "saved"]],
    }


def test_inspection_preserves_identity_alias_and_stable_definition():
    source = _record()
    before = copy.deepcopy(source)
    left = inspect_stored_dag_workflow(source, registered_aliases=["dag.example"])
    right = inspect_stored_dag_workflow(copy.deepcopy(source), registered_aliases=["dag.example"])

    assert source == before
    assert left == right
    assert left["record"]["id"] == "dag-1"
    assert left["record"]["stored_hash_status"] == "missing"
    assert left["aliases"] == ["dag.example"]
    assert left["registered"] is True
    assert left["workflow_hash"].startswith("sha256:")
    assert left["executes"] is False and left["mutates"] is False


def test_definition_hash_changes_with_initial_state_but_not_usage_metadata():
    base = _record()
    usage = {**base, "use_count": 99, "last_used": "later"}
    changed = copy.deepcopy(base)
    changed["initial_state"]["source"] = "artifact://other"

    first = inspect_stored_dag_workflow(base)
    assert inspect_stored_dag_workflow(usage)["record"]["definition_hash"] == first["record"]["definition_hash"]
    assert inspect_stored_dag_workflow(changed)["record"]["definition_hash"] != first["record"]["definition_hash"]


def test_stored_plain_and_monitored_modes_share_the_exact_ir_boundary():
    result = inspect_stored_dag_workflow(_record())

    assert set(result["execution_modes"]) == {"plain", "supervised", "monitored", "streamed", "stepwise"}
    assert result["execution_modes"]["plain"]["workflow_ir_authoritative"] is True
    assert result["execution_modes"]["monitored"]["workflow_ir_authoritative"] is True
    assert result["execution_modes"]["supervised"]["native_authoritative"] is True
    assert result["execution_modes"]["streamed"]["native_authoritative"] is True
    assert result["execution_modes"]["stepwise"]["native_authoritative"] is True


def test_lossy_native_definition_returns_explicit_gaps_without_workflow():
    record = _record()
    record["dag"] = [42]
    result = inspect_stored_dag_workflow(record)

    assert result["ok"] is False
    assert result["workflow"] is None
    assert result["gaps"][0]["blocking"] is True
    assert result["execution_modes"]["plain"]["native_authoritative"] is True
    assert result["execution_modes"]["plain"]["workflow_ir_authoritative"] is False
    assert result["execution_modes"]["plain"]["parity_gate"] == "native_compatibility"


@pytest.mark.parametrize("field,value", [("id", ""), ("name", ""), ("dag", {}), ("initial_state", [])])
def test_malformed_stored_records_fail_closed(field, value):
    record = _record()
    record[field] = value
    with pytest.raises((TypeError, ValueError)):
        inspect_stored_dag_workflow(record)


def test_workshop_and_capability_expose_the_same_read_only_inspection_route():
    root = Path(__file__).parents[1]
    store_source = (root / "vera/dag/dag_store.py").read_text(encoding="utf-8")
    panel_source = (root / "vera/dag/dag_workshop_panel.html").read_text(encoding="utf-8")

    assert '"dag.workflow.inspect"' in store_source
    assert 'http_path="/dag/workflow/inspect"' in store_source
    assert "/dag/workflow/inspect?id=" in panel_source
    assert "plain/monitored use exact IR preparation" in panel_source
    assert "supervised/streamed/stepwise remain native" in panel_source
