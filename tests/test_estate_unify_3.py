"""Round three: every entity kind resolves, and the rows that name secrets,
directory hosts, services, containers and models open the drawer."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_entity_core as ent  # noqa: E402
from vera.estate.estate_nav_core import ENTITY_KINDS  # noqa: E402

pytestmark = pytest.mark.critical


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_every_kind_has_a_reader_now():
    for kind in ENTITY_KINDS:
        out = ent.resolve(f"{kind}:x", ent.Sources())
        assert "no reader joined" not in str(out.get("error", "")), kind


def test_a_secret_resolves_to_its_metadata_and_never_its_value():
    src = ent.Sources(secrets=[{"path": "netctl/door-vera", "updated": "2026-09-19T11:40:57Z", "versions": 1},
                              {"path": "platform/ha-token", "updated": "2026-09-13T15:00:00Z", "versions": 3}])
    rec = ent.resolve("secret:netctl/door-vera", src)
    assert rec["found"] and rec["subtitle"] == "named secret"
    facts = {f["label"]: f["value"] for f in rec["facts"]}
    assert facts["Store"] == "OpenBao · secret/vera/named/netctl/door-vera" and facts["Versions"] == 1
    assert "never shown" in facts["Value"] and "netctl door" in facts["Used by"]
    assert "Platforms" in {f["label"]: f["value"] for f in ent.resolve("secret:platform/ha-token", src)["facts"]}["Used by"]
    assert not ent.resolve("secret:nope", src)["found"]
    # nothing in the record carries anything but the path and its metadata
    assert set(rec["facts"][0].keys()) == {"label", "value"} and not rec["related"]


def test_a_model_resolves_to_the_nodes_that_serve_it():
    src = ent.Sources(
        machines=[{"id": "m-126", "label": "Ollama", "kind": "guest", "vmid": 126, "addr": "192.168.0.250", "ips": []},
                  {"id": "m-130", "label": "Ollama-C", "kind": "guest", "vmid": 130, "addr": "192.168.0.247", "ips": []}],
        instances={"gpu-250": {"label": "GPU Node", "url": "http://192.168.0.250:11435", "has_gpu": True, "status": "online", "models": ["qwen3.5:9b", "phi4:latest"]},
                   "cpu-247": {"label": "CPU Node B", "url": "http://192.168.0.247:11435", "has_gpu": False, "status": "offline", "models": ["qwen3.5:9b"]}})
    rec = ent.resolve("model:qwen3.5:9b", src)
    assert rec["found"] and rec["subtitle"] == "model · 2 nodes"
    facts = {f["label"]: f["value"] for f in rec["facts"]}
    assert facts["Online now"] == "gpu-250" and facts["On the GPU"] == "gpu-250"
    assert [(r["ref"], r["detail"]) for r in rec["related"]] == [("guest:126", "gpu-250"), ("guest:130", "cpu-247")]
    assert "no inference node serves" in ent.resolve("model:llama9000", src)["error"]
    assert ent.resolve("model:phi4:latest", src)["subtitle"] == "model · 1 node"
    caps = read("vera", "estate", "estate_entity_capabilities.py")
    assert '_call("secrets.list")' in caps and '_call("ollama.instances")' in caps


def test_the_rows_that_name_these_things_open_the_drawer():
    assert 'data-entity="secret:${esc(s.path)}"' in read("vera", "security", "secrets_panel.html")
    assert 'data-entity="identity:${esc(h.fqdn)}"' in read("vera", "provisioning", "identity_panel.html")
    assert 'data-entity="integration:${esc(it.id)}"' in read("vera", "integrations", "integrations_panel.html")
    for f in ("security/secrets_panel.html", "provisioning/identity_panel.html", "integrations/integrations_panel.html"):
        assert '<script src="/ui/vera-entity-drawer.js"></script>' in read("vera", *f.split("/")), f
    panel = read("vera", "workers", "workers_ollama_panel.html")
    assert "data-entity=\"container:'+esc((_dkrHostId||'local')+'/'+name)" in panel
    assert 'data-entity="model:${esc(m.name)}"' in panel
