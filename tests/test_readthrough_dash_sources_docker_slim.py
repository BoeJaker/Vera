"""2026-09-27 (the widget review): the dashboard's warnings, ontology coverage and subsystem health read the sandbox's own
empty stores; docker.ps sent ~650 KB for a tile that names three fields."""
import importlib.util
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _guard():
    spec = importlib.util.spec_from_file_location("sandbox_guard_rt", os.path.join(ROOT, "vera", "sandbox_guard.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_dashboard_sources_read_through_in_a_sandbox():
    g = _guard()
    env = {"VERA_IS_DEV_SANDBOX": "1"}
    assert g.read_through_allowed("syslog.errors", "POST", env)
    assert g.read_through_allowed("cap_ontology.stats", "GET", env)
    assert g.read_through_allowed("dash.health.summary", "POST", env)


def test_writes_in_those_groups_still_never_read_through():
    g = _guard()
    env = {"VERA_IS_DEV_SANDBOX": "1"}
    assert not g.read_through_allowed("cap_tracking.set_session", "POST", env)
    assert not g.read_through_allowed("syslog.clear", "GET", env)
    assert not g.read_through_allowed("syslog.errors", "GET", {})   # not a sandbox: inert


def test_docker_ps_answers_slim():
    src = open(os.path.join(ROOT, "vera", "workers", "docker_capabilities.py"), encoding="utf-8").read()
    assert re.search(r"async def cap_docker_ps\(host_id: str = \"\", all: bool = True, slim: bool = False", src)
    assert "rows = [_docker_ps_slim(r) for r in rows if isinstance(r, dict)]" in src
    body = src[src.index("def _docker_ps_slim("):]
    for k in ('"Id"', '"Names"', '"State"', '"Status"', '"project"'):
        assert k in body.split("\n\n")[0]
