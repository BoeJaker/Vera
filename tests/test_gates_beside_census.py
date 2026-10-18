"""Gates beside a census (compute-roles K, owner 11 Oct 2026): a gate's test shards can yield the
CPU to inference (idle_priority) and leave named nodes alone while a loop or census runs
(busy_avoid). Both off by default."""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import test_runner_core as core  # noqa: E402

CAPS = (ROOT / "vera" / "evolve" / "test_runner_capabilities.py").read_text(encoding="utf-8")
NC = (ROOT / "vera" / "workers" / "node_compute_capabilities.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical


def test_config_defaults_off_and_parsed():
    cfg = core.merge_pool_config({})
    assert cfg["idle_priority"] is False and cfg["busy_avoid"] == []
    cfg = core.merge_pool_config({"idle_priority": 1, "busy_avoid": "cpu-246, cpu-247,"})
    assert cfg["idle_priority"] is True and cfg["busy_avoid"] == ["cpu-246", "cpu-247"]
    assert core.merge_pool_config({"busy_avoid": ["Ollama-B", " "]})["busy_avoid"] == ["Ollama-B"]


def test_idle_docker_argv():
    argv = ["docker", "run", "--rm", "vera:latest", "pytest"]
    assert core.idle_docker_argv(argv) == ["docker", "run", "--cpu-shares", "2", "--rm",
                                           "vera:latest", "pytest"]
    assert core.idle_docker_argv(["docker", "run", "--cpu-shares", "512", "x"]) == \
        ["docker", "run", "--cpu-shares", "512", "x"]
    assert core.idle_docker_argv(["pytest", "-q"]) == ["pytest", "-q"]


def test_avoided_matches_name_or_id_any_case():
    pick = {"host": "Ollama-B", "host_id": "42c77e63"}
    assert core.avoided(pick, ["ollama-b"]) and core.avoided(pick, ["42C77E63"])
    assert not core.avoided(pick, []) and not core.avoided(pick, ["Ollama"])


@pytest.mark.skipif(not hasattr(os, "SCHED_IDLE"), reason="no SCHED_IDLE on this platform")
def test_yield_cpu_puts_a_process_at_idle():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert core.yield_cpu(p.pid)
        assert os.sched_getscheduler(p.pid) == os.SCHED_IDLE
        assert os.getpriority(os.PRIO_PROCESS, p.pid) == 19
    finally:
        p.kill()
        p.wait()


def test_wired_through_the_pool_and_node_compute():
    shard = core.node_run_shard.__code__
    assert "yield_cpu" in shard.co_names
    assert '"idle": bool(cfg["idle_priority"])' in CAPS
    assert "idle=bool(cfg[\"idle_priority\"])" in CAPS
    assert "argv = core.idle_docker_argv(argv)" in CAPS
    assert "avoid_now[:] = await _busy_avoid(cfg)" in CAPS
    assert "if core.avoided(p[\"pick\"], avoid):" in CAPS
    # run_on_node takes avoid and skips such picks while it plans
    assert 'serial: str = "", avoid=()) -> Dict[str, Any]:' in NC
    assert 'if p["pick"] is not None and _avoided(p["pick"], avoid):' in NC
    assert NC.count("host_fallback=host_fallback, avoid=avoid)") == 2
