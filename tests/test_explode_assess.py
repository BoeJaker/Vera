"""Assessment (EXPLODE.md §6, track F): scorers with evidence, a heuristic that says it is one, composites with
their weights stated, verdicts persisted into a record and ranked across records, and the scorers riding on both
explode capabilities."""
import asyncio
import json
import sqlite3

import pytest

from vera.research import assess_core as A
from vera.research import assess_capabilities as AC
from vera.research import code_explode_core as C
from vera.research import explode_capabilities as X

HUMAN = ("I didn't expect the bridge to be closed. We'd driven two hours — Meg asleep in the back, the dog complaining — "
         "and there it was: a yellow gate, a sign, nobody. So we turned round. Why do they never put the sign at the "
         "junction? Anyway, the long way took us past the old mill (still standing, just), and by the time we got to "
         "Lyme the light had gone. Fish and chips on the wall. Worth it, honestly; the kids thought so too.\n\n"
         "According to the council's notice (Dorset Council, 2024), the closure runs until March. See "
         "https://example.org/bridge-works for the schedule. Someone had written 'about time' on it in marker pen.")
MACHINE = ("In today's fast-paced world, it is important to note that effective communication is a cornerstone of success. "
           "Moreover, organisations must leverage robust strategies to navigate the landscape of modern challenges. "
           "Furthermore, a holistic approach enables teams to unlock the potential of cutting-edge solutions. "
           "Additionally, stakeholders should delve into the data to make informed decisions. "
           "Ultimately, this seamlessly aligns with the goals of the organisation. "
           "In conclusion, it is worth noting that these practices are a testament to sustained growth. "
           "Overall, the results are a game-changer for the industry as a whole.")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_prose_scorers_carry_evidence_and_the_stylometry_calls_itself_a_heuristic():
    r = A.readability(HUMAN)
    assert r and 0 <= r["score"] <= 1 and r["evidence"] and r["evidence"][0]["note"] == "the hardest sentence" and r["by"] == "Flesch reading ease"
    s = A.sources(HUMAN)
    assert s and s["detail"]["links"] == 1 and s["detail"]["citations"] >= 2 and any(e["note"] == "link" for e in s["evidence"]) and HUMAN[s["evidence"][0]["span"]["start"]:s["evidence"][0]["span"]["end"]].startswith("https://")
    assert A.sources(MACHINE)["score"] == 0.0
    h, m = A.ai_likelihood_stylometry(HUMAN), A.ai_likelihood_stylometry(MACHINE)
    assert h and m and m["score"] > h["score"] + 0.25, (h["detail"], m["detail"])
    assert m["confidence"] <= 0.4 and "heuristic" in m["by"] and any("stock phrase" in e["note"] for e in m["evidence"])
    assert MACHINE[m["evidence"][0]["span"]["start"]:m["evidence"][0]["span"]["end"]].lower() in [p for p in A._AI_PHRASES]
    st = A.structure("word " * 400)
    assert st and st["score"] < 1 and st["evidence"] and "words in one paragraph" in st["evidence"][0]["note"]
    assert A.readability("too short") is None


def test_trust_is_a_composite_with_its_weights_stated_and_never_surer_than_its_weakest_part():
    parts = [A.sources(HUMAN), A.ai_likelihood_stylometry(HUMAN), A.readability(HUMAN), A.structure(HUMAN)]
    t = A.trust(parts)
    assert t and t["key"] == "trust" and "sources 40%" in t["by"] and "not AI-generated" in t["detail"] and t["confidence"] == min(p["confidence"] for p in parts)
    assert A.trust([A.readability(HUMAN)]) is None, "one signal is not a composite"


def test_the_registry_runs_scorers_in_order_and_a_failure_is_its_own_receipt(monkeypatch):
    async def boom(ctx):
        raise RuntimeError("no node")
    monkeypatch.setitem(AC.SCORERS["lang"], "fn", boom)
    res = _run(AC.assess_prose_text(HUMAN, None, "p", ["readability", "lang", "sources", "trust"]))
    keys = [a["key"] for a in res["assessments"]]
    assert keys == ["readability", "sources", "trust"], "trust runs last (it needs the others); the failed scorer adds nothing"
    rec = {r["id"]: r for r in res["receipts"]}
    assert "no node" in rec["assess.lang"]["error"] and rec["assess.readability"]["count"] == 1
    assert [s["key"] for s in AC.scorer_list("code")] == ["complexity", "smells", "clones", "tests", "provenance", "health"]


