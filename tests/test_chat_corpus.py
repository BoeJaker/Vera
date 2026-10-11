"""The chat corpus (four-minds §5.4): the approved cases load, effects are stubbed, and each check judges what a
turn's stream, page calls and canvas showed - pure, from vera.minds.chat_corpus_core."""
import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vera.minds import chat_corpus_core as CC

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tools" / "chat-corpus" / "cases_v0.json"


def _sse(*frames):
    return "".join("data: " + json.dumps(f) + "\n\n" for f in frames)


def _tok(text):
    return {"type": "token", "text": text}


def _phase(stage, pid, state, at_ms):
    return {"type": "phase", "turn_id": "t1", "id": pid, "stage": stage, "label": pid, "state": state, "at_ms": at_ms}


def _case(**kw):
    base = {"id": 99, "text": "q", "route": "chat", "acquire": "none", "mentions": [], "not_mentions": [],
            "canvas": "", "fixture": ""}
    base.update(kw)
    return base


def test_the_approved_cases_load():
    cases = CC.load_cases(json.loads(CASES.read_text(encoding="utf-8")))
    assert [c["id"] for c in cases] == list(range(1, 31))
    assert {c["id"] for c in cases if c["fixture"]} == {7, 8, 9, 10}
    assert {c["id"] for c in cases if c["route"] == "loop"} == {27, 28, 29}
    assert next(c for c in cases if c["id"] == 15)["mentions"] == ["BTC|bitcoin"]


@pytest.mark.parametrize("bad", [
    {"cases": []},
    {"cases": [{"id": 1, "text": "a", "route": "chat"}, {"id": 1, "text": "b", "route": "chat"}]},
    {"cases": [{"id": 1, "text": "a", "route": "walk"}]},
    {"cases": [{"id": 1, "text": "a", "route": "chat", "acquire": "telepathy"}]},
    {"cases": [{"id": 1, "text": "a", "route": "chat", "canvas": "weather"}]},
    {"cases": [{"id": 1, "text": " ", "route": "chat"}]},
])
def test_a_bad_corpus_is_refused(bad):
    with pytest.raises(ValueError):
        CC.load_cases(bad)


def test_select_takes_ids_and_ranges():
    cases = [_case(id=i) for i in range(1, 31)]
    assert [c["id"] for c in CC.select(cases, "1-3,27")] == [1, 2, 3, 27]
    assert len(CC.select(cases, "")) == 30


def test_effects_are_stubbed_and_reads_run():
    for name in ("cal.reminder.add", "cal.event.create", "dag.agent_loop_v7", "workshop.agent_loop.start",
                 "memory.record_turn", "evolve.pipeline.begin", "sys.dev.restart", ""):
        assert CC.effect(name), name
    for name in ("markets.quotes", "sysmon.status", "memory.search", "intel.summary", "cal.events.list"):
        assert not CC.effect(name), name
    assert CC.stub_result("cal.reminder.add")["would_call"] is True


def test_loop_requests_are_the_hand_off_posts_only():
    assert CC.is_loop_request("POST", "https://x:8999/workshop/agent_loop/stream")
    assert CC.is_loop_request("post", "https://x:8999/workshop/agent_loop/message?a=1")
    assert not CC.is_loop_request("GET", "https://x:8999/workshop/agent_loop/stream")
    assert not CC.is_loop_request("POST", "https://x:8999/workshop/agent_loop/cancel")


def test_session_and_canvas_ids_match_the_page():
    sid = CC.session_id("20261011T110000", 7)
    assert sid == "chat-corpus-20261011T110000-7" and CC.ours(sid)
    assert CC.canvas_id(sid) == "cv_session_" + sid
    assert CC.canvas_id("a b/c") == "cv_session_a_b_c"
    assert not CC.ours("chat-1760000000") and not CC.ours("chat-corpus-")


def test_parse_sse_reads_tokens_frames_and_errors():
    s = CC.parse_sse(_sse(_phase("acquire", "web", "start", 5), _tok("Can"), _tok("berra"),
                          {"type": "error", "error": "boom"}) + "data: not json\n\n: comment\n")
    assert s["reply"] == "Canberra" and len(s["frames"]) == 1 and s["error"] == "boom"
    assert s["types"] == {"phase": 1, "token": 2, "error": 1}


