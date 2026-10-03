"""G3 (2026-10-03): with the intent core on, the intent is decided and the catalogue
reordered BEFORE the tier pass, whose brief reads only the first 24 caps."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def _block():
    i = SRC.index("# ── INTENT CORE before the TIER brief")
    return SRC[i:SRC.index("tier_info = await _v7_decide_tier(", i)]


def test_intent_decided_and_core_applied_before_the_tier_brief():
    b = _block()
    assert "if enable_tiering and _ic_mode != \"off\":" in b
    assert b.index("_v7_decide_intent(") < b.index("_intent_core.apply(") < b.index("_tier_catalog_brief")
    assert '"before_tier": True' in b and "_ic_applied = True" in b


def test_no_double_intent_call_and_no_double_apply():
    after = SRC[SRC.index("tier_info = await _v7_decide_tier("):]
    assert "if not _ic_applied:          # decided before the tier pass" in after
    assert 'if _ic_mode != "off" and not _ic_applied:' in after


def test_off_is_unchanged():
    b = _block()
    # with the core off nothing new runs before the tier pass
    assert b.index("if enable_tiering and _ic_mode != \"off\":") < b.index("_v7_decide_intent(")
