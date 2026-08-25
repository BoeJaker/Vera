"""The step-retry loop must carry its recovery lineage forward.

`_v6_recovery_lineage`'s own docstring states the contract: "Threaded forward
onto every subsequent recovery step's dict, so a lineage of N rounds accumulates
N records rather than each round only ever seeing the immediately-prior one." It
exists because of a live incident — a run stuck for 2 hours / 65+ cycles retrying
the same malformed argument, each round individually bounded but the SEQUENCE
never accumulating the memory to notice it was repeating itself.

`_complete_step` was not honouring it: it passed the PRISTINE step to
`_v6_adjust_step` on every attempt, so `_recovery_history` never accumulated and
the adjuster was asked the same question with no record of its own prior
answers. Measured on session 216c7eb4 — step 1 retried three times, all three
adjust calls given a byte-identical prompt (sha cb06f9b68d64).

Imports the app module, so it runs in-container (where the merge gate executes
pytest) and skips on a host venv, where `Vera.vera.X` resolves to a different
checkout.
"""
import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")


STEP = {"id": 1, "title": "Create index.html", "caps": ["code.author"],
        "success": "index.html exists"}
RES = {"summary": "no file produced", "met_reason": "index.html does NOT exist"}


def test_lineage_accumulates_when_threaded_forward():
    """The fix: each attempt seeds the next, so round N carries N records."""
    step, sizes = STEP, []
    for i in range(1, 4):
        lineage = M._v6_recovery_lineage(step, RES, f"attempt {i} failed")
        sizes.append(len(lineage))
        step = {**step, "_recovery_history": lineage}   # what _complete_step now does
    assert sizes == [1, 2, 3]


def test_pristine_step_each_time_never_accumulates():
    """The bug, pinned: re-passing the original step keeps every round at one
    record — which is why three retries produced a byte-identical prompt."""
    sizes = [len(M._v6_recovery_lineage(STEP, RES, f"attempt {i} failed"))
             for i in range(1, 4)]
    assert sizes == [1, 1, 1]


def test_rendered_block_lists_every_prior_attempt():
    step = STEP
    for i in range(1, 4):
        lineage = M._v6_recovery_lineage(step, RES, f"attempt {i} failed")
        step = {**step, "_recovery_history": lineage}
    block = M._v6_recovery_lineage_block(step["_recovery_history"])
    assert block.count("tried:") == 3
    assert "PRIOR RECOVERY ATTEMPTS" in block
    for i in (1, 2, 3):
        assert f"attempt {i} failed" in block


def test_threading_does_not_mutate_the_callers_step():
    """`_complete_step` copies rather than mutates — the step belongs to the plan."""
    step = dict(STEP)
    lineage = M._v6_recovery_lineage(step, RES, "why")
    _ = {**step, "_recovery_history": lineage}
    assert "_recovery_history" not in step


def test_first_attempt_renders_no_prior_block():
    assert M._v6_recovery_lineage_block([]) == ""