CODE = '''import os, pickle

def simple(a):
    return a + 1

def tangled(x, items, mode):
    total = 0
    for i in items:
        if i > x and mode == "a" or mode == "b":
            total += i
        elif i < 0:
            total -= i
        else:
            try:
                total += int(i)
            except ValueError:
                total += 0
    while total > 100:
        total -= 10
        if total % 7 == 0:
            break
    return [t for t in items if t and t > 0] if total else eval("0")

def loader(path):
    with open(path, "rb") as fh:
        return pickle.load(fh)

def copy_one(items):
    out = []
    for i in items:
        if i is not None and i > 0:
            out.append(i * 2)
    return out

def copy_two(items):
    out = []
    for i in items:
        if i is not None and i > 0:
            out.append(i * 2)
    return out
'''


def test_code_scorers_put_verdicts_on_cards_and_a_composite_on_the_source():
    doc = C.explode_sources([{"path": "pkg/mod.py", "text": CODE}])
    texts = {"pkg/mod.py": CODE}
    tests = "from pkg.mod import simple\n\ndef test_simple():\n    assert simple(1) == 2\n"
    # the test corpus and git facts handed in (a caller with a repo reads them from it)
    res = _run(AC.assess_code_doc(doc, texts, None, "", tests=tests, git={"pkg/mod.py": {"sha": "abc1234def", "author": "BoeJaker", "date": "2026-09-21"}}))
    by = {}
    for a in res["assessments"]:
        by.setdefault((a["key"], a["on"]), a)
    cc = by[("complexity", "pkg/mod.py::tangled")]
    assert cc["badge"].startswith("cc ") and int(cc["badge"][3:]) >= 12 and ("complexity", "pkg/mod.py::simple") not in by, "only a complex function wears the badge"
    assert 0 < by[("complexity", "source")]["score"] < 1
    sm = [a for a in res["assessments"] if a["key"] == "smells" and a["on"] != "source"]
    notes = {e["note"] for a in sm for e in a["evidence"]}
    assert "eval()" in notes and "pickle.load — arbitrary code on load" in notes and {a["on"] for a in sm} == {"pkg/mod.py::tangled", "pkg/mod.py::loader"}
    assert CODE[[e for a in sm for e in a["evidence"] if e["note"] == "eval()"][0]["span"]["start"]:][:5] == "eval("
    cl = [a for a in res["assessments"] if a["key"] == "clones" and a["on"] != "source"]
    assert {a["on"] for a in cl} == {"pkg/mod.py::copy_one", "pkg/mod.py::copy_two"} and cl[0]["badge"] == "clone"
    assert by[("clones", "source")]["detail"]["sets"] == 1
    assert ("tests", "pkg/mod.py::simple") in by and ("tests", "pkg/mod.py::tangled") not in by and by[("tests", "source")]["detail"]["named"] == 1
    pv = by[("provenance", "pkg/mod.py::<module>")]
    assert pv["badge"] == "2026-09-21 · abc1234" and by[("provenance", "source")]["score"] == 1.0
    h = by[("health", "source")]
    assert h and set(h["detail"]) == {"syntax", "complexity", "smells", "clones", "tests"}, "no calls in this file, so no `resolved` — the composite renormalises over what was measured"
    import re as _re
    assert 99 <= sum(int(p) for p in _re.findall(r" (\d+)%", h["by"])) <= 101 and "smells" in h["by"]
    assert [r["id"] for r in res["receipts"]] == ["assess.complexity", "assess.smells", "assess.clones", "assess.tests", "assess.provenance", "assess.health"]


