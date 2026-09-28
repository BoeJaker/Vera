"""A recorded agent loop (source=loop) reaches Loop Lab's driver list with who
started it and how it planned, so loops can be filtered and compared like with
like (2026-09-27)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import work_core as W  # noqa: E402


def test_a_loop_record_keeps_origin_engine_and_style():
    row = W.run_row({"run_id": "chat-1", "task": "loop:interactive", "source": "loop",
                     "origin": "interactive", "engine": "v6", "plan_style": "stepwise",
                     "ts": "2026-09-22T21:19:38Z", "elapsed_s": 362.0, "pass_rate": 1.0})
    assert (row["kind"], row["source"]) == ("run", "loop")
    assert (row["origin"], row["engine"], row["plan_style"]) == ("interactive", "v6", "stepwise")


def test_other_runs_carry_empty_loop_fields():
    row = W.run_row({"run_id": "r1", "task": "t", "source": "manual", "ts": "2026-09-22T21:19:38Z"})
    assert (row["origin"], row["engine"], row["plan_style"]) == ("", "", "")