def test_acquired_is_the_turn_records_rule():
    frames = [_phase("acquire", "web", "skip", 3), _phase("acquire", "memory", "done", 9),
              _phase("acquire", "notes", "done", 2)]
    assert CC.acquired(frames) == ["memory"]


def test_canvas_domains_from_item_keys_and_content():
    doc = {"blocks": [{"key": "enrich:markets.quotes:ab12cd34ef"}, {"key": "enrich:sysmon.status"},
                      {"key": "pre:panel:calendar"}, {"key": "widget:b1", "content": {"cap": "intel.summary"}},
                      {"key": "markdown:b2", "content": {"text": "hi"}}, "junk"]}
    assert CC.canvas_domains(doc) == ["calendar", "estate", "intel", "markets"]
    assert CC.canvas_domains(None) == []


def test_a_plain_turn_that_holds_passes():
    ev = {"stream": _sse(_phase("model", "llm", "first", 900), _tok("The capital is Canberra.")),
          "elapsed_ms": 2000, "canvas": {"blocks": []}}
    r = CC.evaluate(_case(mentions=["Canberra"], not_mentions=["Sydney"]), ev)
    assert r["status"] == "pass" and r["route"] == "chat" and r["record"]["ttft_ms"] == 900
    assert [c["check"] for c in r["checks"]] == ["route", "acquire", "mentions", "not_mentions", "ttft_ms"]


def test_each_check_can_fail():
    ev = {"stream": _sse(_phase("acquire", "web", "done", 400), _phase("model", "llm", "first", 3000),
                         _tok("Sydney, probably.")),
          "canvas": {"blocks": [{"key": "enrich:sysmon.status"}]}}
    r = CC.evaluate(_case(mentions=["Canberra"], not_mentions=["Sydney"], canvas="markets"), ev)
    assert r["status"] == "fail"
    bad = {c["check"] for c in r["checks"] if not c["ok"]}
    assert bad == {"acquire", "mentions", "not_mentions", "canvas"}


def test_alternatives_count_as_one_term():
    ev = {"stream": _sse(_tok("Bitcoin is at 60k"))}
    assert CC.evaluate(_case(mentions=["BTC|bitcoin"]), ev)["status"] == "pass"


def test_ttft_is_advisory():
    ev = {"stream": _sse(_phase("model", "llm", "first", 9000), _tok("hello"))}
    r = CC.evaluate(_case(), ev)
    t = next(c for c in r["checks"] if c["check"] == "ttft_ms")
    assert not t["ok"] and t["advisory"] and r["status"] == "pass"


def test_triggered_turns_get_the_wider_budget():
    ev = {"stream": _sse(_phase("acquire", "web", "done", 100), _phase("model", "llm", "first", 9000), _tok("x"))}
    r = CC.evaluate(_case(acquire="web"), ev)
    t = next(c for c in r["checks"] if c["check"] == "ttft_ms")
    assert t["want"] == CC.TTFT_BUDGET_MS["triggered"] and t["ok"]


def test_a_loop_hand_off_is_the_loop_route():
    by_request = CC.evaluate(_case(route="loop"), {"stream": _sse(_tok("On it.")), "loop": [{"url": "/x"}]})
    by_marker = CC.evaluate(_case(route="loop"), {"stream": _sse(_tok("[[loop: build a stopwatch]]"))})
    assert by_request["status"] == by_marker["status"] == "pass"
    assert all(c["check"] != "ttft_ms" for c in by_request["checks"])
    stayed = CC.evaluate(_case(route="loop"), {"stream": _sse(_tok("Here is a stopwatch"))})
    assert stayed["status"] == "fail"


def test_fixture_cases_are_skipped_and_errors_are_errors():
    assert CC.evaluate(_case(fixture="seed me"), {})["status"] == "skipped"
    assert CC.evaluate(_case(), {"stream": ""})["reason"] == "no reply"
    assert CC.evaluate(_case(), {"stream": _sse(_tok("x")), "error": "no end of turn in 180 s"})["status"] == "error"


