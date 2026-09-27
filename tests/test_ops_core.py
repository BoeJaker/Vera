"""Live operations: the snapshot ops_core builds from the readers' answers -
planes, nodes on the lattice, the pipes between them, what is in flight, the
findings pinned to nodes, and a failed reader named rather than invented."""
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import ops_core as oc  # noqa: E402

pytestmark = pytest.mark.critical

NOW = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)


def _src():
    return {
        "estate.machines": {"machines": [
            {"id": "27f0", "label": "PVE01", "kind": "proxmox-node", "status": "", "addr": "192.168.0.200", "node": "corp", "ssh_host_id": "ssh-pve", "hardware": ["Tesla V100", "48 cores"]},
            {"id": "pve:c:250", "label": "Ollama", "kind": "guest", "status": "running", "addr": "192.168.0.250", "ips": ["192.168.0.250"], "node": "corp", "vmid": 250, "type": "lxc", "template": False, "cluster_id": "c", "ssh_host_id": "ssh-250", "hardware": ["8 cores"]},
            {"id": "pve:c:138", "label": "LLM", "kind": "guest", "status": "running", "addr": "192.168.0.138", "ips": [], "node": "corp", "vmid": 138, "type": "qemu", "template": False, "cluster_id": "c"},
            {"id": "pve:c:160", "label": "VFS-02", "kind": "guest", "status": "running", "addr": "192.168.0.160", "ips": ["192.168.0.160"], "node": "corp", "vmid": 160, "type": "lxc", "template": False, "cluster_id": "c"},
            {"id": "pve:c:246", "label": "box-9", "kind": "guest", "status": "running", "addr": "192.168.0.246", "ips": ["192.168.0.246"], "node": "corp", "vmid": 246, "type": "lxc", "template": False, "cluster_id": "c"},
            {"id": "pve:c:999", "label": "old", "kind": "guest", "status": "stopped", "node": "corp", "vmid": 999, "type": "lxc", "template": False},
            {"id": "pve:c:900", "label": "tpl", "kind": "guest", "status": "template", "node": "corp", "vmid": 900, "type": "lxc", "template": True},
            {"id": "lh", "label": "localhost", "kind": "host", "status": "", "addr": "localhost", "ssh_host_id": "ssh-local"},
            {"id": "dk", "label": "192.168.0.250 (vera-worker)", "kind": "docker-host", "status": "", "addr": "192.168.0.250-(vera-worker)", "docker_host_id": "192.168.0.250-(vera-worker)"},
        ]},
        "sysmon.status": {"resources": {"cpu": 21.0, "mem": 56.0}, "proxmox": {"top_guests": [{"vmid": 250, "cpu_pct": 91.0, "mem_pct": 22.0, "status": "running"}]},
                          "docker": {"hosts": [{"id": "local", "label": "local", "reachable": True, "containers": 287, "running": 70}, {"id": "192.168.0.250-(vera-worker)", "label": "vw", "reachable": False, "error": "HTTP 502"}]}},
        "obs.health": {"redis": True, "postgres": True, "chroma": True, "neo4j": False, "caps": 2530, "workers": 2,
                       "gpu_gate": {"busy": True, "held": 1, "capacity": 1}, "census": {"busy": True, "census_run": "run55", "template": "default", "goal": "g", "done": 2, "total": 12}},
        "docker.stack.status": {"host_id": "local", "services": [{"id": "garage", "label": "Garage", "exists": True, "running": True, "status": "up"}, {"id": "redis", "label": "Redis", "exists": False}]},
        "ollama.instances": {"gpu-250": {"label": "GPU Node", "has_gpu": True, "status": "online", "latency_ms": 17, "models": ["a", "b"], "in_use": 1, "errors": 0, "url": "http://192.168.0.250:11435"},
                             "cpu-246": {"label": "CPU A", "has_gpu": False, "status": "offline", "latency_ms": 0, "models": [], "in_use": 0, "errors": 2, "url": "http://192.168.0.246:11435"}},
        "ollama.gate.status": {"nodes": [{"node": "gpu-250", "gated": True, "capacity": 1, "held": 1}]},
        "obs.scheduler": [{"name": "a", "runs": 3}, {"name": "b", "runs": 4}],
        "fabric.health": {"records_count": 1046300, "write_queue_size": 2, "writer_task_alive": True},
        "obs.workers": {"w1": {"status": "busy"}, "w2": {"status": "idle"}},
        "dream.scheduler.status": {"enabled": False, "in_cycle": False, "idle_minutes": 3},
        "background.status": {"running": "ide.claude_sessions.ingest", "quiet_for_s": 94, "queue": {"depth": 1}},
        "evolve.sandbox.list": {"sandboxes": [{"name": "vera-dev", "branch": "loop-lab/sandbox", "running": True, "port": 8998}, {"name": "old", "branch": "x", "running": False}], "capacity": {"available_slots": 0, "exhausted": True}},
        "evolve.pipeline.list": {"pipelines": [{"id": "5e53", "status": "adopted", "branch": "a"}, {"id": "abcd", "status": "review", "branch": "feat/x"}]},
        "loops.program.list": {"programs": [{"id": "p1", "name": "Ingest", "status": "running", "loops": [1, 2]}, {"id": "p2", "name": "Done", "status": "done"}]},
        "mesh.nodes": {"nodes": [{"node_id": "esp-1", "name": "hall", "rssi": -62, "last_seen": "2026-09-21T23:29:00Z", "parent_id": ""}, {"node_id": "esp-2", "name": "far", "rssi": -80, "last_seen": "2026-09-21T20:00:00Z", "parent_id": "esp-1"}],
                       "transports": {"http": True, "ws": False}},
        "obs.node_temps": {"hosts": [{"host_id": "ssh-pve", "label": "PVE01", "max_c": 76.0, "disk_usage": [{"mount": "/", "used_pct": 44.0}]}, {"host_id": "x", "label": "Ollama (cpu)", "max_c": 61.0}]},
        "ollama.request_log": {"entries": [
            {"model": "qwen3:30b", "instance": "gpu-250", "prompt_evidence": {"kind": "chat"}, "ts": "2026-09-21T23:29:50Z", "status": "running"},
            {"model": "nomic-embed-text", "instance": "gpu-250", "prompt_evidence": {"kind": "embed"}, "ts": "2026-09-21T23:20:00Z", "status": "done", "elapsed_s": 3.1},
            {"model": "qwen3:30b", "instance": "gpu-250", "prompt_evidence": {}, "ts": "2026-09-21T23:10:00Z", "status": "done", "elapsed_s": 0.4}]},
        "obs.events": [{"type": "cap.call", "name": "obs.health", "trace_id": "t1", "ts": "2026-09-21T23:29:59Z", "group": "obs"},
                       {"type": "cap.call", "name": "fabric.search", "trace_id": "t2", "ts": "2026-09-21T23:29:58Z", "group": "fabric"},
                       {"type": "cap.ok", "name": "fabric.search", "trace_id": "t2", "ts": "2026-09-21T23:29:59Z"}],
        "estate.health": {"findings": [{"severity": "warn", "section": "containers", "subject": "doc_parser_nginx", "message": "is stopped", "ref": "container:local/doc_parser_nginx"},
                                       {"severity": "error", "section": "guests", "subject": "Ollama", "message": "hot", "ref": "guest:250"},
                                       {"severity": "info", "section": "x", "subject": "y", "message": "z"}]},
        "evolve.errors.list": {"items": [{"title": "ct130 connect timeout", "count": 7, "state": "new"}, {"title": "old", "count": 1, "state": "dismissed"}]},
        "bench.node_perf.history": {"series": {"gpu-250": [{"tps": 1.0, "ping_ms": 10}, {"tps": 2.0, "ping_ms": 12}]}},
        "sysmon.history": {"samples": [{"cpu": 10.0}, {"cpu": 30.0}]},
    }


