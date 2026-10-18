"""DA-01a: a capability declares how its answer reaches the desk - contract["presentation"] - and Contract v2 projects
it, validated, into the manifest and /mcp/tools (DESK-SPINE-DESIGN.md §1.1-1.3)."""
import asyncio
import copy

import pytest

from vera.capability_contract_core import (
    PRESENTATION_KINDS,
    presentation_problems,
    project_contract,
    project_presentation,
)
from vera import capability_orchestration as orchestration


pytestmark = pytest.mark.critical

QUOTES_SCHEMA = {"type": "object", "properties": {"quotes": {"type": "array", "items": {
    "type": "object", "properties": {"symbol": {"type": "string"}, "last": {"type": "number"}, "pct": {"type": "number"}}}}}}


def _codes(problems):
    return sorted({p[0] for p in problems})


def _entry(presentation=None, output_schema=None):
    contract = {"effects": ["read", "network"]}
    if output_schema is not None:
        contract["output_schema"] = output_schema
    if presentation is not None:
        contract["presentation"] = presentation
    return {"description": "Quotes.", "schema": {"type": "object", "properties": {}}, "contract": contract}


def test_no_declaration_is_unknown_not_guessed():
    manifest = project_contract("markets.quotes", _entry())
    assert manifest["presentation"] == {"status": "unknown"}


def test_a_valid_table_is_declared_with_its_kinds_views():
    pres = {"kind": "table", "map": {"rows": "quotes", "name": "symbol", "value": "last"},
            "title": "Quotes · {symbols}", "refresh": {"every_s": 30},
            "detail": {"cap": "markets.chart", "args": {"symbol": "{row.symbol}"}}, "empty": "No quotes"}
    manifest = project_contract("markets.quotes", _entry(pres, QUOTES_SCHEMA))
    got = manifest["presentation"]
    assert got["status"] == "declared" and "problems" not in got
    assert got["views"] == list(PRESENTATION_KINDS["table"]["views"])
    assert got["map"] == pres["map"] and got["refresh"] == {"every_s": 30}


def test_declared_views_are_kept_and_must_be_the_kinds():
    ok = project_presentation({"presentation": {"kind": "records", "map": {"rows": "hits"}, "views": ["list", "cards"]}})
    assert ok["status"] == "declared" and ok["views"] == ["list", "cards"]
    bad = project_presentation({"presentation": {"kind": "records", "map": {"rows": "hits"}, "views": ["list", "gauge"]}})
    assert bad["status"] == "invalid" and [p["code"] for p in bad["problems"]] == ["presentation.view_unknown"]


@pytest.mark.parametrize("pres, code", [
    ("table", "presentation.invalid"),
    ({"map": {"rows": "x"}}, "presentation.kind_invalid"),
    ({"kind": "spreadsheet", "map": {"rows": "x"}}, "presentation.kind_invalid"),
    ({"kind": "table", "map": {"rows": "x", "colour": "c"}}, "presentation.map_key_unknown"),
    ({"kind": "table", "map": {"name": "symbol"}}, "presentation.map_missing"),
    ({"kind": "table", "map": "quotes"}, "presentation.map_invalid"),
    ({"kind": "table", "map": {"rows": ""}}, "presentation.map_path_invalid"),
    ({"kind": "table", "map": {"rows": "x"}, "refresh": {"every_s": 1}}, "presentation.refresh_invalid"),
    ({"kind": "table", "map": {"rows": "x"}, "live": {"key": "symbol"}}, "presentation.live_invalid"),
    ({"kind": "table", "map": {"rows": "x"}, "detail": {"args": {}}}, "presentation.detail_invalid"),
    ({"kind": "table", "map": {"rows": "x"}, "title": 3}, "presentation.title_invalid"),
    ({"kind": "table", "map": {"rows": "x"}, "colour": "red"}, "presentation.field_unknown"),
    ({"kind": "composite"}, "presentation.parts_missing"),
])
def test_invalid_declarations_are_named(pres, code):
    problems = presentation_problems(pres)
    assert code in _codes(problems)
    assert any(p[0] == code and p[1] == "error" for p in problems)
    projected = project_presentation({"presentation": pres})
    assert projected["status"] == "invalid"


def test_media_needs_a_url_or_bytes_and_entities_either_form():
    assert presentation_problems({"kind": "media", "map": {"b64": "png"}}) == []
    assert _codes(presentation_problems({"kind": "media", "map": {"caption": "c"}})) == ["presentation.map_missing"]
    assert presentation_problems({"kind": "entities", "map": {"entities": "found"}}) == []


