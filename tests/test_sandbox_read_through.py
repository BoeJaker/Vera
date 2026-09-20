"""A dev sandbox reads prod's estate ONE WAY (vera/sandbox_guard.py, the read-through half).

The policy is pure (env in, verdict out), so it is tested without a process: outside a sandbox nothing is read
through; inside one, only a GET-routed capability of an estate group with no writing word in its name, and never a
reading that is about the sandbox itself.
"""
import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parents[1] / "vera" / "sandbox_guard.py"
_spec = importlib.util.spec_from_file_location("sandbox_guard_under_test", _p)
sg = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(sg)

SBX = {"VERA_IS_DEV_SANDBOX": "1"}


def test_prod_never_reads_through():
    assert sg.upstream_read_url({}) == ""
    assert sg.read_through_allowed("obs.health", "GET", {}) is False
    assert sg.read_through_allowed("obs.health", "GET", {"VERA_IS_DEV_SANDBOX": "0"}) is False


def test_a_sandbox_reads_the_docker_host_by_default_and_can_be_told_otherwise():
    assert sg.upstream_read_url(SBX) == sg.DEFAULT_UPSTREAM_READ_URL
    assert sg.upstream_read_url(dict(SBX, VERA_UPSTREAM_READ_URL="https://vera.int:8999/mcp/call")) == "https://vera.int:8999/mcp/call"
    for off in ("0", "off", "false", "no", ""):
        assert sg.upstream_read_url(dict(SBX, VERA_UPSTREAM_READ_URL=off)) == ""
        assert sg.read_through_allowed("obs.health", "GET", dict(SBX, VERA_UPSTREAM_READ_URL=off)) is False


def test_only_a_get_routed_estate_reading_qualifies():
    for name in ("obs.health", "obs.events", "sysmon.status", "topology.snapshot", "ollama.gate.status", "ollama.route_stats",
                 "ollama.request_log", "ollama.instances", "bench.results", "catalog.installed", "estate.health",
                 "backup.status", "mesh.nodes", "perf.scan", "jobs.stats", "background.status", "evolve.sandbox.list",
                 "memory.stats", "obs.scheduler", "obs.workers"):
        assert sg.read_through_allowed(name, "GET", SBX) is True, name
    for name in ("obs.health",):
        assert sg.read_through_allowed(name, "POST", SBX) is False       # a POST is never a read-through
        assert sg.read_through_allowed(name, None, SBX) is False


def test_a_writing_word_or_a_reading_about_the_sandbox_itself_stays_local():
    for name in ("ollama.gate.lease.acquire", "ollama.routing.save", "ollama.model_tags.set", "evolve.sandbox.spawn",
                 "evolve.sandbox.prune", "evolve.sandbox.exec", "catalog.install", "bench.run", "ollama.pull",
                 "background.enqueue", "cluster.instance_update"):
        assert sg.read_through_allowed(name, "GET", SBX) is False, name
    for name in ("evolve.sandbox.status", "obs.diagnostics", "obs.modules", "sys.dev.restart", "ui.panels", "widget.layouts"):
        assert sg.read_through_allowed(name, "GET", SBX) is False, name


def test_the_groups_can_be_narrowed_from_the_environment():
    env = dict(SBX, VERA_UPSTREAM_READ_GROUPS="obs, sysmon")
    assert sg.read_through_groups(env) == ("obs", "sysmon")
    assert sg.read_through_allowed("obs.health", "GET", env) is True
    assert sg.read_through_allowed("ollama.instances", "GET", env) is False
    assert sg.read_through_groups(SBX) == sg.READ_THROUGH_GROUPS
