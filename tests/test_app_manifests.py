"""DA-05a: app manifests (vera/apps/<panel>.json) - the format of DESK-SPINE-DESIGN.md §4.1, checked by
vera/apps/app_manifest_core.py (ui.apps.lint / list / get). Offline: the capability names come from the decorators in the
source, so a manifest naming a capability that does not exist fails here, not on the desk."""
import json
import os
import re

import pytest

from vera.apps import app_manifest_core as A

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _caps_in_source() -> set:
    names = set()
    for base, _, files in os.walk(os.path.join(ROOT, "vera")):
        for f in files:
            if f.endswith(".py"):
                try:
                    src = open(os.path.join(base, f), encoding="utf-8").read()
                except (OSError, UnicodeDecodeError):
                    continue
                names.update(re.findall(r"@capability\(\s*\n?\s*[\"']([\w.]+)[\"']", src))
    return names


GOOD = {"id": "markets.overview", "panel": "markets", "title": "Markets overview", "summary": "by group",
        "view": {"kind": "table", "views": ["table", "cards"]},
        "read": {"cap": "markets.overview", "args": {}, "refresh": {"every_s": 60}},
        "live": {"event": "markets.tick", "key": "symbol", "patch": "row"},
        "actions": [{"id": "remove", "label": "Remove", "cap": "markets.overview", "args": {"symbol": "{row.symbol}"},
                     "where": "row", "undo": "markets.overview"}],
        "detail": {"app": "markets.symbol", "args": {"symbol": "{row.symbol}"}}, "related": ["markets.chart"],
        "size": {"default": "m", "min": "s"}, "settings": [{"key": "columns", "label": "Columns", "type": "multi"}]}


def codes(m, **kw):
    return {p["code"] for p in A.check_manifest(m, "t.json", **kw)}


def test_the_repo_manifests_are_clean_and_name_real_capabilities():
    r = A.lint(caps=_caps_in_source())
    assert r["apps"] >= 5 and r["files"] >= 3
    assert r["errors"] == 0 and r["warnings"] == 0, r["problems"]


def test_a_full_manifest_is_valid():
    assert not A.check_manifest(GOOD, "t.json", caps={"markets.overview", "markets.chart"}, panels={"markets"})


@pytest.mark.parametrize("change,code", [
    ({"id": "Markets"}, "id.invalid"),
    ({"panel": ""}, "panel.missing"),
    ({"title": ""}, "title.missing"),
    ({"view": {"kind": "receipt"}}, "view.kind"),
    ({"view": {"kind": "nope"}}, "view.kind"),
    ({"read": None}, "read.missing"),
    ({"read": {"args": {}}}, "read.cap"),
    ({"read": [{"cap": "markets.overview"}, {"cap": "markets.overview", "as": "a"}]}, "read.as"),
    ({"read": {"cap": "markets.overview", "refresh": {"every_s": 0}}}, "read.refresh"),
    ({"live": {"event": "x", "patch": "row"}}, "live.key"),
    ({"live": {"event": "x", "patch": "merge"}}, "live.patch"),
    ({"actions": [{"id": "a", "label": "A", "cap": "markets.overview", "where": "side"}]}, "action.where"),
    ({"actions": [{"id": "a", "label": "A", "cap": "markets.overview"}, {"id": "a", "label": "B", "cap": "markets.overview"}]}, "action.id.duplicate"),
    ({"actions": [{"id": "a", "label": "A", "cap": "markets.overview", "form": "dialog"}]}, "action.form"),
    ({"detail": {"args": {}}}, "detail"),
    ({"size": {"default": "huge"}}, "size"),
    ({"settings": [{"key": "c", "label": "C", "type": "slider"}]}, "setting"),
])
def test_each_rule_is_named(change, code):
    m = dict(GOOD)
    m.update(change)
    assert code in codes(m)


def test_capabilities_and_panels_are_checked_when_known():
    assert "read.cap.unknown" in codes(GOOD, caps={"markets.chart"})
    assert "action.cap.unknown" in codes(GOOD, caps={"x"})
    assert "panel.unknown" in codes(GOOD, panels={"calendar-panel"})


def test_warnings_are_warnings():
    m = dict(GOOD, view={"kind": "table", "views": ["table", "ranked"]}, read={"cap": "markets.overview", "refresh": {"every_s": 2}},
             actions=[{"id": "a", "label": "A", "cap": "markets.overview", "where": "row"}])
    ps = A.check_manifest(m, "t.json")
    assert {p["code"] for p in ps} == {"view.views.unknown", "read.refresh.fast", "action.unguarded"}
    assert all(p["level"] == "warning" for p in ps)


