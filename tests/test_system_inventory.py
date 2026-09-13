from pathlib import Path

import pytest

from vera.inventory.system_inventory import (
    _declared_database_schema,
    build_system_inventory,
    summarize_system_inventory,
)


pytestmark = pytest.mark.critical


class Route:
    def __init__(self, path, methods, name):
        self.path = path
        self.methods = methods
        self.name = name


class App:
    routes = [Route("/z", {"POST"}, "z"), Route("/a", {"GET", "HEAD"}, "a")]


async def alpha():
    return None


def _inputs(tmp_path):
    return {
        "capabilities": {
            "task.run": {
                "raw": alpha,
                "schema": {"type": "object"},
                "tags": ["task"],
                "compatibility_alias_for": "task.execute",
            },
            "internal.health": {"raw": alpha, "mcp_expose": False, "tags": ["internal"]},
        },
        "loaded_modules": [
            {"name": "z", "path": tmp_path / "vera/z.py", "caps_added": 1, "status": "ok"},
            {"name": "a", "path": tmp_path / "vera/a.py", "status": "error: optional dep missing"},
        ],
        "panels": {"z": {"label": "Z"}, "a": {"label": "A"}},
        "app": App(),
        "schedules": [{"name": "tick", "interval": 10}],
        "workers": {"worker-b": {"caps": ["z", "a"], "api_token": "do-not-leak"}},
        "mcp_servers": {"z": "http://user:pass@z/path?token=secret", "a": "http://a"},
        "repo_root": tmp_path,
        "configuration_keys": {"REDIS_URL": True, "GITEA_TOKEN": False},
    }


def test_inventory_fingerprint_is_order_and_time_stable(tmp_path):
    first = build_system_inventory(**_inputs(tmp_path), captured_at="one")
    inputs = _inputs(tmp_path)
    inputs["capabilities"] = dict(reversed(list(inputs["capabilities"].items())))
    inputs["loaded_modules"].reverse()
    inputs["panels"] = dict(reversed(list(inputs["panels"].items())))
    inputs["mcp_servers"] = dict(reversed(list(inputs["mcp_servers"].items())))
    inputs["schedules"][0]["runs"] = 99
    inputs["schedules"][0]["last"] = "later"
    inputs["workers"] = {"other-process": {"pid": 999, "tasks_done": 40}}
    second = build_system_inventory(**inputs, captured_at="two")

    assert first["fingerprint_sha256"] == second["fingerprint_sha256"]
    assert first["captured_at"] != second["captured_at"]
    assert [cap["name"] for cap in first["capabilities"]] == ["internal.health", "task.run"]
    assert first["capabilities"][1]["compatibility_alias_for"] == "task.execute"
    assert [route["path"] for route in first["http_routes"]] == ["/a", "/z"]
    assert "runs" not in first["schedules"][0]["metadata"]


def test_inventory_keeps_optional_module_errors_and_closes_declared_coverage(tmp_path):
    result = build_system_inventory(**_inputs(tmp_path), captured_at="fixed")

    failed = result["modules"][0]
    assert failed == {
        "name": "a",
        "path": "vera/a.py",
        "caps_added": 0,
        "status": "error",
        "error": "optional dep missing",
    }
    assert result["counts"]["module_errors"] == 1
    assert result["coverage"]["not_yet_included"] == []
    assert "stored_workflows" in result["coverage"]["included"]
    assert result["capabilities"][0]["role_inferred"] == "internal"


