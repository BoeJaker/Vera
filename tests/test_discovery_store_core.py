"""The silent-correctness set in discovery (DEEP-07 D07-1 = web-ingress WI-1): every row and query shape runs
against a temp SQLite with the real columns, and an AST pass proves discovery.py / fabric_web_acquisition.py use
the shapes instead of the SQL that failed at debug level for months."""
import ast
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.fabric import discovery_store_core as dsc  # noqa: E402

DISCOVERY = ROOT / "vera" / "fabric" / "discovery.py"
ACQ = ROOT / "vera" / "fabric" / "fabric_web_acquisition.py"


def _schema_sql(path: Path, table: str) -> str:
    """The CREATE TABLE statement for `table` as the module writes it (so the tests see the real columns)."""
    src = path.read_text(encoding="utf-8")
    i = src.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    j = src.index(");", i)
    return src[i:j + 2]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for t in ("fabric_pages", "fabric_surfaces", "fabric_subtables", "fabric_discovery_frontier",
              "fabric_discovery_edges"):
        conn.executescript(_schema_sql(DISCOVERY, t))
    conn.executescript(_schema_sql(ROOT / "vera" / "fabric" / "data_fabric.py", "fabric_records"))
    for t in ("fabric_entities", "fabric_entity_mentions"):
        conn.executescript(_schema_sql(ACQ, t))
    return conn


# ── B2: the frontier's shape ──────────────────────────────────────────────────

def test_queue_items_of_every_legacy_shape_become_six_tuples():
    seq = iter(range(100, 200))
    nxt = lambda: next(seq)  # noqa: E731
    full = (-0.5, 7, "https://a/", 1, 0, "https://p/")
    assert dsc.queue_item(full, nxt) == full
    assert dsc.queue_item(("https://a/", 0, 0, None), nxt) == (-1.0, 100, "https://a/", 0, 0, None)   # expand's shape
    assert dsc.queue_item(["https://a/", 2, 1], nxt) == (-1.0, 101, "https://a/", 2, 1, None)       # a saved frontier
    assert dsc.queue_item("https://a/", nxt) == (-1.0, 102, "https://a/", 0, 0, None)
    import heapq
    q = dsc.normalise_queue([("https://x/", 0, 0, None), (-2.0, 1, "https://y/", 0, 0, None)], nxt)
    heapq.heapify(q)
    heapq.heappush(q, (-3.0, 5, "https://z/", 1, 0, "https://x/"))        # no str-vs-float comparison
    assert heapq.heappop(q)[2] == "https://z/"


# ── B1 / B18: fabric_pages ────────────────────────────────────────────────────

def test_fabric_page_row_writes_and_a_shorter_text_never_overwrites_a_longer_one():
    conn = _db()
    sql, p = dsc.fabric_page_row("topic.x", "crawl1", "https://h/a", "A", "full text " * 50, ["web"], ["h1"], 100)
    conn.execute(sql, p)
    sql2, p2 = dsc.fabric_page_row("topic.x", "", "https://h/a", "A", "a snippet", ["search"], [], 2)
    conn.execute(sql2, p2)                                                # the search snippet (B18)
    row = conn.execute("SELECT * FROM fabric_pages").fetchone()
    assert conn.execute("SELECT count(*) FROM fabric_pages").fetchone()[0] == 1
    assert row["full_text"].startswith("full text") and row["crawl_id"] == "crawl1" and row["word_count"] == 100
    sql3, p3 = dsc.fabric_page_row("topic.x", "crawl2", "https://h/a", "A2", "full text " * 60, ["web"], [], 120)
    conn.execute(sql3, p3)                                                # a fuller page does replace
    row = conn.execute("SELECT * FROM fabric_pages").fetchone()
    assert row["crawl_id"] == "crawl2" and row["title"] == "A2"
    assert row["id"] == dsc.page_id("https://h/a") and json.loads(row["headings"]) == []
    sql4, p4 = dsc.fabric_page_row("topic.x", "crawl3", "https://h/a", "A3", "shrunk page " * 5, ["web"], [], 10)
    conn.execute(sql4, p4)                                                # a re-crawl of a page that got shorter
    row = conn.execute("SELECT * FROM fabric_pages").fetchone()
    assert row["crawl_id"] == "crawl3" and row["title"] == "A3" and row["word_count"] == 10


