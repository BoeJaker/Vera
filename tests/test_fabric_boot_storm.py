"""The fabric redid its work on every boot, and re-embedded it too.

Two independent defects, both measured on prod 2026-09-04.

1. EVERY SOURCE WENT DUE ON EVERY RESTART.
   fabric_sources.last_pulled is written on each pull and reloaded by
   _sqlite_sources (SELECT *) - but _auto_pull_loop schedules from the
   in-memory `_last_pull_ts`, which nothing ever seeded from it:

       last = src.get("_last_pull_ts", 0)
       if now - last >= interval:

   After a restart that is 0 for every source, so `now - 0 >= interval` is
   true for all of them. The log read "pre-loaded 1885 sources from SQLite"
   and then 1,233 "returned no items" pulls after one restart.

2. THE DEDUP GUARD RAISED NameError ON EVERY PULL.
   The pre-insert check called `_db_path(ds_id)` - a function that does not
   exist in this module and never has (only netmon defines that name). The
   surrounding `except Exception: log.debug(...)` swallowed it, so
   existing_ids stayed empty, new_records became "all of them", and
   _run_extras re-embedded the whole pull every cycle - the exact storm the
   comment above it claims was fixed. "will skip embed" appears 0 times in
   the entire log. aiosqlite IS installed (0.22.1), so the block did run; it
   just always threw.

Both bugs are the same shape: a name that was never checked at runtime, hidden
by a broad except. The last test here guards that shape directly.

Pure: no network, no DB.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SRC_PATH = os.path.join(os.path.dirname(__file__), "..",
                        "vera", "fabric", "data_fabric.py")
SRC = open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)

pytestmark = pytest.mark.critical


def _load(name):
    """Execute one top-level function in isolation - data_fabric itself pulls
    in uvicorn and the whole orchestrator, which no unit test should need."""
    fn = next(n for n in TREE.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<f>", "exec"), ns)
    return ns[name]


last_pull_epoch = _load("_last_pull_epoch")


# ── bug 1: the persisted timestamp must be honoured ────────────────────────

def test_a_persisted_last_pulled_is_read_back():
    """The value was loaded from SQLite and then ignored."""
    assert last_pull_epoch({"last_pulled": "2026-09-04T18:00:00Z"}) == 1788544800.0


def test_the_three_timestamp_spellings_all_parse():
    z = last_pull_epoch({"last_pulled": "2026-09-04T18:00:00Z"})
    off = last_pull_epoch({"last_pulled": "2026-09-04T18:00:00+00:00"})
    naive = last_pull_epoch({"last_pulled": "2026-09-04T18:00:00"})
    assert z == off == naive, "a naive stamp must be read as UTC, like now_iso writes"


def test_a_source_never_pulled_is_still_due_immediately():
    """Zero preserves the old behaviour for genuinely new sources."""
    assert last_pull_epoch({}) == 0.0
    assert last_pull_epoch({"last_pulled": ""}) == 0.0


def test_an_unparseable_stamp_falls_back_to_due_rather_than_crashing():
    assert last_pull_epoch({"last_pulled": "not a date"}) == 0.0
    assert last_pull_epoch({"last_pulled": None}) == 0.0


def test_a_live_runtime_timestamp_wins_over_the_persisted_one():
    """Within a running process _last_pull_ts is the fresher truth."""
    assert last_pull_epoch({"_last_pull_ts": 999.0,
                            "last_pulled": "2026-09-04T18:00:00Z"}) == 999.0


def test_startup_seeds_the_scheduler_clock():
    """Without this the helper is dead code and the storm returns."""
    assert 's["_last_pull_ts"] = _last_pull_epoch(s)' in SRC


def test_the_restart_no_longer_makes_every_source_due():
    """The measured estate: 1,885 sources, most pulled recently."""
    interval, now = 3600.0, 1788544800.0 + 600      # 10 min after the last pull
    recent = {"last_pulled": "2026-09-04T18:00:00Z", "interval": interval}
    due = (now - last_pull_epoch(recent)) >= interval
    assert due is False, "a source pulled 10 minutes ago must not be due again"
    stale = {"last_pulled": "2026-09-04T10:00:00Z", "interval": interval}
    assert (now - last_pull_epoch(stale)) >= interval, "a stale source must still pull"


# ── bug 2: the dedup guard must actually run ───────────────────────────────

def test_the_dedup_check_no_longer_calls_a_function_that_does_not_exist():
    """Checked as a CALL, not as text - the fix comment quotes the old name
    while explaining the bug, and a substring match trips on the explanation."""
    called = {n.func.id for n in ast.walk(TREE)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_db_path" not in called, "_db_path has never existed in this module"
    assert "aiosqlite.connect(SQLITE_PATH)" in SRC


def test_a_broken_dedup_check_is_no_longer_whispered_at_debug():
    """It hid a NameError for the entire life of the check; the only visible
    symptom was the embedding bill."""
    assert 'log.debug("pre-insert dedup check' not in SRC
    assert "will be re-embedded" in SRC


def test_only_genuinely_new_records_reach_the_embed_stage():
    """The guard the NameError was defeating."""
    assert 'new_records = [r for r in records if r["id"] not in existing_ids]' in SRC


# ── the shape of both bugs ─────────────────────────────────────────────────

def test_every_name_the_module_calls_at_top_level_actually_exists():
    """Both defects were an undefined name inside a try/except that no test
    ever executed. This catches the next one.

    Collects every plain function call in the module and checks the name is
    defined somewhere - as a module-level def, an import, an assignment, or a
    builtin. Not a type checker; it only catches names that exist NOWHERE,
    which is exactly what _db_path was.
    """
    import builtins
    defined = set(dir(builtins))
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                defined.add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)

    missing = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in defined:
                missing.add(node.func.id)
    assert not missing, "called but never defined anywhere: %s" % sorted(missing)
