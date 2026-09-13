"""Every enrolment entry point reaches the one pipeline: proxmox.lxc.create's
auto_enroll, Foundry's post-provision step and nodes.provision all call
autoenroll.enrol (falling back to the old step when auto-enrol is not loaded),
proxmox.guest.enroll tags the guest it saved, discovery counts logins that live
only in the exec store, and the storage fabric finds guest logins through the
shared rule."""
import ast
import asyncio
import json
import os
import re
import sys
import time
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.provisioning import ssh_store_merge_core as merge  # noqa: E402

pytestmark = pytest.mark.critical

CLUSTER = "2d83c1b5-b25a-4b47-98b8-50660e24b682"
PMX_SRC = os.path.join(ROOT, "vera", "proxmox", "proxmox_capabilities.py")
FOUNDRY_SRC = os.path.join(ROOT, "vera", "foundry", "foundry_capabilities.py")
NODES_SRC = os.path.join(ROOT, "vera", "workers", "nodes_capabilities.py")
ENROLL_SRC = os.path.join(ROOT, "vera", "provisioning", "enroll_capabilities.py")
PX_SRC = os.path.join(ROOT, "vera", "proxmox", "pxstore_capabilities.py")


def load(src, wanted, ns):
    tree = ast.parse(open(src, encoding="utf-8").read())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            node.decorator_list = []
            keep.append(node)
    assert {n.name for n in keep} == set(wanted), f"missing in {src}: {set(wanted) - {n.name for n in keep}}"
    base = {"Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple, "json": json}
    base.update(ns)
    exec(compile(ast.Module(body=keep, type_ignores=[]), src, "exec"), base)
    return base


def recorder(calls, name, result):
    async def fn(**kw):
        calls.append((name, kw))
        return result
    return fn


async def _no_sleep(*a, **k):
    return None


async def _emit(ev):
    return None


def run(coro):
    return asyncio.run(coro)


def test_proxmox_guest_enroll_tags_the_guest_it_saved():
    saves = []

    async def save(**kw):
        saves.append(kw)
        return {"ok": True, "host": {"id": "x160"}}

    async def get_cluster(cid, opened=False):
        return {"id": cid}

    async def guest_ip(rec, node, guest_type, vmid):
        return "192.168.0.160"

    ns = load(PMX_SRC, {"cap_guest_enroll"}, {
        "_get_cluster": get_cluster, "_guest_ip": guest_ip, "emit_event": _emit,
        "_orch": types.SimpleNamespace(CAPABILITY_REGISTRY={"exec.ssh.hosts.save": {"raw": save}})})
    out = run(ns["cap_guest_enroll"](cluster_id=CLUSTER, node="corp", guest_type="lxc", vmid=160,
                                     password="pw"))
    assert out["ok"] and out["ssh_host_id"] == "x160"
    assert saves[0]["label"] == "pve:160@corp"
    assert saves[0]["tags"] == f"proxmox,lxc,corp,guest:{CLUSTER}:160"


def lxc_create_ns(registry):
    async def get_cluster(cid, opened=False):
        return {"id": cid}

    async def pve(rec, method, path, data=None):
        return ("150", None) if path == "/cluster/nextid" else ("UPID:corp:1", None)

    async def guest_ip(rec, node, guest_type, vmid):
        return "192.168.0.150"

    return load(PMX_SRC, {"cap_lxc_create"}, {
        "_get_cluster": get_cluster, "_pve": pve, "_guest_ip": guest_ip, "emit_event": _emit,
        "_cap": registry.get, "asyncio": types.SimpleNamespace(sleep=_no_sleep),
        "_observe_proxmox_effect": lambda **kw: {
            "enforcement": "observe_only", "delivery": {"mode": kw["mode"]}}})


def test_lxc_create_auto_enrol_goes_through_the_pipeline():
    calls = []
    registry = {"autoenroll.enrol": recorder(calls, "autoenroll.enrol", {"ok": True, "status": "ok"}),
                "enroll.guest": recorder(calls, "enroll.guest", {"ok": True})}
    ns = lxc_create_ns(registry)
    out = run(ns["cap_lxc_create"](cluster_id=CLUSTER, node="corp", ostemplate="local:vztmpl/d.tar.zst",
                                   hostname="web", auto_enroll=True))
    assert [c[0] for c in calls] == ["autoenroll.enrol"]
    kw = calls[0][1]
    assert kw["steps"] == "enroll_guest" and kw["via_proxmox"] is True and kw["guest_type"] == "lxc"
    assert kw["fqdn"] == "web.local" and kw["ip"] == "192.168.0.150" and kw["vmid"] == 150
    assert out["enrol"] == {"ok": True, "status": "ok"}


def test_lxc_create_falls_back_to_enroll_guest_without_auto_enrol():
    calls = []
    ns = lxc_create_ns({"enroll.guest": recorder(calls, "enroll.guest", {"ok": True})})
    run(ns["cap_lxc_create"](cluster_id=CLUSTER, node="corp", ostemplate="local:vztmpl/d.tar.zst",
                             hostname="web", auto_enroll=True))
    assert [c[0] for c in calls] == ["enroll.guest"] and "steps" not in calls[0][1]


def foundry_ns(registry):
    return load(FOUNDRY_SRC, {"_call", "_enrol_guest"}, {"CAPABILITY_REGISTRY": registry})


def test_foundry_enrols_new_guests_through_the_pipeline():
    calls = []
    registry = {"autoenroll.enrol": {"raw": recorder(calls, "autoenroll.enrol", {"ok": True})},
                "enroll.guest": {"raw": recorder(calls, "enroll.guest", {"ok": True})}}
    ns = foundry_ns(registry)
    run(ns["_enrol_guest"](cluster_id=CLUSTER, vmid=150, guest_type="lxc", node="corp", fqdn="",
                           via_proxmox=True, skip_mesh=True))
    assert calls == [("autoenroll.enrol", {"steps": "enroll_guest", "cluster_id": CLUSTER, "vmid": 150,
                                           "guest_type": "lxc", "node": "corp", "fqdn": "",
                                           "via_proxmox": True, "skip_mesh": True})]
    calls.clear()
    ns2 = foundry_ns({"enroll.guest": {"raw": recorder(calls, "enroll.guest", {"ok": True})}})
    run(ns2["_enrol_guest"](cluster_id=CLUSTER, vmid=150, guest_type="lxc", node="corp", fqdn=""))
    assert calls[0][0] == "enroll.guest" and "steps" not in calls[0][1]


def test_foundrys_post_provision_step_has_no_direct_enroll_guest_call_left():
    tree = ast.parse(open(FOUNDRY_SRC, encoding="utf-8").read())
    post = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_post_provision")
    names = [c.func.id for c in ast.walk(post) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    literals = [c.value for c in ast.walk(post) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    assert names.count("_enrol_guest") == 2 and "enroll.guest" not in literals


def test_new_node_containers_save_their_login_through_the_pipeline():
    calls = []
    registry = {"proxmox.lxc.create": recorder(calls, "proxmox.lxc.create", {"ok": True, "vmid": 160}),
                "autoenroll.enrol": recorder(calls, "autoenroll.enrol",
                                             {"ok": True, "ssh_host_id": "x7", "ip": "192.168.0.99"})}
    ns = load(NODES_SRC, {"cap_provision_node_new"}, {
        "_rawcap": registry.get, "time": time, "asyncio": types.SimpleNamespace(sleep=_no_sleep)})
    out = run(ns["cap_provision_node_new"](cluster_id=CLUSTER, node="corp",
                                           ostemplate="local:vztmpl/d.tar.zst", hostname="w1",
                                           password="pw"))
    assert [c[0] for c in calls] == ["proxmox.lxc.create", "autoenroll.enrol"]
    kw = calls[1][1]
    assert kw["register_only"] is True and kw["steps"] == "enroll_guest"
    assert (kw["ssh_user"], kw["ssh_password"], kw["label"], kw["vmid"]) == ("root", "pw", "w1", 160)
    assert out["enrolled"] is True and out["ssh_host_id"] == "x7" and out["ip"] == "192.168.0.99"


def test_discovery_counts_logins_that_live_only_in_the_exec_store():
    enrol = [{"id": "e95", "label": "foundry-ct-test.vera.int", "host": "192.168.0.95", "port": 22,
              "user": "root", "auth": "cert", "guest_ref": f"{CLUSTER}:95"}]
    exec_logins = [
        {"id": "x95", "label": "foundry-ct-test.vera.int", "host": "192.168.0.95", "port": 22,
         "user": "root", "auth": "key", "tags": [f"guest:{CLUSTER}:95"]},
        {"id": "x160", "label": "pve:160@corp", "host": "192.168.0.160", "port": 22, "user": "root",
         "auth": "password", "tags": ["proxmox", "qemu", "corp"]}]
    guests = [{"vmid": 95, "name": "vidforge", "type": "lxc", "node": "corp", "status": "running"},
              {"vmid": 160, "name": "t", "type": "qemu", "node": "corp", "status": "running"},
              {"vmid": 170, "name": "fresh", "type": "lxc", "node": "corp", "status": "running"}]

    async def status(cluster_id=""):
        return {"guests": guests}

    async def gip(**kw):
        return {"ip": "192.168.0.170"}

    async def hosts_list():
        return {"hosts": exec_logins}

    async def hosts_raw():
        return enrol

    registry = {"proxmox.status": status, "proxmox.guest.ip": gip, "exec.ssh.hosts.list": hosts_list}
    ns = load(ENROLL_SRC, {"cap_discover", "_exec_hosts"}, {
        "_cap": registry.get, "_hosts_raw": hosts_raw, "_merge": merge, "asyncio": asyncio,
        "log": types.SimpleNamespace(debug=lambda *a, **k: None)})
    rows = {g["vmid"]: g for g in run(ns["cap_discover"](cluster_id=CLUSTER))["guests"]}
    assert (rows[95]["enrolled"], rows[95]["host_id"], rows[95]["exec_id"]) == (True, "e95", "x95")
    assert (rows[160]["enrolled"], rows[160]["host_id"], rows[160]["exec_id"], rows[160]["ip"]) == \
        (True, "x160", "x160", "192.168.0.160")
    assert (rows[170]["enrolled"], rows[170]["exec_id"], rows[170]["ip"]) == (False, "", "192.168.0.170")


def test_the_storage_fabric_finds_guest_logins_through_the_shared_rule():
    src = open(PX_SRC, encoding="utf-8").read()
    assert not re.search(r'rf"\^pve:\{', src)
    assert len(re.findall(r"_ssh_store\.login_for_guest\(hosts\.values\(\), cluster_id, ", src)) == 3
