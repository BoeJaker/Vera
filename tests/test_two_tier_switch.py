"""A feature with no switch is a feature nobody can use.

The two-tier reply shipped with the mechanism, the tests and the plumbing, and
no way to turn it on: the AgentRecord had no field for it, so `getattr(agent,
"two_tier", None)` was always None, and neither the agent settings nor the chat
UI offered a control. It defaulted to off and there was no path to anything else.

These tests hold the whole chain: record field -> persisted -> agent settings
form -> chat request body. Breaking any link puts it back to unreachable.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import two_tier as tt  # noqa: E402

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ── the record can hold the setting ─────────────────────────────────────────

def test_the_agent_record_has_both_fields():
    src = _read("vera", "agents", "agents.py")
    assert re.search(r"^\s*two_tier:\s*str\s*=\s*\"off\"", src, re.M)
    assert re.search(r"^\s*two_tier_decider:\s*str\s*=\s*\"tier2\"", src, re.M)


def test_the_record_defaults_to_off():
    """Opt-in: a split that fired by default would change every existing chat."""
    src = _read("vera", "agents", "agents.py")
    assert 'two_tier:               str  = "off"' in src


def test_the_settings_survive_a_save_and_reload():
    """Without this the field exists but resets on every load."""
    src = _read("vera", "agents", "agents.py")
    assert "two_tier               =_d('two_tier', 'off')" in src
    assert "two_tier_decider       =_d('two_tier_decider', 'tier2')" in src


def test_the_defaults_agree_with_the_module():
    """Three places name a default; they must not drift apart."""
    src = _read("vera", "agents", "agents.py")
    assert f'two_tier_decider:       str  = "{tt.DEFAULT_DECIDER}"' in src
    assert f'two_tier:               str  = "{tt.DEFAULT_LEVEL}"' in src


# ── the agent settings form ─────────────────────────────────────────────────

def test_the_agent_panel_offers_every_level_and_decider():
    html = _read("vera", "agents", "agent_panel.html")
    for lvl in tt.LEVELS:
        assert f'<option value="{lvl}"' in html, lvl
    for dec in tt.DECIDERS:
        assert f'<option value="{dec}"' in html, dec


def test_the_agent_panel_loads_saves_and_defaults_the_setting():
    html = _read("vera", "agents", "agent_panel.html")
    assert "document.getElementById('f-two_tier').value=a.two_tier||'off'" in html
    assert "two_tier:fv('f-two_tier')" in html
    assert "two_tier:'off',two_tier_decider:'tier2'" in html


# ── the chat side ───────────────────────────────────────────────────────────

def test_the_chat_sends_the_setting_with_the_turn():
    html = _read("vera", "chat", "chat_panel.html")
    assert "two_tier:document.getElementById('cfgTwoTier')?.value||undefined" in html
    assert "two_tier_decider:document.getElementById('cfgTwoTierDecider')?.value||undefined" in html


def test_a_blank_chat_choice_leaves_it_to_the_agent():
    """`||undefined` is what makes the chat control an override for this turn
    rather than a permanent setting - sending "" would force it off."""
    html = _read("vera", "chat", "chat_panel.html")
    body = html[html.index("two_tier:document.getElementById('cfgTwoTier')"):]
    assert body[:200].count("undefined") >= 2
    assert '<option value="">Agent default</option>' in html


def test_the_chat_controls_exist():
    html = _read("vera", "chat", "chat_panel.html")
    assert 'id="cfgTwoTier"' in html and 'id="cfgTwoTierDecider"' in html


def test_the_chat_mirrors_the_agents_setting():
    """Otherwise the chat view shows a default that disagrees with the agent."""
    html = _read("vera", "chat", "chat_panel.html")
    assert "two_tier:a.two_tier||'off'" in html
    assert "two_tier:fresh.two_tier||'off'" in html


def test_both_controls_are_explained():
    """A select with three opaque words is not a switch anyone can use."""
    html = _read("vera", "chat", "chat_panel.html")
    assert "cfgTwoTier:'" in html and "cfgTwoTierDecider:'" in html
    assert "judging material it cannot see" in html


# ── the panels still parse ──────────────────────────────────────────────────

@pytest.mark.parametrize("parts", [
    ("vera", "agents", "agent_panel.html"),
    ("vera", "chat", "chat_panel.html"),
])
def test_the_panel_javascript_parses(parts):
    """These are hand-edited HTML files with inline script; a dropped bracket is
    invisible in review and breaks the whole panel."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    html = _read(*parts)
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "no inline script found - the extraction is wrong"
    tmp = os.path.join(os.path.dirname(__file__), "_switch_check.js")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n;\n".join(blocks))
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:2000]
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
