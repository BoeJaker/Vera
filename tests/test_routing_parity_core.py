"""A new sandbox starts with prod's model routing (routing_parity_core).

2026-09-27: the bleeding-edge mirror ran the code defaults - coder
qwen2.5-coder:14b, executor/writer unset - where prod runs coder qwen2.5:7b and
the 9b pinned to the GPU node, so anything measured there measured other models.
Pure tests use the lowercase import; the seeding tests import the app module and
run in-container, where the merge gate runs.
"""
import asyncio
import copy
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.workers import routing_parity_core as P  # noqa: E402

try:
    from Vera.vera import capability_orchestration as M
except Exception:                                    # pragma: no cover
    M = None

SBX = {"VERA_IS_DEV_SANDBOX": "1",
       "VERA_GATE_BROKER_URL": "https://host.docker.internal:8999/mcp/call"}

PROD_ROLES = {"user": {"loop": {"label": "Agentic Loop", "owner": "dag_workshop", "roles": {
    "coder": {"job_type": "loop_coder", "model": "qwen2.5:7b", "prefer_gpu": True},
    "executor": {"job_type": "loop_executor", "model": "jaahas/qwen3.5-uncensored:9b",
                 "pin": "gpu-250", "prefer_gpu": True}}}},
    "declared": {"loop": {"roles": {"coder": {"model": "qwen2.5-coder:14b"}}}},
    "effective": {"loop": {"roles": {}}}}
PROD_ROUTING = {"active_profile": "vera", "profiles": {
    "vera": {"label": "Vera", "rules": {"naming": {"model": "qwen2.5:0.5b", "deny_gpu": True}},
             "effective": {"naming": {}, "chat": {}}}}}
PROD_CAPS = {"user": {"podcast.*": {"job_type": "idle_podcast", "deny_gpu": True}},
             "declared": {"research.plan*": {"job_type": "research_planner"}}}


# ── pure ─────────────────────────────────────────────────────────────────────

def test_only_a_dev_sandbox_seeds_and_it_can_be_switched_off():
    assert P.enabled(SBX)
    assert not P.enabled({})                                   # prod / a plain process
    assert not P.enabled({**SBX, "VERA_ROUTING_PARITY": "0"})


def test_prod_is_found_from_the_broker_url_every_sandbox_already_has():
    assert P.prod_base_url(SBX) == "https://host.docker.internal:8999"
    assert P.prod_base_url({**SBX, "VERA_PROD_URL": "https://llm.int:8999/"}) == "https://llm.int:8999"
    assert P.prod_base_url({"VERA_IS_DEV_SANDBOX": "1"}) == ""


def test_only_the_layers_the_sandbox_never_held_are_seeded():
    assert P.missing_layers({}) == ["routing", "cap_routing", "role_profiles"]
    assert P.missing_layers({"routing": True, "cap_routing": False,
                             "role_profiles": True}) == ["cap_routing"]


def test_the_user_layers_are_taken_never_the_declared_or_effective_views():
    got = P.user_layers(PROD_ROUTING, PROD_CAPS, PROD_ROLES)
    assert got["role_profiles"]["loop"]["roles"]["coder"]["model"] == "qwen2.5:7b"
    assert "declared" not in got["role_profiles"] and "effective" not in got["role_profiles"]
    assert got["cap_routing"] == {"podcast.*": PROD_CAPS["user"]["podcast.*"]}
    assert got["routing"]["active_profile"] == "vera"
    assert got["routing"]["profiles"]["vera"] == {
        "label": "Vera", "rules": {"naming": {"model": "qwen2.5:0.5b", "deny_gpu": True}}}


def test_junk_responses_yield_empty_layers_not_an_exception():
    got = P.user_layers(None, "x", {"user": {"loop": "not a dict"}})
    assert got["cap_routing"] == {} and got["role_profiles"] == {}
    assert got["routing"]["profiles"] == {}


# ── the orchestrator's seeding, prod faked ──────────────────────────────────

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


class _Resp:
    def __init__(self, doc):
        self._doc = doc

    def raise_for_status(self):
        pass

    def json(self):
        return self._doc