# ── B3 / B4: fabric_records as it is ──────────────────────────────────────────

def test_record_summaries_and_compiled_record_use_real_columns():
    conn = _db()
    conn.execute("INSERT INTO fabric_records (id, dataset_id, text, data) VALUES (?,?,?,?)",
                 ("r1", "topic.x", "Body of page one " * 10, json.dumps({"title": "Page one", "url": "https://h/1"})))
    conn.execute("INSERT INTO fabric_records (id, dataset_id, text, data) VALUES (?,?,?,?)",
                 ("r2", "topic.x", "Untitled body", "{}"))
    conn.execute("INSERT INTO fabric_records (id, dataset_id, text, data) VALUES (?,?,?,?)",
                 ("r3", "topic.x", "", "not json"))
    rows = conn.execute(dsc.RECENT_RECORDS_SQL, ("topic.x", 25)).fetchall()
    s = dsc.record_summaries(rows)
    assert s[0] == "Untitled body" and s[1].startswith("Page one: Body of page one") and len(s) == 2
    sql, p = dsc.compiled_record_row("topic.x", "Compiled", "# doc", pages_used=3)
    conn.execute(sql, p)
    conn.execute(sql, p)                                                  # a recompile replaces, not duplicates
    r = conn.execute("SELECT * FROM fabric_records WHERE id=?", (p[0],)).fetchone()
    assert r["text"] == "# doc" and json.loads(r["data"])["kind"] == "document" and r["tags"] == "compiled,document"
    assert conn.execute("SELECT count(*) FROM fabric_records WHERE dataset_id='topic.x'").fetchone()[0] == 4
    conn.execute("INSERT INTO fabric_discovery_frontier (id, dataset_id, config, updated_at) VALUES (?,?,?,?)",
                 ("c1", "topic.x", json.dumps({"topic": "lab space"}), "2026-10-01"))
    r = conn.execute(dsc.TOPIC_FROM_FRONTIER_SQL, ("topic.x",)).fetchone()
    assert dsc.topic_from_frontier(r["config"], "topic.x") == "lab space"
    assert dsc.topic_from_frontier("", "topic.lab_space") == "lab space"


# ── B5: the graph purge ───────────────────────────────────────────────────────

def test_purge_cypher_keys_on_the_ids_the_rows_hold():
    conn = _db()
    conn.execute("INSERT INTO fabric_surfaces (id, crawl_id, parent_dataset) VALUES ('s1','c1','topic.x')")
    conn.execute("INSERT INTO fabric_subtables (id, crawl_id, parent_dataset, sub_dataset) VALUES ('t1','c1','topic.x','topic.x.table1')")
    s = [r[0] for r in conn.execute(dsc.SCAN_SURFACE_IDS_SQL, ("c1",))]
    d = [r[0] for r in conn.execute(dsc.SCAN_SUBTABLE_IDS_SQL, ("c1",))]
    cy = dsc.purge_cypher(s, d)
    assert [c[0] for c in cy] == ["MATCH (s:Surface) WHERE s.id IN $ids DETACH DELETE s",
                                  "MATCH (d:Dataset) WHERE d.id IN $ids DETACH DELETE d"]
    assert cy[0][1] == {"ids": ["s1"]} and cy[1][1] == {"ids": ["topic.x.table1"]}
    assert dsc.purge_cypher([], []) == [] and "crawl_id" not in str(cy)


# ── DATA-01: overwrite on one connection ──────────────────────────────────────

