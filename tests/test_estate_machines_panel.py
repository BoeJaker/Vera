"""The Machines pane handles every action the machine list can offer, and its
console, terminal and enrolment calls use the row's own cluster rather than
whichever cluster the Proxmox pane has selected."""
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_machines_core as core  # noqa: E402

pytestmark = pytest.mark.critical

PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")


def panel():
    return open(PANEL, encoding="utf-8").read()


def every_action_id():
    rows = [
        {"kind": "guest", "cluster_id": "c", "node": "n", "type": "lxc", "vmid": 1, "status": "running",
         "ssh_host_id": "h"},
        {"kind": "guest", "cluster_id": "c", "node": "n", "type": "qemu", "vmid": 2, "status": "stopped"},
        {"kind": "proxmox-node", "ssh_host_id": "h"},
        {"kind": "docker-host", "docker_host_id": "d"},
        {"kind": "host", "ssh_host_id": "o", "runs": ["ollama"], "status": "running"},   # an inference node
    ]
    return {a["id"] for r in rows for a in core.actions(r)}


def test_every_action_the_list_can_offer_has_a_handler():
    body = panel()
    handler = body[body.index("async function mcAct(idx, action){"):body.index("function showPane(name, el){")]
    ids = every_action_id()
    assert ids == {"console", "shutdown", "reboot", "start", "ssh", "cpu", "detect", "containers", "provision", "backend"}
    for action in ids:
        assert f"'{action}'" in handler, action


def test_the_machines_pane_is_listed_and_loads():
    body = panel()
    assert 'data-pane="machines" data-view="estate"' in body
    assert 'id="pane-machines"' in body
    assert "if(name==='machines'){ mcInit(); }" in body


def test_console_terminal_and_enrolment_take_the_rows_cluster():
    body = panel()
    for fn in ("pmxSsh", "pmxEnrollSsh", "pmxConsole"):
        match = re.search(r"async function " + fn + r"\(([^)]*)\)", body)
        assert match and match.group(1).split(",")[-1] == "cid", fn
    assert "cluster_id:_pmxCur," not in body[body.index("async function pmxSsh("):body.index("async function pmxPower(")]
    assert "{cluster_id:cid||_pmxCur,node,guest_type:type,vmid:parseInt(vmid),mode}" in body
