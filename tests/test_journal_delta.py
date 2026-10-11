"""Roadmap A6 (compute-roles, 11 Oct 2026): with per-run journal_delta the executor's live journal
block shows only entries added since the step-start digest the step goal already carries; without it
the prompt is unchanged."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_switch_plumbed_per_run():
    assert 'v6_journal_delta = bool(body.get("journal_delta", False))' in SRC
    assert "journal_delta=v6_journal_delta," in SRC
    assert "    journal_delta: bool = False,\n" in SRC


def test_both_injection_sites_record_the_frozen_length_only_when_asked():
    assert SRC.count('step["_journal_injected"] = True\n') == 2
    assert SRC.count('if journal_delta:\n') >= 2
    assert SRC.count('step["_journal_from"] = len(journal)') == 2


def test_live_block_default_unchanged_and_delta_when_marked():
    i = SRC.index('_jfrom = step.get("_journal_from")')
    seg = SRC[i:i + 1200]
    assert "journal[_jfrom:]" in seg and "RUN JOURNAL - new during this step" in seg
    # the default (no marker) keeps the exact old block
    assert ('_journal_live = ("\\n\\nRUN JOURNAL (live — reuse, don\'t re-derive):\\n"\n'
            '                                      + _jdig[:1200])') in seg