def test_would_calls_are_recorded():
    ev = {"stream": _sse(_tok("Booked for Friday")),
          "calls": [{"name": "cal.reminder.add", "would_call": True}, {"name": "cal.events.list", "would_call": False}]}
    r = CC.evaluate(_case(mentions=["Friday"]), ev)
    assert r["would_call"] == ["cal.reminder.add"] and r["called"] == ["cal.events.list"]


def test_summary_and_markdown():
    rs = [CC.evaluate(_case(id=1), {"stream": _sse(_phase("model", "llm", "first", 800), _tok("hi"))}),
          CC.evaluate(_case(id=2, mentions=["zzz"]), {"stream": _sse(_phase("model", "llm", "first", 1200), _tok("hi"))}),
          CC.evaluate(_case(id=3, fixture="f"), {}),
          CC.evaluate(_case(id=4), {"stream": ""})]
    s = CC.summarise(rs)
    assert s["status"] == {"pass": 1, "fail": 1, "skipped": 1, "error": 1} and s["pass_rate"] == 0.5
    assert s["failed"] == [{"id": 2, "checks": ["mentions"]}] and s["ttft_ms"]["plain"]["n"] == 2
    assert s["ttft_ms"]["plain"]["p50"] == 800 and s["ttft_ms"]["plain"]["p90"] == 1200
    md = CC.render_md({"run_id": "r1", "target": "t"}, s, rs)
    assert "pass 1" in md and "| 2 | fail |" in md and "fixture not seeded" in md


def test_percentile():
    assert CC.percentile([], 0.5) is None
    assert CC.percentile([5, 1, 3], 0.5) == 3 and CC.percentile([5, 1, 3], 0.9) == 5