def test_needs_args_from_arg_templates():
    assert A.needs_args({"read": {"cap": "markets.chart", "args": {"symbol": "{arg.symbol}", "tf": "1d"}}}) == ["symbol"]
    assert A.needs_args({"read": [{"cap": "a", "as": "x", "args": {"h": "{arg.host}"}}, {"cap": "b", "as": "y", "args": {"h": "{arg.host}"}}]}) == ["host"]
    assert A.needs_args(GOOD) == []


def test_optional_arg_templates_are_not_needed():
    # {arg.x?}: optional - not listed by needs_args, listed by optional_args, accepted by the validator
    single = {"id": "x.y", "panel": "markets", "title": "T", "view": {"kind": "table"},
              "read": {"cap": "tg.history", "args": {"chat_id": "{arg.chat_id?}", "q": "{arg.q}", "limit": 20}}}
    assert A.needs_args(single) == ["q"] and A.optional_args(single) == ["chat_id"]
    assert not A.check_manifest(single, "t.json", caps={"tg.history"})
    comp = {"id": "x.z", "panel": "markets", "title": "T", "view": {"kind": "composite", "layout": "dashboard"},
            "read": [{"as": "a", "cap": "cal.events.list", "args": {}}, {"as": "b", "cap": "tg.history", "args": {"chat_id": "{arg.chat_id?}"}}]}
    assert A.needs_args(comp) == [] and A.optional_args(comp) == ["chat_id"]
    assert not A.check_manifest(comp, "t.json", caps={"cal.events.list", "tg.history"})
    both = dict(single, read={"cap": "tg.history", "args": {"a": "{arg.chat_id?}", "b": "{arg.chat_id}"}})
    assert A.needs_args(both) == ["chat_id"] and A.optional_args(both) == []   # required somewhere wins
    s = A.summary(comp)
    assert s["needs_args"] == [] and s["optional_args"] == ["chat_id"] and "optional_args" not in A.summary(GOOD)


def test_the_briefing_reads_telegram_only_when_given_a_chat():
    apps, _ = A.load()
    b = next(m for m in apps if m["id"] == "calendar.briefing")
    assert A.needs_args(b) == [] and A.optional_args(b) == ["chat_id"]
    assert [r["as"] for r in b["read"]] == ["today", "inbox", "telegram"] and b["view"]["layout"] == "dashboard"


