"""DO-0.3 (desk output redesign, "registry truth"): ONE forms list.

The browser's widget element (vera/widgets/widget_element.js) and the python catalogue (vera/widgets/widget_record.py)
name the same forms, with the same shapes, the same aliases, the same capability-output forms and the same Loop Lab
pictures. The element's tables are the drawing truth; this test holds the catalogue to them, so a form added on one side
and not the other fails here instead of drawing raw data on the desk.

- DRAWN (every form the element draws in its own right) == widget_record FORMS without the aliases, shape for shape;
- ALIAS (a name drawn as another form's face) == widget_record.DRAW_AS, and every FORMS entry that is an alias says so;
- every renderer the element has (R.x, not the @iso variants) is in DRAWN, so VeraWidget.forms() lists it;
- DRAWN is one table: no form is added to it later in the file (Object.assign(DRAWN, ...));
- no name is both an alias and a drawn form (status-matrix was: the alias was dead);
- CAP_FORMS and CI_FORMS are the same lists on both sides.
"""
import importlib.util
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = open(os.path.join(ROOT, "vera", "widgets", "widget_element.js"), encoding="utf-8").read()


def _load(name, *rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REC = _load("widget_record_forms_contract", "vera", "widgets", "widget_record.py")
CO = _load("widget_cap_output_forms_contract", "vera", "widgets", "widget_cap_output.py")


def _table(name):
    m = re.search(r"const %s = \{(.*?)\};" % name, SRC, re.S)
    assert m, "const %s = {...}; not found in widget_element.js" % name
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    pairs = re.findall(r"(?:'([\w@-]+)'|([A-Za-z_]\w*))\s*:\s*'([\w@-]+)'", body)
    return {(a or b): v for a, b, v in pairs}


DRAWN = _table("DRAWN")
ALIAS = _table("ALIAS")


def test_the_tables_were_read():
    assert len(DRAWN) >= 150 and len(ALIAS) >= 20, (len(DRAWN), len(ALIAS))


def test_drawn_is_one_table():
    assert "Object.assign(DRAWN" not in SRC, "a form added to DRAWN later in the file: name it in the DRAWN table"


def test_every_renderer_is_a_listed_form():
    renderers = set(re.findall(r"\bR\.([A-Za-z_]\w*)\s*=", SRC)) | set(re.findall(r"\bR\['([\w-]+)'\]\s*=", SRC))
    missing = sorted(renderers - set(DRAWN))
    assert not missing, "renderers VeraWidget.forms() would not list: %s" % missing
    assert "function forms() { return Object.keys(DRAWN)" in SRC


def test_no_name_is_both_alias_and_drawn():
    both = sorted(set(DRAWN) & set(ALIAS))
    assert not both, both
    assert all(v in DRAWN for v in ALIAS.values()), sorted(v for v in ALIAS.values() if v not in DRAWN)


def test_the_catalogue_is_the_elements_list():
    py = {f["id"]: f for f in REC.FORMS}
    drawn = {k: f for k, f in py.items() if not f.get("as")}
    assert set(drawn) == set(DRAWN), {"python only": sorted(set(drawn) - set(DRAWN)), "browser only": sorted(set(DRAWN) - set(drawn))}
    bad = {k: (drawn[k]["shape"], DRAWN[k]) for k in DRAWN if drawn[k]["shape"] != DRAWN[k]}
    assert not bad, "shapes differ (python, browser): %s" % bad
    assert sorted(REC.drawn_ids()) == sorted(DRAWN)
    assert all(s in REC.SHAPES for s in DRAWN.values())


def test_the_aliases_are_one_table():
    assert REC.DRAW_AS == ALIAS
    for f in REC.FORMS:
        if f["id"] in ALIAS:
            assert f.get("as") == ALIAS[f["id"]], f["id"]
    # a record written with an alias resolves to the form the browser draws for it
    assert REC.form("bar")["id"] == "bar" and REC.form("cellmap")["id"] == "cellmap"


def test_cap_forms_and_ci_forms_are_the_same_lists():
    m = re.search(r"const CAP_FORMS = \[([^\]]*)\];", SRC)
    assert m
    assert re.findall(r"'([\w-]+)'", m.group(1)) == list(CO.CAP_FORMS)
    m = re.search(r"const CI_FORMS = /\^\(([^)]*)\)\$/;", SRC)
    assert m
    assert m.group(1).split("|") == list(CO.CI_FORMS)
    assert all(f in DRAWN for f in CO.CAP_FORMS) and all(f in DRAWN for f in CO.CI_FORMS)
