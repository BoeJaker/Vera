"""Tests for the System Comms feed normaliser.

The weight is on merging heterogeneous sources: mixed timestamp formats sort
wrongly, undated entries hijack the top of the list, and a source that labels
nothing "warning" still needs its failures noticed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.syscomms import syscomms_core as sc  # noqa: E402


# ── timestamps ──────────────────────────────────────────────────────────────
def test_parse_ts_passes_iso_through():
    assert sc.parse_ts("2026-09-09T12:00:00Z") == "2026-09-09T12:00:00Z"


def test_parse_ts_converts_unix_seconds():
    got = sc.parse_ts(1788614074)
    assert got.startswith("2026-") and got.endswith("Z")


def test_parse_ts_converts_unix_seconds_given_as_a_string():
    assert sc.parse_ts("1788614074").startswith("2026-")


def test_parse_ts_handles_missing_values():
    for bad in (None, "", 0):
        assert sc.parse_ts(bad) == ""


def test_unix_and_iso_sort_together_after_parsing():
    # Unsorted these interleave wrongly: "1788614074" < "2026-..." as strings,
    # so a recent Telegram message would sink below an older fabric row.
    a = sc.parse_ts(1788614074)          # 2026
    b = sc.parse_ts("2020-01-01T00:00:00Z")
    assert a > b


# ── severity ────────────────────────────────────────────────────────────────
def test_derive_severity_from_content():
    assert sc.derive_severity("service is DOWN") == "critical"
    assert sc.derive_severity("workflow failed twice") == "critical"
    assert sc.derive_severity("schedule is stale") == "warning"
    assert sc.derive_severity("daily brief delivered") == "info"


def test_explicit_priority_is_honoured():
    assert sc.derive_severity("anything", "high") == "critical"
    assert sc.derive_severity("anything", "medium") == "warning"


def test_content_wins_when_no_explicit_level():
    # A source that labels nothing still gets its failures noticed.
    assert sc.derive_severity("ha-sync unreachable", "") == "critical"


# ── per-source normalisers ──────────────────────────────────────────────────
def test_from_telegram_marks_direction_from_the_real_shape():
    # tg.history returns {ts, from, text, ok}; "bot" means outbound.
    got = sc.from_telegram([
        {"text": "Daily brief", "ts": "2026-09-09T08:00:00Z", "from": "bot"},
        {"text": "ok thanks", "ts": "2026-09-09T08:01:00Z", "from": "joe"},
    ])
    assert [e["kind"] for e in got] == ["sent", "received"]
    assert got[0]["source"] == "telegram"
    assert got[0]["ts"] == "2026-09-09T08:00:00Z"


def test_from_telegram_still_reads_legacy_keys():
    got = sc.from_telegram([{"text": "x", "date": 1788614074, "from_bot": True}])
    assert got[0]["kind"] == "sent"


def test_from_telegram_content_does_not_escalate_severity():
    # Chat quoting the word "failed" is not itself an incident.
    got = sc.from_telegram([{"text": "the build failed earlier", "from": "joe"}])
    assert got[0]["severity"] == "info"


def test_from_telegram_skips_empty_messages():
    assert sc.from_telegram([{"date": 1}, {"text": ""}]) == []


def test_from_actions_ignores_closed_items():
    got = sc.from_actions([
        {"id": "a", "text": "open thing", "status": "open", "created": "2026-09-09"},
        {"id": "b", "text": "done thing", "status": "done", "created": "2026-09-09"},
    ])
    assert len(got) == 1 and got[0]["ref"] == "a"


def test_from_actions_uses_priority_for_severity():
    got = sc.from_actions([{"id": "x", "text": "do a thing",
                            "priority": "high", "status": "open"}])
    assert got[0]["severity"] == "critical"


def test_from_n8n_health_only_reports_failures():
    got = sc.from_n8n_health([
        {"workflow": "good", "ok": 10, "err": 0},
        {"workflow": "bad", "ok": 1, "err": 3, "error_rate": 0.75},
    ])
    assert len(got) == 1
    assert "bad" in got[0]["title"] and "3 of 4" in got[0]["title"]


def test_from_reports_takes_the_first_line_as_the_title():
    got = sc.from_reports([{"text": "Vera daily brief\n\nbody here",
                            "category": "daily_report", "created": "2026-09-09"}])
    assert got[0]["title"] == "Vera daily brief"
    assert got[0]["severity"] == "info"


# ── merge ───────────────────────────────────────────────────────────────────
def _e(ts, src, title, sev="info"):
    return {"ts": ts, "source": src, "kind": "k", "severity": sev,
            "title": title, "detail": "", "ref": ""}


def test_merge_orders_newest_first():
    got = sc.merge_feed([[_e("2026-09-01", "a", "old")],
                         [_e("2026-09-09", "b", "new")]])
    assert [e["title"] for e in got] == ["new", "old"]


def test_merge_puts_undated_entries_last():
    # An unknown time is not "just now" — letting it lead would bury real news.
    got = sc.merge_feed([[_e("", "a", "undated"), _e("2026-09-09", "b", "dated")]])
    assert [e["title"] for e in got] == ["dated", "undated"]


def test_merge_filters_by_source():
    got = sc.merge_feed([[_e("2026-09-09", "telegram", "t"),
                          _e("2026-09-09", "n8n", "n")]], sources=["n8n"])
    assert [e["title"] for e in got] == ["n"]


def test_merge_filters_by_minimum_severity():
    got = sc.merge_feed([[_e("2026-09-09", "a", "info one", "info"),
                          _e("2026-09-09", "a", "bad one", "critical")]],
                        min_severity="warning")
    assert [e["title"] for e in got] == ["bad one"]


def test_merge_deduplicates_identical_entries():
    dup = _e("2026-09-09", "a", "same")
    got = sc.merge_feed([[dup], [dict(dup)]])
    assert len(got) == 1


def test_merge_drops_entries_with_no_title():
    got = sc.merge_feed([[{"ts": "2026-09-09", "source": "a", "title": ""}]])
    assert got == []


def test_merge_respects_the_limit():
    many = [_e("2026-09-%02d" % (i + 1), "a", "e%d" % i) for i in range(20)]
    assert len(sc.merge_feed([many], limit=5)) == 5


# ── summary ─────────────────────────────────────────────────────────────────
def test_summarise_counts_by_severity_and_source():
    feed = [_e("2026-09-09", "n8n", "a", "critical"),
            _e("2026-09-08", "telegram", "b", "info")]
    s = sc.summarise(feed)
    assert s["total"] == 2
    assert s["by_severity"]["critical"] == 1
    assert s["by_source"] == {"n8n": 1, "telegram": 1}
    assert s["newest"] == "2026-09-09"


def test_summarise_handles_an_empty_feed():
    s = sc.summarise([])
    assert s["total"] == 0 and s["newest"] == ""
