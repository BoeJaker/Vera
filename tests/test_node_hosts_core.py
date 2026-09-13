"""One node -> SSH login map on the Proxmox cluster record: how a node's login
is resolved, and what copying the storage fabric's older map would change.
Shapes follow prod on 12 Sep 2026 (two records for one API host, both mapping
node corp to the PVE01 login)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.proxmox import node_hosts_core as core  # noqa: E402

pytestmark = pytest.mark.critical

PVE01 = "27f0ce6b-1be7-4e3c-8240-af94780bb4ae"
EXEC = [{"id": PVE01, "label": "PVE01", "host": "192.168.0.200"},
        {"id": "pve02-login", "label": "pve02.vera.int", "host": "192.168.0.201"},
        {"id": "llm", "label": "LLM", "host": "192.168.0.138"}]
CORP = {"id": "70ffbe32", "label": "corp (PVE01)", "api_url": "https://192.168.0.200:8006"}
HOME = {"id": "2d83c1b5", "label": "Home", "api_url": "https://192.168.0.200:8006/api2/json"}


def test_the_cluster_record_map_wins():
    cluster = dict(CORP, node_hosts={"corp": "llm"})
    got = core.resolve_node_host("corp", cluster, {"node_hosts": {"corp": PVE01}}, EXEC)
    assert got == {"host_id": "llm", "source": "cluster"}


def test_the_storage_fabric_map_is_read_when_the_record_has_none():
    got = core.resolve_node_host("corp", CORP, {"node_hosts": {"corp": PVE01}}, EXEC)
    assert got == {"host_id": PVE01, "source": "pxstore"}


def test_without_a_node_a_single_mapping_is_the_node():
    assert core.resolve_node_host("", dict(CORP, node_hosts={"corp": PVE01}), None, EXEC)["host_id"] == PVE01


def test_a_login_the_exec_store_no_longer_has_is_skipped():
    cluster = dict(CORP, node_hosts={"corp": "deleted-login"})
    assert core.resolve_node_host("corp", cluster, None, EXEC) == {"host_id": PVE01, "source": "api-host"}


def test_a_second_node_is_found_by_its_login_name_not_the_api_host():
    assert core.resolve_node_host("pve02", CORP, None, EXEC) == {"host_id": "pve02-login", "source": "label"}


def test_nothing_fits():
    cluster = {"id": "x", "api_url": "https://10.0.0.9:8006"}
    assert core.resolve_node_host("ghost", cluster, None, EXEC) == {"host_id": "", "source": ""}


def test_merging_copies_the_fabric_map_onto_both_records_for_prod():
    cfgs = {"70ffbe32": {"node_hosts": {"corp": PVE01}}, "2d83c1b5": {"node_hosts": {"corp": PVE01}}}
    plan = core.plan_merge([CORP, HOME], cfgs, EXEC)
    assert [s["action"] for s in plan["steps"]] == ["update", "update"]
    assert all(s["add"] == {"corp": PVE01} for s in plan["steps"])
    assert plan["steps"][1]["api_host"] == "192.168.0.200"
    assert plan["clusters_changed"] == 2 and plan["counts"] == {"update": 2}


def test_a_merged_record_is_current_and_disagreements_are_reported_not_applied():
    cfgs = {"70ffbe32": {"node_hosts": {"corp": PVE01}},
            "2d83c1b5": {"node_hosts": {"corp": PVE01, "old": "deleted-login"}}}
    plan = core.plan_merge([dict(CORP, node_hosts={"corp": PVE01}), dict(HOME, node_hosts={"corp": "llm"})],
                           cfgs, EXEC)
    current, conflict = plan["steps"]
    assert current["action"] == "current" and current["add"] == {}
    assert conflict["action"] == "conflict" and conflict["add"] == {}
    assert conflict["conflicts"] == [{"node": "corp", "cluster": "llm", "pxstore": PVE01}]
    assert conflict["stale"] == [{"node": "old", "host_id": "deleted-login"}]
    assert plan["clusters_changed"] == 0