def test_files_ids_and_panels(tmp_path):
    (tmp_path / "markets.json").write_text(json.dumps([GOOD, dict(GOOD, title="Again")]), encoding="utf-8")
    (tmp_path / "calendar-panel.json").write_text(json.dumps([dict(GOOD, id="calendar.x")]), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    r = A.lint(str(tmp_path))
    got = {p["code"] for p in r["problems"]}
    assert {"id.duplicate", "panel.file", "file.json"} <= got and not r["ok"]


def test_list_summary_shape():
    apps, _ = A.load()
    s = A.summary(next(m for m in apps if m["id"] == "markets.symbol"))
    assert s == {"id": "markets.symbol", "panel": "markets", "title": "Symbol", "summary": s["summary"], "icon": "chart",
                 "kind": "series", "needs_args": ["symbol"]}


def test_an_action_says_what_it_does_and_how_a_removal_is_put_back():
    # DA-08a: effect (remove / create / update) and undo_args, the inverse's arguments from the row
    def issues(a):
        m = {"id": "x.y", "panel": "markets", "title": "T", "read": {"cap": "a.b.list"}, "view": {"kind": "table"}, "actions": [a]}
        return [(i.get("level") or i.get("severity"), i["code"]) for i in A.check_manifest(m, "f.json")]
    base = {"id": "rm", "label": "Remove", "cap": "a.b.remove", "where": "row", "confirm": True}
    ok = issues(dict(base, effect="remove", undo="a.b.add", undo_args={"k": "{row.k}"}))
    assert not [c for s, c in ok if s == "error"]
    assert ("error", "action.effect") in issues(dict(base, effect="explode"))
    assert ("error", "action.undo_args") in issues(dict(base, undo="a.b.add", undo_args="k"))
    assert ("warning", "action.undo_args.orphan") in issues(dict(base, undo_args={"k": "v"}))


def test_a_composite_names_its_parts_and_lays_them_out():
    # DA-07a: read is a list of {as, cap}; view.layout one the window draws; each part names a read
    reads = [{"as": "quote", "cap": "markets.quote"}, {"as": "chart", "cap": "markets.chart"}]
    def got(view, read=reads):
        m = {"id": "markets.symbolx", "panel": "markets", "title": "Symbol", "read": read, "view": dict({"kind": "composite"}, **view)}
        return {(p["level"], p["code"]) for p in A.check_manifest(m, "f.json")}
    assert got({"layout": "dashboard", "parts": [{"from": "quote", "kind": "metric", "title": "Price"}, {"from": "chart", "kind": "series"}]}) == set()
    assert got({}) == set()   # layout and parts are optional: a dashboard of every read
    assert ("error", "view.composite.read") in got({}, read={"cap": "markets.quote"})
    assert ("error", "view.layout") in got({"layout": "grid"})
    assert ("error", "view.parts") in got({"parts": "quote"})
    assert ("error", "view.parts.from") in got({"parts": [{"kind": "metric"}]})
    assert ("error", "view.parts.unknown") in got({"parts": [{"from": "news"}]})
    assert ("error", "view.parts.kind") in got({"parts": [{"from": "quote", "kind": "receipt"}]})
    assert got({"parts": [{"from": "quote", "kind": "table", "columns": ["symbol", "last"]}]}) == set()   # DA-07c
    assert ("error", "view.parts.columns") in got({"parts": [{"from": "quote", "columns": "symbol"}]})
    assert ("error", "view.parts.columns") in got({"parts": [{"from": "quote", "columns": []}]})
    assert ("warning", "view.layout.split") in got({"layout": "split"}, read=reads + [{"as": "news", "cap": "info.seek"}])


def test_a_form_from_the_capabilitys_own_inputs():
    # form: "schema" with no fields: the fields come from the capability's input schema, minus what the action fills
    schema = {"type": "object", "required": ["symbol"], "properties": {
        "exchange": {"type": "string", "default": "binance", "description": "Where it trades"},
        "symbol": {"type": "string"}, "backfill": {"type": "boolean", "default": True},
        "days": {"type": "integer"}, "tf": {"type": "string", "enum": ["1h", "1d"], "default": "1d"},
        "opts": {"type": "object"}, "trace_id": {"type": "string"}, "row_id": {"type": "string"}}}
    f = A.schema_fields(schema, {"row_id": "{row.id}"})
    assert [x["key"] for x in f] == ["symbol", "exchange", "backfill", "days", "tf"]   # required first; filled / carried / structured left out
    assert f[0] == {"key": "symbol", "label": "Symbol", "required": True}
    assert f[1]["default"] == "binance" and f[1]["title"] == "Where it trades"
    assert f[2]["type"] == "checkbox" and f[2]["default"] is True
    assert f[3]["type"] == "number" and f[4] == {"key": "tf", "label": "Tf", "type": "select", "options": ["1h", "1d"], "default": "1d"}
    assert A.schema_fields(None) == [] and A.schema_fields({"properties": "x"}) == []
    app = {"id": "x.y", "actions": [{"id": "a", "form": "schema", "cap": "c.add", "args": {"days": 3}},
                                    {"id": "b", "form": "schema", "cap": "c.add", "fields": [{"key": "symbol"}]},
                                    {"id": "c", "cap": "c.add"}]}
    served = A.with_schema_forms(app, lambda cap: schema)
    assert [x["key"] for x in served["actions"][0]["fields"]] == ["symbol", "exchange", "backfill", "tf", "row_id"]   # days is filled by args
    assert served["actions"][1]["fields"] == [{"key": "symbol"}] and "fields" not in served["actions"][2]   # written fields win
    assert "fields" not in app["actions"][0]   # the manifest itself is not changed
    assert A.with_schema_forms(app, lambda cap: 1 / 0)["actions"][0]["fields"] == []   # a schema it cannot get: no fields, no failure


def test_a_composite_joins_its_parts_by_field():
    # DA-07b: view.join pairs "as.field" sides of two different reads
    reads = [{"as": "hosts", "cap": "nodes.list"}, {"as": "containers", "cap": "docker.ps"}]
    def got(join):
        m = {"id": "estate.hostx", "panel": "estate", "title": "Host", "read": reads, "view": {"kind": "composite", "join": join}}
        return {(p["level"], p["code"]) for p in A.check_manifest(m, "f.json")}
    assert got({"hosts.name": "containers.host"}) == set()
    assert got([{"hosts.name": "containers.host"}, ["hosts.ip", "containers.ip"]]) == set()
    assert ("error", "view.join") in got("hosts.name")
    assert ("error", "view.join") in got({"hosts": "containers.host"})
    assert ("error", "view.join") in got([["hosts.name"]])
    assert ("error", "view.join.same") in got({"hosts.name": "hosts.ip"})
    assert ("error", "view.join.unknown") in got({"hosts.name": "logs.host"})
