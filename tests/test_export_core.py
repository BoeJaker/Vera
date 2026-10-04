"""export_core: a reply as a file - its page, its slides, its tables (owner, 2026-10-04: "options for documents,
presentations and spreadsheets ... as many types as you can implement")."""
import importlib.util
import json
import pathlib
import xml.dom.minidom

_P = pathlib.Path(__file__).resolve().parent.parent / "vera" / "render" / "export_core.py"
_spec = importlib.util.spec_from_file_location("export_core", _P)
xc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xc)

MD = """# Quarterly review

Revenue grew **12%**.

## Numbers

| Region | Q1 | Q2 |
|---|---|---|
| North | 1,200 | 1,350 |
| South | 900 | 870 |
| Total | =SUM(B2:B3) | =SUM(C2:C3) |

## Next steps

- Hire two engineers
1. first

Note: speak slowly
"""


def test_every_format_has_a_family_and_a_type():
    assert set(xc.FAMILIES) == {f["family"] for f in xc.FORMATS.values()}
    for fid, f in xc.FORMATS.items():
        assert f["ext"] and f["mime"] and f["label"] and f["via"] in ("py", "lo")
        assert f["via"] == "py" or fid in xc.LO_FILTER, fid


def test_slides_cut_at_headings_or_rules_with_their_notes():
    s = xc.slides_of(MD)
    assert [x["title"] for x in s] == ["Quarterly review", "Numbers", "Next steps"]
    assert s[2]["notes"] == "speak slowly" and "speak" not in s[2]["body"]
    ruled = xc.slides_of("````slides\n# One\nhello\n---\n# Two\n```python\n# not a slide\n---\n```\n````")
    assert [x["title"] for x in ruled] == ["One", "Two"]
    assert "# not a slide" in ruled[1]["body"]


def test_the_slides_are_a_presentation_libreoffice_reads():
    doc = xc.odp_flat(xc.slides_of(MD), "Q")
    xml.dom.minidom.parseString(doc.encode("utf-8"))             # well formed
    assert doc.count("<draw:page ") == 3 and "<table:table>" in doc and ">2100<" in doc   # a real table, totals worked out
    assert 'text:style-name="L2"' in doc                              # numbered items numbered
    deck = xc.deck_html(xc.slides_of(MD), "Q")
    assert deck.count('<section class="s') == 3 and "keydown" in deck


def test_tables_from_fences_and_prose_named_by_their_heading():
    t = xc.tables_of("```csv name=\"Sales\"\na,b\n1,2\n```\n\n" + MD)
    assert [x["name"] for x in t] == ["Sales", "Numbers"]
    assert t[0]["rows"] == [["a", "b"], ["1", "2"]] and t[1]["rows"][0] == ["Region", "Q1", "Q2"]
    assert xc.tables_of("```tsv\nx\ty\n3\t4\n```")[0]["rows"] == [["x", "y"], ["3", "4"]]
    assert xc.tables_of("```sheet\na,b\n1,2")[0]["rows"] == [["a", "b"], ["1", "2"]]      # still being written: what has come


def test_cells_are_read_as_a_spreadsheet_would_and_formulas_worked_out():
    assert xc.cell_value("1,234") == {"number": 1234, "pct": False}
    assert xc.cell_value("12.5%")["number"] == 0.125 and xc.cell_value("(3)")["number"] == -3
    assert xc.cell_value("=SUM(A1:A2)") == {"formula": "=SUM(A1:A2)"} and xc.cell_value("North") == "North"
    vals = xc.evaluate([["n", "m"], ["2", "3"], ["4", "5"], ["=SUM(A2:A3)", "=AVERAGE(B2:B3)*2"], ["=A4+B4", "=MAX(A2:B3)"]])
    assert vals[3] == [6, 8] and vals[4] == [14, 5]
    assert xc.evaluate([["=import os"]])[0][0] == "=import os"       # not arithmetic: left as written


def test_csv_tsv_and_json_from_the_tables():
    t = xc.tables_of(MD)
    assert xc.delimited_text(t, ",").splitlines()[1] == 'North,"1,200","1,350"'
    rows = json.loads(xc.tables_json(t))
    assert rows[0] == {"Region": "North", "Q1": 1200, "Q2": 1350} and rows[2]["Q1"] == "=SUM(B2:B3)"


def test_the_page_is_standalone_and_its_fence_is_chrome():
    page = xc.document_html("```document\n# Memo\nHello **you**\n```")
    assert page.startswith("<!doctype html>") and "<title>Memo</title>" in page and "<b>you</b>" in page or "<strong>you</strong>" in page
    assert "```" not in xc.plain_text("# T\n```py\nx\n```") and xc.first_heading(MD) == "Quarterly review"
