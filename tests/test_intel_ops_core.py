"""Intelligence: the pure operational rules - NLP-tier scoring, competitor watch,
the brief, drop-folder planning, the advisor's drafts, the command, sessions and
material-change alerts. No app, no network."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.intel import intel_ops_core as ops  # noqa: E402


def test_scores_lead_with_the_reranker_and_fall_back_without_it():
    assert ops.squash(0.7) == 0.7 and 0.99 < ops.squash(8) <= 1 and ops.squash(-8) < 0.01
    assert ops.squash("x") == 0.0
    assert ops.blend_score(relevance=None, heuristic=6.3, novelty=1) == 6.3
    hi = ops.blend_score(relevance=0.95, heuristic=3, novelty=0.9)
    lo = ops.blend_score(relevance=0.05, heuristic=9, novelty=0.9)
    assert hi > lo and 0 <= lo <= 10 and hi <= 10


def test_vectors_find_the_same_story():
    a, b, c = [1, 0, 0], [0.98, 0.2, 0], [0, 1, 0]
    assert ops.cosine(a, a) == 1.0 and ops.cosine(a, c) == 0.0 and ops.cosine([], a) == 0.0
    sid, cos = ops.nearest(a, [("x", c), ("y", b)])
    assert sid == "y" and cos > 0.95


def test_ner_qa_and_stance_shapes():
    ents = ops.ner_entities({"entities": [{"word": "Savills", "score": 0.9}, {"word": "Savills", "score": 0.5},
                                          {"word": "the", "score": 0.99}, {"word": "RICS", "score": 0.3}, "junk"]})
    assert ents == ["Savills"]
    assert ops.qa_answer("Q?", {"answer": "in 2027", "score": 0.6})["answer"] == "in 2027"
    assert ops.qa_answer("Q?", {"answer": "x", "score": 0.1}) is None
    assert ops.stance({"top": "LABEL_2"}) == "positive" and ops.stance({}) == ""


def test_competitors_are_whole_words():
    assert ops.competitor_hits("Savills launches AI", [], ["Savills", "JLL"]) == ["Savills"]
    assert ops.competitor_hits("JLLX is not JLL's rival", [], ["JLL"]) == ["JLL"]
    assert ops.competitor_hits("nothing here", ["Knight Frank"], ["Knight Frank"]) == ["Knight Frank"]
    assert ops.competitor_hits("CBREs", [], ["CBRE"]) == []


def test_brief_prompt_and_apply():
    p = ops.brief_prompt("2026-10-04", [{"topic": "AI", "headline": "H", "changed": ["c"],
                                         "signals": [{"score": 8, "title": "T", "summary": "S", "url": "https://u"}]}],
                         [{"firm": "Savills", "title": "X", "url": "https://v"}])
    assert "top_line" in p and "https://u" in p and "Savills" in p
    d = ops.apply_brief({"id": "r"}, {"top_line": ["a", "b", "c", "d"], "follow_ups": ["q"], "competitor_watch": "w"})
    assert d["top_line"] == ["a", "b", "c"] and d["follow_ups"] == ["q"] and d["competitor_note"] == "w"
    assert ops.apply_brief({"id": "r"}, None) == {"id": "r"}


def test_drop_folder_planning_and_note_diffs():
    assert ops.wanted_file("notes", "a.md", 10) and not ops.wanted_file("notes", "a.csv", 10)
    assert ops.wanted_file("inbox", "a.csv", 10) and not ops.wanted_file("inbox", ".hidden.md", 1)
    assert not ops.wanted_file("notes", "a.md", 10 ** 7)
    new = ops.plan_file("notes", "/n/a.md", "Line one about AI\n\nLine two", None, False)
    assert new["kind"] == "note_new" and new["llm"]
    known = {"hash": new["hash"], "norm": new["norm"], "summary": "S"}
    assert ops.plan_file("notes", "/n/a.md", "Line one about AI\n  Line two  ", known, False) is None   # whitespace only
    typo = ops.plan_file("notes", "/n/a.md", "Line one about A.I.\nLine two", known, False)
    assert typo["kind"] == "note_trivial" and not typo["llm"]
    real = ops.plan_file("notes", "/n/a.md", "Line one about AI\nLine two\nBrand new decision: buy the GPU", known, False)
    assert real["kind"] == "note_changed" and real["added"] == ["Brand new decision: buy the GPU"]
    assert ops.plan_file("print", "/p/x.txt", "hello", None, True) is None
    assert ops.plan_file("print", "/p/x.txt", "hello", None, False)["llm"] is False


def test_apply_note_keeps_history_and_terms():
    item = ops.plan_file("notes", "/n/a.md", "Plan: evaluate GLiNER for leases", None, False)
    rec, terms, line = ops.apply_note(item, {"title": "Leases", "summary": "S", "search_terms": ["gliner leases"],
                                             "action_items": [{"task": "try it"}]}, "2026-10-04T00:00:00Z")
    assert rec["title"] == "Leases" and terms == ["gliner leases"] and rec["action_items"] == ["try it"] and "NEW" in line
    ch = ops.plan_file("notes", "/n/a.md", "Plan: evaluate GLiNER for leases\nDecided: use it on rent reviews", rec, False)
    rec2, terms2, line2 = ops.apply_note(ch, {"summary": "S2", "delta_summary": "decided", "new_search_terms": ["rent review ai"],
                                              "resolved_action_items": ["try it"]}, "2026-10-05T00:00:00Z")
    assert rec2["action_items"] == [] and "rent review ai" in rec2["search_terms"] and rec2["revisions"] == 2
    rec3, terms3, line3 = ops.apply_note(ch, None, "2026-10-06T00:00:00Z")
    assert rec3["hash"] == rec["hash"] and rec3["read_failures"] == 1 and terms3 == [] and line3 == ""   # a failed read is retried


def test_advisor_drafts_are_inactive_manual_and_quote_safely():
    d = ops.compile_draft({"title": "Watch GPUs", "rationale": "r", "steps": [
        {"name": "check", "cap": "ollama.gate.status", "args": {"x": "it's \"quoted\""}, "note": "n"}, "junk"]},
        "https://h/mcp/call", "2026-10-04")
    assert d["name"] == "draft-watch-gpus"
    assert d["nodes"][0]["type"] == "n8n-nodes-base.manualTrigger"
    code = d["nodes"][1]["parameters"]["jsCode"]
    assert json.dumps({"x": "it's \"quoted\""}) in code
    assert d["connections"]["Run by hand"]["main"][0][0]["node"].startswith("Prep 1")
    p = ops.advisor_prompt([{"name": "a", "active": True}], ["svc"], [{"score": 8, "title": "MCP tools", "summary": "an agent tool",
                                                                      "tags": []}], ["old idea"])
    assert "old idea" in p and "MCP tools" in p and "suggestions" in p


def test_command_parsing():
    assert ops.parse_command("/add AI :: llm agents") == ("add", "AI :: llm agents")
    assert ops.parse_command("STATUS") == ("status", "")
    assert ops.parse_command("rm -rf")[0] == "help"


def test_sessions_wrap_midnight_and_respect_days():
    s = [{"name": "night owl", "days": [4], "start": "22:00", "end": "02:00", "budget": 5}]   # Friday night
    fri_late = datetime(2026, 10, 9, 23, 30)
    sat_early = datetime(2026, 10, 10, 1, 0)
    sat_late = datetime(2026, 10, 10, 23, 0)
    a = ops.active_session(s, fri_late)
    assert a and a["ends"] == datetime(2026, 10, 10, 2, 0)
    assert ops.active_session(s, sat_early)["ends"] == datetime(2026, 10, 10, 2, 0)
    assert ops.active_session(s, sat_late) is None
    day = [{"name": "sat", "days": [5], "start": "09:00", "end": "13:00"}]
    assert ops.active_session(day, datetime(2026, 10, 10, 10, 0))["budget"] == 6
    assert ops.active_session(day, datetime(2026, 10, 10, 13, 0)) is None
    assert ops.normalise_session({"start": "25:00", "end": "02:00"}) is None
    assert ops.normalise_session({"start": "9:00", "end": "9:00"}) is None


def test_material_change():
    prev = {"headline": "Pilots are early and small", "confidence": "low", "changed": []}
    assert ops.material_change(None, {"headline": "x"}) == (False, "")
    same = {"headline": "Pilots are early and small.", "confidence": "low", "changed": ["a"]}
    assert ops.material_change(prev, same)[0] is False
    moved = {"headline": "Three firms now run production AI valuations", "confidence": "high", "changed": ["a", "b", "c"]}
    ok, why = ops.material_change(prev, moved)
    assert ok and "headline" in why and "confidence" in why
    assert ops.material_change(prev, same, competitor_hits_n=1)[0] is True
    assert "why now" in ops.alert_text("AI", moved, why, "https://h/intel/panel")


def test_cyrb53_matches_n8n():
    # values produced by the files-night JavaScript itself (node)
    assert ops.cyrb53("print me") == "1a6f417ab11736"
    assert ops.cyrb53("Plan: buy a GPU for the lab" + chr(10) + "line two") == "93a4bc4c83706"
    assert ops.cyrb53(chr(0xa3) + " h" + chr(0xe9) + "llo " + chr(0x2014) + " " + chr(0x1f680)) == "1a074ded7ec76a"
    assert ops.cyrb53("") == "bdcb81aee8d83"


def test_a_failed_note_read_is_retried():
    known = {"hash": "h0", "norm": "old line", "summary": "S", "path": "/n/a.md", "name": "a.md"}
    item = ops.plan_file("notes", "/n/a.md", "old line" + chr(10) + "A real new decision about the lab", known, False)
    rec, terms, line = ops.apply_note(item, None, "t1")
    assert rec["hash"] == "h0" and rec["read_failures"] == 1 and not line      # kept: read again next time
    rec2, _, _ = ops.apply_note(dict(item, prev=rec), None, "t2")
    rec3, _, _ = ops.apply_note(dict(item, prev=rec2), None, "t3")
    assert rec3["hash"] == item["hash"] and "read_failures" not in rec3       # third failure: accepted, not looped


def test_draft_notes_never_become_code():
    note = "ok" + chr(13) + chr(0x2028) + "alert(1)//"
    d = ops.compile_draft({"title": "x", "steps": [{"cap": "a.b", "note": note}]}, "https://h", "d")
    code = d["nodes"][1]["parameters"]["jsCode"]
    assert "const note=" in code and chr(13) not in code and chr(0x2028) not in code


def test_page_genre():
    lab = ops.GENRE_LABELS
    r = {"labels": [{"label": lab["marketing"], "score": 0.81}, {"label": lab["news"], "score": 0.12}]}
    assert ops.genre_of(r) == ("marketing", 0.81)
    assert ops.genre_of({"labels": [{"label": lab["news"], "score": 0.3}]}) == ("", 0.3)     # unsure: no genre
    assert ops.genre_of({}) == ("", 0.0)
    assert ops.genre_factor("marketing") == 0.5 and ops.genre_factor("marketing", vendors_ok=True) == 1.0
    assert ops.genre_factor("listing") == 0.35 and ops.genre_factor("news") == 1.0


def test_topic_labels_follow_the_kind_of_topic():
    prop = {"name": "AI in real estate", "brief": "valuation and planning in UK property"}
    assert ops.topic_kind(prop) == "property" and "planning authority" in ops.topic_labels(prop)
    assert ops.topic_kind({"name": "Fed rate path", "brief": "inflation and bond yields"}) == "finance"
    assert ops.topic_kind({"name": "Red Sea shipping", "brief": "vessel attacks and sanctions"}) == "osint"
    assert ops.topic_kind({"name": "Gardening"}) == "general"
    mine = {"name": "Gardening", "kind": "tech", "labels": ["seed company", "company"]}
    labs = ops.topic_labels(mine)
    assert labs[0] == "seed company" and labs.count("company") == 1 and "AI model" in labs and len(labs) <= 16


def test_entity_types_keep_the_best_label_per_entity():
    r = {"entities": [{"word": "Savills", "entity": "company", "score": 0.6},
                      {"word": "Savills", "entity": "property agency or consultancy", "score": 0.9},
                      {"word": "Leeds", "entity": "location", "score": 0.8},
                      {"word": "noise", "entity": "location", "score": 0.9}]}
    assert ops.entity_types(r, ["Savills", "Leeds"]) == {"Savills": "property agency or consultancy", "Leeds": "location"}


def test_company_names_match_their_listings():
    assert ops.org_key("Land Securities Group Plc") == "land securities"
    assert ops.name_match("Savills", "Savills plc") == 1.0
    assert ops.name_match("Land Securities", "Land Securities Group Plc") == 1.0
    assert ops.name_match("Acme", "ACM Research Inc") < 0.8 and ops.name_match("Modus", "Modus Therapeutics") < 0.95
    hits = [{"provider": "yahoo", "symbol": "XNVDA=F", "name": "Micro NVIDIA Corp Stock Futures", "asset_class": "future"},
            {"provider": "yahoo", "symbol": "NVDA", "name": "NVIDIA Corporation", "asset_class": "stock"}]
    assert ops.pick_listing("Nvidia", hits)["symbol"] == "NVDA"
    # what markets.lookup really returned on 4 Oct
    assert ops.pick_listing("Meta", [{"provider": "binance", "symbol": "META/USDT:USDT", "name": "META/USDT", "asset_class": "crypto"},
                                     {"provider": "yahoo", "symbol": "META", "name": "Meta Platforms, Inc.", "asset_class": "stock"}])["symbol"] == "META"
    assert ops.pick_listing("JLL", [{"provider": "yahoo", "symbol": "JLL", "name": "Jones Lang LaSalle Incorporated", "asset_class": "stock"}])
    assert ops.pick_listing("Google", [{"provider": "yahoo", "symbol": "DEPW", "name": "Google DeepMind AI Lab Ecosyste", "asset_class": "etf"},
                                       {"provider": "yahoo", "symbol": "GOOG", "name": "Alphabet Inc.", "asset_class": "stock"}])["symbol"] == "GOOG"
    assert ops.pick_listing("Modus", [{"provider": "yahoo", "symbol": "MODTX.ST", "name": "Modus Therapeutics Holding AB (publ)", "asset_class": "stock"}]) is None
    assert ops.pick_listing("Cushman & Wakefield", [{"provider": "yahoo", "symbol": "CWK", "name": "Cushman & Wakefield Limited", "asset_class": "stock"}])
    sigs = [{"entities": ["Savills", "UK"], "entity_types": {"Savills": "company", "UK": "location"}},
            {"entities": ["Savills plc"], "entity_types": {"Savills plc": "company"}},
            {"entities": ["CBRE"]}]
    assert ops.org_mentions(sigs) == [("Savills", 2)]
    assert ops.org_mentions(sigs, follow=["JLL"])[0] == ("JLL", 999)


def test_corpus_is_curated_private_and_stratified():
    sigs = [{"id": f"a{i}", "topic_id": "t", "title": f"A{i}", "site": "big.example", "score": i, "seen": f"2026-10-0{1 + i % 3}T00:00:00Z"}
            for i in range(8)]
    sigs += [{"id": "b1", "topic_id": "t", "title": "B", "site": "other.example", "score": 5, "seen": "2026-10-02T00:00:00Z",
              "published": "2026-09-30T00:00:00Z", "entities": ["Acme"], "vec": [0.1]},
             {"id": "n1", "topic_id": "from-your-notes", "title": "my plan", "site": "", "seen": "2026-10-02T00:00:00Z"},
             {"id": "f1", "topic_id": "t", "title": "file", "source": "files", "seen": "2026-10-02T00:00:00Z"}]
    rows, st = ops.corpus_rows(sigs, max_share=0.3, private_topics=["from-your-notes"])
    assert st["private_dropped"] == 2 and not any(r["id"] in ("n1", "f1") for r in rows)
    big = [r for r in rows if r["site"] == "big.example"]
    assert len(big) == 2 and {r["id"] for r in big} == {"a7", "a6"} and st["capped"] == {"big.example": 6}
    assert rows[0]["id"] == "b1" and "vec" not in rows[0] and rows == sorted(rows, key=lambda r: r["t"])
    seq = ops.sequences(rows, [{"id": "s2", "topic_id": "t", "ts": "2026-10-03"}, {"id": "s1", "topic_id": "t", "ts": "2026-10-01"}])
    assert seq["stories"]["t"][0] == "b1" and [x["id"] for x in seq["assessments"]["t"]] == ["s1", "s2"]


def test_corpus_order_is_stable_whatever_order_the_store_returns():
    sigs = [{"id": i, "topic_id": "t", "title": i, "site": f"{i}.example", "seen": "2026-10-02T00:00:00Z"} for i in "dcba"]
    a, _ = ops.corpus_rows(sigs, max_share=1.0)
    b, _ = ops.corpus_rows(list(reversed(sigs)), max_share=1.0)
    assert [r["id"] for r in a] == [r["id"] for r in b] == ["a", "b", "c", "d"]
    assert ops.sequences(a, [])["stories"]["t"] == ops.sequences(b, [])["stories"]["t"]


def test_night_note_only_when_the_night_did_not_finish():
    assert ops.night_note({"stats": {"topics": 12}, "pending": ["a"] * 10}).startswith("Overnight: 2 of 12 topics refreshed. The other 10")
    assert ops.night_note({"stats": {"topics": 12}, "pending": []}) == ""
    assert ops.night_note({}) == ""


def test_night_note_names_what_held_the_queue():
    # 12 Oct: censuses and probes filled the night; the note blamed "while Vera is in use"
    n = {"stats": {"topics": 12}, "pending": ["a"] * 12}
    assert "a census was running" in ops.night_note(n, "a census is running")
    assert "you were using Vera" in ops.night_note(n, "interactive use in the last 180s")
    assert "ten quiet minutes" in ops.night_note(n, "only 40s of quiet so far, need 600s (a census leaves...)")
    assert "models were busy" in ops.night_note(n, "the GPU gate was held 30s ago (interactive work is bursty)")
    assert "waits until Vera is quiet" in ops.night_note(n) and "in use" not in ops.night_note(n)
    assert ops.blocker_phrase("") == "" and ops.blocker_phrase("something new") == "other work was running"


def test_headline_hubs_are_indexes_not_stories():
    # the two "signals" at the top of the 5 Oct brief, and two real stories
    assert ops.index_hint("https://newsnow.co.uk/h/Business+&+Finance/UK+Housing+Market", "UK Housing Market News & House Price News - NewsNow")
    assert ops.index_hint("https://propertyprice.uk/news", "UK Property News, House Prices & Landlord Updates | PropertyPrice")
    assert ops.index_hint("https://www.rightmove.co.uk/news/", "Property News & Housing Market Updates | Rightmove")
    assert not ops.index_hint("https://www.bbc.co.uk/news/articles/c1234", "House prices rise 0.2% in August, Nationwide says")
    assert not ops.index_hint("https://www.ft.com/content/abc", "Property news: Landsec sells its stake")
    assert ops.genre_factor("index") == 0.4


def test_old_undated_text_is_marked_down():
    assert ops.newest_year("Zoopla May 2024 data; forecasts to 2030", 2026) == 2024
    assert ops.newest_year("phone 0207 123 4567, 12000 homes", 2026) is None
    assert ops.staleness_factor({"title": "Housing outlook 2023", "snippet": "as of 2023"}, 2026) == 0.7
    assert ops.staleness_factor({"title": "Prices rise in 2026"}, 2026) == 1.0
    assert ops.staleness_factor({"title": "2019 review", "published": "2026-10-01"}, 2026) == 1.0


def test_questions_written_as_objects_become_text():
    assert ops.question_text({"question": "Who is next?", "answer": "?"}) == "Who is next?"
    assert ops.question_text("{'question': 'What drives the drop?', 'answer': 'No explanation'}") == "What drives the drop?"
    assert ops.question_text("Plain question?") == "Plain question?"
    prof = ops.question_profile({"name": "T", "questions": ["{'question': 'Why now?', 'answer': 'x'}"]})
    assert "Why now?" in prof and "answer" not in prof
