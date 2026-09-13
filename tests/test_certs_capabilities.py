"""certs.list against stand-in modules: services come from Vera, Proxmox records
and storages, FreeIPA and https Integrations records without repeats; FreeIPA's
cert_find, step-ca records and netctl's status (read in its guest) all feed one
answer; a failing source is a finding, and the answer is cached."""
import ast
import asyncio
import calendar
import json
import os
import sys
import time
import types
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_nav_core as nav  # noqa: E402
from vera.security import certs_core as core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "security", "certs_capabilities.py")
WANTED = {"_flag", "_host_port", "_targets", "_endpoints", "_freeipa", "_letsencrypt", "_timed", "cap_certs_list"}
FAR = time.time() + 400 * 86400


class World:
    def __init__(self, ipa_error="", guest_found=True):
        self.probed: List[Tuple[str, int]] = []
        self.exec_calls: List[Dict] = []
        self.list_calls = 0
        world = self

        async def all_raw():
            return [{"id": "home", "label": "Home", "api_url": "https://192.168.0.200:8006"}]

        async def pve(rec, method, path, data=None):
            return [{"storage": "pbs-estate", "type": "pbs", "server": "192.168.0.170"},
                    {"storage": "local", "type": "dir"}], None

        async def state_raw():
            return {"ipa_url": "https://dc.vera.int"}

        async def state_opened():
            return {"ipa_url": "https://dc.vera.int", "ipa_user": "admin", "ipa_password": "x"}

        async def ipa_call(st, method, args=None, options=None):
            if ipa_error:
                return None, ipa_error
            return {"result": [{"subject": "CN=dc.vera.int,O=VERA.INT", "valid_not_after": "Thu Aug 03 09:28:17 2028 UTC"}],
                    "count": 1}, ""

        modules = {"proxmox.status": {"_all_raw": all_raw, "_open": lambda r: r, "_pve": pve},
                   "identity.status": {"_state_raw": state_raw, "_state_opened": state_opened, "_ipa_call": ipa_call}}

        async def call(name, **kw):
            if name == "integration.list":
                world.list_calls += 1
                return {"integrations": [
                    {"id": "n", "label": "netctl (NWM-02)", "base_url": "http://192.168.0.221:8088"},
                    {"id": "g", "label": "Gitea", "base_url": "https://192.168.0.112"},
                    {"id": "p", "label": "Proxmox again", "base_url": "https://192.168.0.200:8006"},
                    {"id": "v", "label": "vera local", "base_url": "https://127.0.0.1:8999"}]}
            if name == "pki.cert.list":
                return {"certs": [{"fqdn": "app.vera.int", "issued_at": "2026-09-01"}]}
            if name == "estate.machines":
                rows = [{"kind": "guest", "label": "NWM-02", "addr": "192.168.0.221", "ips": ["192.168.0.221"],
                         "cluster_id": "home", "node": "corp", "type": "lxc", "vmid": 145}]
                return {"machines": rows if guest_found else []}
            if name == "proxmox.guest.exec":
                world.exec_calls.append(kw)
                return {"ok": True, "stdout": json.dumps({"domain": "*.boejaker.duckdns.org", "wildcard": None})}
            return {"error": name + " is not loaded"}

        async def probe(name, host, port):
            world.probed.append((host, port))
            if host == "192.168.0.170":
                return {"name": name, "host": host, "port": port, "error": "TimeoutError"}
            return {"name": name, "host": host, "port": port, "subject": "CN=" + host, "issuer": "CN=Certificate Authority,O=VERA.INT",
                    "names": [host], "not_after": FAR, "self_signed": False}

        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
        for n in keep:
            n.decorator_list = []
        self.ns = {"asyncio": asyncio, "json": json, "time": time, "urlparse": urlparse, "Any": Any, "Dict": Dict,
                   "List": List, "Optional": Optional, "Tuple": Tuple, "core": core, "_nav": nav,
                   "_module_of": lambda n: modules.get(n), "_call": call, "_probe": probe,
                   "SOURCE_TIMEOUT_S": 5.0, "CACHE_TTL_S": 300.0, "_CACHE": {"at": 0.0, "value": None},
                   "log": types.SimpleNamespace(debug=lambda *a, **k: None)}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)


def run(coro):
    return asyncio.run(coro)


def test_services_come_from_every_record_without_repeats():
    w = World()
    targets = run(w.ns["_targets"]())
    assert targets == {("127.0.0.1", 8999): "Vera", ("192.168.0.200", 8006): "Proxmox (Home)",
                       ("192.168.0.170", 8007): "PBS (pbs-estate)", ("dc.vera.int", 443): "FreeIPA",
                       ("192.168.0.112", 443): "Gitea"}


def test_every_source_feeds_one_answer():
    w = World()
    out = run(w.ns["cap_certs_list"]())
    sources = sorted({c["source"] for c in out["certs"]})
    assert sources == ["freeipa", "netctl", "service", "step-ca"]
    assert any(c["name"] == "PBS (pbs-estate)" and c["state"] == "unreachable" for c in out["certs"])
    assert w.exec_calls[0]["vmid"] == 145 and w.exec_calls[0]["command"] == "cd /opt/netctl/app && python3 certs.py"
    msgs = [f["message"] for f in out["findings"]]
    assert "No Let's Encrypt certificate has been issued for *.boejaker.duckdns.org yet." in msgs
    assert out["cached"] is False and run(w.ns["cap_certs_list"]())["cached"] is True
    assert run(w.ns["cap_certs_list"](refresh="true"))["cached"] is False


def test_a_failing_source_is_a_finding_and_missing_netctl_guest_is_explained():
    w = World(ipa_error="login HTTP 401", guest_found=False)
    out = run(w.ns["cap_certs_list"]())
    msgs = [(f["severity"], f["message"], f["detail"]) for f in out["findings"]]
    assert ("warn", "Could not read certificates from FreeIPA.", "login HTTP 401") in msgs
    assert any(m[1] == "Could not read certificates from netctl." and "192.168.0.221" in m[2] for m in msgs)
