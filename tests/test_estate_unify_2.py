"""Second unification round: Nodes folds into Machines, Overview findings open
the drawer, containers resolve, Storage's backup tables read the one backup
reader, and the last host selectors read the one list."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_entity_core as ent  # noqa: E402
from vera.estate import estate_health_core as health  # noqa: E402

pytestmark = pytest.mark.critical


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_nodes_is_folded_into_machines_with_its_two_actions_kept():
    panel = read("vera", "workers", "workers_ollama_panel.html")
    assert 'data-pane="nodes" data-view="estate"' not in panel, "the Nodes entry is gone from the nav"
    assert "if(name==='nodes'){" in panel and "return showPane('machines'" in panel, "opening it lands on Machines"
    assert "if(action==='provision'){" in panel and "ndProv(n.id)" in panel
    assert "if(action==='backend'){" in panel and "ndSwitch(n.id)" in panel
    assert "_mcFloat('nd-prov')" in panel, "the provisioning modal is lifted out of the hidden pane"


def test_a_finding_about_a_container_or_guest_carries_its_reference():
    assert health.entity_ref_for("containers", "doc_parser_nginx") == "container:local/doc_parser_nginx"
    assert health.entity_ref_for("guests", "147") == "guest:147"
    assert health.entity_ref_for("backups", "147") == "guest:147"
    assert health.entity_ref_for("guests", "192.168.0.200") == "", "a host address is not a guest"
    assert health.entity_ref_for("containers", "docker") == "", "the docker section note is not a container"
    assert health.entity_ref_for("storage", "corp/sdj") == ""
    out = health.summarize({"containers": {"findings": [health.finding("warn", "containers", "redis", "Redis is stopped.")]},
                            "guests": {"findings": [health.finding("warn", "guests", "147", "VM 147 will not start.")]}})
    refs = {f["subject"]: f.get("ref") for f in out["findings"]}
    assert refs == {"redis": "container:local/redis", "147": "guest:147"}
    panel = read("vera", "workers", "workers_ollama_panel.html")
    assert "f.ref?' <span class=\"mc-ent\" data-entity=\"'+esc(f.ref)" in panel, "the Overview draws the chip"


def test_a_container_resolves_from_the_engine_rows():
    src = ent.Sources(
        docker_hosts=[{"id": "local", "label": "local", "ssh_host_id": "ssh-llm"}],
        machines=[{"id": "m-104", "label": "LLM", "kind": "guest", "vmid": 104, "ssh_host_id": "ssh-llm", "addr": "192.168.0.138", "ips": []}],
        containers=[{"Id": "e1a52aa0b490", "Names": ["/doc_parser_nginx"], "Image": "nginx:1.25", "State": "exited",
                     "Status": "Exited (0) 6 months ago", "Labels": {"com.docker.compose.project": "pdf_pipeline"},
                     "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8085, "Type": "tcp"}],
                     "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                     "Mounts": [{"Type": "bind", "Source": "/opt/pdf/nginx.conf"}]}])
    rec = ent.resolve("container:local/doc_parser_nginx", src)
    assert rec["found"] and rec["title"] == "doc_parser_nginx" and rec["subtitle"] == "container · exited"
    facts = {f["label"]: f["value"] for f in rec["facts"]}
    assert facts["Compose project"] == "pdf_pipeline" and facts["Restart policy"] == "unless-stopped"
    assert facts["Ports"] == "8085->80/tcp" and "/opt/pdf/nginx.conf" in facts["Mounts"]
    assert [(r["noun"], r["ref"]) for r in rec["related"]] == [("Docker host", "docker-host:local"), ("machine", "guest:104")]
    assert ent.resolve("container:doc_parser_nginx", src)["found"], "a bare name means the local host"
    assert ent.resolve("container:local/e1a52aa0", src)["found"], "an id prefix works too"
    assert not ent.resolve("container:local/nope", src)["found"]
    caps = read("vera", "estate", "estate_entity_capabilities.py")
    assert '_call("docker.ps", all=True)' in caps and 'containers=got["docker.ps"]' in caps


def test_storage_backup_tables_read_the_one_backup_reader():
    storage = read("vera", "proxmox", "pxstore_panel.html")
    assert "api('/backup/status')" in storage
    assert "veraEstate.chip('backup-job:'+j.id" in storage and "veraEstate.chip('guest:'+g.vmid" in storage
    assert "no job covers it" in storage and "<th>Covered</th>" in storage
    assert "(r.jobs||[]).map(j=>" in storage, "the node's own job list stays as the fallback"


def test_the_last_host_selectors_read_the_one_list():
    ide = read("vera", "ide", "ide_remote_panel.html")
    assert "veraEstate.logins()" in ide and '<script src="/ui/vera-estate.js"></script>' in ide
    panel = read("vera", "workers", "workers_ollama_panel.html")
    assert "r._estate=veraEstate.label(m)" in panel and "(r._estate||r.label)" in panel