def test_paths_are_checked_against_the_declared_output_schema_as_warnings():
    good = {"kind": "table", "map": {"rows": "quotes", "name": "symbol", "value": "last"}}
    assert presentation_problems(good, QUOTES_SCHEMA) == []
    off = {"kind": "table", "map": {"rows": "quote", "name": "symbol"}}
    probs = presentation_problems(off, QUOTES_SCHEMA)
    assert [(p[0], p[1], p[2]) for p in probs] == [("presentation.map_unresolved", "warning", "presentation.map.rows")]
    row_off = {"kind": "table", "map": {"rows": "quotes", "name": "ticker"}}
    assert [(p[0], p[2]) for p in presentation_problems(row_off, QUOTES_SCHEMA)] == [("presentation.map_unresolved", "presentation.map.name")]
    # a warning does not make the declaration invalid
    projected = project_presentation({"presentation": off}, QUOTES_SCHEMA)
    assert projected["status"] == "declared" and projected["problems"][0]["severity"] == "warning"
    # no output_schema: nothing to check against, no warnings
    assert presentation_problems(off) == []


def test_composite_parts_are_checked_each():
    pres = {"kind": "composite", "parts": [{"kind": "metric", "map": {"value": "load"}, "title": "Load"},
                                            {"kind": "log", "map": {"text": "line"}},
                                            {"kind": "composite", "parts": []}]}
    probs = presentation_problems(pres)
    assert ("presentation.map_missing", "presentation.parts.1.map") in [(p[0], p[2]) for p in probs]
    assert ("presentation.parts_nested", "presentation.parts.2") in [(p[0], p[2]) for p in probs]
    assert not any(p[2].startswith("presentation.parts.0") for p in probs)


def test_every_kind_in_the_vocabulary_projects_with_its_content_key():
    for kind, spec in PRESENTATION_KINDS.items():
        if kind == "composite":
            pres = {"kind": "composite", "parts": [{"kind": "metric", "map": {"value": "v"}}]}
        elif kind == "receipt":
            pres = {"kind": "receipt", "map": {"ok": "ok"}}
        else:
            pres = {"kind": kind, "map": {spec["keys"][0]: "x"}}
            if kind == "terminal":
                pres["map"] = {"stdout": "out"}
        assert project_presentation({"presentation": pres})["status"] == "declared", kind


def test_projection_does_not_touch_the_registry_entry():
    entry = _entry({"kind": "table", "map": {"rows": "quotes"}}, QUOTES_SCHEMA)
    before = copy.deepcopy(entry)
    manifest = project_contract("markets.quotes", entry)
    manifest["presentation"]["map"]["rows"] = "changed"
    assert entry == before


def test_mcp_tools_carries_a_declared_presentation_only(monkeypatch):
    registry = {
        "demo.quotes": dict(_entry({"kind": "table", "map": {"rows": "quotes"}}, QUOTES_SCHEMA), mcp_expose=True),
        "demo.plain": dict(_entry(), mcp_expose=True),
        "demo.hidden": dict(_entry({"kind": "table", "map": {"rows": "quotes"}}), mcp_expose=False),
    }
    monkeypatch.setattr(orchestration, "CAPABILITY_REGISTRY", registry)
    tools = {t["name"]: t for t in asyncio.run(orchestration.mcp_tools())}
    assert set(tools) == {"demo.quotes", "demo.plain"}
    assert tools["demo.quotes"]["presentation"]["status"] == "declared"
    assert tools["demo.quotes"]["presentation"]["views"] == list(PRESENTATION_KINDS["table"]["views"])
    assert "presentation" not in tools["demo.plain"]


# ── DA-01b: the builders ──────────────────────────────────────────────────────

from vera import presentation_core as P  # noqa: E402
from vera.capability_contract_core import contract_coverage, gate_contracts, lint_contracts  # noqa: E402


def test_every_kind_has_a_builder_whose_declaration_is_valid():
    assert set(P.BUILDERS) == set(PRESENTATION_KINDS)
    for kind, build in P.BUILDERS.items():
        pres = build(P.metric("v")) if kind == "composite" else build("x")
        assert pres["kind"] == kind
        assert project_presentation({"presentation": pres})["status"] == "declared", (kind, pres)


