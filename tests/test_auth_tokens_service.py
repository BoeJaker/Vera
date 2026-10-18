"""Caller tokens end to end in R2 (optional, nothing refused): the store and cache, the per-request check with its
evidence and throttled last_used, the middleware setting CALLER_TOKEN, the usage actor, and auth.token.issue /
list / revoke (never returning a secret, only from their own route).

Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import json
import os
import stat
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
from Vera.vera import auth_tokens as at  # noqa: E402
from Vera.vera import auth_tokens_core as tc  # noqa: E402
from Vera.vera import caller_context as ccm  # noqa: E402

NOW = 1_800_000_000.0


class FakePipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def hincrby(self, k, f, n):
        self.ops.append((k, f, n))

    def expire(self, k, s):
        pass

    async def execute(self):
        for k, f, n in self.ops:
            h = self.r.h.setdefault(k, {})
            h[f] = int(h.get(f, 0)) + n


class FakeRedis:
    def __init__(self):
        self.h, self.writes = {}, 0

    async def hget(self, k, f):
        v = self.h.get(k, {}).get(f)
        return v.encode() if isinstance(v, str) else v

    async def hset(self, k, f, v):
        self.writes += 1
        self.h.setdefault(k, {})[f] = v

    async def hgetall(self, k):
        return {f.encode(): (v.encode() if isinstance(v, str) else str(v).encode())
                for f, v in self.h.get(k, {}).items()}

    def pipeline(self, transaction=True):
        return FakePipe(self)


def run(c):
    return asyncio.run(c)


@pytest.fixture
def svc():
    r = FakeRedis()
    s = at.TokenService(lambda: r, clock=lambda: NOW)
    s.evidence.last_flush = __import__("time").monotonic()     # not due: tests flush explicitly
    s.r = r
    return s


def issue(svc, **kw):
    token, tid, secret = tc.mint()
    rec = tc.record(tid, secret, kind=kw.get("kind", "mcp_bridge"), subject=kw.get("subject", "claude@lenovo"), now=NOW)
    run(svc.put(rec))
    return token, rec


def test_check_valid_invalid_none_with_evidence(svc):
    token, rec = issue(svc)
    info, res = run(svc.check({"authorization": "Bearer " + token}, "192.168.0.190"))
    assert res == "valid" and info == {"id": rec["id"], "kind": "mcp_bridge", "subject": "claude@lenovo", "scopes": []}
    assert run(svc.check({"authorization": "Bearer vat_000000000000." + "A" * 43}, "192.168.0.190")) == (None, "invalid:unknown")
    assert run(svc.check({}, "127.0.0.1")) == (None, "none")
    assert run(svc.check({"origin": "https://h"}, "192.168.0.9")) == (None, "none")
    ev = svc.evidence.snapshot()
    assert ev == {"program|lan|valid": 1, "program|lan|invalid:unknown": 1, "program|loopback|none": 1,
                  "browser|lan|none": 1}


def test_last_used_is_written_at_most_once_a_minute(svc):
    token, rec = issue(svc)
    w0 = svc.r.writes
    for _ in range(5):
        run(svc.check({"authorization": "Bearer " + token}, "10.66.66.2"))
    assert svc.r.writes == w0 + 1
    stored = json.loads(svc.r.h[at.KEY][rec["id"]])
    assert stored["last_used"] == NOW and stored["last_peer"] == "10.66.66.2" and stored["digest"] == rec["digest"]


def test_revoke_is_immediate_despite_the_cache(svc):
    token, rec = issue(svc)
    assert run(svc.check({"authorization": "Bearer " + token}, "::1"))[1] == "valid"
    run(svc.revoke(rec["id"]))
    assert run(svc.check({"authorization": "Bearer " + token}, "::1"))[1] == "invalid:revoked"


def test_evidence_summary_counts_offhost_programs_without_a_valid_token(svc):
    token, _ = issue(svc)
    run(svc.check({}, "192.168.0.5"))
    run(svc.check({"x-vera-token": token}, "192.168.0.5"))
    run(svc.check({}, "127.0.0.1"))
    run(svc.check({"origin": "https://x"}, "192.168.0.5"))
    run(svc.flush_evidence())
    s = run(svc.evidence_summary(3))
    assert s["offhost_program_without_valid_token"] == 1
    assert s["counts"]["program|lan|valid"] == 1 and s["counts"]["program|loopback|none"] == 1


def test_a_check_never_raises():
    s = at.TokenService(lambda: (_ for _ in ()).throw(RuntimeError("boom")), clock=lambda: NOW)
    assert run(s.check({"authorization": "Bearer vat_0123456789ab." + "A" * 43}, "1.2.3.4"))[0] is None


def test_the_middleware_sets_the_caller_token_only_when_valid(svc):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    token, rec = issue(svc)
    app = FastAPI()

    @app.get("/t")
    async def t():
        return {"tok": orch.CALLER_TOKEN.get(), "surface": list(orch.CALLER_SURFACE.get())}

    app.add_middleware(ccm.CallerSurface, var=orch.CALLER_SURFACE, token_var=orch.CALLER_TOKEN, tokens=svc)
    c = TestClient(app)
    got = c.get("/t", headers={"Authorization": "Bearer " + token, "X-Vera-Surface": "lhm; trigger=person"}).json()
    assert got["tok"]["kind"] == "mcp_bridge" and got["tok"]["id"] == rec["id"] and got["surface"] == ["lhm", "person"]
    assert c.get("/t", headers={"Authorization": "Bearer vat_000000000000." + "A" * 43}).json()["tok"] == {}
    assert c.get("/t").json() == {"tok": {}, "surface": ["", ""]}


def test_a_valid_token_names_the_usage_actor(monkeypatch):
    from Vera.vera import caller_context_core as cc
    buf = cc.UsageBuffer(interval=3600)
    buf.last_flush = __import__("time").monotonic()
    monkeypatch.setattr(orch, "_USAGE", buf)
    monkeypatch.setattr(orch, "REDIS", None)
    tok = orch.CALLER_TOKEN.set({"id": "x", "kind": "mcp_bridge", "subject": "claude@lenovo", "scopes": []})
    ck = orch.CALLER_KIND.set("claude")
    mc = orch.MCP_CALL_ACTIVE.set(True)
    try:
        run(orch.CAPABILITY_REGISTRY["caps.usage"]["func"](days=1))
    finally:
        orch.MCP_CALL_ACTIVE.reset(mc)
        orch.CALLER_KIND.reset(ck)
        orch.CALLER_TOKEN.reset(tok)
    assert any(k.startswith("caps.usage|token:mcp_bridge|") for k in buf.snapshot())


@pytest.fixture
def caps(monkeypatch, tmp_path, svc):
    monkeypatch.setattr(orch, "_TOKENS", svc)
    monkeypatch.setattr(orch, "_token_file_dir", lambda: tmp_path)
    events = []

    async def fake_emit(e):
        events.append(e)
    monkeypatch.setattr(orch, "emit_event", fake_emit)
    return {"svc": svc, "dir": tmp_path, "events": events}


def raw(name):
    return orch.CAPABILITY_REGISTRY[name]["raw"]


def test_issue_to_a_file_writes_0600_and_never_returns_the_secret(caps):
    out = run(raw("auth.token.issue")(kind="tool", subject="census@llm", deliver="file", scopes="evolve.,obs."))
    assert out["ok"] and "vat_" not in json.dumps(out) and "digest" not in out["token"]
    files = list(caps["dir"].iterdir())
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600
    token = files[0].read_text().strip()
    assert tc.parse(token)[0] == out["token"]["id"]
    assert run(caps["svc"].check({"authorization": "Bearer " + token}, "127.0.0.1"))[1] == "valid"
    assert out["token"]["scopes"] == ["evolve.", "obs."]
    assert not any("vat_" in json.dumps(e) for e in caps["events"])


def test_issue_to_keydrop_hands_the_token_to_keydrop_only(caps, monkeypatch):
    seen = {}

    def fake_put(label, payload):
        seen.update(label=label, payload=payload)
        return {"ok": True, "entry": 7}
    monkeypatch.setattr(orch, "_keydrop_put", fake_put)
    out = run(raw("auth.token.issue")(kind="mcp_bridge", subject="claude@lenovo"))
    assert out["ok"] and out["delivered_to"] == "keydrop entry 7" and "vat_" not in json.dumps(out)
    assert seen["label"] == "vera-token:mcp_bridge:claude@lenovo" and seen["payload"]["token"].startswith("vat_")
    monkeypatch.setattr(orch, "_keydrop_put", lambda l, p: {"error": "chain broken"})
    bad = run(raw("auth.token.issue")(kind="mcp_bridge", subject="claude@lenovo"))
    assert not bad["ok"] and len(run(caps["svc"].all())) == 1        # nothing stored when delivery failed


def test_issue_rejects_bad_input(caps):
    assert not run(raw("auth.token.issue")(kind="root", subject="x", deliver="file"))["ok"]
    assert not run(raw("auth.token.issue")(kind="tool", subject="", deliver="file"))["ok"]
    assert not run(raw("auth.token.issue")(kind="tool", subject="x", deliver="email"))["ok"]


def test_list_and_revoke_show_metadata_only(caps):
    run(raw("auth.token.issue")(kind="node", subject="cpu-246", deliver="file"))
    lst = run(raw("auth.token.list")(days=1))
    assert len(lst["tokens"]) == 1 and "digest" not in lst["tokens"][0] and "evidence" in lst
    tid = lst["tokens"][0]["id"]
    out = run(raw("auth.token.revoke")(id=tid))
    assert out["ok"] and out["token"]["revoked"] is True and "digest" not in out["token"]
    assert not run(raw("auth.token.revoke")(id="nope"))["ok"]


def test_issue_and_revoke_run_only_from_their_own_route(caps):
    def through_wrap(name, **ctx):
        toks = [(getattr(orch, k), getattr(orch, k).set(v)) for k, v in ctx.items()]
        try:
            return run(orch.CAPABILITY_REGISTRY[name]["func"](kind="tool", subject="x", deliver="file")
                       if name.endswith("issue") else orch.CAPABILITY_REGISTRY[name]["func"](id="nope"))
        finally:
            for cv, t in reversed(toks):
                cv.reset(t)
    for name in ("auth.token.issue", "auth.token.revoke"):
        r = through_wrap(name, MCP_CALL_ACTIVE=True)
        assert r["refused"] == "direct" and "/mcp/call" in r["error"]
        assert through_wrap(name, MCP_CALL_ACTIVE=True, CALLER_KIND="claude")["refused"] == "direct"
        assert through_wrap(name, BACKGROUND_LLM="dream:x")["refused"] == "direct"
        assert "refused" not in through_wrap(name, CURRENT_HTTP_CAP=name)
