"""Node workers: which tasks they may take, which jobs they run, and how they
are provisioned (worker_placement_core, components_core, ollama_node_core).

Imported lowercase with the worktree on sys.path so the worktree copy is what
is tested, not the main checkout's."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.workers import worker_placement_core as wp  # noqa: E402
from vera.provisioning.components_core import (  # noqa: E402
    native_worker_cmd, WORKER_DIR_CANDIDATES, edge_dir_probe_cmd)
from vera.provisioning.ollama_node_core import registration_threads  # noqa: E402


# ── placement: fail closed ────────────────────────────────────────────────────
def test_vetted_namespaces_run_anywhere():
    for cap in ("llm.generate", "nlp.ner", "text.split_chunks", "math.compute",
                "http.get", "memory.store", "memory.search"):
        assert wp.placement(cap)[0] == "any", cap


def test_unvetted_namespaces_stay_on_the_host():
    # the estate, host processes, host credentials, host-local stores, and
    # composers that run their children in-process
    for cap in ("evolve.sandbox.prune", "docker.worker.spawn", "exec.ssh.run",
                "proxmox.node.exec", "web.fetch", "research.run", "dag.run",
                "fabric.upsert", "ollama.add_instance", "some.new_thing"):
        where, why = wp.placement(cap)
        assert where == "host", cap
        assert why


def test_setting_mutators_stay_on_the_host_even_in_a_safe_namespace():
    # nlp.config.set flips nlp.local in the process that runs it
    assert wp.placement("nlp.config.set")[0] == "host"
    assert wp.placement("llm.config")[0] == "host"
    assert wp.placement("memory.config.get")[0] == "host"


def test_operator_overrides():
    assert wp.placement("web.search", node_ok=["web"])[0] == "any"
    # an exact name admits one cap, even past the mutator rule
    assert wp.placement("nlp.config.set", node_ok=["nlp.config.set"])[0] == "any"
    # a namespace admission does not open its mutators
    assert wp.placement("web.config.set", node_ok=["web"])[0] == "host"
    # the brake wins over everything
    assert wp.placement("llm.generate", node_ok=["llm.generate"],
                        host_only=["llm"])[0] == "host"
    env = {"VERA_WORKER_NODE_OK": "web, research", "VERA_WORKER_HOST_ONLY": "memory"}
    assert wp.placement_from_env("research.run", env)[0] == "any"
    assert wp.placement_from_env("memory.store", env)[0] == "host"


def test_streams():
    assert wp.stream_for("llm.generate", env={}) == wp.TASK_STREAM
    assert wp.stream_for("evolve.pipeline.adopt", env={}) == wp.HOST_TASK_STREAM
    assert wp.streams_to_read(is_worker=True) == (wp.TASK_STREAM,)
    assert set(wp.streams_to_read(is_worker=False)) == {wp.TASK_STREAM, wp.HOST_TASK_STREAM}


def test_the_host_runs_everything_a_worker_hands_host_bound_work_on():
    assert wp.may_run_here("evolve.sandbox.prune", is_worker=False, env={})[0]
    ok, why = wp.may_run_here("evolve.sandbox.prune", is_worker=True, env={})
    assert not ok and why
    assert wp.may_run_here("llm.generate", is_worker=True, env={})[0]


# ── scheduler: a worker is not a second host ─────────────────────────────────
def test_worker_runs_no_periodic_job_and_only_loading_hooks():
    S = wp.STARTUP_INTERVAL
    assert wp.scheduler_may_run("anything", 60, is_worker=False)
    assert not wp.scheduler_may_run("evolve.sandbox.idle_sweep", 3600, is_worker=True)
    assert not wp.scheduler_may_run("memory_startup", 60, is_worker=True)  # periodic, even if named
    assert wp.scheduler_may_run("memory_startup", S, is_worker=True)
    assert wp.scheduler_may_run("worker_metrics", S, is_worker=True)
    # pollers, promoters, proxies, sweeps and host-local loaders
    for hook in ("telegram_startup", "email_startup", "dream_startup",
                 "cluster_startup", "job_persist_startup", "fabric_startup",
                 "mesh_startup", "syslog_startup", "evolve_startup"):
        assert not wp.scheduler_may_run(hook, S, is_worker=True), hook


def test_worker_thread_env_caps_every_pool():
    env = wp.worker_thread_env(3)
    assert set(env) == set(wp.THREAD_ENV_VARS) and set(env.values()) == {"3"}
    assert set(wp.worker_thread_env(0).values()) == {str(wp.DEFAULT_WORKER_THREADS)}


# ── ollama registration: the thread count lives on the node ──────────────────
def test_registration_threads():
    assert registration_threads(True) == 0           # GPU node: none
    assert registration_threads(True, 12) == 0
    assert registration_threads(False) == 6          # six real cores each (SMT pair)
    assert registration_threads(False, 4) == 4
    assert registration_threads(False, "junk") == 6


# ── provisioning: the native worker ──────────────────────────────────────────
def _bundle_cmd(**kw):
    return native_worker_cmd(root="/opt/vera/worker", repo="", redis_url="redis://10.0.0.1:6379",
                             backend_kv={"POSTGRES_URL": "postgresql://a:b@10.0.0.1/p"},
                             port=8990, bundle=True,
                             extra_env={"VERA_IS_WORKER": "1", "HOME": "/",
                                        **wp.worker_thread_env(2)},
                             nice=wp.WORKER_NICE, cpu_weight=wp.WORKER_CPU_WEIGHT, **kw)


def test_bundle_mode_unpacks_stdin_and_swaps_the_tree():
    cmd = _bundle_cmd()
    assert "git clone" not in cmd
    assert "base64 -d | tar -xzf - -C /opt/vera/worker/src.new" in cmd
    # unpack beside, then swap: a file the new commit deleted must not linger
    assert cmd.index("tar -xzf") < cmd.index("rm -rf /opt/vera/worker/src;") \
        < cmd.index("mv /opt/vera/worker/src.new /opt/vera/worker/src")
    # the venv is outside the swapped tree, and not rebuilt when present
    assert "/opt/vera/worker/venv/bin/python" in cmd
    assert "[ -x /opt/vera/worker/venv/bin/python ] || python3 -m venv" in cmd


def test_bundle_unit_is_a_node_worker_below_the_nodes_services():
    import base64
    import re
    cmd = _bundle_cmd()
    b64 = re.search(r"printf %s '?([A-Za-z0-9+/=]+)'? \| base64 -d > /etc/systemd/system/vera-worker.service",
                    cmd).group(1)
    unit = base64.b64decode(b64).decode()
    assert 'Environment="VERA_IS_WORKER=1"' in unit
    assert 'Environment="HOME=/"' in unit
    assert 'Environment="OMP_NUM_THREADS=2"' in unit
    assert "Nice=5" in unit and "CPUWeight=50" in unit
    assert "WorkingDirectory=/tmp" in unit
    # a re-provision restarts onto the new commit
    assert "systemctl restart vera-worker" in cmd


def test_worker_dir_never_assumes_home():
    assert WORKER_DIR_CANDIDATES[0] == "/opt/vera/worker"
    assert WORKER_DIR_CANDIDATES[-1].startswith("$HOME")
    probe = edge_dir_probe_cmd(WORKER_DIR_CANDIDATES)
    assert probe.index("/opt/vera/worker") < probe.index("$HOME/.vera/worker")
