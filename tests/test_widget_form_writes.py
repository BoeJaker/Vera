"""A form that saves (widget_record.form_problems / validate): only through the allowed capabilities."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.widgets import widget_record as wr  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _rec(draw):
    return {"form": "form", "title": "Agent", "source": "agent.get", "read": {"args": {"name": "aide"}}, "draw": draw}


def test_an_agent_editor_record_validates():
    rec, problems, warnings = wr.validate(_rec({
        "fields": [{"key": "system_prompt", "type": "textarea"}, {"key": "temperature", "type": "number"},
                   {"key": "memory_inject", "type": "bool"}, {"key": "domain_caps", "type": "list"}],
        "save": {"cap": "agent.patch", "from": {"agent_id": "id"}, "edits_as": "fields"}}))
    assert problems == [], problems
    assert rec["draw"]["save"]["cap"] == "agent.patch" and len(rec["draw"]["fields"]) == 4   # not dropped as unknown


def test_a_form_may_not_save_through_anything_else():
    _, problems, _ = wr.validate(_rec({"fields": [{"key": "x"}], "save": {"cap": "exec.bash.run"}}))
    assert any("may not save through exec.bash.run" in p for p in problems)
    _, problems, _ = wr.validate(_rec({"fields": [{"key": "x"}], "save": {}}))
    assert any("needs a cap" in p for p in problems)


def test_bad_fields_are_named():
    _, problems, _ = wr.validate(_rec({"fields": [{"label": "no key"}, {"key": "k", "type": "colour"}]}))
    assert any("fields[0] needs a key" in p for p in problems)
    assert any("fields[1] type 'colour'" in p for p in problems)


def test_a_form_without_fields_is_what_it_was():
    _, problems, _ = wr.validate({"form": "form", "title": "values", "data": {"a": 1}})
    assert problems == []


def test_the_element_allows_exactly_the_same_capabilities():
    js = open(os.path.join(ROOT, "vera", "widgets", "widget_element.js"), encoding="utf-8").read()
    m = re.search(r"const FORM_WRITES = new Set\(\[([^\]]*)\]\)", js)
    assert m, "FORM_WRITES not found in widget_element.js"
    in_js = set(re.findall(r"'([^']+)'", m.group(1)))
    assert in_js == set(wr.FORM_WRITES)
