"""The node agent is a component provision.deploy installs and a release syncs.

It was installed by hand on each node (edge/vera-node-agent.service) and nothing
redeployed it: on 2026-10-02 every node still ran the 2026-09-16 copy, so
/node/status had no `gpus`. Deployed into the layout the nodes already use - the
media server's dir and ./env, its own unit - and never touching
/etc/default/vera-node-agent, which holds the node's own token.
"""
import os

import pytest

import Vera.vera.provisioning.components_capabilities as components  # noqa: E402

pytestmark = pytest.mark.critical

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
D = "/home/Servers/StableDiffustionWhisper"


async def _async(v):
    return v


def test_the_agent_is_a_syncable_component_in_its_real_layout():
    c = components._COMPONENTS["node_agent"]
    assert c["install_dir"] == D and c["venv"] == "env"
    assert c["port"] == 8770 and c["health"] == "/node/status"
    assert c["service"] == "vera-node-agent" and c["sync"] and c["systemd"]
    assert [d for _src, d, *_m in c["files"]] == ["vera_node_agent.py", "node_runner_core.py"]
    assert "env_file" not in c                      # the token file is the node's own
    unit = open(os.path.join(_ROOT, c["unit_file"][0]), encoding="utf-8").read()
    assert f"ExecStart={D}/env/bin/python3 {D}/vera_node_agent.py" in unit
    assert "EnvironmentFile=-/etc/default/vera-node-agent" in unit


def test_no_other_component_claims_the_agents_port():
    ports = {k: c["port"] for k, c in components._COMPONENTS.items()}
    assert [k for k, p in ports.items() if p == 8770] == ["node_agent"]


def test_the_version_covers_both_files_and_the_unit():
    v = components.host_component_version("node_agent")
    assert v.get("version", "").startswith("code-")
    files = v.get("files") or {}
    for name in ("vera_node_agent.py", "node_runner_core.py", "<unit>", "<deps>"):
        assert name in files, (name, sorted(files))


@pytest.mark.asyncio
async def test_a_deploy_installs_restarts_and_waits_for_status_without_touching_the_token(monkeypatch):
    calls = []

    async def host(_hid):
        return {"id": "h", "host": "192.0.2.10", "user": "root"}

    async def ssh(_hid, cmd, timeout=0, **_kw):
        calls.append(cmd)
        if "VERA_HEALTH" in cmd:
            return {"ok": True, "stdout": 'VERA_HEALTH {"node": "Ollama", "gpus": []}', "stderr": ""}
        return {"ok": True, "stdout": "VERA_DEPS_DONE\nVERA_LAUNCHED service=vera-node-agent", "stderr": ""}

    monkeypatch.setattr(components, "_host_rec", host)
    monkeypatch.setattr(components, "_ssh", ssh)
    monkeypatch.setattr(components, "_ssh_stored_with_input", lambda: None)
    monkeypatch.setattr(components, "_node_has_gpu", lambda _a: True)
    monkeypatch.setattr(components, "observe_infrastructure_effect", lambda **k: {"mode": k["mode"]})
    monkeypatch.setattr(components, "emit_event", lambda *_a: _async(None))

    res = await components.cap_deploy.__wrapped__(host_id="h", component="node_agent",
                                                  install_deps=True, launch=True)
    assert res["ok"], res
    allc = "\n".join(calls)
    assert f"> {D}/vera_node_agent.py" in allc and f"> {D}/node_runner_core.py" in allc
    # the existing ./env is kept; the three packages without -U
    assert f'[ -x "{D}/env/bin/python" ] || python3 -m venv "{D}/env"' in allc
    assert f'"{D}/env/bin/pip" install fastapi uvicorn pydantic' in allc
    # the unit, a restart (not enable --now), the health wait on :8770
    assert "/etc/systemd/system/vera-node-agent.service" in allc
    assert "systemctl restart vera-node-agent" in allc and "enable --now" not in allc
    assert "http://127.0.0.1:8770/node/status" in allc
    # the node's token file is never read into, copied or written
    assert "/etc/default/vera-node-agent" not in allc
    assert "/etc/default/vera-inference" not in allc
    assert res["health"] == {"node": "Ollama", "gpus": []}
    assert res["version"].startswith("code-")
