"""
The widget record (UI redesign m1 foundations; the WidgetSpec and Sizes boards):
one schema for every widget, accepted in three shapes - the registry's template,
the board's full record, the fence's short form - normalised to the full one and
back to the template without loss; validated at bind time the way the spec
says (source shape = form shape, unknown draw options dropped with a warning,
a missing source is "no source"); a size is a composition and a span picks it.

widget_record.py is pure (no orchestrator), so it is imported straight from
the file.
"""
import importlib.util
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load():
    p = os.path.join(ROOT, "vera", "widgets", "widget_record.py")
    spec = importlib.util.spec_from_file_location("widget_record_under_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = _load()


# ── the catalogue data ────────────────────────────────────────────────────────

def test_the_shapes_and_forms_are_the_spec_boards():
    for s in ("level", "series", "values", "events", "graph", "items", "stages", "rate", "parts", "ohlcv", "matrix",
              "calendar", "string", "points", "panel", "composite"):
        assert s in R.SHAPES, s
    ids = R.form_ids()
    assert len(ids) == len(set(ids)), "form ids are unique"
    assert len(ids) >= 66, "the gallery's sixty-six and the three the spec adds: %d" % len(ids)
    for f in ("panel", "form", "scatter"):
        assert f in ids, f + " is one of the three the spec adds"
    # the nine the chat draws in a reply today, and the registry's eighteen
    for f in ("trace", "radial", "thermo", "heat", "log", "lane", "pipes", "table", "files",
              "counter", "meter", "sparkline", "node", "chart", "graph", "terminal", "program", "iso", "ask",
              "list", "tree", "controls", "button", "header", "rail"):
        assert f in ids, f
    for f in R.FORMS:
        assert f["shape"] in R.SHAPES, f["id"]
        assert set(f["proj"]) <= set(R.PROJECTIONS), f["id"]
        assert set(f["sizes"]) <= set(R.SIZES), f["id"]
    # the boards' captions resolve through the aliases
    assert R.form("hero + trend")["id"] == "hero" and R.form("heat map")["id"] == "heat" and R.form("candlestick")["id"] == "candles"
    assert R.form("nope") is None


def test_a_size_is_a_composition_and_a_span_picks_it():
    assert R.size_for_span(2) == "s" and R.size_for_span(3) == "s"
    assert R.size_for_span(4) == "m"
    assert R.size_for_span(6) == "l" and R.size_for_span(6, 3) == "xl"
    assert R.size_for_span(8) == "xl" and R.size_for_span(12) == "xl"
    assert R.composition("xs").startswith("glyph") and "table" in R.composition("xl")


# ── three shapes in, one out ──────────────────────────────────────────────────

def test_the_short_form_normalises_to_read_and_frame():
    r = R.normalise({"form": "trace", "source": "sysmon.history", "window": "1h", "size": "s", "refresh": "5s", "title": "GPU"})
    assert r["read"]["window"] == "1h" and r["read"]["refresh"] == "5s" and r["frame"]["size"] == "s"
    assert r["shape"] == "series" and r["projection"] == "flat" and r["title"] == "GPU" and r["id"] == "gpu"
    assert r["policy"] == {"agent": "ask"} and r["actions"] == ["dive", "pin", "ask"]


def test_the_registry_template_shape_is_accepted_and_round_trips():
    t = {"id": "gpu-queue", "name": "GPU + queue", "form": "meter", "reads": {"cap": "sysmon.status", "args": {"node": "ct126"}, "every": "10s", "note": "n"},
         "frame": "used / window", "draw": {"form": "meter", "size": "S", "motion": ""}, "can": ["refresh", "pin"], "placed": ["dashboard"],
         "source": {"origin": "you", "from": "", "panel": ""}, "version": 3, "tags": ["ops"]}
    r = R.normalise(t)
    assert r["title"] == "GPU + queue" and r["source"] == "sysmon.status" and r["read"]["args"] == {"node": "ct126"}
    assert r["read"]["refresh"] == "10s" and r["frame"]["size"] == "s" and r["frame"]["note"] == "used / window"
    back = R.to_template(r)
    for k in ("id", "name", "form", "can", "placed", "version", "tags"):
        assert back[k] == t[k], k
    assert back["reads"]["cap"] == "sysmon.status" and back["reads"]["args"] == {"node": "ct126"} and back["reads"]["every"] == "10s"
    assert back["draw"] == {"form": "meter", "size": "S", "motion": ""} and back["frame"] == "used / window"


def test_the_full_record_keeps_its_fields_and_a_span_sets_the_size():
    r = R.normalise({"id": "w-gpu-util", "form": "trace", "projection": "flat", "source": "gpu.util", "title": "GPU utilisation",
                     "read": {"refresh": "5s", "window": "1h", "map": {"series": "gpu.util"}, "args": {"node": "$subject"}},
                     "frame": {"span": [4, 2], "caption": True, "legend": False, "motion": True, "deep_dive": True, "max_body": 290},
                     "draw": {"palette": "load", "bands": [.6, .85]}, "skin": "inherit", "actions": ["dive", "pin", "ask", "ops"],
                     "place": "dashboard", "panel": "system-monitor", "policy": {"agent": "drive"}})
    assert r["frame"]["size"] == "m" and r["frame"]["span"] == [4, 2] and r["frame"]["max_body"] == 290
    assert r["read"]["map"] == {"series": "gpu.util"} and r["draw"] == {"palette": "load", "bands": [.6, .85]}
    assert r["place"] == "dashboard" and r["panel"] == "system-monitor" and r["policy"]["agent"] == "drive"


def test_a_panel_record_names_its_panel_both_ways():
    a = R.normalise({"form": "panel", "source": "panel:system-monitor", "title": "Monitor"})
    b = R.normalise({"form": "panel", "panel": "system-monitor", "title": "Monitor"})
    assert a["panel"] == "system-monitor" and b["source"] == "panel:system-monitor"


def test_the_composite_record():
    r = R.normalise({"id": "c-node", "form": "composite", "subject": "nodes.ct126", "layout": "2x2",
                     "slots": {"a": [0, 0], "b": [1, 0]},
                     "children": [{"slot": "a", "record": {"form": "radial", "source": "$subject.gate.load", "frame": {"size": "s"}}},
                                  {"slot": "b", "record": "w-latency"}],
                     "text": "{{subject}} is serving {{a.value}}", "frame": {"size": "l"}})
    assert r["shape"] == "composite" and r["layout"] == "2x2" and r["subject"] == "nodes.ct126"
    assert r["children"][0]["record"]["form"] == "radial" and r["children"][0]["record"]["frame"]["size"] == "s"
    assert r["children"][1]["record"] == "w-latency" and r["frame"]["size"] == "l"
    rr, problems, _ = R.validate(r)
    assert problems == [], problems      # a composite needs no source of its own


# ── the template rules moved here behave exactly as the registry's did ─────────

def test_normalise_template_and_its_problems_are_the_registrys():
    FORMS = ("counter", "meter", "sparkline", "node", "chart", "graph", "terminal", "table",
             "program", "iso", "ask", "panel", "list", "tree", "controls", "button", "header", "rail")
    WHERES = ("dashboard", "canvas", "LHM", "reply", "notebook", "ops map", "iso plate")
    t = R.normalise_template({"name": "GPU + queue", "form": "meter", "reads": "sysmon.status", "can": "refresh . pin", "placed": ["dashboard", {"where": "LHM"}]})
    assert t["id"] == "gpu-queue" and t["reads"]["cap"] == "sysmon.status" and t["can"] == ["refresh", "pin"] and t["placed"] == ["dashboard", "LHM"]
    assert "a template needs a name" in R.template_problems(R.normalise_template({"form": "meter", "reads": {"cap": "x"}}), FORMS, WHERES)
    assert any("unknown form" in p for p in R.template_problems(R.normalise_template({"name": "x", "form": "blob", "reads": {"cap": "x"}}), FORMS, WHERES))
    assert any("must say what it reads" in p for p in R.template_problems(R.normalise_template({"name": "x", "form": "meter"}), FORMS, WHERES))
    assert R.template_problems(R.normalise_template({"name": "x", "form": "button"}), FORMS, WHERES) == []
    # a catalogue form the registry's own list lacks is valid all the same
    assert R.template_problems(R.normalise_template({"name": "x", "form": "radial", "reads": {"cap": "obs.health"}}), FORMS, WHERES) == []


# ── bind-time validation ──────────────────────────────────────────────────────

def test_validate_checks_shape_source_and_drops_unknown_draw_options():
    r, problems, warnings = R.validate({"form": "trace", "source": "sysmon.history", "title": "x", "draw": {"palette": "load", "wobble": 3}})
    assert problems == [] and r["draw"] == {"palette": "load"}
    assert any("wobble" in w and "dropped" in w for w in warnings)
    # the source's shape must equal the form's
    _, p2, _ = R.validate({"form": "trace", "source": "obs.health", "title": "x"}, source_shape="values")
    assert any("wants series" in p for p in p2)
    _, p3, _ = R.validate({"form": "trace", "source": "sysmon.history", "title": "x"}, source_shape="series")
    assert p3 == []
    # a missing source is a problem, unless the form needs none, or the data rides inline
    assert "no source" in R.validate({"form": "trace", "title": "x"})[1]
    assert R.validate({"form": "header", "title": "x"})[1] == []
    assert R.validate({"form": "trace", "title": "x", "data": [{"t": 1, "v": 2}]})[1] == []
    # unknown form; a form's missing size / projection fall back with a warning
    assert any("unknown form" in p for p in R.validate({"form": "blob", "source": "x"})[1])
    r4, _, w4 = R.validate({"form": "terminal", "source": "sandbox.session.exec", "size": "xs"})
    assert r4["frame"]["size"] == "xl" and any("no xs size" in w for w in w4)
    r5, _, w5 = R.validate({"form": "counter", "source": "obs.pending", "projection": "iso"})
    assert r5["projection"] == "flat" and any("no iso projection" in w for w in w5)
    # a refresh it cannot parse is ignored, with a warning
    r6, _, w6 = R.validate({"form": "trace", "source": "sysmon.history", "refresh": "soonish"})
    assert r6["read"]["refresh"] == "" and any("refresh" in w for w in w6)
