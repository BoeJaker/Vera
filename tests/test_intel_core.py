"""Intelligence: the pure rules (topics, signals, novelty, timeline, reports,
actions, channels) and the store - no app, no network."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.intel import intel_core as core  # noqa: E402
from vera.intel.intel_store import IntelStore  # noqa: E402


def _topic(**kw):
    return core.normalise_topic({"name": "Agentic AI in property valuation", "brief": "AI agents doing valuation work",
                                 "questions": ["Which UK firms run pilots?"], "entities": ["RICS"], **kw})


def test_topic_defaults_and_update_keeps_what_was_not_mentioned():
    t = _topic(depth="deep")
    assert t["id"] == "agentic-ai-in-property-valuation"
    assert t["depth"] == "deep" and t["cadence"] == "nightly" and t["status"] == "active"
    t2 = core.normalise_topic({"name": t["name"], "status": "paused"}, t)
    assert t2["status"] == "paused" and t2["questions"] == t["questions"] and t2["created"] == t["created"]
    with pytest.raises(ValueError):
        core.normalise_topic({})
    assert core.normalise_topic({"name": "x", "depth": "bogus"})["depth"] == "standard"


def test_queries_rotate_so_every_angle_gets_a_turn():
    t = _topic(queries=["a", "b", "c"], sites=["rics.org"])
    allq = core.topic_queries(t)
    assert allq[:3] == ["a", "b", "c"] and any(q.startswith("site:rics.org") for q in allq)
    first, cur = core.rotate_queries(t, 2)
    second, _ = core.rotate_queries(dict(t, query_cursor=cur), 2)
    assert first == ["a", "b"] and second == ["c", allq[3]]


def test_due_follows_cadence_and_status():
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    t = _topic()
    assert core.is_due(t, now)
    assert not core.is_due(dict(t, last_sweep="2026-10-04T08:00:00Z"), now)
    assert core.is_due(dict(t, last_sweep="2026-10-03T08:00:00Z"), now)
    assert not core.is_due(dict(t, status="paused"), now)
    assert not core.is_due(dict(t, cadence="manual"), now)


def test_canonical_url_and_ids_ignore_tracking_and_spelling():
    a = core.canonical_url("https://www.Example.com/post/?utm_source=x&id=3#frag")
    assert a == "https://example.com/post?id=3"
    assert core.signal_id("t", "https://example.com/post?id=3&fbclid=9") == core.signal_id("t", "http://example.com/post/?id=3".replace("http:", "https:"))
    assert core.signal_id("t", "https://example.com/a") != core.signal_id("u", "https://example.com/a")


def test_repo_detection():
    r = core.repo_of("https://github.com/eugeneyan/open-llms/tree/main")
    assert r["clone"] == "https://github.com/eugeneyan/open-llms.git"
    assert core.repo_of("https://github.com/topics/llm") is None
    assert core.repo_of("https://example.com/a/b") is None
    assert core.signal_kind("https://arxiv.org/abs/2401.1") == "paper"


def test_junk_is_dropped_and_signals_normalised():
    assert core.normalise_signal("t", {"url": "https://x.io", "title": "Just a moment..."}) is None
    s = core.normalise_signal("t", {"url": "https://www.x.io/a?utm_medium=1", "title": " A  title ", "snippet": "s"}, query="q")
    assert s["title"] == "A title" and s["site"] == "x.io" and s["url"] == "https://x.io/a" and s["query"] == "q"


def test_novelty_and_score():
    t = _topic()
    s = core.normalise_signal("t", {"url": "https://a.io", "title": "RICS publishes AI valuation standard for UK firms"})
    assert core.novelty(s, ["RICS publishes AI valuation standard for UK firms!"]) < 0.1
    assert core.novelty(s, ["Weather in Leeds"]) > 0.6
    hi = core.heuristic_score(s, t, novelty_v=1.0)
    off = core.heuristic_score(core.normalise_signal("t", {"url": "https://b.io", "title": "Football scores tonight"}), t)
    assert hi > off
    assert core.heuristic_score(s, dict(t, exclude=["rics"])) == 0.0


def test_note_delta_ignores_cosmetic_edits():
    assert core.note_hash("a  b\n\n c") == core.note_hash("a b\nc")
    assert core.note_delta("The price is 300k", "The price is 300K")["cosmetic"]
    d = core.note_delta("one line", "one line\nA brand new fact about planning")
    assert not d["cosmetic"] and d["added"] == ["A brand new fact about planning"]


def test_parse_json_loose_takes_the_outer_object():
    txt = 'Thinking Process: blah {"x":1}\nAnswer: {"summary": "s", "nested": {"a": "}"}, "relevance": 8} trailing'
    assert core.parse_json_loose(txt) == {"x": 1}
    assert core.parse_json_loose('```json\n{"summary": "s", "n": {"a": 1}}\n```') == {"summary": "s", "n": {"a": 1}}
    assert core.parse_json_loose("no json") is None


def test_enrichment_and_digest():
    s = core.normalise_signal("t", {"url": "https://a.io", "title": "T"})
    page = {"markdown": "Short.\n\n" + ("A real paragraph with enough words to count as content. " * 3) + "\n\n![img](x.png)",
            "image": "https://a.io/i.png", "minutes": 3, "byline": "Jo"}
    e = core.apply_enrichment(s, {"summary": "S", "relevance": "11", "tags": ["AI", "AI"], "code": "x=1"}, page=page)
    assert e["score"] == 10.0 and e["tags"] == ["ai"] and e["enriched"] and e["image"].endswith("i.png")
    assert "img" not in e["digest"]["text"] and e["digest"]["text"].startswith("A real paragraph")


def test_mermaid_is_sanitised():
    assert core.sanitize_mermaid("```mermaid\nflowchart LR\n A-->B\n click A call evil()\n```") == "flowchart LR\n A-->B"
    assert core.sanitize_mermaid("sequenceDiagram\n A->>B: x") == ""
    assert core.sanitize_mermaid('flowchart LR\n A["<img src=x onerror=1>"]-->B') == "flowchart LR"


def test_snapshot_and_as_of():
    t = _topic()
    snap = core.make_snapshot(t, {"headline": "H", "state": "S", "changed": ["c"], "confidence": "HIGH",
                                  "answered": [{"question": "Q", "answer": "A"}], "diagram": "graph TD\n a-->b"}, [],
                              ts="2026-10-01T06:00:00Z")
    assert snap["confidence"] == "high" and snap["diagram"].startswith("graph TD")
    older = dict(snap, id="o", ts="2026-09-20T06:00:00Z", headline="old")
    assert core.as_of([snap, older], "2026-09-25T00:00:00Z")["headline"] == "old"
    assert core.as_of([snap, older], "2026-10-02")["headline"] == "H"
    assert core.as_of([snap], "2026-01-01") is None


def test_timeline_buckets_and_events():
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    sigs = [{"id": "a", "seen": "2026-10-04T01:00:00Z", "score": 8, "title": "A"},
            {"id": "b", "seen": "2026-10-04T02:00:00Z", "score": 3, "title": "B"},
            {"id": "c", "seen": "2026-09-01T02:00:00Z", "score": 3, "title": "old"}]
    snaps = [{"id": "s", "ts": "2026-10-03T06:00:00Z", "headline": "H"}]
    tl = core.timeline(sigs, snaps, [{"id": "r", "ts": "2026-10-04T07:00:00Z", "title": "R"}], days=7, now=now)
    assert len(tl["days"]) == 7 and tl["to"] == "2026-10-04"
    last = tl["days"][-1]
    assert last["signals"] == 2 and last["top"] == "A" and last["reports"] == 1
    assert tl["days"][-2]["snapshots"] == 1
    assert [e["id"] for e in tl["events"]][:2] == ["r", "b"]


def test_actions_are_one_time_and_built_from_the_record():
    a = core.make_action("repo", topic_id="t", target="https://github.com/o/r")
    assert a and len(a["token"]) > 20 and core.action_usable(a) == (True, "")
    assert core.action_args(a) == {"url": "https://github.com/o/r", "topic_id": "t"}
    assert core.make_action("repo", target="https://example.com") is None
    assert core.make_action("shell", target="rm -rf /") is None
    assert core.action_usable(dict(a, status="done"))[0] is False
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert core.action_usable(dict(a, expires=past)) == (False, "expired")
    r = core.make_action("research", query="what next?")
    assert core.action_args(r)["query"] == "what next?"


def _doc():
    t = _topic()
    s = core.normalise_signal(t["id"], {"url": "https://github.com/o/r", "title": "Repo <b>x</b>", "image": "https://i/x.png"})
    s.update(score=8, summary="sum", code="def f():\n    return 1  # one", code_lang="python",
             digest={"text": "Para one.\n\nPara two.", "minutes": 2}, entities=["RICS", "Acme"])
    snap = core.make_snapshot(t, {"headline": "Big shift", "state": "Now", "changed": ["x"], "new_questions": ["Q2?"]}, [s])
    acts = core.actions_for(t, [s], snap)
    return core.build_report([t], {t["id"]: {"signals": [s], "snapshot": snap, "actions": acts}}), acts


def test_report_model_and_renderings():
    doc, acts = _doc()
    kinds = [p["kind"] for p in doc["sections"][1]["parts"]]
    assert doc["sections"][0]["kind"] == "headline"
    for k in ("assessment", "images", "signals", "digests", "diagram", "code", "actions"):
        assert k in kinds, k
    assert {a["kind"] for a in acts} >= {"repo", "research", "discover"}
    html = core.render_html(doc)
    assert "Repo &lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert "<vera-mermaid" in html and 'data-token="' in html and "<details" in html
    assert '<span class="k">def</span>' in html and '<span class="c"># one</span>' in html
    md = core.render_markdown(doc, report_url="https://h/r")
    assert "## Top line" in md and "```python" in md and "[Full report](https://h/r)" in md
    tg = core.render_telegram(doc, report_url="https://h/r", action_url="https://h/intel/act")
    assert tg["photo"] == "https://i/x.png" and tg["buttons"][0][0]["url"] == "https://h/r"
    assert any("token=" in b["url"] for row in tg["buttons"][1:] for b in row)
    assert "<b>x</b>" not in tg["text"]


def test_channel_args():
    doc, _ = _doc()
    assert core.channel_args("chat", doc, report_url="u") is None            # needs a session
    assert core.channel_args("chat", doc, report_url="u", target="s1")["session_id"] == "s1"
    tg = core.channel_args("telegram", doc, report_url="u", action_url="a")
    assert tg["parse_mode"] == "HTML" and isinstance(json.loads(tg["buttons"]), list)
    cv = core.channel_args("canvas", doc, report_url="u", target="cv_1")
    assert cv["id"] == "cv_1" and cv["kind"] == "panel" and cv["content"]["src"] == "u"
    assert core.channel_args("nope", doc, report_url="u") is None


def test_store_roundtrip(tmp_path):
    st = IntelStore(tmp_path / "i.db")
    t = st.put_topic(_topic())
    assert st.get_topic(t["id"])["name"] == t["name"] and len(st.topics("active")) == 1
    s = core.normalise_signal(t["id"], {"url": "https://a.io", "title": "A"})
    s["score"] = 7
    st.put_signal(s)
    assert st.has_signal(s["id"]) and st.count_signals(topic_id=t["id"]) == 1
    assert st.signals(t["id"], unreported=True) and not st.signals(t["id"], enriched=True)
    st.mark_reported([s["id"]])
    assert not st.signals(t["id"], unreported=True)
    doc, acts = _doc()
    st.put_report(doc, "<html>")
    assert st.get_report(doc["id"], with_html=True)["_html"] == "<html>"
    assert st.reports(topic_id=t["id"])[0]["id"] == doc["id"]
    st.put_action(acts[0], report_id=doc["id"])
    assert st.claim_action(acts[0]["token"]) and not st.claim_action(acts[0]["token"])
    st.finish_action(acts[0]["token"], "done", {"ok": True})
    assert st.get_action(acts[0]["token"])["status"] == "done"
    st.set_kv("config", {"a": 1})
    assert st.get_kv("config") == {"a": 1}
    assert st.delete_topic(t["id"], purge=True) and st.count_signals() == 0


def test_only_web_links_survive():
    s = core.normalise_signal("t", {"url": "javascript:alert(1)", "title": "Click me"})
    assert s["url"] == "" and s["site"] == ""
    assert core.normalise_signal("t", {"url": "http://[bad", "title": "Odd url"})["site"] == ""


def test_highlighter_never_rescans_its_own_markup():
    h = core.highlight("class A: x = 'it''s' # note", "python")
    assert h.count("<span") == h.count("</span>")
    assert 'class="k">class</span>' in h and 'class="c"># note</span>' in h
    assert "&lt;span" not in h


def test_store_retries_a_transient_open_failure(tmp_path, monkeypatch):
    import sqlite3 as _sq
    st = IntelStore(tmp_path / "i.db")
    real = _sq.connect
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("unable to open database file")
        return real(*a, **k)
    monkeypatch.setattr(_sq, "connect", flaky)
    assert st.topics() == [] and calls["n"] == 2


def test_live_run_quality_fixes():
    from vera.intel import intel_ops_core as ops
    s = core.normalise_signal("t", {"url": "https://a.io", "title": "AI in PropTech &amp; Real Estate", "snippet": "x &lt; y"})
    assert s["title"] == "AI in PropTech & Real Estate" and s["snippet"] == "x < y"
    ents = ops.ner_entities({"entities": [{"word": w, "score": 0.9} for w in
                                          ["Mozilla", "AppleWebKit", "AI", "JLL", "$550 billion", "61%", "Savills"]]})
    assert ents == ["JLL", "Savills"]
    t = core.normalise_topic({"name": "x"})
    p = core.enrich_prompt(t, {"title": "t", "url": "u"}, "w" * 20000, triaged=True)
    assert p.count("w") <= core.TRIAGED_TEXT_CHARS + 50


def test_place_pins_group_size_and_light():
    from vera.intel import intel_geo_core as geo
    sigs = [{"title": "A", "score": 3, "seen": "2026-10-04T00:00:00Z", "url": "https://a", "places": [{"name": "Leeds", "lat": 53.797, "lon": -1.544}]},
            {"title": "B", "score": 8, "seen": "2026-09-01T00:00:00Z", "url": "https://b", "places": [{"name": "Leeds", "lat": 53.7974, "lon": -1.5438},
                                                                                                      {"name": "Paris", "lat": 48.85, "lon": 2.35}]},
            {"title": "C", "places": [{"name": "Nowhere", "lat": "x", "lon": 1}]}]
    p = geo.pins(sigs, recent_after=1790000000)
    assert [x["name"] for x in p] == ["Leeds", "Paris"] and p[0]["size"] == 2 and p[0]["detail"] == "B"
    assert p[0]["lit"] and not p[1]["lit"] and p[0]["meta"].startswith("2 sources")


def test_a_first_assessment_has_nothing_changed():
    p = {"headline": "h", "state": "s", "changed": ["Previous assessment was null; this is the baseline"]}
    assert core.make_snapshot({"id": "t"}, p, [], first=True)["changed"] == []
    assert core.make_snapshot({"id": "t"}, p, [])["changed"]


def test_prompts_carry_today_and_the_source_date():
    sig = {"title": "T", "url": "https://x", "published": "2024-05-02T00:00:00Z", "summary": "s", "score": 5, "site": "x"}
    for triaged in (False, True):
        p = core.enrich_prompt({"name": "n"}, sig, "text", triaged=triaged)
        assert "TODAY: " in p and "SOURCE PUBLISHED: 2024-05-02" in p and "background, not news" in p
    sp = core.snapshot_prompt({"name": "n"}, None, [sig])
    assert "published 2024-05-02" in sp and "Today is " in sp and "never the current picture" in sp
