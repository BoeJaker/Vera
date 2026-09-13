"""autoenroll.enrol and the pipeline inside auto-enrol, against stand-in
capabilities: a container Foundry just built runs only the login step through
Proxmox, a VM logs in with Vera's key, a failed login stops the steps that need
it, register_only saves a login without touching the guest, a saved host gets
its directory record and mesh join, a VM with no agent and no password is queued
rather than failed, and discovery's address and exec login reach the plan."""
import ast
import asyncio
import json
import os
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.provisioning import enrol_pipeline_core as pipe  # noqa: E402
from vera.provisioning import ssh_store_merge_core as merge  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "provisioning", "autoenroll_capabilities.py")
WANTED = {"_exec_logins", "_flag", "_need_prompt", "_login_step", "_enrol_one", "cap_ae_enrol",
          "_fqdn_for", "_opts_from_box", "_discover", "_docker_addr", "cap_ae_scan",
          "cap_ae_cred_provide"}
CLUSTER = "2d83c1b5-b25a-4b47-98b8-50660e24b682"
KEY = "/home/boejaker/.vera/ssh/id_vera"
CFG = {"enabled": False, "dry_run": True, "do_certs": True, "do_mesh": True, "do_ldap": True,
       "do_apps": True, "src_proxmox": True, "src_docker": False, "src_ssh_hosts": False,
       "base_domain": "vera.lab"}
STEP_CALLS = ("enroll.guest", "proxmox.guest.enroll", "cert", "ldap", "mesh", "apps")


class FakeRedis:
    def __init__(self):
        self.h: Dict[str, Dict[str, str]] = {}

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value


def default_enrol(kw):
    mesh = {"skipped": "handled by mesh feature (self-enrol)"} if kw.get("skip_mesh") else {"ok": True}
    return {"ok": True, "fqdn": kw.get("fqdn"), "host_id": "e1", "exec_host_id": "x9",
            "steps": {"ssh": {"ok": True}, "identity": {"ok": True}, "mesh": mesh}}


class Harness:
    def __init__(self, exec_logins=(), cfg=None, mesh=(), enrol=None, register=None,
                 discover=None, provided=None):
        self.calls: List = []
        self.pending: List = []
        self.events: List = []
        self.redis = FakeRedis()

        def rec(name, result):
            async def fn(**kw):
                self.calls.append((name, kw))
                return result(kw) if callable(result) else result
            return fn

        registry = {
            "enroll.guest": rec("enroll.guest", enrol or default_enrol),
            "proxmox.guest.enroll": rec("proxmox.guest.enroll",
                                        register or {"ok": True, "ssh_host_id": "x7", "ip": "192.168.0.99"}),
            "provisioning.asset.online": rec("cert", {"ok": True, "fqdn": "f"}),
            "identity.resolve.host": rec("ldap", {"ok": True, "backend": "freeipa"}),
            "netsec.mesh.join": rec("mesh", {"ok": True}),
            "exec.ssh.hosts.list": rec("exec.list", {"hosts": [dict(h) for h in exec_logins]}),
            "enroll.discover": rec("enroll.discover", discover or {"guests": []}),
            "proxmox.cluster.list": rec("cluster.list", {"clusters": [{"id": CLUSTER}]}),
        }
        conf = dict(CFG, **(cfg or {}))
        boxes = dict(provided or {})

        async def cfg_fn():
            return dict(conf)

        async def mesh_ids():
            return set(mesh)

        async def pending_add(asset_key, cred, prompt, meta):
            self.pending.append((asset_key, cred))

        async def pending_clear(asset_key, cred=""):
            self.pending[:] = [p for p in self.pending if p[0] != asset_key]

        async def provided_cred(asset_key):
            return dict(boxes.get(asset_key, {}))

        async def mount_apps(a, cfg):
            self.calls.append(("apps", {}))
            return {"ok": True, "count": 0}

        async def emit(ev):
            self.events.append(ev)

        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED:
                node.decorator_list = []
                keep.append(node)
        self.ns = {"json": json, "asyncio": asyncio, "Any": Any, "Dict": Dict, "List": List,
                   "Optional": Optional, "_pipe": pipe, "_merge": merge, "_cap": registry.get,
                   "_cfg": cfg_fn, "_mesh_member_ids": mesh_ids, "_pending_add": pending_add,
                   "_pending_clear": pending_clear, "_provided_cred": provided_cred,
                   "_mount_docker_apps": mount_apps, "emit_event": emit,
                   "now_iso": lambda: "2026-09-13T12:00:00Z", "_redis": lambda: self.redis,
                   "log": types.SimpleNamespace(debug=lambda *a, **k: None),
                   "vsecrets": types.SimpleNamespace(
                       seal=lambda v: "sealed:" + v,
                       open_secret=lambda v: v[len("sealed:"):] if str(v).startswith("sealed:") else v,
                       backend_status=lambda: {"backend": "fernet"})}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)

    def steps_called(self):
        return [c[0] for c in self.calls if c[0] in STEP_CALLS]

    def kw(self, name):
        return [c[1] for c in self.calls if c[0] == name]