def test_builder_reads_like_the_spec_example():
    pres = P.table("quotes", name="symbol", value="last", refresh=30, live="markets.tick",
                   detail={"cap": "markets.chart", "args": {"symbol": "{row.symbol}"}}, title="Quotes · {symbols}",
                   views=("table", "ranked"))
    assert pres == {"kind": "table", "map": {"rows": "quotes", "name": "symbol", "value": "last"},
                    "refresh": {"every_s": 30}, "live": {"event": "markets.tick"},
                    "detail": {"cap": "markets.chart", "args": {"symbol": "{row.symbol}"}},
                    "title": "Quotes · {symbols}", "views": ["table", "ranked"]}
    # 'ranked' is not a table view: the builder does not raise, the projection says so
    assert project_presentation({"presentation": pres})["status"] == "invalid"


def test_builder_content_keys_and_strays():
    assert P.terminal("out")["map"] == {"stdout": "out"}
    assert P.media("img.url", caption="cap")["map"] == {"url": "img.url", "caption": "cap"}
    assert P.media(b64="png")["map"] == {"b64": "png"}
    stray = P.records("hits", colour="c")
    assert stray["map"]["colour"] == "c"
    assert "presentation.map_key_unknown" in _codes(presentation_problems(stray))
    assert P.composite(P.metric("load", title="Load"), P.log("lines"), title="Host")["parts"][0] == {
        "kind": "metric", "map": {"value": "load"}, "title": "Load"}


# ── DA-01c: lint, coverage, gate ──────────────────────────────────────────────

def test_lint_lists_presentation_problems():
    good = project_contract("a.good", _entry(P.table("quotes"), QUOTES_SCHEMA))
    bad = project_contract("a.bad", _entry({"kind": "table", "map": {"name": "symbol"}}))
    warn = project_contract("a.warn", _entry(P.table("quote"), QUOTES_SCHEMA))
    issues = lint_contracts([good, bad, warn])
    got = [(i["name"], i["code"], i["severity"]) for i in issues if i["code"].startswith("presentation.")]
    assert ("a.bad", "presentation.map_missing", "error") in got
    assert ("a.warn", "presentation.map_unresolved", "warning") in got
    assert not any(n == "a.good" for n, _, _ in got)


def test_coverage_counts_valid_presentations_overall_and_per_family():
    manifests = [project_contract("m.quotes", _entry(P.table("quotes"))),
                 project_contract("m.chart", _entry({"kind": "nope"})),
                 project_contract("m.plain", _entry()),
                 project_contract("w.read", _entry(P.document("text")))]
    result = contract_coverage(manifests)
    assert result["coverage"]["presentation"] == {"declared": 2, "total": 4, "rate": 0.5}
    groups = {g["group"]: g for g in result["groups"]}
    assert groups["m"]["presentation_declared"] == 1 and groups["m"]["presentation_rate"] == 0.3333
    assert groups["w"]["presentation_declared"] == 1
    # not a contract-completeness dimension: the hotspots' missing lists do not change
    assert all("presentation" not in h["missing"] for h in result["hotspots"])


def test_gate_can_require_a_presentation_for_a_migration_set():
    declared = project_contract("m.quotes", _entry(P.table("quotes")))
    missing = project_contract("m.plain", _entry())
    assert gate_contracts([declared], required_dimensions=("presentation",))["ok"] is True
    res = gate_contracts([declared, missing], required_dimensions=("presentation",))
    assert res["ok"] is False
    assert [(i["name"], i["code"]) for i in res["issues"]] == [("m.plain", "gate.declaration_missing")]


# ── DA-01d: the widget hints are declarations on their capabilities ──────────────────────────────────────────

def test_the_former_hints_are_declared_on_their_capabilities():
    import os
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from presentation_declarations import declared_in_source, load_fixture
    source = declared_in_source()
    assert len(source) >= 51
    # the fixture the widget tests feed the page is what the code declares
    assert source == load_fixture()
    for cap, pres in source.items():
        got = project_presentation({"presentation": pres})
        assert got["status"] == "declared", (cap, got.get("problems"))
    # the 51 former hints keep the widget form they named; later declarations (DA-01e) may give the kind alone
    assert sum(1 for p in source.values() if p.get("form")) >= 51


def test_the_hint_tables_are_empty():
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    py = open(os.path.join(root, "vera", "widgets", "widget_cap_output.py"), encoding="utf-8").read()
    js = open(os.path.join(root, "vera", "widgets", "widget_element.js"), encoding="utf-8").read()
    assert "CAP_HINTS: Dict[str, Dict[str, Any]] = {\n}" in py
    assert "  const CAP_HINTS = {};" in js


