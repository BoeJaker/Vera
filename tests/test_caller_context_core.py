"""Caller context (pure): the surface header, the actor kind, the counter's field, the buffer and the aggregate."""
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vera import caller_context_core as cc  # noqa: E402


@pytest.mark.parametrize("header, want", [
    (None, ("", "")),
    ("", ("", "")),
    ("desk", ("desk", "")),
    ("desk; trigger=person", ("desk", "person")),
    ("Panel:Markets ; trigger=AUTO", ("panel:markets", "auto")),
    ("dashboard;trigger=sometimes", ("dashboard", "")),
    ("evil<script>; trigger=person", ("other", "person")),
    ("lhm; foo=bar; trigger=auto", ("lhm", "auto")),
])
def test_parse_surface(header, want):
    assert cc.parse_surface(header) == want


def test_a_long_or_dirty_surface_is_cut_and_cleaned():
    s, _ = cc.parse_surface("panel:" + "x" * 500 + "|;")
    assert s.startswith("panel:") and len(s) <= 64 and "|" not in s


@pytest.mark.parametrize("kw, want", [
    ({"running_caps": ("dag.agent_loop_v8",), "http_cap": "x"}, "nested"),
    ({"background": "dream:c12"}, "background:dream"),
    ({"background": "v8:abc", "mcp_call": True}, "background:v8"),
    ({"caller_kind": "claude", "mcp_call": True}, "bridge:claude"),
    ({"caller_kind": "Census Tool"}, "caller:censustool"),
    ({"mcp_call": True}, "ui:mcp"),
    ({"http_cap": "x"}, "ui:route"),
    ({"http_cap": "another.cap"}, "internal"),
    ({}, "internal"),
])
def test_actor_kind(kw, want):
    assert cc.actor_kind(name="x", **kw) == want


def test_usage_field_round_trips_and_never_carries_a_pipe():
    f = cc.usage_field("a.b", "ui:route", "panel:mk|ts", "")
    assert f.count("|") == 3
    assert cc.split_field(f) == {"cap": "a.b", "actor": "ui:route", "surface": "panel:mkts", "trigger": "-"}


def test_day_keys_are_utc_newest_first_and_bounded():
    ks = cc.day_keys(3, now=86400 * 3 + 5)      # 1970-01-04 00:00:05 UTC
    assert ks == ["vera:caps:usage:19700104", "vera:caps:usage:19700103", "vera:caps:usage:19700102"]
    assert len(cc.day_keys(500)) == cc.RETENTION_DAYS and len(cc.day_keys(0)) == 1


def test_buffer_counts_flushes_restores_and_bounds():
    b = cc.UsageBuffer(interval=30, max_fields=2)
    assert not b.due(1000.0)                   # nothing to flush
    b.add("f1"); b.add("f1"); b.add("f2"); b.add("f3")
    assert b.snapshot() == {"f1": 2, "f2": 1} and b.overflow == 1
    assert b.due(1000.0)
    batch = b.drain(now=1000.0)
    assert batch["f1"] == 2 and sum(batch.values()) == 4 and not b.snapshot()
    b.add("f1")
    assert not b.due(1010.0) and b.due(1030.0)
    b.flushing = True
    assert not b.due(1100.0)                   # one flush at a time
    b.restore({"f2": 5})
    assert b.snapshot() == {"f1": 1, "f2": 5}


def test_aggregate_ranks_by_person_and_filters_by_prefix():
    d1 = {b"a.read|ui:route|desk|auto": b"900", "b.read|ui:mcp|panel:markets|person": "12"}
    d2 = {"a.read|ui:route|desk|person": "3", "b.read|nested|-|-": "40", "c.x|internal|-|-": "1000"}
    out = cc.aggregate([d1, d2])
    assert out["calls"] == 900 + 12 + 3 + 40 + 1000
    assert [b["cap"] for b in out["by_cap"]][:2] == ["b.read", "a.read"]     # person first, not raw volume
    a = next(b for b in out["by_cap"] if b["cap"] == "a.read")
    assert (a["person"], a["auto"], a["total"]) == (3, 900, 903)
    f = cc.aggregate([d1, d2], surface="panel")
    assert f["calls"] == 12 and [r["cap"] for r in f["rows"]] == ["b.read"]
    assert cc.aggregate([d1, d2], trigger="person")["calls"] == 15
    assert cc.aggregate([d1, d2], actor="nested")["calls"] == 40
