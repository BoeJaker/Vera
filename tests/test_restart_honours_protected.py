"""Every restart path honours protected scheduled runs (backbone B1.0, 18 Oct 2026: a direct sys.dev.restart at
14:12 BST closed a protected run - protect held releases only)."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
CAP = ORCH[ORCH.index("async def cap_sys_dev_restart("):]
CAP = CAP[:CAP.index("\n@capability(")]
REL = open(os.path.join(ROOT, "vera", "evolve", "release_capabilities.py"), encoding="utf-8").read()


def test_restart_checks_protected_runs_before_anything_else_happens():
    i_guard = CAP.index("protected_running()")
    assert i_guard < CAP.index('log.warning("sys.dev.restart requested')
    assert i_guard < CAP.index("census_before_restart(")      # nothing paused or killed before the check
    assert '"held": True' in CAP and '"protected": _prot[:8]' in CAP


def test_force_overrides_and_the_release_passes_it():
    assert "force: bool = False" in CAP
    i = REL.index('_call("sys.dev.restart"')
    assert "force=True" in REL[i:i + 160]