def test_a_form_declaration_validates_its_own_parts():
    assert presentation_problems({"kind": "status", "form": "status-matrix"}) == []
    assert presentation_problems({"kind": "series", "form": "candles", "map": {"bars": "bars"}}) == []
    assert presentation_problems({"kind": "table", "form": "table", "map": {"rows": "guests"}, "draw": {"columns": ["a"]}}) == []
    assert "presentation.form_invalid" in _codes(presentation_problems({"kind": "status", "form": "Not A Form"}))
    assert "presentation.draw_invalid" in _codes(presentation_problems({"kind": "status", "form": "status", "draw": "x"}))
    assert "presentation.draw_without_form" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r"}, "draw": {}}))
    assert "presentation.form_composite" in _codes(presentation_problems({"kind": "composite", "form": "table", "parts": [P.metric("v")]}))


def test_a_contract_that_only_says_how_it_is_drawn_is_not_a_declared_contract():
    from vera.capability_policy_core import evaluate_policy_shadow
    only = project_contract("x.drawn", {"description": "d", "contract": {"presentation": P.status("ok", form="status")}})
    assert only["declaration"]["status"] == "legacy_projected"
    assert only["presentation"]["status"] == "declared"
    plain = evaluate_policy_shadow("x", {})
    drawn = evaluate_policy_shadow("x", {"presentation": {"kind": "status", "form": "status"}})
    assert plain == drawn


def test_a_series_keyed_by_name_is_a_flag_on_the_series_map():
    # DA-01e: map.keyed true reads map.points as {name: [{t, v}, ...]} - one line per key
    ok = {"kind": "series", "map": {"points": "series", "keyed": True, "t": "t", "v": "ping_ms"}}
    assert presentation_problems(ok) == []
    assert P.series("series", keyed=True, t="t", v="ping_ms") == {"kind": "series", "map": {"points": "series", "t": "t", "v": "ping_ms", "keyed": True}}
    assert "presentation.map_keyed_invalid" in _codes(presentation_problems({"kind": "series", "map": {"points": "s", "keyed": "yes"}}))
    assert "presentation.map_keyed_split" in _codes(presentation_problems({"kind": "series", "map": {"points": "s", "keyed": True, "split": ["a"]}}))
    # a flag is a series word only: another kind does not read it
    assert "presentation.map_key_unknown" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r", "keyed": True}}))
    # the row keys are checked one level down, inside each named series
    schema = {"type": "object", "properties": {"series": {"type": "object", "additionalProperties": {"type": "array", "items": {
        "type": "object", "properties": {"t": {"type": "number"}, "ping_ms": {"type": "number"}}}}}}}
    assert presentation_problems(ok, schema) == []
    off = {"kind": "series", "map": {"points": "series", "keyed": True, "t": "t", "v": "pong"}}
    assert [(p[0], p[1], p[2]) for p in presentation_problems(off, schema)] == [("presentation.map_unresolved", "warning", "presentation.map.v")]


def test_the_timeline_matrix_and_parts_kinds_validate_like_the_rest():
    # DA-01e (1): dated spans (the gantt), rows x columns of a measure (heat map / status matrix), shares of a whole (donut)
    assert presentation_problems({"kind": "timeline", "map": {"spans": "runs", "name": "title", "start": "started_at", "end": "finished_at", "status": "result"}}) == []
    assert presentation_problems({"kind": "matrix", "map": {"cells": "candidates", "row": "label", "entries": "assessments", "column": "dimension", "value": "vera"}}) == []
    assert presentation_problems({"kind": "parts", "map": {"parts": "docker[0]"}}) == []
    assert P.timeline("runs", start="started_at", end="finished_at") == {"kind": "timeline", "map": {"spans": "runs", "start": "started_at", "end": "finished_at"}}
    assert P.matrix("nodes") == {"kind": "matrix", "map": {"cells": "nodes"}}
    assert P.parts("by_type", name="label", value="size", views=["treemap", "donut"])["views"] == ["treemap", "donut"]
    # each needs its content, reads only its own map keys, takes paths and its own views
    assert _codes(presentation_problems({"kind": "timeline", "map": {"start": "t"}})) == ["presentation.map_missing"]
    assert _codes(presentation_problems({"kind": "matrix", "map": {"cells": "c", "lanes": "l"}})) == ["presentation.map_key_unknown"]
    assert _codes(presentation_problems({"kind": "parts", "map": {"parts": "p", "value": 3}})) == ["presentation.map_path_invalid"]
    assert _codes(presentation_problems({"kind": "parts", "map": {"parts": "p"}, "views": ["donut", "trace"]})) == ["presentation.view_unknown"]
    assert project_presentation({"presentation": {"kind": "timeline", "map": {"spans": "runs"}}})["views"] == ["gantt", "stepper", "table"]


