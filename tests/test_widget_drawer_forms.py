"""
The widget review, round 3 (python side): the calendar panel's parts and the graphs are catalogue forms and built-in
templates - placeable from the widget sheet on dashboards, the canvas and LHM menus - and every template is valid.
"""
import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, *rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


REC = _load("widget_record", "vera", "widgets", "widget_record.py")


def test_the_new_forms_are_in_the_catalogue():
    for fid, shape in (("month", "calendar"), ("schedule", "calendar"), ("calnav", "calendar"), ("vgraph", "graph")):
        f = REC.form(fid)
        assert f and f["shape"] == shape and "widgets" in f["boards"], fid
    assert "calnav" in REC.NO_SOURCE_FORMS, "the controls read nothing: they drive the calendars of their group"
    assert REC.form("vgraph")["options"] == ["mode", "layer"]


def test_the_builtins_name_the_calendar_and_the_graphs_and_are_valid():
    src = open(os.path.join(ROOT, "vera", "widgets", "widget_registry.py"), encoding="utf-8").read()
    for tid, form, cap in (("cal:month", "month", "cal.events.list"), ("cal:schedule", "schedule", "cal.events.list"),
                           ("cal:controls", "calnav", ""), ("cal:todos", "checklist", "cal.todos.list"),
                           ("graph:fabric", "vgraph", "fabric.graphs.snapshot"), ("graph:memory", "vgraph", "memory.graph_full"),
                           ("graph:topology", "vgraph", "topology.snapshot"), ("graph:mesh", "vgraph", "mesh.topology")):
        assert '"id": "%s"' % tid in src and '"form": "%s"' % form in src, tid
        if cap:
            assert '"cap": "%s"' % cap in src, tid
    assert '"args": {"start": "@month_start", "end": "@month_end"}' in src and '"args": {"start": "@today", "end": "@today+14d"}' in src
    # the same rules the registry applies on save: a known form, a source unless the form needs none, known placements
    import re
    block = src[src.index('{"id": "cal:month"'):src.index("]\n\n\ndef _builtin_lhm")]
    for m in re.finditer(r'"form": "([a-z_-]+)",\s*\n\s*"reads": \{"cap": "([a-z_.]*)"', block):
        form, cap = m.group(1), m.group(2)
        assert REC.form(form), form
        assert cap or form in REC.NO_SOURCE_FORMS, form
