"""The one enrolment pipeline's rules: containers enrol through Proxmox with no
password, a VM without an agent waits for one, the directory and mesh steps are
not repeated after the login step does them, naming steps runs exactly those,
and a guest's login is found by its tag before the old pve:<vmid>@ label."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.provisioning import enrol_pipeline_core as pipe  # noqa: E402
from vera.provisioning import ssh_store_merge_core as merge  # noqa: E402

pytestmark = pytest.mark.critical

CLUSTER = "2d83c1b5-b25a-4b47-98b8-50660e24b682"
ALL_ON = {"do_certs": True, "do_ldap": True, "do_mesh": True, "do_apps": True}


def guest(vmid=150, guest_type="lxc", ip="", enrolled=False, host_id="", in_mesh=False):
    return {"key": pipe.guest_key(CLUSTER, vmid), "name": f"g{vmid}", "source": "proxmox",
            "kind": "lxc" if guest_type == "lxc" else "vm", "guest_type": guest_type,
            "cluster_id": CLUSTER, "vmid": vmid, "ip": ip, "enrolled_ssh": enrolled,
            "host_id": host_id, "in_mesh": in_mesh}


def by_step(p):
    return {s["step"]: s for s in p["steps"]}


def test_a_container_enrols_through_proxmox_without_a_password():
    p = pipe.plan(guest(), ALL_ON)
    steps = by_step(p)
    assert p["needs"] == [] and p["method"] == "proxmox"
    assert [s["step"] for s in p["steps"]] == ["enroll_guest", "cert", "ldap", "mesh", "apps"]
    assert steps["enroll_guest"]["run"] and steps["cert"]["run"]
    assert steps["ldap"]["covered"] and not steps["ldap"]["run"]
    assert steps["mesh"]["covered"] and not steps["mesh"]["run"]
    assert not steps["apps"]["run"]


def test_a_vm_whose_agent_answered_also_needs_no_password():
    assert pipe.plan(guest(guest_type="qemu", ip="192.168.0.151"), ALL_ON)["method"] == "proxmox"


def test_a_caller_can_insist_on_going_through_proxmox():
    p = pipe.plan(guest(guest_type="qemu"), ALL_ON, provided={"via_proxmox": True, "ssh_password": "x"})
    assert p["method"] == "proxmox" and p["needs"] == []


def test_a_vm_without_an_agent_waits_for_a_password_and_nothing_else_runs():
    p = pipe.plan(guest(guest_type="qemu"), ALL_ON)
    assert p["needs"] == ["ssh_password"]
    assert not any(s["run"] for s in p["steps"])
    assert by_step(p)["cert"]["reason"] == "waiting for the login step"


def test_a_supplied_password_for_a_vm_without_an_address_asks_for_the_address():
    p = pipe.plan(guest(guest_type="qemu"), ALL_ON, provided={"ssh_password": "x"})
    assert p["needs"] == ["ip"]
    p2 = pipe.plan(guest(guest_type="qemu", ip="10.0.0.5"), ALL_ON, provided={"ssh_password": "x"})
    assert p2["method"] == "password" and p2["needs"] == []
    p3 = pipe.plan(guest(), ALL_ON, provided={"ssh_key_path": "/k"})
    assert p3["method"] == "key" and p3["needs"] == []


def test_an_enrolled_guest_skips_the_login_and_runs_directory_and_mesh_itself():
    p = pipe.plan(guest(enrolled=True, host_id="x1"), ALL_ON)
    steps = by_step(p)
    assert not steps["enroll_guest"]["run"] and steps["enroll_guest"]["reason"] == "already has a saved login"
    assert steps["ldap"]["run"] and steps["mesh"]["run"]


def test_mesh_waits_for_a_login_and_skips_members():
    host = {"key": "docker:d1", "kind": "docker", "source": "docker_host", "host_id": "", "in_mesh": False}
    steps = by_step(pipe.plan(host, ALL_ON))
    assert "enroll_guest" not in steps
    assert steps["mesh"]["reason"] == "no SSH login yet"
    assert steps["apps"]["run"] and not steps["cert"]["run"]
    member = by_step(pipe.plan(guest(enrolled=True, host_id="x1", in_mesh=True), ALL_ON))
    assert member["mesh"]["reason"] == "already in the mesh"


def test_config_switches_turn_steps_off():
    p = pipe.plan(guest(enrolled=True, host_id="x1"), {"do_certs": False, "do_ldap": True})
    assert [s["step"] for s in p["steps"]] == ["enroll_guest", "ldap"]


def test_naming_steps_runs_exactly_those_and_forces_the_login_step():
    p = pipe.plan(guest(enrolled=True, host_id="old"), ALL_ON, only="enroll_guest")
    assert [s["step"] for s in p["steps"]] == ["enroll_guest"] and p["steps"][0]["run"]
    p2 = pipe.plan(guest(), {}, only=["mesh", "enroll_guest"], skip_mesh=True)
    assert [(s["step"], s["run"]) for s in p2["steps"]] == [("enroll_guest", True), ("mesh", False)]
    assert p2["steps"][1]["reason"] == "handled elsewhere"


def test_register_only_saves_a_login_and_leaves_directory_and_mesh_to_the_pipeline():
    p = pipe.plan(guest(), ALL_ON, only="enroll_guest,ldap,mesh", register_only=True)
    steps = by_step(p)
    assert p["method"] == "register" and steps["enroll_guest"]["run"]
    assert steps["ldap"]["run"] and steps["mesh"]["run"]


def test_a_plain_host_named_for_the_login_step_is_told_why_not():
    host = {"key": "ssh:x1", "kind": "host", "source": "ssh_host", "host_id": "x1"}
    p = pipe.plan(host, ALL_ON, only="enroll_guest")
    assert not p["steps"][0]["run"] and "Proxmox guests" in p["steps"][0]["reason"]


def test_step_names_parse_in_pipeline_order_and_unknown_ones_are_reported():
    assert pipe.parse_steps("mesh, enroll_guest,bogus") == ["enroll_guest", "mesh"]
    assert pipe.unknown_steps("mesh,bogus") == ["bogus"]


def test_scan_rows_list_covered_steps_as_actions():
    row = pipe.scan_row(guest(), ALL_ON)
    assert row == {"actions": ["enroll_guest", "cert", "ldap", "mesh"], "needs": [], "login": "proxmox"}


def test_status_counts_errors_and_waiting():
    assert pipe.status_of({"cert": {"ok": True}, "mesh": {"skipped": "x"}}) == "ok"
    assert pipe.status_of({"identity": {"error": "no directory"}}) == "partial"
    assert pipe.status_of({"cert": {"ok": False}}) == "partial"
    assert pipe.status_of({}, ["ssh_password"]) == "needs_cred"


def test_a_guest_login_keeps_the_old_label_first_and_falls_back_to_its_tag():
    logins = [{"id": "tagged", "label": "g150.vera.int", "tags": [f"guest:{CLUSTER}:150", "enrolled"]},
              {"id": "legacy", "label": "pve:150@corp", "tags": ["proxmox", "lxc", "corp"]},
              {"id": "other", "label": "pve:1500@corp", "tags": "proxmox"}]
    assert merge.login_for_guest(logins, CLUSTER, 150)["id"] == "legacy"
    assert merge.login_for_guest([logins[0], logins[2]], CLUSTER, 150)["id"] == "tagged"
    assert merge.login_for_guest([logins[0]], "", 150) is None
    assert merge.login_for_guest(logins, CLUSTER, 151) is None


def test_guest_refs_reads_every_tag_from_a_list_or_a_string():
    assert merge.guest_refs(f"proxmox,guest:{CLUSTER}:95,guest:{CLUSTER}:144") == [f"{CLUSTER}:95", f"{CLUSTER}:144"]
    assert merge.guest_refs(None) == []
