"""Narrator output tiers and the user-vs-background weighting.

Two behaviours the user asked for explicitly:

  · the narrator should still SEE system activity, but not weight it like the
    user's own actions;
  · output length should be tiered rather than one fixed budget, which
    truncated anything substantial and padded anything thin.
"""

import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_SRC = os.path.join(_ROOT, "vera", "dream", "dream_capabilities.py")


def _load(*names):
    with open(_SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = [n for n in tree.body
              if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.name in names)
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) in names for t in n.targets))
              # NARRATOR_TIERS is an ANNOTATED assignment (AnnAssign), which is a
              # different node type — matching only Assign silently missed it.
              or (isinstance(n, ast.AnnAssign)
                  and getattr(n.target, "id", None) in names)]
    from typing import Any, Dict, List, Optional
    ns = {"Dict": Dict, "Any": Any, "List": List, "Optional": Optional}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), _SRC, "exec"), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f"not found in dream_capabilities.py: {missing}"
    return ns


# ── length tiers ─────────────────────────────────────────────────────────────

@pytest.mark.critical
def test_three_tiers_exist_and_increase_in_size():
    ns = _load("NARRATOR_TIERS")
    tiers = ns["NARRATOR_TIERS"]
    assert set(tiers) == {"brief", "standard", "long"}
    assert (tiers["brief"]["tokens"] < tiers["standard"]["tokens"]
            < tiers["long"]["tokens"])
    assert (tiers["brief"]["chars"] < tiers["standard"]["chars"]
            < tiers["long"]["chars"])
    # Every tier must carry prompt guidance, or the model gets a budget with no
    # instruction about how to use it.
    for t in tiers.values():
        assert t["guide"].strip()


@pytest.mark.critical
def test_long_tier_is_actually_long():
    """The old fixed budget cut replies mid-word; 'long' must be a real step up."""
    ns = _load("NARRATOR_TIERS")
    assert ns["NARRATOR_TIERS"]["long"]["tokens"] >= 2000
    assert ns["NARRATOR_TIERS"]["long"]["chars"] >= 8000


@pytest.mark.critical
def test_tier_resolution_and_overrides():
    ns = _load("NARRATOR_TIERS", "_narrator_tier")
    fn = ns["_narrator_tier"]
    assert fn({}, "brief")["name"] == "brief"
    assert fn({}, "nonsense")["name"] == "standard"      # unknown falls back
    assert fn({}, "")["name"] == "standard"
    over = fn({"narrator_tier_long_tokens": 4321}, "long")
    assert over["tokens"] == 4321
    assert over["chars"] == ns["NARRATOR_TIERS"]["long"]["chars"]   # untouched
    # A junk override must not explode or silently zero the budget.
    assert fn({"narrator_tier_long_tokens": "not-a-number"}, "long")["tokens"] > 0


# ── auto tier keys on USER activity, not total noise ─────────────────────────

@pytest.mark.critical
def test_auto_tier_is_brief_when_the_user_did_nothing():
    """A busy background is not a reason to talk more."""
    fn = _load("_narrator_auto_tier")["_narrator_auto_tier"]
    loud_background = {"user": [], "system": ["s"] * 40}
    assert fn(loud_background, recent_takes=0) == "brief"


@pytest.mark.critical
def test_auto_tier_grows_with_real_user_activity():
    fn = _load("_narrator_auto_tier")["_narrator_auto_tier"]
    assert fn({"user": ["a"], "system": []}, recent_takes=3) == "standard"
    assert fn({"user": ["a"] * 8, "system": []}, recent_takes=3) == "long"


# ── the weighting itself ─────────────────────────────────────────────────────

def _block(view):
    return _load("_narrator_activity_block")["_narrator_activity_block"](view)


@pytest.mark.critical
def test_system_activity_is_shown_but_marked_secondary():
    """It must still SEE the system — just not treat it as the user's doing."""
    out = _block({"user": ["- session abc [you] 3 actions: markets.backtest.run"],
                  "system": ["- session dream:x [system:dream] 9 actions: web.fetch"]})
    assert "markets.backtest.run" in out
    assert "web.fetch" in out, "system activity must remain visible"
    assert out.index("USER") < out.index("BACKGROUND"), "user section must come first"
    assert "LOWER" in out.upper()


@pytest.mark.critical
def test_empty_user_activity_forbids_inventing_it():
    """With nothing from the user, the prompt must say so rather than leave a
    vacuum the model fills with background work relabelled as theirs."""
    out = _block({"user": [], "system": ["- session dream:x [system:dream] 9 actions"]})
    low = out.lower()
    assert "nothing recorded" in low
    assert "do not" in low or "never" in low
