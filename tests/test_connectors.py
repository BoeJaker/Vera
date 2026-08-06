"""Connectors: normalize each registry into connectables + resolve (mocked caps)."""

import asyncio

from vera.operator import connectors as C


def _dispatcher(table):
    async def call(name, **kw):
        v = table.get(name, {})
        return v(kw) if callable(v) else v
    return call


TABLE = {
    "integration.list": {"integrations": [
        {"id": "grafana", "label": "Grafana", "kind": "grafana",
         "base_url": "http://192.168.0.90:3000", "access": {"interact": True}, "sensitive": False},
        {"id": "vault", "label": "Vault", "base_url": "http://x:8200",
         "access": {"interact": False}, "sensitive": True}]},
    "ollama.instances": {"gpu-250": {"url": "http://192.168.0.250:11435",
                                     "label": "GPU", "has_gpu": True, "status": "online"}},
    "nodes.list": {"nodes": [
        {"id": "cpu-246", "label": "CPU A", "host": "192.168.0.246", "url": ""},
        {"id": "web1", "label": "Web", "url": "http://192.168.0.9:8080"}]},
    "docker.hosts.list": {"hosts": [{"id": "h1", "addr": "192.168.0.90", "label": "corp"}]},
    "docker.ps": {"containers": [{"Names": ["/portainer"], "Image": "portainer/portainer",
                                  "Ports": [{"PublicPort": 9000, "Type": "tcp"}]}]},
    "proxmox.cluster.list": {"clusters": [{"id": "c1"}]},
    "proxmox.status": {"guests": [{"vmid": 900, "node": "corp", "type": "qemu",
                                   "name": "vera-test", "status": "running"}]},
    "integration.get": {"base_url": "http://192.168.0.90:3000"},
    "proxmox.console.ticket": {"deeplink_url": "https://192.168.0.200:8006/?console=kvm&vmid=900"},
}


def _run(coro):
    return asyncio.run(coro)


def test_list_connectables_all_sources():
    r = _run(C.list_connectables(_dispatcher(TABLE)))
    assert r["count"] == 7
    assert set(r["groups"]) == {"Integrations", "Ollama", "Nodes", "Docker", "Proxmox"}


def test_integration_access_gate_controls_driveable():
    items = _run(C.from_integrations(_dispatcher(TABLE)))
    by = {i["ref"]: i for i in items}
    assert by["grafana"]["driveable"] is True           # interact granted
    assert by["vault"]["driveable"] is False             # sensitive + interact off


def test_ollama_is_api_not_driveable():
    items = _run(C.from_ollama(_dispatcher(TABLE)))
    assert items and items[0]["type"] == C.API and items[0]["driveable"] is False


def test_node_web_vs_ssh():
    items = _run(C.from_nodes(_dispatcher(TABLE)))
    by = {i["ref"]: i for i in items}
    assert by["cpu-246"]["type"] == C.SSH and by["cpu-246"]["driveable"] is False
    assert by["web1"]["type"] == C.WEB and by["web1"]["driveable"] is True


def test_docker_published_ports():
    assert C._published_ports({"Ports": [{"PublicPort": 8080, "Type": "tcp"}]}) == [8080]
    assert C._published_ports({"Ports": "0.0.0.0:9000->9000/tcp, :::9000->9000/tcp"}) == [9000]
    items = _run(C.from_docker(_dispatcher(TABLE)))
    assert items and items[0]["url"] == "http://192.168.0.90:9000"


def test_proxmox_guest_is_vnc():
    items = _run(C.from_proxmox(_dispatcher(TABLE)))
    assert items and items[0]["type"] == C.VNC
    assert items[0]["ref"] == "c1|corp|qemu|900" and items[0]["driveable"] is True


def test_resolve_integration_and_proxmox_and_docker():
    call = _dispatcher(TABLE)
    assert _run(C.resolve("integration", "grafana", call))["url"] == "http://192.168.0.90:3000"
    px = _run(C.resolve("proxmox", "c1|corp|qemu|900", call))
    assert px["canvas"] is True and px["url"].startswith("https://192.168.0.200:8006")
    dk = _run(C.resolve("docker", "h1|192.168.0.90|9000", call))
    assert dk["url"] == "http://192.168.0.90:9000"


def test_resolve_unknown_source_errors():
    assert "error" in _run(C.resolve("nope", "x", _dispatcher(TABLE)))


def test_ensure_target_uses_connectors():
    from vera.operator import targets as T
    resolved = _run(T.ensure_target({"source": "integration", "ref": "grafana"},
                                    _dispatcher(TABLE)))
    assert resolved["ready"] and resolved["kind"] == "integration"
    assert resolved["start_url"] == "http://192.168.0.90:3000"
