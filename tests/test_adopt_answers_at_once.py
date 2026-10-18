"""adopt answers at once (B1.0 / E1, 18 Oct 2026): the caller gets the pipeline id while the critical tier runs in
the background; a restart mid-gate is resumed because the pipeline was saved with a critical-queue step first."""
import os
from datetime import datetime, timezone

from vera.evolve import gate_resume_core as G

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EV = open(os.path.join(ROOT, "vera", "evolve", "evolve_capabilities.py"), encoding="utf-8").read()
ADOPT = EV[EV.index("async def evolve_pipeline_adopt("):EV.index('@capability("evolve.pipeline.begin"')]


def test_adopt_runs_the_tier_in_the_background_unless_asked_to_wait():
    assert "wait: bool = False" in ADOPT
    assert "if runs_tier and not wait:" in ADOPT
    assert "asyncio.create_task(_bg_gate())" in ADOPT
    assert '"gate": "running"' in ADOPT and '"gate": "done"' in ADOPT


def test_the_pipeline_is_saved_resumable_before_the_background_gate_starts():
    i_step = ADOPT.index('_pstep(rec, "critical-queue", True')
    i_save = ADOPT.index("await _save_pipeline(rec)", i_step)
    i_task = ADOPT.index("asyncio.create_task(_bg_gate())")
    assert i_step < i_save < i_task
    rec = {"adopted": True, "gate_passed": None, "decision": "pending", "status": "adopted", "branch": "fix/x",
           "current": "queued for the critical tier", "created_at": datetime.now(timezone.utc).isoformat(),
           "steps": [{"stage": "adopt"}, {"stage": "gate"}, {"stage": "critical-queue"}]}
    assert G.interrupted(rec, datetime.now(timezone.utc))      # a restart now resumes it


def test_a_crashed_background_gate_ends_failed_not_hanging():
    assert '"the background gate crashed:' in ADOPT and 'rec["gate_passed"] = False' in ADOPT


def test_the_scheduler_still_waits_for_the_gate():
    src = open(os.path.join(ROOT, "vera", "evolve", "schedule_capabilities.py"), encoding="utf-8").read()
    i = src.index('_call("evolve.pipeline.adopt"')
    assert "wait=True" in src[i:i + 300]
