"""The four-minds switchboard: registry, strict validation, layered resolution, kill switch,
and that every switch the code reads is registered (no behaviour without a switch)."""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical

F = SB.Feature
REG = {f.id: f for f in (
    F("a.x", ("off", "on"), "loop", "FM-T"),
    F("b.y", ("llm_only", "shadow", "student_first"), "loop", "FM-T"),
    F("c.z", ("on", "off"), "research", "FM-T", census=False),
)}


def test_digest_is_default_for_empty_and_stable_otherwise():
    assert SB.digest({}) == SB.digest(None) == "default"
    d1 = SB.digest({"a.x": "on", "b.y": "shadow"})
    d2 = SB.digest({"b.y": "shadow", "a.x": "on"})
    assert d1 == d2 and len(d1) == 12


def test_validate_fails_closed():
    v = SB.validate({"a.x": "on", "nope": "on", "b.y": "bogus"}, REG)
    assert not v["ok"] and v["unknown_ids"] == ["nope"]
    assert v["bad_modes"][0]["id"] == "b.y" and v["digest"] == "invalid"
    assert "unknown ids: nope" in SB.explain(v)
    assert not SB.validate("not a dict", REG)["ok"]
    assert SB.validate({}, REG) == {"ok": True, "unknown_ids": [], "bad_modes": [], "not_census": [], "digest": "default"}


def test_census_may_not_set_non_census_switches():
    assert SB.validate({"c.z": "off"}, REG)["ok"]
    assert SB.validate({"c.z": "off"}, REG, census=True)["not_census"] == ["c.z"]


def test_resolution_order_call_arm_session_global_default():
    eff = SB.resolve(call={"a.x": "on"}, arm={"b.y": "shadow"},
                     session={"b.y": "student_first"}, global_={"c.z": {"mode": "off"}}, registry=REG)
    assert eff["modes"] == {"a.x": "on", "b.y": "shadow", "c.z": "off"}
    assert eff["source"] == {"a.x": "call", "b.y": "arm", "c.z": "global"}
    assert SB.resolve(registry=REG)["modes"] == {"a.x": "off", "b.y": "llm_only", "c.z": "on"}


def test_stale_global_entries_are_ignored_not_fatal():
    eff = SB.resolve(global_={"gone.feature": "on", "a.x": "weird", "b.y": "shadow"}, registry=REG)
    assert eff["modes"]["b.y"] == "shadow" and eff["modes"]["a.x"] == "off"
    assert set(eff["ignored"]) == {"gone.feature=on", "a.x=weird"}


def test_kill_switch_puts_globals_back_to_default_but_not_explicit_runs():
    g = {"*": {"mode": "default"}, "a.x": "on", "b.y": "student_first"}
    eff = SB.resolve(global_=g, registry=REG)
    assert eff["killed"] and eff["modes"] == {"a.x": "off", "b.y": "llm_only", "c.z": "on"}
    assert SB.resolve(call={"a.x": "on"}, global_=g, registry=REG)["modes"]["a.x"] == "on"


def test_mode_of_reads_the_run_context_and_defaults():
    tok = SB.RUN_MINDS.set(SB.resolve(call={"a.x": "on"}, registry=REG))
    try:
        assert SB.mode_of("a.x", registry=REG) == "on"
        assert SB.mode_of("b.y", registry=REG) == "llm_only"
    finally:
        SB.RUN_MINDS.reset(tok)
    assert SB.mode_of("a.x", registry=REG) == "off"
    with pytest.raises(KeyError):
        SB.mode_of("unregistered", registry=REG)


def test_registry_refuses_security_and_malformed_entries():
    with pytest.raises(ValueError):
        SB._check_registry({"s": F("s", ("off", "on"), "loop", "X", security=True)})
    with pytest.raises(ValueError):
        SB._check_registry({"d": F("d", ("on", "on"), "loop", "X")})


def test_real_registry_is_well_formed():
    SB._check_registry(SB.REGISTRY)
    assert SB.REGISTRY["fix.worldview_source_gate"].default == "on"


def test_every_switch_read_in_the_code_is_registered():
    """No behaviour may read a switch the registry does not know (four-minds §4)."""
    pat = re.compile(r"""mode_of\(\s*["']([a-z0-9_.]+)["']""")
    missing = set()
    for base, _dirs, files in os.walk(os.path.join(ROOT, "vera")):
        for name in files:
            if name.endswith(".py") and name != "switchboard_core.py":
                src = open(os.path.join(base, name), encoding="utf-8", errors="ignore").read()
                for fid in pat.findall(src):
                    if fid not in SB.REGISTRY:
                        missing.add(fid)
    assert not missing, f"switches read but not registered: {sorted(missing)}"


def test_the_loop_validates_minds_before_any_work():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_dag_agent_loop_v6")
    assert "minds" in [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    body = ast.get_source_segment(src, fn)
    i_validate = body.index("_minds_core.validate(")
    assert i_validate < body.index("RUN_ROLE_OVERRIDES.set") and '"minds": _minds_rec' in body
