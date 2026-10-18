"""DEEP-21 D21-14 (compute-roles, 12 Oct 2026): scheduled censuses say the scheduler drove them, and a
census never starts on an unhealthy prod (it recorded 'skipped: box busy' rows that read as results)."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SH = (ROOT / "tools" / "loop-census" / "census_all.sh").read_text(encoding="utf-8")
SC = (ROOT / "vera" / "evolve" / "schedule_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_operator_tool_session_from_env_with_old_defaults():
    assert 'OPERATOR="${CENSUS_OPERATOR:-claude}"' in SH and 'TOOL="${VERA_CENSUS_TOOL:-census_all.sh}"' in SH
    assert '--operator "$OPERATOR"' in SH and '--tool "$TOOL"' in SH
    assert 'SID="${CENSUS_SESSION_ID:-c4cceff0' in SH


def test_scheduler_passes_who_it_is():
    assert 'env["CENSUS_OPERATOR"] = "vera-scheduler"' in SC and 'env["VERA_CENSUS_TOOL"] = "sched"' in SC
    assert 'env["CENSUS_SESSION_ID"] = "sched:%s"' in SC and 'str(rec.get("id") or ""))' in SC


def test_unhealthy_prod_stops_the_set_before_a_template():
    i = SH.index("ABORT before $t: prod unhealthy")
    assert SH.index('while [ "$(health)" != "200" ]') < i < SH.index('echo "running $t since')
    assert "exit 4" in SH[i:i + 200]
