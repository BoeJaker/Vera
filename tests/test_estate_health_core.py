"""estate.health's rules: which facts about the state store, the Vera host's
containers and the Proxmox guests become findings, and how loud each one is.
The fixtures are the estate as found on 12 Sep 2026."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.estate import estate_health_core as core  # noqa: E402

pytestmark = pytest.mark.critical

ALL_KEYS = {k: True for k in core.EXPECTED_STATE_KEYS}
CONTAINER_REDIS = {"redis_version": "7.4.8", "executable": "/data/redis-server"}


def test_the_container_redis_with_its_estate_keys_is_healthy():
    out = core.state_store_section(CONTAINER_REDIS, ALL_KEYS, "redis://localhost:6379/0", 3717)
    assert out["findings"] == []
    assert out["facts"]["expected_present"] == out["facts"]["expected_total"] == 4


def test_a_store_without_the_estate_keys_is_an_error_naming_what_is_missing():
    present = dict(ALL_KEYS, **{"vera:proxmox:clusters": False, "vera:netsec:mesh": False})
    [f] = core.state_store_section(CONTAINER_REDIS, present)["findings"]
    assert f["severity"] == core.ERROR
    assert "Proxmox cluster records" in f["message"] and "host mesh" in f["message"]
    assert "vera:proxmox:clusters" in f["detail"]


def test_the_host_package_redis_is_flagged_even_when_the_keys_exist():
    out = core.state_store_section({"executable": "/usr/bin/redis-server"}, ALL_KEYS)
    assert [f["severity"] for f in out["findings"]] == [core.WARN]


def container(name, state, restart="", error="", exit_code=0,
              finished="2026-09-02T19:28:30Z", labels=None):
    return {"name": name, "state": state, "restart": restart, "error": error,
            "exit_code": exit_code, "finished_at": finished,
            "labels": labels if labels is not None else {"com.docker.compose.project": "src"}}


def test_container_rules_on_the_hosts_real_failures():
    out = core.container_section([
        container("vikunja-web", "exited", "unless-stopped", exit_code=1,
                  error="driver failed programming external connectivity on endpoint "
                        "vikunja-web: Bind for 0.0.0.0:8081 failed: port is already allocated"),
        container("vera-builder-svc", "created", "unless-stopped", finished="0001-01-01T00:00:00Z"),
        container("doc_parser_nginx", "exited", "unless-stopped",
                  labels={"com.docker.compose.project": "pdf_pipeline"}),
        container("weaviate", "exited", "", exit_code=1),
        container("redis", "running", "unless-stopped"),
    ], listed=265, sandboxes=40)
    by_name = {f["subject"]: f for f in out["findings"]}
    assert by_name["vikunja-web"]["severity"] == core.ERROR
    assert "port" in by_name["vikunja-web"]["message"]
    assert by_name["vera-builder-svc"]["severity"] == core.WARN
    assert "never started" in by_name["vera-builder-svc"]["message"]
    assert by_name["doc_parser_nginx"]["severity"] == core.WARN
    assert by_name["weaviate"]["severity"] == core.INFO
    assert "redis" not in by_name
    assert out["facts"] == {"listed": 265, "sandboxes_ignored": 40, "not_running": 4}


@pytest.mark.parametrize("name,labels", [
    ("vera-sbx-dc609732-269c-4662-beb6-332c9efd032f", {"vera.sandbox": "dc609732"}),
    ("vera-sbx-chat-1789231579253", {}),
    ("vera-dev-feat-automations-hub", {"com.docker.compose.project": "vera-dev-feat-automations-hub"}),
    ("5ff9a7b62819_vera-dev-feat-the-suite-must-yield-the-box-like-t-redis",
     {"com.docker.compose.project": "vera"}),
])
def test_sandboxes_are_never_findings(name, labels):
    assert core.is_sandbox(name, labels)
    assert not core.needs_inspection(name, labels, "exited")
    stopped = container(name, "exited", "unless-stopped", labels=labels)
    assert core.container_section([stopped])["findings"] == []


def test_only_stopped_infrastructure_containers_are_inspected():
    labels = {"com.docker.compose.project": "src"}
    assert core.needs_inspection("vikunja-web", labels, "exited")
    assert core.needs_inspection("vera-builder-svc", labels, "created")
    assert not core.needs_inspection("redis", labels, "running")
    assert not core.needs_inspection("vera-dev", {}, "paused")


def guest(vmid, name, status="running", onboot=1, gtype="lxc", **kw):
    g = {"vmid": vmid, "name": name, "type": gtype, "node": "corp", "status": status,
         "template": False, "onboot": onboot}
    g.update(kw)
    return g


def test_running_guests_without_autostart_are_warnings():
    out = core.guest_section([
        guest(160, "VFS-02", onboot=1),
        guest(104, "LLM", gtype="qemu", onboot=None),
        guest(126, "ollama-gpu", onboot=0),
        guest(141, "debian-12-tmpl", gtype="qemu", onboot=None, template=True),
        guest(900, "scratch", status="stopped", onboot=None),
    ])
    assert sorted(f["subject"] for f in out["findings"]) == ["104", "126"]
    assert all(f["severity"] == core.WARN for f in out["findings"])
    assert "LLM (VM 104)" in next(f["message"] for f in out["findings"] if f["subject"] == "104")
    assert out["facts"]["running_checked"] == 3


def test_an_unreadable_guest_config_is_not_reported_as_missing_autostart():
    [f] = core.guest_section([guest(131, "ollama-cpu", onboot=None, config_error="HTTP 500")])["findings"]
    assert f["severity"] == core.INFO
    assert "could not read" in f["message"].lower()


def test_two_cluster_records_for_one_host_are_flagged_once():
    records = [
        {"id": "70ffbe32", "label": "corp (PVE01)", "api_url": "https://192.168.0.200:8006"},
        {"id": "2d83c1b5", "label": "Home", "api_url": "https://192.168.0.200:8006/api2/json"},
    ]
    out = core.guest_section([], records)
    [f] = out["findings"]
    assert f["severity"] == core.WARN
    assert "corp (PVE01)" in f["detail"] and "Home" in f["detail"]
    assert (out["facts"]["clusters"], out["facts"]["records"]) == (1, 2)


def test_an_unreachable_source_is_a_finding_not_a_blank():
    result = core.summarize({
        "state_store": core.state_store_section(CONTAINER_REDIS, ALL_KEYS),
        "containers": {"error": "no answer within 25 s", "elapsed_ms": 25001},
        "guests": core.guest_section([guest(104, "LLM", gtype="qemu", onboot=0)]),
    })
    assert result["level"] == core.WARN
    assert result["counts"] == {"error": 0, "warn": 2, "info": 0}
    containers = result["sections"]["containers"]
    assert containers["elapsed_ms"] == 25001
    assert "no answer within 25 s" in containers["findings"][0]["message"]
    assert list(result["sections"])[:3] == ["state_store", "containers", "guests"]


def test_errors_sort_first_and_set_the_level():
    result = core.summarize({
        "state_store": core.state_store_section({}, {}),
        "containers": core.container_section([container("weaviate", "exited")]),
        "guests": core.guest_section([]),
    })
    assert [f["severity"] for f in result["findings"]] == [core.ERROR, core.INFO]
    assert result["level"] == core.ERROR
