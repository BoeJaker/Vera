"""A periodic job registered with interval=0 starts once (4 Oct 2026: vllm's endless
_health_loop was re-created every second - ~2,950 live copies 50 min after a restart)."""

import importlib.util
import pathlib
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sdc", ROOT / "vera" / "scheduler_due_core.py")
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)
SRC = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")

T0 = datetime(2026, 10, 4, 3, 0, 0)


def test_interval_zero_starts_once():
    assert D.due(None, 0, T0) is True
    assert D.due(T0, 0, T0 + timedelta(seconds=1)) is False
    assert D.due(T0, 0, T0 + timedelta(hours=5)) is False
    assert D.due(T0, -1, T0 + timedelta(hours=5)) is False


def test_positive_intervals_unchanged():
    assert D.due(None, 30, T0) is True
    assert D.due(T0, 30, T0 + timedelta(seconds=29)) is False
    assert D.due(T0, 30, T0 + timedelta(seconds=30)) is True


def test_bad_interval_is_start_once():
    assert D.due(T0, "x", T0 + timedelta(hours=1)) is False


def test_the_scheduler_loop_uses_it():
    loop = SRC[SRC.index("async def scheduler_loop():"):]
    loop = loop[:loop.index("await asyncio.sleep(1)")]
    assert "_scheduler_due_core.due(last, task[\"int\"], now)" in loop
    assert '(now-last).total_seconds()>=task["int"]' not in loop
