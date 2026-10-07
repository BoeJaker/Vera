"""The docker-resilience Foundry feature and the host stack's Docker files (2026-09-29: dockerd OOM-killed after a
two-week leak; its restart stopped every container and hung 16 h). Lowercase vera.* so pytest binds to the worktree."""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from vera.foundry import features_core as fc
from vera.foundry.features_core import FEATURES, feature_script

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCKER = os.path.join(ROOT, "deploy", "host-stack", "docker")


def test_the_feature_is_registered_and_wraps_the_adapter():
    assert "docker-resilience" in FEATURES
    s = feature_script("docker-resilience", {})
    assert s.startswith("#!/bin/sh") and "pkg_install()" in s


def test_it_merges_live_restore_installs_the_oom_dropins_and_reloads_not_restarts():
    s = feature_script("docker-resilience", {"core_containers": ["redis", "neo4j"]})
    assert "daemon_json_merge '{\"live-restore\": true}'" in s
    assert "for u in docker containerd; do" in s and "OOMScoreAdjust=-900" in s
    assert "systemctl reload docker" in s and "systemctl restart docker" not in s     # a restart would stop containers
    assert "echo -900 > /proc/$p/oom_score_adj" in s
    assert "docker update --restart=always 'redis'" in s and "docker update --restart=always 'neo4j'" in s


def test_a_host_without_docker_is_left_alone():
    assert "docker is not installed - nothing to make resilient'; exit 0" in feature_script("docker-resilience", {})


def test_a_container_name_cannot_break_out_of_the_command():
    s = feature_script("docker-resilience", {"core_containers": ["x'; rm -rf / #"]})
    assert "docker update --restart=always 'x'\\''; rm -rf / #'" in s


def test_the_worker_feature_merges_daemon_json_instead_of_overwriting_it():
    s = feature_script("distributed-compute", {"registry": "reg:5000"})
    assert "cat > /etc/docker/daemon.json" not in s
    assert "daemon_json_merge '{\"insecure-registries\": [\"reg:5000\"]}'" in s


def test_the_deploy_files_say_what_the_feature_installs():
    dj = json.load(open(os.path.join(DOCKER, "daemon.json")))
    assert dj["live-restore"] is True and json.loads(fc.DOCKER_DAEMON_SETTINGS_JSON).items() <= dj.items()
    for unit in ("docker", "containerd"):
        assert open(os.path.join(DOCKER, unit + ".service.d", fc.DOCKER_OOM_DROPIN_NAME)).read() == fc.DOCKER_OOM_DROPIN
    sysctl = os.path.join(ROOT, "deploy", "host-stack", "sysctl", fc.HOST_SWAPPINESS_CONF_NAME)
    assert open(sysctl).read() == fc.HOST_SWAPPINESS_CONF


def test_it_keeps_service_memory_out_of_swap():
    s = feature_script("docker-resilience", {})
    assert "> /etc/sysctl.d/60-vera-swappiness.conf" in s
    assert "sysctl -q -p /etc/sysctl.d/60-vera-swappiness.conf" in s
    assert "vm.swappiness = 10" in s


@pytest.mark.skipif(not shutil.which("sh") or not shutil.which("python3"), reason="needs sh and python3")
def test_the_merge_keeps_every_existing_key_and_unions_lists():
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "daemon.json")
        json.dump({"data-root": "/mnt/dockerdata/docker", "insecure-registries": ["a:5000"]}, open(path, "w"))
        fn = fc.DAEMON_JSON_MERGE_FN.replace("/etc/docker/daemon.json", path).replace("mkdir -p /etc/docker", "mkdir -p " + d)
        script = fn + "daemon_json_merge '{\"live-restore\": true}'\ndaemon_json_merge '{\"insecure-registries\": [\"b:5000\", \"a:5000\"]}'\n"
        subprocess.run(["sh", "-c", script], check=True)
        out = json.load(open(path))
        assert out == {"data-root": "/mnt/dockerdata/docker", "insecure-registries": ["a:5000", "b:5000"], "live-restore": True}
    finally:
        shutil.rmtree(d)


STACK = os.path.join(ROOT, "deploy", "host-stack")


def test_the_stack_file_adopts_existing_volumes_and_commits_no_values():
    y = open(os.path.join(STACK, "docker-compose.yml")).read()
    assert y.count("\n    restart: always") == y.count("\n    container_name:") >= 12
    # every volume is external (adopted by its real name): never an empty new one for Redis, Neo4j or OpenBao
    vols = y.split("\nvolumes:\n", 1)[1].split("\nnetworks:", 1)[0]
    assert vols.count("external: true") == vols.count("    name: ") > 10
    # environment values are only references to .env
    env_lines = [l.strip() for l in y.splitlines() if l.startswith("      ") and ": \"${" in l]
    assert env_lines and all(l.endswith("}\"") for l in env_lines)
    ex = open(os.path.join(STACK, ".env.example")).read()
    assert all("=<" in l for l in ex.splitlines() if l and not l.startswith("#"))
    ignored = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "deploy/host-stack/.env"]).returncode == 0
    assert ignored, ".env must be ignored by git"