def run(coro):
    return asyncio.run(coro)


def test_a_container_foundry_built_runs_only_the_login_step_through_proxmox():
    h = Harness()
    out = run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=150, node="corp", guest_type="lxc",
                                   fqdn="g150.vera.lab", via_proxmox=True, steps="enroll_guest",
                                   skip_mesh=True))
    assert h.steps_called() == ["enroll.guest"]
    kw = h.kw("enroll.guest")[0]
    assert kw["via_proxmox"] is True and kw["skip_mesh"] is True and kw["fqdn"] == "g150.vera.lab"
    assert "ssh_password" not in kw and kw["ip"] == "" and kw["guest_type"] == "lxc"
    assert out["ok"] and out["error"] == "" and out["status"] == "ok"
    assert out["steps"]["identity"] == {"ok": True} and "skipped" in out["steps"]["mesh"]
    assert out["exec_host_id"] == "x9" == out["ssh_host_id"]
    assert h.events[-1]["type"] == "autoenroll.enrol"


def test_a_vm_logs_in_with_veras_key_over_ssh():
    h = Harness()
    out = run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=151, node="corp", guest_type="qemu",
                                   fqdn="vm1.vera.lab", ip="192.168.0.151", ssh_user="vera",
                                   ssh_key_path=KEY, steps="enroll_guest", skip_mesh="false"))
    kw = h.kw("enroll.guest")[0]
    assert kw["ssh_user"] == "vera" and kw["ssh_key_path"] == KEY and kw["ip"] == "192.168.0.151"
    assert "via_proxmox" not in kw and kw["skip_mesh"] is False
    assert out["ok"]


def test_a_failed_login_is_an_error_and_the_steps_that_need_it_do_not_run():
    h = Harness(enrol={"ok": False, "steps": {"ssh": {"ok": False, "error": "Authentication failed"}}})
    out = run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=152, node="corp", guest_type="qemu",
                                   ip="192.168.0.152", ssh_password="pw", fqdn="x.vera.lab",
                                   steps="enroll_guest,cert,mesh"))
    assert h.steps_called() == ["enroll.guest"]
    assert out["ok"] is False and out["error"] == "Authentication failed" and out["status"] == "partial"
    assert out["steps"]["cert"]["skipped"].startswith("login step failed")
    assert h.kw("enroll.guest")[0]["ssh_password"] == "pw"


def test_register_only_saves_the_login_without_touching_the_guest():
    h = Harness()
    out = run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=160, node="corp", guest_type="lxc",
                                   ssh_user="root", ssh_password="pw", label="ct-160",
                                   steps="enroll_guest", register_only=True))
    assert h.steps_called() == ["proxmox.guest.enroll"]
    kw = h.kw("proxmox.guest.enroll")[0]
    assert (kw["user"], kw["password"], kw["label"], kw["ip"], kw["vmid"]) == ("root", "pw", "ct-160", "", 160)
    assert out["ok"] and out["ssh_host_id"] == "x7" and out["ip"] == "192.168.0.99"


def test_a_saved_host_gets_its_certificate_directory_record_and_mesh_join():
    h = Harness(exec_logins=[{"id": "x1", "label": "n8n", "host": "192.168.0.93", "tags": []}])
    out = run(h.ns["cap_ae_enrol"](host_id="x1"))
    assert h.steps_called() == ["cert", "ldap", "mesh"]
    assert h.kw("ldap")[0] == {"fqdn": "n8n.vera.lab", "ip": "192.168.0.93"}
    assert h.kw("mesh")[0] == {"host_id": "x1"}
    assert out["ok"] and out["status"] == "ok"


def test_a_vm_without_an_agent_or_a_password_is_queued_not_failed():
    h = Harness()
    out = run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=170, node="corp", guest_type="qemu"))
    assert h.steps_called() == []
    assert out["status"] == "needs_cred" and out["ok"] is False and out["error"] == "missing ssh_password"
    assert h.pending == [(pipe.guest_key(CLUSTER, 170), "ssh_password")]


def test_bad_requests_are_refused_before_anything_runs():
    h = Harness()
    assert "unknown steps: bogus" in run(h.ns["cap_ae_enrol"](host_id="x", steps="bogus"))["error"]
    assert "name the asset" in run(h.ns["cap_ae_enrol"]())["error"]
    assert "guest_type" in run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=5))["error"]
    assert "no exec SSH login" in run(h.ns["cap_ae_enrol"](host_id="ghost"))["error"]
    assert h.steps_called() == []


