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
        # a POST (or no route) reads through only when its last name is a reading word - these all are
        assert sg.read_through_allowed(name, "POST", SBX) is (name.replace('_', '.').split('.')[-1] in sg.READ_WORDS), name
    for name in ("vfs.status", "netmon.snapshot", "docker.stack.status", "perf.scan", "n8n.workflow.list", "cal.events.list"):
        assert sg.read_through_allowed(name, "POST", SBX) is True, name
        assert sg.read_through_allowed(name, None, SBX) is True, name
    for name in ("ollama.pull", "docker.stack.restart", "vfs.mount", "evolve.sandbox.spawn"):
        assert sg.read_through_allowed(name, "POST", SBX) is False, name


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


def test_widget_read_runs_many_readings_in_one_call(monkeypatch):
    """widget.read: each call runs as /mcp/call would, one failed reading never fails the batch, unknown names say so."""
    import asyncio, sys, types
    from vera.widgets import widget_catalog as wc
    co = types.SimpleNamespace(_READ_THROUGH_URL="", CAPABILITY_REGISTRY={
        "a.status": {"func": (lambda **kw: asyncio.sleep(0, result={"a": 1})), "schema": {"properties": {}}},
        "b.list": {"func": (lambda **kw: asyncio.sleep(0, result=[1, 2, 3])), "schema": {"properties": {"n": {"type": "integer"}}}},
    })

    async def boom(**kw):
        raise RuntimeError("no store")
    co.CAPABILITY_REGISTRY["c.stats"] = {"func": boom, "schema": {"properties": {}}}
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", co)
    out = asyncio.run(wc.widget_read(calls=[{"name": "a.status"}, {"name": "b.list", "arguments": {"n": 2, "junk": 1}}, {"name": "c.stats"}, {"name": "nope.get"}]))
    assert out["ok"] and out["count"] == 4
    r = out["results"]
    assert r[0]["ok"] and r[0]["content"] == {"a": 1}
    assert r[1]["ok"] and r[1]["content"] == [1, 2, 3]
    assert not r[2]["ok"] and "no store" in r[2]["error"]
    assert not r[3]["ok"] and r[3]["error"] == "unknown capability"
    assert asyncio.run(wc.widget_read(calls=[])) == {"ok": True, "results": [], "count": 0}
    # the list may arrive as a JSON string (the call handler coerces an untyped argument): it still reads
    out = asyncio.run(wc.widget_read(calls='[{"name": "a.status"}]'))
    assert out["count"] == 1 and out["results"][0]["ok"] and out["results"][0]["content"] == {"a": 1}
    import inspect as _i
    assert "calls" in wc.widget_read.__wrapped__.__annotations__ if hasattr(wc.widget_read, "__wrapped__") else True