def test_assess_rides_on_both_explode_capabilities_and_a_slice_keeps_record_coordinates(monkeypatch, tmp_path):
    saved = {k: dict(v) for k, v in X.LAYERS.items()}

    async def ner(ctx):
        ctx["ent_list"] = []
        return {"cards": []}
    for k in X.LAYERS:
        monkeypatch.setitem(X.LAYERS[k], "fn", ner)
    text = "Intro paragraph here, short and plain.\n\n" + HUMAN
    out = _run(X.explode_prose(text=text, layers=["ner"], assess=True))
    keys = [a["key"] for a in out["assessments"]]
    assert "readability" in keys and "sources" in keys and "ai_likelihood" in keys and "trust" in keys and "lang" not in keys
    assert any(r["id"] == "assess.trust" for r in out["layers"])
    store = {"r1": {"id": "r1", "dataset_id": "d", "text": text, "source_id": "s", "tags": ""}}
    monkeypatch.setattr(X, "_read_record", lambda rid: store.get(rid))
    s0 = text.find("I didn't")
    sl = _run(X.explode_prose(record_id="r1", ranges=[[s0, len(text)]], layers=["ner"], assess=["sources"]))
    src = [a for a in sl["assessments"] if a["key"] == "sources"][0]
    link = [e for e in src["evidence"] if e["note"] == "link"][0]["span"]
    assert text[link["start"]:link["end"]].startswith("https://"), "a scorer's span on a slice is back in the record's coordinates"
    for k, v in saved.items():
        X.LAYERS[k].update(v)
    # code: the explode with assess=True carries per-card badges the renderer shows
    root = tmp_path / "repo"; (root / "pkg").mkdir(parents=True); (root / "tests").mkdir()
    (root / "pkg" / "mod.py").write_text(CODE, encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text("from pkg.mod import simple\n", encoding="utf-8")
    monkeypatch.setattr(X, "_repo_root", lambda: str(root))
    d = _run(X.explode_code(path="pkg/mod.py", depth=0, assess=True))
    ons = {(a["key"], a["on"]) for a in d["assessments"]}
    assert ("smells", "pkg/mod.py::tangled") in ons and ("tests", "pkg/mod.py::simple") in ons and ("health", "source") in ons
    assert ("provenance", "source") in ons, "git facts come from spawn_core on the host; a directory that is not a repo scores 0 tracked"
    assert d["counts"]["assessments"] == len(d["assessments"]) and any(r["id"] == "assess.health" for r in d["layers"])


def test_verdicts_persist_into_the_record_and_rank_across_records(monkeypatch, tmp_path):
    db = tmp_path / "f.db"

    def conn():
        c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row; return c
    c = conn()
    c.execute("CREATE TABLE fabric_records (id TEXT PRIMARY KEY, dataset_id TEXT, text TEXT, data TEXT, source_id TEXT, tags TEXT, created_at TEXT)")
    c.execute("INSERT INTO fabric_records VALUES ('a','d','alpha text',?, '', '', '')", (json.dumps({"kept": 1}),))
    c.execute("INSERT INTO fabric_records VALUES ('b','d','beta text','{}', '', '', '')")
    c.execute("INSERT INTO fabric_records VALUES ('c','d','gamma text','{}', '', '', '')")
    c.commit(); c.close()
    monkeypatch.setattr(AC, "_sqlite_conn", conn)
    AC.persist_assessments("a", [{"key": "sources", "score": 0.9, "confidence": 0.5, "by": "x", "on": "source"}, {"key": "ai_likelihood", "score": 0.2, "confidence": 0.3, "by": "y", "on": "source"},
                                {"key": "complexity", "score": 0.1, "on": "some::card"}])
    AC.persist_assessments("b", [{"key": "sources", "score": 0.1, "confidence": 0.5, "by": "x", "on": "source"}, {"key": "ai_likelihood", "score": 0.9, "confidence": 0.3, "by": "y", "on": "source"}])
    row = conn().execute("SELECT data FROM fabric_records WHERE id='a'").fetchone()
    data = json.loads(row["data"])
    assert data["kept"] == 1 and set(data["assess"]) == {"sources", "ai_likelihood"}, "source-level verdicts only, beside what the record already held"
    r = AC.rank_records(dataset_id="d")
    assert [x["id"] for x in r["records"]] == ["a", "b"] and r["records"][0]["composite"] > r["records"][1]["composite"] and "c" not in [x["id"] for x in r["records"]]
    assert r["records"][0]["scores"] == {"sources": 0.9, "ai_likelihood": 0.2} and r["invert"] == ["ai_likelihood"]
    r2 = AC.rank_records(keys=["ai_likelihood", "trust"], weights={"ai_likelihood": 2})
    assert r2["records"][0]["id"] == "a" and r2["records"][0]["missing"] == ["trust"]
    assert AC.persist_assessments("nope", [])["error"] == "record nope not found"
