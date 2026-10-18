"""
WSU-58 S8 (DEEP-29 11A.4 Decision 5.3): the widest a widget card of a short-content form is on the desk (WSU-69) moved out of
the canvas element (DESK_FORM_MAXW) into the widget catalogue as maxW per form - here (widget_record.DESK_MAXW, on each FORMS
entry, so widget.forms serves it) and in the browser's catalogue (widget_element.js FORM_MAXW, read by VeraWidget.forms).
The numbers are the element's as they were.
"""
import importlib.util
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
OLD = {"checklist": 420, "list": 440, "links": 440, "notices": 460, "people": 460, "kv": 520, "pills": 480, "string": 460,
       "announcement": 460, "counter": 380, "hero": 400, "ring": 360, "radial": 360, "dial": 360, "gauge": 420, "meter": 420,
       "level": 380, "glance": 460, "numbers": 520, "status": 480, "stepper": 640, "files": 520}


def _rec():
    spec = importlib.util.spec_from_file_location("widget_record_maxw", os.path.join(ROOT, "vera", "widgets", "widget_record.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_catalogue_holds_the_desk_widths_as_they_were():
    assert _rec().DESK_MAXW == OLD


def test_each_form_carries_its_max_width_and_only_those_forms():
    got = {f["id"]: f["maxW"] for f in _rec().FORMS if "maxW" in f}
    assert got == OLD


def test_the_browser_catalogue_holds_the_same():
    js = open(os.path.join(ROOT, "vera", "widgets", "widget_element.js"), encoding="utf-8").read()
    m = re.search(r"const FORM_MAXW = \{([^}]*)\}", js)
    assert m, "widget_element.js has FORM_MAXW"
    got = {k: int(v) for k, v in re.findall(r"([\w-]+):\s*(\d+)", m.group(1))}
    assert got == OLD


def test_the_canvas_element_has_no_table_of_its_own():
    src = open(os.path.join(ROOT, "vera", "canvas", "canvas_element.js"), encoding="utf-8").read()
    assert "DESK_FORM_MAXW" not in src and "const formMaxW = (f) =>" in src
