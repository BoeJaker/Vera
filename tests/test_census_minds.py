"""A census arm runs its switchboard set (CENSUS_MINDS contract v1, four-minds ROADMAP section 4.1)."""
import hashlib
import importlib.util
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.critical


def _rc(monkeypatch, tmp_path):
    monkeypatch.setenv("VERA_CENSUS_DIR", str(tmp_path))
    monkeypatch.delenv("CENSUS_MINDS", raising=False)
    spec = importlib.util.spec_from_file_location("run_census_t", ROOT / "tools" / "loop-census" / "run_census.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_parse_the_arm(monkeypatch, tmp_path):
    rc = _rc(monkeypatch, tmp_path)
    assert rc.parse_minds("", "", "default") == ({}, "")
    assert rc.parse_minds("{}", "", "default") == ({}, "")
    assert rc.parse_minds('{"b.x":"on","a.y":"first"}', "student", "default") == (
        {"a.y": "first", "b.x": "on"}, "student")
    for raw, label in (('{"a":"b"}', ""), ('{"a":"b"}', "Bad_Label"), ('{"a":"b"}', "style-x"),
                       ('{"a":"b"}', "core-order"), ('{"a":"b"}', "default"), ("[1]", "x"), ("{nope", "x"),
                       ('{"a": 1}', "x"), ('{"a":"b"}', "x" * 25)):
        with pytest.raises(ValueError):
            rc.parse_minds(raw, label, "default")


def test_digest_matches_the_switchboard(monkeypatch, tmp_path):
    rc = _rc(monkeypatch, tmp_path)
    from vera.minds import switchboard_core as SB
    m = {"decide.student.step_verify": "first", "context.needs_web": "rules"}
    assert rc.minds_digest(m) == SB.digest(m) and rc.minds_digest({}) == "default" == SB.digest({})


def test_series_names_keep_their_order_and_old_names(monkeypatch, tmp_path):
    rc = _rc(monkeypatch, tmp_path)
    assert rc.series_name("default") == "default"
    assert rc.series_name("default", "stepwise") == "default-style-stepwise"
    assert rc.series_name("default", "", "order") == "default-core-order"
    assert rc.series_name("default", "auto", "order", "student") == "default-style-auto-core-order-student"
    assert rc.series_name("default", "", "off", "student") == "default-student"


def test_the_harness_sends_validates_and_records_the_set():
    src = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    assert main.index('mcp("minds.validate", {"minds": _minds, "census": True}') < main.index("reserve_census_run(")
    assert "sys.exit(2)" in main
    assert 'if MINDS:                                   # a census arm' in src and '_args["minds"] = dict(MINDS)' in src
    for key in ('"minds_requested": dict(MINDS)', '"minds_digest": MINDS_DIGEST', '"minds_label": MINDS_LABEL if MINDS else ""'):
        assert key in src
    sched = (ROOT / "vera" / "evolve" / "schedule_capabilities.py").read_text(encoding="utf-8")
    assert 'env["CENSUS_MINDS"]' in sched and 'env["CENSUS_MINDS_LABEL"]' in sched     # what the scheduler sets
