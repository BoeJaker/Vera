"""The Observe page leads with warnings and errors (owner, 2026-09-28: "the widgets in observe panel are not focused on
errors and they must be"). syslog.error_summary counts them over a window from the errors stream; the counting is the
pure syslog_summary_core.summarise, tested here. The syslog.py half is checked by reading its source (it imports the app)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from vera.workers.syslog_summary_core import summarise, entry_ms  # noqa: E402

NOW = 1_790_000_000_000   # ms


def _e(ago_s, level, cap="", cat="cap", msg="m"):
    ms = NOW - ago_s * 1000
    return (f"{ms}-0", {"level": level, "cap_name": cap, "category": cat, "message": msg, "ts": f"t{ago_s}"})


def test_counts_only_warnings_and_errors_inside_the_window():
    rows = [_e(10, "ERROR", "a.b"), _e(20, "INFO"), _e(30, "WARNING", "a.b"), _e(40, "CRITICAL", "c.d"),
            _e(4000, "ERROR", "old.one")]                     # older than the hour: not counted
    s = summarise(rows, NOW, window_s=3600)
    assert (s["errors"], s["warnings"], s["critical"], s["total"]) == (2, 1, 1, 3)
    assert [c["name"] for c in s["by_cap"]] == ["a.b", "c.d"]   # errors first, then count
    assert s["by_cap"][0] == {"name": "a.b", "errors": 1, "warnings": 1, "count": 2, "last": "t10"}
    assert s["last_error"]["cap_name"] == "a.b" and s["last_error"]["level"] == "ERROR"
    # errors only, newest first: the warning is not in it
    assert [e["cap_name"] for e in s["recent_errors"]] == ["a.b", "c.d"]
    assert [e["level"] for e in s["entries"]] == ["ERROR", "WARNING", "CRITICAL"]


def test_an_hour_without_errors_has_no_recent_errors():
    s = summarise([_e(10, "WARNING", "a.b"), _e(20, "INFO")], NOW)
    assert s["recent_errors"] == [] and s["last_error"] is None and s["warnings"] == 1


def test_the_series_buckets_the_window_and_places_each_entry():
    s = summarise([_e(10, "ERROR"), _e(3500, "WARNING")], NOW, window_s=3600, bucket_s=300)
    assert len(s["series"]) == 12
    assert s["series"][-1]["errors"] == 1 and s["series"][0]["warnings"] == 1
    assert sum(b["errors"] + b["warnings"] for b in s["series"]) == 2


def test_no_cap_name_falls_back_to_the_category_then_system():
    s = summarise([_e(5, "ERROR", "", "worker"), _e(6, "ERROR", "", "")], NOW)
    assert sorted(c["name"] for c in s["by_cap"]) == ["system", "worker"]


def test_limit_caps_the_entries_not_the_counts():
    rows = [_e(i, "ERROR", "x") for i in range(1, 30)]
    s = summarise(rows, NOW, limit=5)
    assert s["errors"] == 29 and len(s["entries"]) == 5


def test_entry_ms_reads_a_stream_id():
    assert entry_ms("1790000000000-3") == 1790000000000 and entry_ms("junk") == 0


SYSLOG = open(os.path.join(ROOT, "vera", "workers", "syslog.py"), encoding="utf-8").read()


def test_warnings_and_errors_are_copied_to_their_own_stream_with_the_main_id():
    assert 'SYSLOG_ERR_STREAM  = "vera:syslog:errors"' in SYSLOG
    assert 'if str(getattr(rec, "level", "") or "").upper() in SYSLOG_ERR_LEVELS:' in SYSLOG
    assert 'await r.xadd(SYSLOG_ERR_STREAM, {"data": data, "main_id": mid},' in SYSLOG


def test_a_filtered_read_scans_the_whole_stream_and_levels_read_the_errors_stream():
    assert "stream, scan = SYSLOG_STREAM, (max(limit * 3, SYSLOG_MAXLEN) if filtered else limit * 3)" in SYSLOG
    assert "stream, scan = SYSLOG_ERR_STREAM, max(limit * 3, SYSLOG_ERR_MAXLEN)" in SYSLOG
    assert 'rec["_redis_id"] = main_id.decode() if isinstance(main_id, bytes) else str(main_id)' in SYSLOG


def test_clear_trims_the_errors_stream_too_and_the_summary_is_a_capability():
    assert "await r.xtrim(SYSLOG_ERR_STREAM, maxlen=keep, approximate=False)" in SYSLOG
    assert '"syslog.error_summary", memory="off"' in SYSLOG
