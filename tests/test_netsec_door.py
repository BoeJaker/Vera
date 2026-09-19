"""Vera's mesh as devices on netctl's door.

Vera ran a second WireGuard control plane (10.88.0.0/16) beside netctl's, which
owns the estate's real door: the server key, the endpoint, the DuckDNS name and
the router forward. It never carried anything - five members, two of them the
same test host - so the mesh moves onto the door as its own profile instead.
What must hold:

  * a device's private key is still generated on the host and never travels;
  * the door allocates the address, so Vera must not hand out a 10.88 one;
  * a member is found again by a stable device name on the next sync, and a
    rotated key is re-enrolled rather than silently left stale;
  * tearing a member down revokes its device at the door.
"""
import ast
import asyncio
import os
import sys
import types
from typing import Dict, List

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.networking import netsec_core as core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "networking", "netsec_capabilities.py")
PUB_A = "A" * 43 + "="
PUB_B = "B" * 43 + "="


# ── the rules, with no app behind them ───────────────────────────────────────

def test_a_device_name_is_stable_readable_and_fits_netctl():
    name = core.door_device_name("Ollama-B (cpu-246)", "42c77e63-07e7-402d-969f-c8fa31682477")
    assert name == core.door_device_name("Ollama-B (cpu-246)", "42c77e63-07e7-402d-969f-c8fa31682477")
    assert len(name) <= 32 and all(c.isalnum() or c in "-_" for c in name)
    assert name.startswith("vera-Ollama-B")
    # Two hosts that share a label do not collide.
    assert core.door_device_name("worker", "aaaaaaaa-1") != core.door_device_name("worker", "bbbbbbbb-2")
    # A label that is already ours is not prefixed twice.
    assert core.door_device_name("vera-worker-01", "abc123").count("vera") == 1
    # Nothing to go on still yields a legal name.
    assert core.door_device_name("", "") == "vera"


def test_the_member_config_names_the_door_and_never_a_private_key():
    conf = core.wg_door_config("10.66.66.2", {
        "server_pubkey": PUB_A, "endpoint": "192.168.0.221:51821",
        "allowed_ips": ["192.168.0.138/32", "10.66.66.0/24"], "keepalive": 25},
        "/etc/wireguard/vera0.key")
    assert "PrivateKey = $(cat /etc/wireguard/vera0.key)" in conf, "the key must be read on the host"
    assert PUB_A in conf and "Endpoint = 192.168.0.221:51821" in conf
    assert "AllowedIPs = 192.168.0.138/32, 10.66.66.0/24" in conf
    assert conf.count("[Peer]") == 1, "at a door a member has exactly one peer: the door"
    assert "PersistentKeepalive = 25" in conf
    # An address that arrives with a mask is still written as a single host.
    assert "Address = 10.66.66.2/32" in core.wg_door_config("10.66.66.2/24", {}, "/k")


def test_a_rotated_key_is_spotted_rather_than_left_stale():
    devices = [{"name": "vera-a", "pubkey": PUB_A}, {"name": "vera-b", "pubkey": PUB_B}]
    assert core.door_device_for(devices, "vera-a", PUB_A)[0] == "ok"
    assert core.door_device_for(devices, "vera-a", PUB_B)[0] == "rekey"
    assert core.door_device_for(devices, "vera-c", PUB_A) == ("add", None)
    # A door that does not report keys is taken at its word rather than churned.
    assert core.door_device_for([{"name": "vera-a"}], "vera-a", PUB_A)[0] == "ok"


# ── the provider, against a stand-in door and host ───────────────────────────

class World:
    """The provider with its door and its SSH channel replaced."""

    def __init__(self, devices=None, add_fails=""):
        self.calls = []
        self.scripts = []
        self.devices = list(devices or [])
        world = self

        async def door(cfg, method, path, body=None):
            world.calls.append((method, path, body))
            if path == "/api/door/peers":
                return {"peers": world.devices, "server_pubkey": PUB_A,
                        "endpoint": "192.168.0.221:51821", "profile": "vera",
                        "allowed_ips": ["192.168.0.138/32", "10.66.66.0/24"]}
            if path == "/api/door/peer":
                if add_fails:
                    return {"error": add_fails}
                dev = {"ok": True, "name": body["name"], "address": "10.66.66.7",
                       "pubkey": body.get("pubkey", ""), "server_pubkey": PUB_A,
                       "endpoint": "192.168.0.221:51821", "keepalive": 25,
                       "allowed_ips": ["192.168.0.138/32", "10.66.66.0/24"]}
                world.devices.append({"name": dev["name"], "pubkey": dev["pubkey"],
                                      "address": dev["address"]})
                return dev
            if path.startswith("/api/door/peer/delete/"):
                name = path.rsplit("/", 1)[-1]
                world.devices = [d for d in world.devices if d.get("name") != name]
                return {"ok": True}
            return {"error": "unexpected path " + path}

        async def ssh(host_id, command, timeout=120):
            world.scripts.append(command)
            return {"ok": True, "rc": 0, "stdout": "VERA_WG_UP\n", "stderr": ""}

        tree = ast.parse(open(SRC, encoding="utf-8").read())
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "NetctlDoorProvider")
        cls.bases = [ast.copy_location(ast.Name(id="_Base", ctx=ast.Load()), cls)]

        class _Base:                      # what the provider inherits and we stub
            teardown_called = []

            def _iface(self, cfg):
                return cfg.get("iface", "vera0")

            async def teardown(self, host_id, cfg):
                _Base.teardown_called.append(host_id)
                return {"ok": True, "detail": "down"}

        self.base = _Base
        ns = {"_door": door, "_ssh": ssh, "_root_wrap": lambda s: "sudo sh -c " + s,
              "wg_door_config": core.wg_door_config, "door_device_name": core.door_device_name,
              "door_device_for": core.door_device_for, "_Base": _Base,
              "Dict": Dict, "List": List, "WireGuardProvider": _Base,
              "log": types.SimpleNamespace(debug=lambda *a, **k: None)}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), SRC, "exec"), ns)
        self.provider = ns["NetctlDoorProvider"]()