def test_planes_nodes_and_lattice():
    s = oc.build(_src(), own_ips=["192.168.0.138"], now=NOW)
    ids = {n["id"]: n for n in s["nodes"]}
    assert [p["id"] for p in s["planes"]] == ["work", "core", "service", "runtime", "host", "edge"]
    # running guests only, never templates or stopped ones
    assert "guest:250" in ids and "guest:160" in ids and "guest:999" not in ids and "guest:900" not in ids
    assert ids["guest:250"]["domain"] == oc.COMPUTE and ids["guest:160"]["domain"] == oc.STORAGE
    # the role decides the lane: a guest with no hint in its name that serves a model instance is compute, and says so
    assert ids["guest:246"]["domain"] == oc.COMPUTE and ids["guest:246"]["role"] == "serves CPU A"
    assert ids["guest:138"]["role"] == "runs Vera" and ids["guest:138"]["domain"] == oc.COMPUTE
    assert ids["guest:250"]["ref"] == "guest:250" and ids["guest:250"]["vmid"] == 250
    assert ids["guest:250"]["req"] == {"name": "sysmon.status", "arguments": {}}   # a reading that reads through (proxmox.guest.ip did not)
    # the Vera process's own machine is the guest that carries its IP; the localhost SSH host folds into it
    assert "host:vera" not in ids and "Vera runs here" in ids["guest:138"]["detail"]
    # hosts and the docker runtimes
    assert ids["pve:corp"]["plane"] == "host" and "Tesla V100" in ids["pve:corp"]["detail"]
    assert ids["docker:local"]["plane"] == "runtime" and ids["docker:local"]["detail"] == "70 of 287 running"
    assert ids["docker:192.168.0.250-(vera-worker)"]["status"] == "down"
    # services: the stores as obs.health sees them, the stack's extras, the ollama instances
    assert ids["svc:neo4j"]["status"] == "down" and ids["svc:redis"]["status"] == "ok" and ids["svc:garage"]["domain"] == oc.STORAGE
    assert ids["ollama:gpu-250"]["status"] == "run" and ids["ollama:cpu-246"]["status"] == "down"
    assert ids["ollama:gpu-250"]["load"] == 100.0 and ids["ollama:gpu-250"]["caps"] == ["a", "b"]
    # core and work
    assert ids["core:router"]["detail"] == "gate 1/1 held" and ids["core:cap-bus"]["detail"] == "2530 registered"
    assert ids["core:fabric"]["detail"].startswith("1,046,300 records") and ids["core:workers"]["detail"] == "1 busy of 2"
    assert ids["work:census"]["status"] == "run" and ids["prog:p1"]["inflight"] == 2 and "prog:p2" not in ids
    assert ids["pipe:abcd"]["status"] == "warn" and "pipe:5e53" not in ids
    assert ids["work:bg"]["label"] == "ide.claude_sessions.ingest"
    assert ids["core:looplab"]["status"] == "warn" and ids["sbx:vera-dev"]["plane"] == "runtime"
    # the mesh: a child hangs off its parent, a root off the hub; a silent device is down
    assert ids["mesh:esp-2"]["status"] == "down" and ids["mesh:esp-1"]["status"] == "ok"
    assert {"a": "mesh:esp-2", "b": "mesh:esp-1", "kind": "mesh"} in s["links"]
    assert {"a": "mesh:esp-1", "b": "core:mesh", "kind": "mesh"} in s["links"]