def _fake_client(docs, calls, fail=False):
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            if fail:
                raise ConnectionError("prod down")
            for path, doc in docs.items():
                if url.endswith(path):
                    return _Resp(doc)
            raise AssertionError(url)
    return _C


@pytest.fixture
def isolated(monkeypatch):
    """Snapshot the three live layers and stub persistence, so a test never
    leaks routing into another test or writes anywhere."""
    snap = (copy.deepcopy(M.ROUTING), copy.deepcopy(M.CAP_ROUTING_USER),
            copy.deepcopy(M.ROLE_PROFILES_USER))
    saved = []

    async def _save(kind):
        saved.append(kind)
    monkeypatch.setattr(M, "_save_routing", lambda: _save("routing"))
    monkeypatch.setattr(M, "_save_cap_routing", lambda: _save("cap_routing"))
    monkeypatch.setattr(M, "_save_role_profiles", lambda: _save("role_profiles"))
    yield saved
    M.ROUTING.clear(); M.ROUTING.update(snap[0])
    M.CAP_ROUTING_USER.clear(); M.CAP_ROUTING_USER.update(snap[1])
    M.ROLE_PROFILES_USER.clear(); M.ROLE_PROFILES_USER.update(snap[2])


DOCS = {"/ollama/routing": PROD_ROUTING, "/ollama/cap_routing": PROD_CAPS,
        "/ollama/role_profiles": PROD_ROLES}


@needs_app
def test_a_fresh_sandbox_takes_prods_routing_and_saves_it(monkeypatch, isolated):
    for k, v in SBX.items():
        monkeypatch.setenv(k, v)
    calls = []
    monkeypatch.setattr(M.httpx, "AsyncClient", _fake_client(DOCS, calls))
    seeded = asyncio.run(M._seed_routing_parity({}))
    assert seeded == ["routing", "cap_routing", "role_profiles"]
    assert sorted(isolated) == ["cap_routing", "role_profiles", "routing"]
    assert M.ROLE_PROFILES_USER["loop"]["roles"]["coder"]["model"] == "qwen2.5:7b"
    assert M.ROLE_PROFILES_USER["loop"]["roles"]["executor"]["pin"] == "gpu-250"
    assert M.CAP_ROUTING_USER["podcast.*"]["deny_gpu"] is True
    assert M.ROUTING["active_profile"] == "vera"
    assert all(u.startswith("https://host.docker.internal:8999/ollama/") for u in calls)


@needs_app
def test_a_layer_the_sandbox_already_holds_is_never_overwritten(monkeypatch, isolated):
    for k, v in SBX.items():
        monkeypatch.setenv(k, v)
    M.ROLE_PROFILES_USER.clear()
    M.ROLE_PROFILES_USER["loop"] = {"label": "mine", "owner": "user", "roles": {}}
    calls = []
    monkeypatch.setattr(M.httpx, "AsyncClient", _fake_client(DOCS, calls))
    seeded = asyncio.run(M._seed_routing_parity(
        {"routing": True, "cap_routing": True, "role_profiles": True}))
    assert seeded == [] and calls == [] and isolated == []
    assert M.ROLE_PROFILES_USER["loop"]["label"] == "mine"


@needs_app
def test_prod_unreachable_leaves_the_code_defaults(monkeypatch, isolated):
    for k, v in SBX.items():
        monkeypatch.setenv(k, v)
    before = copy.deepcopy(M.ROLE_PROFILES_USER)
    monkeypatch.setattr(M.httpx, "AsyncClient", _fake_client(DOCS, [], fail=True))
    assert asyncio.run(M._seed_routing_parity({})) == []
    assert M.ROLE_PROFILES_USER == before and isolated == []


@needs_app
def test_prod_itself_never_seeds(monkeypatch, isolated):
    monkeypatch.delenv("VERA_IS_DEV_SANDBOX", raising=False)
    calls = []
    monkeypatch.setattr(M.httpx, "AsyncClient", _fake_client(DOCS, calls))
    assert asyncio.run(M._seed_routing_parity({})) == [] and calls == []