def test_inventory_reports_duplicate_pattern_families(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["capabilities"]["workflow.run"] = {"raw": alpha, "tags": []}
    result = build_system_inventory(**inputs, captured_at="fixed")

    assert result["duplication_signals"]["run"] == ["task.run", "workflow.run"]
    assert result["schema_version"] == "vera.system-inventory/v1"


def test_inventory_summary_keeps_evidence_without_large_records(tmp_path):
    snapshot = build_system_inventory(**_inputs(tmp_path), captured_at="fixed")
    summary = summarize_system_inventory(snapshot)

    assert summary["fingerprint_sha256"] == snapshot["fingerprint_sha256"]
    assert summary["counts"] == snapshot["counts"]
    assert summary["module_errors"][0]["name"] == "a"
    assert summary["role_counts_inferred"] == {"internal": 1, "public_task": 1}
    assert "capabilities" not in summary
    assert summary["detail"] is False


def test_inventory_redacts_runtime_secrets_and_endpoint_credentials(tmp_path):
    snapshot = build_system_inventory(**_inputs(tmp_path), captured_at="fixed")

    assert snapshot["workers"][0]["metadata"]["api_token"] == "[redacted]"
    assert snapshot["mcp_servers"][1]["url"] == "http://z/path"
    assert "do-not-leak" not in str(snapshot)
    assert "user:pass" not in str(snapshot)


def test_inventory_reports_configuration_presence_without_values(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["configuration_keys"] = {
        "REDIS_URL": "redis://user:password@secret-host/0",
        "GITEA_TOKEN": "must-not-leak",
        "OPTIONAL_EMPTY": "",
    }
    snapshot = build_system_inventory(**inputs, captured_at="fixed")
    assert snapshot["configuration_keys"] == [
        {"key": "GITEA_TOKEN", "explicit": True},
        {"key": "OPTIONAL_EMPTY", "explicit": False},
        {"key": "REDIS_URL", "explicit": True},
    ]
    assert "must-not-leak" not in str(snapshot)
    assert "secret-host" not in str(snapshot)
    assert "configuration_keys" in snapshot["coverage"]["included"]
    assert "configuration_keys" not in snapshot["coverage"]["not_yet_included"]


def test_inventory_adds_bounded_workflow_schema_artifact_connection_and_caller_evidence(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["capabilities"]["project.artifacts.list"] = {
        "raw": alpha,
        "http_method": "GET",
        "http_path": "/artifacts",
    }
    inputs["stored_workflows"] = {
        "dag_store": [{
            "id": "wf-1", "name": "example", "category": "ops",
            "tags": ["safe"],
            "dag": [["task.run", "result", {"prompt": "must not leak"}]],
            "initial_state": {"api_token": "must not leak"},
        }],
    }
    inputs["database_schema"] = [
        {"table": "jobs", "source": "vera/jobs.py"},
    ]
    inputs["connections"] = [{
        "id": "remote-1", "label": "Builder", "kind": "ssh",
        "ssh_host_id": "credential-record-9", "password": "must not leak",
        "tags": ["build"], "hostname": "private.example",
    }]

    snapshot = build_system_inventory(**inputs, captured_at="fixed")

    assert snapshot["stored_workflows"] == [{
        "source": "dag_store", "id": "wf-1", "name": "example",
        "category": "ops", "tags": ["safe"], "node_count": 1,
        "capabilities": ["task.run"],
    }]
    assert snapshot["database_schema"] == [
        {"table": "jobs", "source": "vera/jobs.py"},
    ]
    assert snapshot["artifacts"][0]["scope"] == "provider_surface_not_stored_content"
    assert snapshot["connections"] == [{
        "id": "remote-1", "label": "Builder", "kind": "ssh",
        "tags": ["build"], "has_credential_reference": True,
    }]
    assert {
        "caller": "stored-definition:dag_store:wf-1",
        "capability": "task.run",
        "basis": "workflow_node",
    } in snapshot["caller_graph"]
    assert "must not leak" not in str(snapshot)
    assert "private.example" not in str(snapshot)


def test_declared_database_schema_reads_names_not_sql_or_rows(tmp_path):
    module = tmp_path / "vera" / "storage.py"
    module.parent.mkdir()
    module.write_text(
        'SQL = "CREATE TABLE IF NOT EXISTS jobs (secret TEXT)"\n'
        'OTHER = "create table [events] (payload TEXT)"\n',
        encoding="utf-8",
    )

    records = _declared_database_schema(
        [{"path": module, "status": "ok"}], tmp_path)

    assert records == [
        {"table": "events", "source": "vera/storage.py"},
        {"table": "jobs", "source": "vera/storage.py"},
    ]
    assert "secret" not in str(records)