def test_pipes_metrics_and_inflight():
    s = oc.build(_src(), own_ips=["192.168.0.138"], now=NOW)
    ids = {n["id"]: n for n in s["nodes"]}
    L = {(l["a"], l["b"], l["kind"]) for l in s["links"]}
    assert ("guest:250", "pve:corp", "runs") in L
    assert ("ollama:gpu-250", "guest:250", "runs") in L          # the instance runs on the guest that owns its IP
    assert ("docker:192.168.0.250-(vera-worker)", "guest:250", "runs") in L or ("docker:192.168.0.250-(vera-worker)", "dockerhost:192.168.0.250-(vera-worker)", "runs") in L
    assert ("svc:redis", "docker:local", "runs") in L
    assert ("docker:local", "guest:138", "runs") in L             # the stack runs on the machine Vera runs on
    assert ("sbx:vera-dev", "docker:local", "runs") in L
    assert ("core:router", "ollama:gpu-250", "req") in L and ("core:fabric", "svc:chroma", "data") in L
    assert ("pipe:abcd", "core:looplab", "repo") in L
    assert all(l["a"] != l["b"] for l in s["links"])
    # metrics land on their nodes
    assert ids["guest:250"]["load"] == 91.0 and ids["guest:250"]["status"] == "warn"   # busy, but an error finding outranks it
    assert ids["guest:138"]["load"] == 21.0
    assert ids["pve:corp"]["temp"] == 76.0 and ids["pve:corp"]["disk_pct"] == 44.0
    assert ids["guest:250"]["temp"] == 61.0                       # matched by the normalised label
    # in flight: the running request, the open cap call, the fabric queue, the pipeline
    k = s["inflight_kinds"]
    assert k["llm.generate"] == 1 and k["cap call"] == 1 and k["fabric write"] == 2 and k["git op"] == 1 and k["background"] == 1
    assert ids["ollama:gpu-250"]["inflight"] == 2                 # gate held + the running request
    assert any(f["kind"] == "llm.generate" and f["b"] == "ollama:gpu-250" for f in s["inflight"])
    assert ids["ollama:gpu-250"]["through"][0]["n"] == "llm.generate qwen3:30b"
    assert ids["ollama:gpu-250"]["p95_ms"] == 3100.0
    assert s["series"]["ollama:gpu-250"]["tps"] == [1.0, 2.0] and s["series"]["guest:138"]["cpu"] == [10.0, 30.0]