def test_the_new_kinds_row_keys_are_checked_where_they_are_read():
    # a matrix's column and value inside each row's entries; a grid's and a dict of parts' rows one level down, by name
    schema = {"type": "object", "properties": {
        "candidates": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "assessments": {
            "type": "array", "items": {"type": "object", "properties": {"dimension": {"type": "string"}, "vera": {"type": "string"}}}}}}},
        "by_type": {"type": "object", "additionalProperties": {"type": "object", "properties": {"size": {"type": "number"}}}}}}
    ok = {"kind": "matrix", "map": {"cells": "candidates", "row": "label", "entries": "assessments", "column": "dimension", "value": "vera"}}
    assert presentation_problems(ok, schema) == []
    off = dict(ok, map=dict(ok["map"], value="upstream"))
    assert [(p[0], p[2]) for p in presentation_problems(off, schema)] == [("presentation.map_unresolved", "presentation.map.value")]
    assert presentation_problems({"kind": "parts", "map": {"parts": "by_type", "value": "size"}}, schema) == []
    assert [p[2] for p in presentation_problems({"kind": "parts", "map": {"parts": "by_type", "value": "bytes"}}, schema)] == ["presentation.map.value"]


def test_the_presentation_options_sort_group_by_and_units():
    # DA-01e (3): draw {sort, group_by, units} - with a row form, or without any form (draw then holds only these)
    ok = {"kind": "table", "map": {"rows": "quotes"}, "draw": {"sort": "-pct", "group_by": "symbol", "units": {"last": "USD", "pct": "%"}}}
    assert presentation_problems(ok) == []
    assert presentation_problems({"kind": "records", "map": {"rows": "hits"}, "draw": {"sort": "title", "units": {"score": "%"}}}) == []
    assert presentation_problems({"kind": "kv", "map": {"fields": "host"}, "draw": {"units": {"uptime_s": "s"}}}) == []
    assert presentation_problems({"kind": "table", "form": "table", "map": {"rows": "r"}, "draw": {"columns": ["a"], "sort": "a", "group_by": "b"}}) == []
    # without a form, draw holds the options only (an empty draw is still a draw without a form)
    assert "presentation.draw_without_form" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r"}, "draw": {"sort": "a", "columns": ["a"]}}))
    assert "presentation.draw_without_form" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r"}, "draw": {}}))
    # each option is read by its kinds only
    assert "presentation.draw_option_kind" in _codes(presentation_problems({"kind": "series", "map": {"points": "p"}, "draw": {"sort": "t"}}))
    assert "presentation.draw_option_kind" in _codes(presentation_problems({"kind": "records", "map": {"rows": "r"}, "draw": {"group_by": "a"}}))
    assert "presentation.draw_option_kind" in _codes(presentation_problems({"kind": "kv", "map": {"fields": "f"}, "draw": {"sort": "a"}}))
    # each in its own shape
    for bad in ("", "-", "--a", 3, ["a"]):
        assert "presentation.draw_sort_invalid" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r"}, "draw": {"sort": bad}})), bad
    for bad in ("", " ", "-a", 3):
        assert "presentation.draw_group_by_invalid" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r"}, "draw": {"group_by": bad}})), bad
    for bad in ("ms", {}, {"a": ""}, {"a": 3}, {"a": "far too long a unit"}, {"": "ms"}):
        assert "presentation.draw_units_invalid" in _codes(presentation_problems({"kind": "table", "map": {"rows": "r"}, "draw": {"units": bad}})), bad
    # another form's draw is its own words: not read as options, not checked as them
    assert presentation_problems({"kind": "kv", "form": "temps", "map": {}, "draw": {"sort": "name"}}) == []
    # the fields against the declared rows: a warning, like a map path
    assert presentation_problems(dict(ok, draw={"sort": "-pct", "group_by": "symbol", "units": {"last": "USD"}}), QUOTES_SCHEMA) == []
    off = {"kind": "table", "map": {"rows": "quotes"}, "draw": {"sort": "-change", "units": {"price": "USD"}}}
    assert [(p[0], p[1], p[2]) for p in presentation_problems(off, QUOTES_SCHEMA)] == [
        ("presentation.draw_unresolved", "warning", "presentation.draw.sort"),
        ("presentation.draw_unresolved", "warning", "presentation.draw.units.price")]
    # the builders fold them into draw
    assert P.table("quotes", sort="-pct", units={"pct": "%"}) == {"kind": "table", "map": {"rows": "quotes"}, "draw": {"sort": "-pct", "units": {"pct": "%"}}}
    assert P.table("quotes", form="table", draw={"columns": ["a"]}, group_by="a")["draw"] == {"columns": ["a"], "group_by": "a"}
