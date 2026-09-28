"""The Estate's node-activity pane: who called, what ran, how it went.

Records come from the node-side taps. A Vera tags its own calls with
X-Vera-Origin so prod, each sandbox and external callers can be told apart."""
import pytest

from Vera.vera.workers import node_activity_core as core
import Vera.vera.capability_orchestration as CO

pytestmark = pytest.mark.critical

NOW = 1_000_000.0


def _rec(**kw):
    r = {"id": "a1", "node": "cpu-246", "port": 11435, "service": "ollama", "kind": "generate",
         "model": "qwen2.5:7b", "caller": "192.168.0.138", "origin": "prod|chat|r1|llm.generate",
         "start": NOW - 30, "end": NOW - 20, "duration_s": 10.0, "status": 200,
         "eval_count": 100, "tps": 10.0, "prompt": "hello there", "response": "general kenobi"}
    r.update(kw)
    return r


def test_caller_classes():
    assert core.caller_class(_rec()) == "prod"
    assert core.caller_class(_rec(origin="sandbox:vera-dev-x|loop_executor|r2|")) == "sandbox"
    assert core.caller_class(_rec(origin="")) == "external"
    assert core.origin_parts(_rec()) == {"who": "prod", "job_type": "chat", "req_id": "r1",
                                         "cap": "llm.generate"}


def test_filters():
    r = _rec()
    assert core.matches(r, node="cpu-246", caller="prod", kind="generate", text="KENOBI")
    assert not core.matches(r, node="cpu-247")
    assert not core.matches(r, caller="sandbox")
    assert not core.matches(r, text="nothing like it")
    assert not core.matches(r, since=NOW - 5)                 # ended before the window


def test_summary_per_node_includes_whats_running_now():
    recs = [_rec(), _rec(id="a2", origin="", kind="embed", eval_count=None, tps=None,
                         duration_s=0.2, vectors=16),
            _rec(id="a3", node="gpu-250-cpu", status=500, error="boom"),
            _rec(id="old", end=NOW - 5000)]                    # outside the window
    infl = {"cpu-247": [{"id": "z", "model": "qwen3.6:35b-a3b", "start": NOW - 12,
                         "origin": "prod|dream_director|r9|"}]}
    s = core.summarize(recs, infl, window_s=900, now=NOW)
    assert s["cpu-246"]["calls"] == 2
    assert s["cpu-246"]["by_caller"] == {"prod": 1, "external": 1}
    assert s["cpu-246"]["by_kind"] == {"generate": 1, "embed": 1}
    assert s["cpu-246"]["tokens_out"] == 100 and s["cpu-246"]["mean_tps"] == 10.0
    assert s["gpu-250-cpu"]["errors"] == 1
    run = s["cpu-247"]["running"][0]
    assert run["running_s"] == 12.0 and run["caller_class"] == "prod"
    row = core.row(_rec(prompt="x" * 1000))
    assert len(row["prompt_preview"]) == 240 and row["prompt_chars"] == 1000
    assert row["caller_class"] == "prod" and row["job_type"] == "chat"


def test_a_vera_names_itself_on_every_ollama_call(monkeypatch):
    monkeypatch.delenv("VERA_ORIGIN_NAME", raising=False)
    monkeypatch.delenv("VERA_IS_DEV_SANDBOX", raising=False)
    h = CO.vera_origin_header("chat", "r1", "llm.generate")
    assert h == {"X-Vera-Origin": "prod|chat|r1|llm.generate"}
    monkeypatch.setenv("VERA_IS_DEV_SANDBOX", "1")
    assert CO.vera_origin_header("x")["X-Vera-Origin"].startswith("sandbox:")
    monkeypatch.setenv("VERA_ORIGIN_NAME", "bench-rig")
    assert CO.vera_origin_header()["X-Vera-Origin"].startswith("bench-rig|")
    # header-safe whatever the job type carries
    assert all(32 <= ord(c) < 127 for c in CO.vera_origin_header("café\n")["X-Vera-Origin"])
