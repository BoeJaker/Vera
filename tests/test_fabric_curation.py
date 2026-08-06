"""Curated-dataset layer (Phase 1): identity, gap-fill merge, select-SQL, validation.

Tests the pure core (vera.fabric.curation_core) so they run without booting the
app. Covers the two motivating scenarios: a re-fetched pokedex must not
duplicate and can be topped up; a market series appends and serves a range.
"""

import sqlite3

from vera.fabric import curation_core as C


# ── Identity: pokedex idempotency ───────────────────────────────────────────

def test_key_id_is_deterministic_and_key_scoped():
    row = {"id": 25, "name": "pikachu", "type": "electric"}
    a = C.key_id("pokedex.gen1", row, ["id"])
    b = C.key_id("pokedex.gen1", dict(row), ["id"])
    assert a == b, "same dataset + key must yield the same record id"
    # A different key value → different id.
    assert a != C.key_id("pokedex.gen1", {"id": 26, "name": "raichu"}, ["id"])
    # Same key value in a different dataset → different id (no cross-collision).
    assert a != C.key_id("pokedex.gen2", row, ["id"])


def test_reingest_same_keys_collapses_to_one_id_each():
    # Simulate ingesting the gen-1 pokedex twice: the id set is identical, so an
    # INSERT OR REPLACE on the PK leaves exactly one row per pokemon.
    dex = [{"id": i, "name": f"mon{i}"} for i in range(1, 152)]
    ids_first = {C.key_id("pokedex.gen1", r, ["id"]) for r in dex}
    ids_second = {C.key_id("pokedex.gen1", r, ["id"]) for r in dex}
    assert ids_first == ids_second
    assert len(ids_first) == 151


# ── Gap-fill merge ──────────────────────────────────────────────────────────

def test_merge_fills_missing_and_updates_present():
    stored = {"id": 1, "name": "bulbasaur", "type": "grass", "hp": 45}
    incoming = {"id": 1, "type": "grass/poison", "weight_kg": 6.9, "hp": ""}
    merged = C.merge_row(stored, incoming)
    assert merged["name"] == "bulbasaur"          # kept (not in incoming)
    assert merged["type"] == "grass/poison"       # updated (present, non-empty)
    assert merged["weight_kg"] == 6.9             # added (new field)
    assert merged["hp"] == 45                     # empty incoming did NOT clobber
    assert "_id" not in merged                    # routing key never stored


# ── memory.select SQL builder ───────────────────────────────────────────────

def test_build_select_sql_binds_values_and_orders():
    sql, params = C.build_select_sql(
        "market.btcusd",
        where=[{"field": "date", "op": "gte", "value": "2026-01-01"}],
        sort=[{"field": "date", "dir": "asc"}],
        limit=50, offset=0,
    )
    assert "dataset_id = ?" in sql
    assert "json_extract(data, '$.date') >=" in sql
    assert "ORDER BY json_extract(data, '$.date') ASC" in sql
    assert params[0] == "market.btcusd"
    assert "2026-01-01" in params
    assert params[-2:] == [50, 0]


def test_build_select_sql_rejects_bad_field_and_op():
    # A malicious field name / unknown op is dropped, not interpolated.
    sql, params = C.build_select_sql(
        "d",
        where=[{"field": "x'; DROP TABLE fabric_records;--", "op": "eq", "value": 1},
               {"field": "ok", "op": "nonsense", "value": 2}],
        sort=[], limit=10, offset=0,
    )
    assert "DROP TABLE" not in sql
    assert "nonsense" not in sql
    # Only the dataset_id clause survives.
    assert sql.count("json_extract") == 0
    assert params == ["d", 10, 0]


def test_build_select_sql_runs_against_real_sqlite():
    # Prove the generated SQL + json_extract actually selects/sorts on a real DB.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fabric_records (id TEXT, dataset_id TEXT, data TEXT, "
                 "text TEXT, created_at TEXT)")
    import json
    series = [{"symbol": "BTC", "date": f"2026-01-{d:02d}", "close": 100 + d}
              for d in range(1, 11)]
    for i, row in enumerate(series):
        conn.execute("INSERT INTO fabric_records VALUES (?,?,?,?,?)",
                     (f"r{i}", "market.btc", json.dumps(row), "", ""))
    sql, params = C.build_select_sql(
        "market.btc",
        where=[{"field": "date", "op": "gte", "value": "2026-01-05"},
               {"field": "date", "op": "lte", "value": "2026-01-08"}],
        sort=[{"field": "date", "dir": "desc"}],
        limit=100, offset=0,
    )
    rows = conn.execute(sql, params).fetchall()
    # SELECT column order is (id, data, text, created_at) → data is index 1.
    got = [json.loads(r[1])["date"] for r in rows]
    assert got == ["2026-01-08", "2026-01-07", "2026-01-06", "2026-01-05"]


# ── Schema normalisation + validation ───────────────────────────────────────

def test_normalise_schema_forces_key_required():
    norm = C.normalise_schema({"name": {"type": "string"}}, ["id"])
    assert norm["id"]["required"] is True
    assert norm["name"]["type"] == "string"


def test_normalise_schema_rejects_bad_field():
    try:
        C.normalise_schema({"bad field!": "string"}, [])
    except ValueError:
        return
    assert False, "expected ValueError on invalid field name"


