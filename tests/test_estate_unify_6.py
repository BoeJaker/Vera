"""Round six: the Traffic page names its talkers through the one list, says
why every byte count is zero, and offers the fix through the one confirm flow."""
import ast
import asyncio
import os
import re
import sys
from typing import Dict, Tuple

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical
PX_SRC = os.path.join(ROOT, "vera", "proxmox", "pxstore_capabilities.py")


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def functions(src, wanted, extra_assigns=()):
    tree = ast.parse(open(src, encoding="utf-8").read())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            node.decorator_list = []
            keep.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(getattr(t, "id", "") in extra_assigns for t in targets):
                keep.append(node)
    return ast.Module(body=keep, type_ignores=[])


def nwm(stdout, rc=0):
    """cap_nwm_flows + cap_nwm_accounting over a fake monitor that answers `stdout`."""
    calls = []

    async def _nwm_ssh(cluster_id, host_id, command, timeout=60):
        calls.append({"host_id": host_id, "command": command})
        return {"ok": rc == 0, "rc": rc, "stdout": stdout, "stderr": ""}

    async def _cfg_get(cluster_id):
        return {"nwm_host_id": "nwm-login"}

    ns = {"re": re, "Dict": Dict, "Tuple": Tuple, "_nwm_ssh": _nwm_ssh, "_cfg_get": _cfg_get,
          "_sh": lambda s: s, "capability": lambda *a, **k: (lambda f: f)}
    exec(compile(functions(PX_SRC, {"cap_nwm_flows", "cap_nwm_accounting"}, {"_CT_RE"}), PX_SRC, "exec"), ns)
    return ns, calls


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


CT = ("###ACCT 0\n###CT\n"
      "ipv4 2 tcp 6 431999 ESTABLISHED src=10.33.33.7 dst=192.168.0.138 sport=51000 dport=8999 packets=10 bytes=0 "
      "src=192.168.0.138 dst=10.33.33.7 sport=8999 dport=51000 packets=8 bytes=0 [ASSURED] mark=0 use=1\n")


def test_flows_say_whether_bytes_are_counted_and_which_monitor_answered():
    ns, calls = nwm(CT)
    out = run(ns["cap_nwm_flows"](cluster_id="c1"))
    assert out["tool"] == "conntrack" and out["total_flows"] == 1
    assert out["accounting"] is False, "the byte columns are honestly zero, and the page can say why"
    assert out["host_id"] == "nwm-login", "the monitor is named so the page can link it"
    assert calls[0]["host_id"] == "nwm-login" and "nf_conntrack_acct" in calls[0]["command"]
    ns, _ = nwm(CT.replace("###ACCT 0", "###ACCT 1"))
    assert run(ns["cap_nwm_flows"](cluster_id="c1"))["accounting"] is True
    ns, _ = nwm(CT.replace("###ACCT 0", "###ACCT ?"))
    assert run(ns["cap_nwm_flows"](cluster_id="c1"))["accounting"] is None
    ns, _ = nwm("###CT\n")
    assert run(ns["cap_nwm_flows"](cluster_id="c1"))["accounting"] is None, "an older answer without the line still parses"


def test_accounting_is_a_dry_run_until_confirmed():
    ns, calls = nwm("1\n")
    plan = run(ns["cap_nwm_accounting"](cluster_id="c1"))
    assert plan["dry_run"] and plan["plan"]["commands"] == ["sysctl -w net.netfilter.nf_conntrack_acct=1"]
    assert any("reboot" in w for w in plan["plan"]["warnings"]) and not calls, "nothing ran"
    done = run(ns["cap_nwm_accounting"](cluster_id="c1", confirm=True))
    assert done["ok"] and done["accounting"] is True and len(calls) == 1
    assert "sysctl -w net.netfilter.nf_conntrack_acct=1" in calls[0]["command"]
    ns, calls = nwm("0\n")
    assert run(ns["cap_nwm_accounting"](cluster_id="c1", enable=False, confirm=True))["accounting"] is False


def test_the_traffic_page_names_talkers_through_the_one_list():
    p = read("vera", "proxmox", "netops_panel.html")
    assert "NO.who=(addr)=>{" in p and "veraEstate.find({addr})" in p and "veraEstate.chip(veraEstate.ref(m), m.label+' · '+addr)" in p
    assert p.count("NO.who(") >= 5, "talkers, flow src/dst and capture src/dst"
    assert "veraEstate.chip('host:'+r.host_id" in p, "the monitor itself is linked"
    assert "nf_conntrack_acct off" in p and "veraEstate.plan('/pxstore/nwm/accounting'" in p
    assert '<script src="/ui/vera-estate.js"></script>' in p and '<script src="/ui/vera-entity-drawer.js"></script>' in p
