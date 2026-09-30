"""A census schedule can force the loop's intent core (roadmap G, user 2026-09-30:
"add the intent-core testing to the schedule")."""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import schedule_core as S  # noqa: E402


def _census(**target):
    return S._normalize_target("census", dict(template="intent-core-probe", **target))


def test_intent_core_is_validated_and_off_is_the_baseline():
    assert _census(intent_core="order")["intent_core"] == "order"
    assert _census(intent_core=" ORDER ")["intent_core"] == "order"
    assert _census(intent_core="off")["intent_core"] == ""
    assert _census()["intent_core"] == ""
    with pytest.raises(ValueError):
        _census(intent_core="trim")


def test_the_title_names_it():
    t = _census(intent_core="order")
    assert S._default_title("census", t) == "Census · intent-core-probe · intent core order"
    assert S._default_title("census", _census()) == "Census · intent-core-probe"


def test_the_launch_passes_it_to_the_harness_and_never_inherits_it(monkeypatch, tmp_path):
    try:
        from Vera.vera.evolve import schedule_capabilities as SK
    except Exception:                                # pragma: no cover
        pytest.skip("app module not importable here")
    if "intent_core" not in SK._launch_census_sync.__code__.co_varnames:
        pytest.skip("app module resolves to a checkout without this change")
    seen = {}

    class P:
        pid = 1

        def __init__(self, args, cwd=None, env=None, **kw):
            seen["env"] = env

        def poll(self):
            return None
    (tmp_path / "census_all.state").write_text("running x", encoding="utf-8")
    monkeypatch.setattr(SK, "_CENSUS_DIR", tmp_path)
    monkeypatch.setattr(SK, "_clear_own_control_sync", lambda: None)
    monkeypatch.setattr(SK.subprocess, "Popen", P)
    monkeypatch.setattr(SK.time, "sleep", lambda s: None)
    monkeypatch.setenv("CENSUS_INTENT_CORE", "order")
    SK._launch_census_sync("intent-core-probe")
    assert "CENSUS_INTENT_CORE" not in seen["env"]
    SK._launch_census_sync("intent-core-probe", "", "order")
    assert seen["env"]["CENSUS_INTENT_CORE"] == "order"
    assert seen["env"]["CENSUS_TEMPLATES"] == "intent-core-probe"