def test_an_old_login_for_a_reused_vmid_is_not_used_when_the_guest_is_enrolled_again():
    old = [{"id": "old", "label": "pve:150@corp", "host": "192.168.0.50", "tags": []}]
    h = Harness(exec_logins=old)
    run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=150, node="corp", guest_type="lxc",
                             fqdn="g.vera.lab", via_proxmox=True, steps="enroll_guest"))
    assert h.kw("enroll.guest")[0]["ip"] == ""
    h2 = Harness(exec_logins=old)
    out = run(h2.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=150, node="corp", guest_type="lxc", name="g150"))
    assert h2.steps_called() == ["cert", "ldap", "mesh"]
    assert h2.kw("cert")[0]["ip"] == "192.168.0.50" and h2.kw("mesh")[0] == {"host_id": "old"}
    assert h2.kw("ldap")[0] == {"fqdn": "g150.vera.lab", "ip": "192.168.0.50"}
    assert out["ok"]


def test_no_directory_name_is_made_up_for_a_guest_without_a_name():
    h = Harness(exec_logins=[{"id": "old", "label": "pve:150@corp", "host": "192.168.0.50", "tags": []}])
    out = run(h.ns["cap_ae_enrol"](cluster_id=CLUSTER, vmid=150, node="corp", guest_type="lxc"))
    assert h.steps_called() == ["cert", "mesh"]
    assert out["steps"]["ldap"] == {"skipped": "identity module not configured or no fqdn"}


def test_a_discovered_container_is_enrolled_without_a_password_and_nothing_is_repeated():
    h = Harness()
    a = {"key": pipe.guest_key(CLUSTER, 144), "name": "vidforge", "kind": "lxc", "ip": "",
         "host_id": "", "enrolled_ssh": False, "in_mesh": False, "cluster_id": CLUSTER,
         "vmid": 144, "guest_type": "lxc", "node": "corp", "source": "proxmox"}
    out = run(h.ns["_enrol_one"](a, dict(CFG)))
    assert h.steps_called() == ["enroll.guest", "cert"]
    assert h.kw("enroll.guest")[0]["fqdn"] == "vidforge.vera.lab"
    assert h.pending == [] and out["host_id"] == "x9" and out["status"] == "ok"


def test_discovery_carries_the_address_and_the_exec_login_into_the_asset():
    disc = {"cluster_id": CLUSTER, "guests": [
        {"vmid": 151, "name": "vm1", "type": "qemu", "node": "corp", "status": "running",
         "ip": "192.168.0.151", "enrolled": True, "host_id": "e5", "exec_id": "x5"},
        {"vmid": 152, "name": "off", "type": "lxc", "node": "corp", "status": "stopped"}]}
    h = Harness(discover=disc, mesh=("x5",))
    assets = run(h.ns["_discover"]())
    assert [(a["key"], a["ip"], a["host_id"], a["in_mesh"]) for a in assets] == \
        [(pipe.guest_key(CLUSTER, 151), "192.168.0.151", "x5", True)]


def test_the_scan_needs_a_password_only_where_proxmox_cannot_reach():
    disc = {"cluster_id": CLUSTER, "guests": [
        {"vmid": 180, "name": "ct", "type": "lxc", "node": "corp", "status": "running", "ip": ""},
        {"vmid": 181, "name": "vm", "type": "qemu", "node": "corp", "status": "running", "ip": ""},
        {"vmid": 182, "name": "vm2", "type": "qemu", "node": "corp", "status": "running", "ip": ""}]}
    boxes = {pipe.guest_key(CLUSTER, 182): {"cred": "ssh_password", "value": "pw", "ip": "10.0.0.8"}}
    h = Harness(discover=disc, provided=boxes)
    rows = {r["name"]: r for r in run(h.ns["cap_ae_scan"]())["plan"]}
    assert rows["ct"]["needs"] == [] and rows["ct"]["login"] == "proxmox"
    assert rows["ct"]["actions"] == ["enroll_guest", "cert", "ldap", "mesh"]
    assert rows["vm"]["needs"] == ["ssh_password"]
    assert rows["vm2"]["needs"] == [] and rows["vm2"]["login"] == "password" and rows["vm2"]["ip"] == "10.0.0.8"


def test_an_address_and_a_password_supplied_separately_both_stay():
    h = Harness()
    k = pipe.guest_key(CLUSTER, 190)
    run(h.ns["cap_ae_cred_provide"](asset=k, cred="ip", value="10.0.0.7"))
    run(h.ns["cap_ae_cred_provide"](asset=k, cred="ssh_password", value="pw", ssh_user="admin"))
    box = json.loads(h.redis.h["vera:autoenroll:creds"][k])
    assert box["ip"] == "10.0.0.7" and box["cred"] == "ssh_password" and box["value"] == "sealed:pw"
    opts = h.ns["_opts_from_box"](dict(box, value="pw"))
    assert opts == {"ip": "10.0.0.7", "ssh_user": "admin", "ssh_port": 22, "ssh_password": "pw"}
    assert h.ns["_opts_from_box"]({"cred": "ip", "value": "10.0.0.9"}) == {"ip": "10.0.0.9"}
