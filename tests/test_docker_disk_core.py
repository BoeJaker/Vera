"""Where a Docker host's disk has gone, from the Engine's /system/df: totals
per category, what is reclaimable and why, the containers that grew, orphan
volumes, untagged images, the sandboxes the reaper would take."""
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.workers import docker_disk_core as core  # noqa: E402

pytestmark = pytest.mark.critical
GB = 1 << 30

DF = {
    "LayersSize": 40 * GB,
    "Images": [
        {"Id": "sha256:a", "RepoTags": ["vera:latest"], "Size": 12 * GB, "SharedSize": 2 * GB, "Containers": 3},
        {"Id": "sha256:b", "RepoTags": ["redis:7"], "Size": 1 * GB, "SharedSize": 0, "Containers": 1},
        {"Id": "sha256:c", "RepoTags": ["<none>:<none>"], "Size": 3 * GB, "SharedSize": 1 * GB, "Containers": 0},
        {"Id": "sha256:d", "RepoTags": ["old:1"], "Size": 2 * GB, "SharedSize": 0, "Containers": 0},
    ],
    "Containers": [
        {"Id": "1", "Names": ["/vera-orchestrator"], "Image": "vera:latest", "State": "running", "SizeRw": 5 * GB, "SizeRootFs": 17 * GB},
        {"Id": "2", "Names": ["/redis"], "Image": "redis:7", "State": "running", "SizeRw": 100 << 20, "SizeRootFs": GB},
        {"Id": "3", "Names": ["/vera-sbx-chat-1789675339370"], "Image": "vera:latest", "State": "exited", "SizeRw": 700 << 20, "SizeRootFs": 12 * GB},
        {"Id": "4", "Names": ["/sbxw-abc"], "Image": "vera:latest", "State": "running", "SizeRw": 50 << 20, "SizeRootFs": 12 * GB},
        {"Id": "5", "Names": ["/doc_parser_nginx"], "Image": "nginx", "State": "exited", "SizeRw": 0, "SizeRootFs": 200 << 20},
    ],
    "Volumes": [
        {"Name": "pgdata", "UsageData": {"Size": 20 * GB, "RefCount": 1}},
        {"Name": "orphan-7f3", "UsageData": {"Size": 4 * GB, "RefCount": 0}},
        {"Name": "tiny", "UsageData": {"Size": 1 << 20, "RefCount": 0}},
    ],
    "BuildCache": [{"ID": "x", "Type": "regular", "Size": 6 * GB, "InUse": False, "Shared": False},
                   {"ID": "y", "Type": "regular", "Size": 1 * GB, "InUse": True, "Shared": False}],
}
REAP = {"session_total": 50, "running_kept": 0, "recent_kept": 31, "reapable": 19, "retain_hours": 24.0}
MOUNT = {"mount": "/mnt/dockerdata", "total_gb": 738.0, "free_gb": 60.0, "pct_used": 91.9, "note": "/mnt/dockerdata: 60G free of 738G (91.9% used)"}


def test_categories_add_up_and_say_what_is_reclaimable_and_why():
    out = core.summarize(DF, reap=REAP, mount=MOUNT)
    cats = {c["name"]: c for c in out["categories"]}
    assert cats["images"]["size"] == 15 * GB, "shared layers are counted once"
    assert cats["images"]["reclaimable"] == 4 * GB and "2 not used by any container" in cats["images"]["why"]
    assert "1 untagged" in cats["images"]["why"]
    assert cats["containers"]["size"] == 5 * GB + (850 << 20) and cats["containers"]["reclaimable"] == 700 << 20
    assert cats["containers"]["why"] == "2 stopped"
    assert cats["volumes"]["size"] == 24 * GB + (1 << 20) and cats["volumes"]["reclaimable"] == 4 * GB + (1 << 20)
    assert cats["build"]["reclaimable"] == 6 * GB
    assert out["reclaimable_total"] == sum(c["reclaimable"] for c in out["categories"])
    assert out["layers_size"] == 40 * GB


def test_the_big_the_orphaned_and_the_reapable_are_called_out():
    out = core.summarize(DF, reap=REAP, mount=MOUNT)
    msgs = [f["message"] for f in out["findings"]]
    assert msgs[0] == "The Docker data disk is 91.9% full." and out["findings"][0]["severity"] == "warn"
    assert "19 finished session sandboxes are older than 24.0 h and can be reaped." in msgs
    assert "2 volumes are attached to nothing (4.0 GB)." in msgs
    assert "1 untagged images (2.0 GB)." in msgs
    grew = next(f for f in out["findings"] if "grown past 1 GB" in f["message"])
    assert "vera-orchestrator" in grew["detail"]
    assert out["top_containers"][0]["name"] == "vera-orchestrator" and out["top_containers"][0]["writable"] == 5 * GB
    assert out["top_volumes"][0]["name"] == "pgdata" and out["top_volumes"][1]["used_by"] == 0
    assert out["sandboxes"] == {"count": 2, "writable": 750 << 20, "reapable": 19, "retain_hours": 24.0}


def test_a_quiet_host_has_no_findings_and_missing_blocks_are_fine():
    out = core.summarize({"Images": [{"RepoTags": ["a:1"], "Size": GB, "SharedSize": 0, "Containers": 1}],
                          "Containers": [{"Names": ["/a"], "State": "running", "SizeRw": 0}]})
    assert out["findings"] == [] and out["reclaimable_total"] == 0
    assert out["sandboxes"]["count"] == 0 and out["top_volumes"] == []
    assert core.summarize({})["categories"][0]["size"] == 0


def test_the_capability_computes_in_the_background_and_the_pane_polls():
    src = open(os.path.join(ROOT, "vera", "workers", "docker_disk_capabilities.py"), encoding="utf-8").read()
    assert "asyncio.create_task(_compute(host_id))" in src, "the slow walk must not block the caller"
    assert '"/system/df"' in src and "status == 409" in src, "the Engine's 'already running' answer is handled"
    assert "CACHE_TTL_S = 600.0" in src
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'workers/docker_disk_capabilities.py' in orch
    panel = open(os.path.join(ROOT, "vera", "proxmox", "pxstore_panel.html"), encoding="utf-8").read()
    assert 'data-p="docker"' in panel and 'id="p-docker"' in panel
    assert "if(r.computing){" in panel and "setTimeout(()=>P.dockerFsLoad(false), 5000)" in panel
    assert re.search(r"if\(p==='docker'\) P\.dockerFsLoad\(false\);", panel)
