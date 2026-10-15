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