def test_the_runner_and_capability_parse_and_the_runner_matches_turns():
    for p in (ROOT / "tools" / "chat-corpus" / "run_chat_corpus.py", ROOT / "vera" / "minds" / "chat_corpus.py"):
        ast.parse(p.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("run_chat_corpus", ROOT / "tools" / "chat-corpus" / "run_chat_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved               # the runner puts the repo root first; leave the process as it was
    assert mod._sent({"body": json.dumps({"message": "What is 17% of 2,340?\n\n(hint)"})}, "What is 17% of 2,340?")
    assert not mod._sent({"body": json.dumps({"message": "name this chat"})}, "What is 17% of 2,340?")
    assert not mod._sent({"body": "not json"}, "x")
    # 11 Oct: every turn's body ran past the wrapper's 4000-character copy, so none was recognised
    long_body = json.dumps({"message": "hello, what can you help me with?", "history": ["x" * 5000]})
    assert mod._sent({"body": long_body[:4000], "message": "hello, what can you help me with?"},
                     "hello, what can you help me with?")
    assert not mod._sent({"body": long_body[:4000]}, "hello, what can you help me with?")      # the old failure
    assert "JSON.parse(raw).message" in mod.INIT_JS and "message: message.slice(0, 2000)" in mod.INIT_JS
    assert "/agents/chat/stream" in mod.INIT_JS and "vera_auto_loop" in mod.INIT_JS


def test_the_capability_module_is_loaded():
    src = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")
    assert 'os.path.join(_here, "minds/chat_corpus.py")' in src


# ── info-seeking cases (cases_info_v1.json, plan SK-4.1): tools and citations ──

INFO_CASES = ROOT / "tools" / "chat-corpus" / "cases_info_v1.json"


def test_the_info_cases_load_and_do_not_collide_with_v0():
    info = CC.load_cases(json.loads(INFO_CASES.read_text(encoding="utf-8")))
    v0 = CC.load_cases(json.loads(CASES.read_text(encoding="utf-8")))
    assert len(info) == 70 and not ({c["id"] for c in info} & {c["id"] for c in v0})
    kinds = {c["kind"] for c in info}
    assert kinds == {"latest", "fact", "overview", "memory", "nosearch"}
    assert all(c["tools_any"] and c["cites"] for c in info if c["kind"] in ("latest", "fact", "overview"))
    assert all(c["tools_none"] == ["*"] and c["acquire"] == "none" for c in info if c["kind"] == "nosearch")


def _info_ev(reply, calls):
    return {"stream": _sse(_phase("model", "llm", "first", 900), _tok(reply)), "elapsed_ms": 9000,
            "canvas": {"blocks": []}, "calls": calls}


def test_a_lookup_turn_that_searched_and_cited_passes():
    case = _case(acquire="any", tools_any=["info.seek", "web.research"], cites=True, mentions=["Bank Rate|base rate"])
    r = CC.evaluate(case, _info_ev("Bank Rate is 3.75% [1] (2026-09-17).", [{"name": "info.seek"}]))
    assert r["status"] == "pass", r["checks"]
    assert [c["check"] for c in r["checks"]] == ["route", "tools_any", "cites", "mentions", "ttft_ms"]


def test_answering_from_memory_without_a_tool_or_a_citation_fails():
    case = _case(acquire="any", tools_any=["info.seek"], cites=True)
    r = CC.evaluate(case, _info_ev("The base rate is 4%.", []))
    bad = {c["check"] for c in r["checks"] if not c["ok"]}
    assert r["status"] == "fail" and bad == {"tools_any", "cites"}


def test_tools_none_for_small_talk_and_web_none_for_memory():
    assert CC.evaluate(_case(tools_none=["*"]), _info_ev("Hi!", []))["status"] == "pass"
    assert CC.evaluate(_case(tools_none=["*"]), _info_ev("Hi!", [{"name": "memory.seek"}]))["status"] == "fail"
    mem = _case(acquire="any", tools_none=["web.search", "info.seek"])
    assert CC.evaluate(mem, _info_ev("We decided X.", [{"name": "memory.seek"}]))["status"] == "pass"
    assert CC.evaluate(mem, _info_ev("We decided X.", [{"name": "info.seek"}]))["status"] == "fail"


def test_a_stubbed_call_still_counts_as_the_tool_chosen():
    case = _case(acquire="any", tools_any=["research.run"])
    assert CC.evaluate(case, _info_ev("Started.", [{"name": "research.run", "would_call": True}]))["status"] == "pass"


def test_a_seeded_fixture_case_runs_and_records_its_minds():
    case = _case(fixture="decided to move the cache from Redis to Valkey", acquire="memory", mentions=["Valkey"],
                 minds={"context.needs_memory": "rules"})
    ev = {"stream": _sse(_phase("acquire", "memory", "done", 300), _phase("model", "llm", "first", 900),
                         _tok("We decided on Valkey.")), "fixture_seeded": True}
    r = CC.evaluate(case, ev)
    assert r["status"] == "pass" and r["fixture"] == "seeded" and r["minds"] == {"context.needs_memory": "rules"}
    unseeded = CC.evaluate(case, {"fixture_error": "seed failed: boom"})
    assert unseeded["status"] == "skipped" and unseeded["reason"] == "seed failed: boom"


def test_minds_are_carried_and_checked():
    doc = {"cases": [{"id": 1, "text": "a", "route": "chat", "minds": {"context.needs_memory": "rules"}}]}
    assert CC.load_cases(doc)[0]["minds"] == {"context.needs_memory": "rules"}
    with pytest.raises(ValueError):
        CC.load_cases({"cases": [{"id": 1, "text": "a", "route": "chat", "minds": ["rules"]}]})


def test_fixture_session_and_turn():
    fsid = CC.fixture_session_id("20261011T140000", 7)
    assert CC.ours(fsid) and fsid.endswith("-7-fixture")
    t = CC.fixture_turn("  decided   to use Valkey ")
    assert "decided to use Valkey" in t["human_text"] and "decided to use Valkey" in t["ai_text"]


def test_the_runner_seeds_tears_down_and_sends_the_minds():
    src = (ROOT / "tools" / "chat-corpus" / "run_chat_corpus.py").read_text(encoding="utf-8")
    assert 'page.route("**/agents/chat/stream", on_chat)' in src and 'body["minds"]' in src
    assert '"memory.record_turn"' in src and '"memory.forget"' in src and "finally:" in src
    assert "if not CC.ours(fsid):" in src
    cases = CC.load_cases(json.loads(CASES.read_text(encoding="utf-8")))
    assert all(c["minds"] == {"context.needs_memory": "rules"} for c in cases if c["fixture"])
