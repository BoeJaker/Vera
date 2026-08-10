"""Unit tests for Foundry cluster-join script generation (foundry_core.cluster_join_script).

Provisioned hosts join Docker Swarm + other common cluster / distributed-compute
systems via a feature bundle. Pure builder → unit-testable app-free; imported via
lowercase vera.* so it binds to the worktree (see worktree-testable-cores-pattern)."""
from vera.foundry.foundry_core import cluster_join_script, CLUSTER_KINDS


def test_docker_swarm_join():
    s = cluster_join_script("docker-swarm", "10.0.0.1", token="SWMTKN-abc")
    assert "docker swarm join --token" in s
    assert "SWMTKN-abc" in s and "10.0.0.1:2377" in s
    assert "get.docker.com" in s          # installs docker if missing


def test_docker_swarm_custom_port():
    s = cluster_join_script("docker-swarm", "mgr", token="t", opts={"port": 12377})
    assert "mgr:12377" in s


def test_k3s_agent_default():
    s = cluster_join_script("k3s", "10.0.0.2", token="K10node")
    assert "get.k3s.io" in s
    assert "K3S_URL=https://10.0.0.2:6443" in s and "K3S_TOKEN=K10node" in s
    assert "sh -s - server" not in s      # agent, not server


def test_k3s_server_role():
    s = cluster_join_script("k3s", "10.0.0.2", token="K10node", role="server")
    assert "sh -s - server --server https://10.0.0.2:6443" in s


def test_nomad_client_config():
    s = cluster_join_script("nomad", "10.0.0.3", role="client")
    assert "client {" in s and 'servers = ["10.0.0.3:4647"]' in s
    assert "hashicorp" in s               # installs nomad on apt hosts


def test_ray_worker_join():
    s = cluster_join_script("ray", "10.0.0.4", token="rpw")
    assert "ray start --address=10.0.0.4:6379" in s
    assert "--redis-password=rpw" in s
    assert "ray[default]" in s            # installs ray if missing


def test_ray_without_password():
    s = cluster_join_script("ray", "10.0.0.4")
    assert "--redis-password" not in s


def test_generic_runs_supplied_command():
    s = cluster_join_script("generic", opts={"command": "myctl join --here"})
    assert "myctl join --here" in s


def test_unknown_kind_is_empty():
    assert cluster_join_script("kubernetes-but-typo", "x", "y") == ""
    assert cluster_join_script("") == ""


def test_token_is_shell_quoted_no_injection():
    # a token with shell metacharacters must be single-quoted, not break out
    evil = "t; rm -rf /"
    s = cluster_join_script("docker-swarm", "10.0.0.1", token=evil)
    assert "'t; rm -rf /'" in s           # single-quoted as one arg
    assert "--token 't; rm -rf /'" in s   # the metachars stay inside the quotes


def test_addr_is_shell_quoted():
    s = cluster_join_script("ray", "$(evil)", token="")
    assert "'$(evil)'" in s                # command-substitution neutralised


def test_all_kinds_declared_produce_output():
    for k in CLUSTER_KINDS:
        s = cluster_join_script(k, "addr", token="tok", opts={"command": "x"})
        assert s and s.startswith("# ---"), f"{k} produced no script"