def test_validate_clean_dataset_scores_high():
    schema = {"id": {"type": "integer", "required": True},
              "name": {"type": "string", "required": True},
              "close": {"type": "number", "required": False}}
    rows = [{"id": i, "name": f"m{i}", "close": 1.5 * i} for i in range(1, 21)]
    res = C.validate_rows(rows, schema, ["id"], declared_trust=0.8)
    assert res["ok"] is True
    assert res["quality_score"] == 1.0
    assert res["duplicate_keys"] == 0
    assert res["coverage"]["name"] == 1.0
    assert res["trust"] == 0.8  # 0.8 * (0.5 + 0.5*1.0)


def test_validate_flags_missing_type_and_dupes():
    schema = {"id": {"type": "integer", "required": True},
              "name": {"type": "string", "required": True}}
    rows = [
        {"id": 1, "name": "a"},
        {"id": 2},                       # missing required name
        {"id": 3, "name": 999},          # type error (name not string)
        {"id": 1, "name": "dup"},        # duplicate key
    ]
    res = C.validate_rows(rows, schema, ["id"], declared_trust=0.6)
    assert res["missing_required"].get("name") == 1
    assert res["type_errors"].get("name") == 1
    assert res["duplicate_keys"] == 1
    assert res["ok"] is False
    assert 0.0 <= res["quality_score"] < 1.0


# ── Phase 2: gaps, backoff suppression, fusion ──────────────────────────────

def test_field_and_key_gaps():
    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b", "weight": 5}]
    fg = C.compute_field_gaps(rows, ["name", "weight"], coverage_threshold=0.9)
    refs = {g["ref"]: g for g in fg}
    assert "name" not in refs             # full coverage → not a gap
    assert refs["weight"]["missing"] == 1 # half coverage → gap
    # Key gaps from a range: 1..5 present {1,2} → missing 3,4,5.
    kg = C.compute_key_gaps({"1", "2"}, key_range={"min": 1, "max": 5})
    assert sorted(g["ref"] for g in kg) == ["3", "4", "5"]


def test_backoff_grows_and_caps():
    assert C.backoff_cooldown(0) == 0
    assert C.backoff_cooldown(1, base=300) == 300
    assert C.backoff_cooldown(2, base=300) == 600
    assert C.backoff_cooldown(3, base=300) == 1200
    assert C.backoff_cooldown(50, base=300, cap=7 * 86400) == 7 * 86400  # capped


def test_gap_actionable_suppresses_noise_and_cooldown():
    now = 1_000_000.0
    # Never-seen gap → actionable.
    assert C.gap_actionable(None, now=now)[0] is True
    # Marked noise / unfillable → suppressed.
    assert C.gap_actionable({"status": "noise"}, now=now) == (False, "noise")
    assert C.gap_actionable({"status": "unfillable"}, now=now)[0] is False
    # In cooldown → suppressed; past cooldown → actionable again.
    assert C.gap_actionable({"status": "open", "attempts": 1,
                             "cooldown_until_ts": now + 100}, now=now)[0] is False
    assert C.gap_actionable({"status": "open", "attempts": 1,
                             "cooldown_until_ts": now - 1}, now=now)[0] is True
    # Attempts exhausted → suppressed even if status still 'open'.
    assert C.gap_actionable({"status": "open", "attempts": C.MAX_FILL_ATTEMPTS},
                            now=now)[0] is False


def test_gap_id_stable():
    a = C.gap_id("d", {"type": "key", "ref": "42"})
    assert a == C.gap_id("d", {"type": "key", "ref": "42"})
    assert a != C.gap_id("d", {"type": "key", "ref": "43"})
    assert a != C.gap_id("d", {"type": "field", "ref": "42"})


def test_join_rows_inner_left_outer():
    left = [{"id": 1, "a": "x"}, {"id": 2, "a": "y"}]
    right = [{"id": 1, "b": "p"}, {"id": 3, "b": "q"}]
    inner = C.join_rows(left, right, ["id"], "inner")
    assert inner == [{"id": 1, "a": "x", "b": "p"}]
    left_join = C.join_rows(left, right, ["id"], "left")
    assert {r["id"] for r in left_join} == {1, 2}
    outer = C.join_rows(left, right, ["id"], "outer")
    assert {r["id"] for r in outer} == {1, 2, 3}
    # LEFT wins on field conflict.
    conflict = C.join_rows([{"id": 1, "v": "L"}], [{"id": 1, "v": "R"}], ["id"], "inner")
    assert conflict[0]["v"] == "L"


def test_match_score_prefers_id_substring():
    cand = {"dataset_id": "pokedex.gen1", "fields": ["id", "name"], "tags": []}
    s = C.match_score(cand, subject="pokedex gen1",
                      expected_fields=["id", "name"], want_tags=[])
    assert s > 0.7
    other = {"dataset_id": "cve.nvd", "fields": ["cve_id"], "tags": []}
    assert C.match_score(other, subject="pokedex gen1",
                         expected_fields=["id", "name"], want_tags=[]) < s


# ── Phase 3: trust-ranked context ───────────────────────────────────────────

def test_rank_context_datasets_trust_first():
    cands = [
        {"dataset_id": "mem.notes", "trust": 0.3, "relevance": 0.9, "tag_match": 0.0},
        {"dataset_id": "curated.dex", "trust": 0.9, "relevance": 0.4, "tag_match": 1.0},
        {"dataset_id": "curated.dex", "trust": 0.9, "relevance": 0.4, "tag_match": 1.0},  # dup
        {"dataset_id": "mid.set", "trust": 0.6, "relevance": 0.5, "tag_match": 0.0},
    ]
    ranked = C.rank_context_datasets(cands, top=5)
    ids = [d["dataset_id"] for d in ranked]
    assert ids == ["curated.dex", "mid.set", "mem.notes"]   # trust-desc, deduped
    # top= caps the list.
    assert len(C.rank_context_datasets(cands, top=1)) == 1
