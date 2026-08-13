"""Unit tests for Foundry cluster-INIT script generation + token parsing
(foundry_core.cluster_init_script / parse_init_token).

foundry.cluster.init bootstraps a NEW Swarm/k3s on a host and captures its join
token into the registry. Pure builder + parser → unit-testable app-free; imported
via lowercase vera.* (see worktree-testable-cores-pattern)."""
from vera.foundry.foundry_core import cluster_init_script, parse_init_token


# ── init script ──────────────────────────────────────────────────────────────────
def test_swarm_init_script():
    s = cluster_init_script("docker-swarm", "10.0.0.1")
    assert "docker swarm init --advertise-addr 10.0.0.1" in s
    assert "get.docker.com" in s                       # installs docker if missing
    assert "VERA_SWARM_TOKEN=$(docker swarm join-token -q worker" in s
    assert "|| true" in s                              # idempotent: no-op if already a manager


def test_swarm_init_without_advertise_addr():
    s = cluster_init_script("docker-swarm")
    assert "docker swarm init >" in s or "docker swarm init  >" in s   # no --advertise-addr
    assert "--advertise-addr" not in s


def test_swarm_init_addr_shell_quoted():
    s = cluster_init_script("docker-swarm", "$(evil)")
    assert "'$(evil)'" in s                            # command substitution neutralised


def test_k3s_init_script():
    s = cluster_init_script("k3s")
    assert "get.k3s.io" in s
    assert "VERA_K3S_TOKEN=$(cat /var/lib/rancher/k3s/server/node-token" in s


def test_uninitable_kinds_empty():
    for k in ("nomad", "ray", "generic", "", "bogus"):
        assert cluster_init_script(k, "x") == ""


# ── token parsing ────────────────────────────────────────────────────────────────
def test_parse_swarm_token_and_addr():
    out = ("Swarm initialized: current node (abc) is now a manager.\n"
           "VERA_SWARM_TOKEN=SWMTKN-1-abcdef\nVERA_SWARM_MGR=10.0.0.1\n")
    r = parse_init_token("docker-swarm", out)
    assert r["token"] == "SWMTKN-1-abcdef" and r["addr"] == "10.0.0.1"


def test_parse_swarm_no_token_when_daemon_down():
    # marker present but empty (subshell returned nothing) → no token captured
    r = parse_init_token("docker-swarm", "VERA_SWARM_TOKEN=\nVERA_SWARM_MGR=\n")
    assert r["token"] == "" and r["addr"] == ""


def test_parse_k3s_token():
    r = parse_init_token("k3s", "noise\nVERA_K3S_TOKEN=K10::server:deadbeef\nmore\n")
    assert r["token"] == "K10::server:deadbeef"


def test_parse_ignores_unrelated_output():
    r = parse_init_token("docker-swarm", "just logs, no markers here")
    assert r["token"] == "" and r["addr"] == ""


def test_parse_unknown_kind():
    r = parse_init_token("nomad", "VERA_SWARM_TOKEN=x")
    assert r["token"] == ""