def run(coro):
    return asyncio.run(coro)


def cfg():
    return {"iface": "vera0", "door_url": "http://192.168.0.221:8088",
            "door_secret": "netctl/door-vera", "members": {}}


def test_a_new_member_is_enrolled_at_the_door_and_takes_its_address():
    w = World()
    member = {"host_id": "h1", "label": "vera-worker-01", "pubkey": PUB_B, "ip": ""}
    out = run(w.provider.apply("h1", cfg(), member, peers=[{"host_id": "other", "ip": "10.66.66.9"}]))
    assert out["ok"], out
    assert member["ip"] == "10.66.66.7", "the door's address wins"
    assert member["door_device"].startswith("vera-worker-01")
    assert member["endpoint"] == "192.168.0.221:51821"
    posted = [c for c in w.calls if c[1] == "/api/door/peer"]
    assert posted and posted[0][2]["pubkey"] == PUB_B, "only the public half is sent"
    script = w.scripts[0]
    assert "PrivateKey = $(cat /etc/wireguard/vera0.key)" in script
    assert script.count("[Peer]") == 1, "the other members are not peers here - the door is"
    assert "10.66.66.9" not in script


def test_an_existing_device_is_reused_rather_than_re_enrolled():
    w = World(devices=[{"name": "vera-worker-01-h1", "pubkey": PUB_B, "address": "10.66.66.3"}])
    member = {"host_id": "h1", "label": "vera-worker-01", "pubkey": PUB_B,
              "door_device": "vera-worker-01-h1", "ip": "10.66.66.3"}
    out = run(w.provider.apply("h1", cfg(), member, peers=[]))
    assert out["ok"] and member["ip"] == "10.66.66.3"
    assert not [c for c in w.calls if c[1] == "/api/door/peer"], "no second enrolment"


def test_a_rotated_key_revokes_the_old_device_before_enrolling_again():
    w = World(devices=[{"name": "vera-worker-01-h1", "pubkey": PUB_A, "address": "10.66.66.3"}])
    member = {"host_id": "h1", "label": "vera-worker-01", "pubkey": PUB_B,
              "door_device": "vera-worker-01-h1"}
    out = run(w.provider.apply("h1", cfg(), member, peers=[]))
    assert out["ok"], out
    paths = [c[1] for c in w.calls]
    assert "/api/door/peer/delete/vera-worker-01-h1" in paths
    assert paths.index("/api/door/peer/delete/vera-worker-01-h1") < paths.index("/api/door/peer")


def test_a_door_that_refuses_is_reported_and_nothing_is_written_to_the_host():
    w = World(add_fails="netctl refused the door token")
    member = {"host_id": "h1", "label": "w", "pubkey": PUB_B}
    out = run(w.provider.apply("h1", cfg(), member, peers=[]))
    assert not out["ok"] and "refused" in out["error"]
    assert w.scripts == [], "no tunnel is configured when the door said no"


def test_leaving_revokes_the_device_at_the_door():
    w = World(devices=[{"name": "vera-w-h1", "pubkey": PUB_B, "address": "10.66.66.3"}])
    c = cfg()
    c["members"]["h1"] = {"host_id": "h1", "door_device": "vera-w-h1"}
    out = run(w.provider.teardown("h1", c))
    assert out["ok"] and "h1" in w.base.teardown_called
    assert [c for c in w.calls if c[1] == "/api/door/peer/delete/vera-w-h1"]
    assert w.devices == [], "the device is gone from the door, not just from Vera"


def test_the_provider_declares_that_the_door_allocates_addresses():
    """cap_mesh_join must not hand out a 10.88 address the door will overwrite."""
    src = open(SRC, encoding="utf-8").read()
    assert "assigns_addresses = True" in src
    assert '"ip": "" if prov.assigns_addresses else _alloc_ip(cfg)' in src
    assert '"provider": "netctl"' in src, "the door is the default for a new estate"