def test_findings_pin_to_nodes_and_failed_readers_are_named():
    s = oc.build(_src(), own_ips=["192.168.0.138"], now=NOW)
    ids = {n["id"]: n for n in s["nodes"]}
    assert ids["guest:250"]["errors"] == 1 and ids["docker:local"]["errors"] == 1
    assert ids["core:looplab"]["errors"] == 1
    assert s["errors"][0]["sev"] == "error" and s["errors"][0]["node"] == "guest:250"
    assert not any(e["n"].startswith("y ") for e in s["errors"])   # info is not a finding here
    assert all(v == "ok" for v in s["sources"].values())
    # a reader that failed or was not read
    src = _src()
    src["mesh.nodes"] = {"error": "mesh.nodes took longer than 6 s"}
    del src["estate.health"]
    s2 = oc.build(src, own_ips=[], now=NOW)
    assert s2["sources"]["mesh.nodes"].startswith("mesh.nodes took longer")
    assert "estate.health" not in s2["sources"]
    assert not any(n["id"].startswith("mesh:") for n in s2["nodes"])
    # no own IP and a localhost SSH host: the Vera host stands on its own
    assert any(n["id"] == "host:vera" for n in s2["nodes"])
    assert {"a": "docker:local", "b": "host:vera", "kind": "runs"} in s2["links"]


def test_only_the_newest_pipelines_stand_on_the_work_plane():
    src = _src()
    src["evolve.pipeline.list"] = {"pipelines": [{"id": f"p{i:02d}", "status": "tested" if i % 2 else "drafting", "branch": f"feat/x{i}", "created_at": f"2026-09-{10 + i:02d}T10:00:00Z"} for i in range(1, 16)]}
    s = oc.build(src, own_ips=["192.168.0.138"], now=NOW)
    pipes = [n for n in s["nodes"] if n["id"].startswith("pipe:")]
    assert len(pipes) == oc.PIPELINES_SHOWN == 6
    assert {n["id"] for n in pipes} == {"pipe:p15", "pipe:p14", "pipe:p13", "pipe:p12", "pipe:p11", "pipe:p10"}   # the newest
    lab = next(n for n in s["nodes"] if n["id"] == "core:looplab")
    assert lab["pipelines_more"] == 9 and "9 more pipelines in flight" in lab["detail"]


def test_empty_sources_build_an_empty_but_well_formed_snapshot():
    s = oc.build({}, now=NOW)
    assert all(n["plane"] == "core" for n in s["nodes"])           # the router and the bus are always there
    assert all(l["a"].startswith("core:") and l["b"].startswith("core:") for l in s["links"])
    assert s["inflight_total"] == 0 and s["errors"] == []
    assert [p["id"] for p in s["planes"]] == ["work", "core", "service", "runtime", "host", "edge"]