def test_overwrite_dataset_deletes_on_one_connection():
    conn = _db()
    conn.execute("INSERT INTO fabric_records (id, dataset_id, text) VALUES ('r1','topic.x','t')")
    conn.execute("INSERT INTO fabric_records (id, dataset_id, text) VALUES ('r9','topic.other','t')")
    conn.execute("INSERT INTO fabric_pages (id, dataset_id, url) VALUES ('p1','topic.x','https://h/')")
    conn.execute("INSERT INTO fabric_entities (id, name, type) VALUES ('e1','Acme','ORG')")
    conn.execute("INSERT INTO fabric_entity_mentions (entity_id, record_id, dataset_id) VALUES ('e1','r1','topic.x')")
    conn.execute("INSERT INTO fabric_discovery_frontier (id, dataset_id) VALUES ('c1','topic.x')")
    conn.commit()
    out = dsc.overwrite_dataset(conn, "topic.x")
    assert out["fabric_records"] == 1 and out["fabric_pages"] == 1 and out["fabric_entities"] == 1
    assert out["fabric_entity_mentions"] == 1 and out["fabric_discovery_frontier"] == 1
    assert conn.execute("SELECT count(*) FROM fabric_records").fetchone()[0] == 1          # the other dataset stays
    assert conn.execute("SELECT count(*) FROM fabric_entities").fetchone()[0] == 0


# ── B11: continue resumes ─────────────────────────────────────────────────────

def test_continue_kwargs_carry_the_visited_set():
    acq = {"source_url": "https://docs.example/", "dataset_id": "web.docs",
           "config": json.dumps({"max_depth": 3, "topic": "api", "same_domain": False, "tags": "docs"}),
           "state": json.dumps({"visited": ["https://docs.example/", "https://docs.example/a"]})}
    kw = dsc.continue_kwargs(acq, 20)
    assert kw["resume_visited"] == ["https://docs.example/", "https://docs.example/a"]
    assert kw["max_pages"] == 20 and kw["max_depth"] == 3 and kw["same_domain"] is False and kw["topic"] == "api"
    assert dsc.continue_kwargs({"config": "x", "state": None}, 0)["max_pages"] == 50     # the acquire default


# ── the modules use the shapes ────────────────────────────────────────────────

def _funcs(tree: ast.AST, name: str):
    out = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert out, name
    return out


def _func(tree: ast.AST, name: str):
    out = _funcs(tree, name)
    assert len(out) == 1, f"{name}: {len(out)} definitions"
    return out[0]


def test_discovery_uses_the_store_shapes():
    src = DISCOVERY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # the crawl's page worker (one of several nested _process functions in the module)
    assert any("_write_fabric_page" in ast.dump(f) for f in _funcs(tree, "_process"))   # B1
    impl = ast.dump(_func(tree, "_run_crawl_impl"))
    assert "normalise_queue" in impl and "_CRAWL_STOPS" in impl                   # B2
    assert "_CRAWL_STOPS" in ast.dump(_func(tree, "_run_crawl"))
    steer = ast.dump(_func(tree, "_llm_steer_crawl"))
    assert "RECENT_RECORDS_SQL" in steer and "description FROM fabric_records" not in src   # B3
    compile_ = ast.dump(_func(tree, "cap_discover_compile"))
    assert "compiled_record_row" in compile_ and "fabric_crawl_history" not in src          # B4
    assert "(id, dataset_id, url, kind, title, data, created_at)" not in src      # the columns that never existed
    delete = ast.dump(_func(tree, "cap_discover_delete_scan"))
    assert "purge_cypher" in delete and "Surface {crawl_id" not in src                     # B5
    extract = ast.dump(_func(tree, "cap_discover_entity_extract"))
    assert "batch_size" not in extract                                              # B17
    topic = _func(tree, "cap_discover_topic")
    assert "overwrite_dataset" in ast.dump(topic)                                   # DATA-01
    for n in ast.walk(topic):                   # no _sqlite_conn().execute(...) chains left in the overwrite
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute":
            inner = n.func.value
            assert not (isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "_sqlite_conn")
    assert src.count("INSERT OR REPLACE INTO fabric_pages") == 0                   # one writer (B1/B18)


def test_acquisition_registers_through_the_capability_and_resumes():
    src = ACQ.read_text(encoding="utf-8")
    assert '.get("fn")' not in src                                                  # B10
    tree = ast.parse(src)
    acquire = _func(tree, "cap_web_acquire")
    assert "resume_visited" in [a.arg for a in acquire.args.args + acquire.args.kwonlyargs]   # B11
    cont = ast.dump(_func(tree, "cap_web_continue"))
    assert "continue_kwargs" in cont
