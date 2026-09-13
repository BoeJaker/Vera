"""vfs.peer.* on netctl's WireGuard door: the door token comes from the secrets
service and rides in X-Netctl-Door, add/list/remove map netctl's answers, a
missing or refused token is a clear error, and VFS-02's health no longer counts
its old door."""
import ast
import asyncio
import json
import os
import string
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "vera", "vfs", "vfs_capabilities.py")

pytestmark = pytest.mark.critical

WANTED = {"_valid_peer", "_door", "cap_peer_add", "cap_peer_list", "cap_peer_remove"}
DEFAULTS = {"door_url": "http://192.168.0.221:8088", "door_secret": "netctl/door-files"}


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.content = json.dumps(data).encode()

    def json(self):
        return self._data


class World:
    def __init__(self, token="door-token", netctl_status=200, answers=None):
        self.requests: List = []
        self.events: List = []
        world = self
        answers = answers or {}

        class Client:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, headers=None, json=None):
                world.requests.append((method, url, dict(headers or {}), json))
                path = url.split("8088", 1)[1]
                return FakeResponse(netctl_status, answers.get((method, path), {"ok": True}))

        async def get_named(path):
            world.asked = path
            return {"value": token} if token else None

        async def cfg():
            return dict(DEFAULTS)

        async def emit(ev):
            world.events.append(ev)

        tree = ast.parse(open(SRC, encoding="utf-8").read())
        keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
        for n in keep:
            n.decorator_list = []
        self.ns = {"Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "DEFAULTS": DEFAULTS,
                   "_PEER_NAME_OK": set(string.ascii_letters + string.digits + "-_"),
                   "_cfg": cfg, "_secrets_service": lambda: {"get_named": get_named},
                   "httpx": types.SimpleNamespace(AsyncClient=Client), "emit_event": emit}
        exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), self.ns)


def run(coro):
    return asyncio.run(coro)


def test_adding_a_device_uses_the_door_token_and_the_files_route():
    w = World(answers={("POST", "/api/door/files/peer"): {"ok": True, "address": "10.66.66.2",
                                                           "config": "[Interface]\nPrivateKey = k", "qr_svg": "<svg/>"}})
    out = run(w.ns["cap_peer_add"](name="laptop"))
    method, url, headers, body = w.requests[0]
    assert (method, url) == ("POST", "http://192.168.0.221:8088/api/door/files/peer")
    assert headers == {"X-Netctl-Door": "door-token"} and body == {"name": "laptop", "dns": False}
    assert w.asked == "netctl/door-files"
    assert out["ok"] and out["address"] == "10.66.66.2" and out["config"].startswith("[Interface]")
    assert "door-token" not in json.dumps(out)


def test_listing_maps_devices_and_the_door_state():
    w = World(answers={("GET", "/api/door/files"): {
        "enabled": False, "up": False, "endpoint": "", "port": 51821, "router": "forward UDP 51821",
        "peers": [{"name": "laptop", "address": "10.66.66.2", "last_handshake_s": None, "connected": False},
                  {"name": "phone", "address": "10.66.66.3", "last_handshake_s": 40, "connected": True}]}})
    out = run(w.ns["cap_peer_list"]())
    assert out["count"] == 2 and out["peers"][0]["last_handshake"] == "never"
    assert out["peers"][1] == {"name": "phone", "address": "10.66.66.3", "last_handshake": "40s ago", "connected": True}
    assert out["door"]["enabled"] is False and out["door"]["port"] == 51821


def test_removing_uses_the_files_delete_route():
    w = World()
    assert run(w.ns["cap_peer_remove"](name="laptop")) == {"ok": True, "name": "laptop"}
    assert w.requests[0][:2] == ("POST", "http://192.168.0.221:8088/api/door/files/peer/delete/laptop")


def test_a_missing_or_refused_token_is_a_clear_error():
    assert "holds no netctl door token" in run(World(token="").ns["cap_peer_list"]())["error"]
    assert run(World(netctl_status=401).ns["cap_peer_list"]())["error"] == "netctl refused the door token"
    closed = World(answers={("POST", "/api/door/files/peer"): {"ok": False, "message": "open the door first"}})
    assert run(closed.ns["cap_peer_add"](name="laptop"))["error"] == "open the door first"
    forbidden = World(netctl_status=403, answers={("POST", "/api/door/files/peer/delete/phone"):
                                                  {"error": "this token can only revoke files-only devices"}})
    assert "files-only" in run(forbidden.ns["cap_peer_remove"](name="phone"))["error"]


def test_names_follow_netctls_rule():
    w = World()
    assert "up to 32" in run(w.ns["cap_peer_add"](name="x" * 33))["error"]
    assert "up to 32" in run(w.ns["cap_peer_remove"](name="bad name"))["error"]
    assert w.requests == []


def test_vfs_health_no_longer_counts_the_old_door():
    src = open(SRC, encoding="utf-8").read()
    assert "wg-quick@wg0" not in src and "wg show wg0" not in src and "vfs-peer" not in src
    assert '"door_url": "http://192.168.0.221:8088"' in src
