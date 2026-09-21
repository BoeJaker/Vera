"""FreeIPA DNS: a zone's records can be read, and one A value removed from a
name without touching the others - the registration reader and the portal
names both needed this."""
import ast
import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical
SRC = os.path.join(ROOT, "vera", "provisioning", "identity_capabilities.py")


def caps(calls):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    keep = []
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name in ("cap_dns_records", "cap_dns_delete"):
            n.decorator_list = []
            keep.append(n)

    async def _state_opened():
        return {"dns_zone": "vera.int"}

    async def _ipa_call(st, method, args=None, options=None):
        calls.append((method, args, options))
        if method == "dnsrecord_find":
            rows = [{"idnsname": ["vera"], "arecord": ["192.168.0.138", "192.168.0.221"]},
                    {"idnsname": ["pve"], "arecord": ["192.168.0.200"]}]
            if len(args) > 1:
                rows = [r for r in rows if r["idnsname"][0] == args[1]]
            return {"result": {"result": rows}}, ""
        return {"result": {}}, ""

    async def emit_event(ev):
        calls.append(("event", ev))

    ns = {"Dict": Dict, "List": List, "Optional": Optional, "Any": Any, "_state_opened": _state_opened,
          "_ipa_call": _ipa_call, "emit_event": emit_event}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


def test_records_are_read_by_zone_and_filtered_by_name():
    calls = []
    ns = caps(calls)
    out = asyncio.run(ns["cap_dns_records"]())
    assert out["zone"] == "vera.int" and out["count"] == 2
    assert out["records"][0] == {"name": "vera", "a": ["192.168.0.138", "192.168.0.221"], "aaaa": [], "cname": [], "txt": []}
    assert calls[0] == ("dnsrecord_find", ["vera.int"], {"sizelimit": 2000})
    one = asyncio.run(ns["cap_dns_records"](name="pve"))
    assert [r["name"] for r in one["records"]] == ["pve"] and calls[1][1] == ["vera.int", "pve"]


def test_delete_removes_exactly_one_a_value():
    calls = []
    ns = caps(calls)
    assert asyncio.run(ns["cap_dns_delete"](name="vera")) == {"error": "name and ip required"}
    out = asyncio.run(ns["cap_dns_delete"](name="vera", ip="192.168.0.138"))
    assert out == {"ok": True, "name": "vera", "removed": "192.168.0.138"}
    assert calls[0] == ("dnsrecord_del", ["vera.int", "vera"], {"arecord": ["192.168.0.138"]}), "one value, not the name"
    assert calls[1][0] == "event" and calls[1][1]["type"] == "identity.dns.deleted"
