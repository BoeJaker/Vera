"""An operator that stopped must say why, to the caller and not only to itself.

The step records below are the real shape operator_loop builds: the no-progress
branch stores operator_progress.describe() output under "reason", the repeat
branch stores its own sentence, and the returned dict carried neither.

Census run 21, author-then-edit step 3: three operator.run calls on the same
preview URL - 243228ms, 162733ms, 329288ms, 735 seconds - each reported to the
agentic loop as the single word "no_progress".
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.operator import stop_explanation as se  # noqa: E402

pytestmark = pytest.mark.critical


DESCRIBE_TEXT = ("stopped after 5 consecutive actions that changed nothing on the page "
                 "- same URL, same title, same controls. The page is not responding to "
                 "what is being tried (recent actions: click, wait). This is not "
                 "evidence the goal was reached.")
NO_PROGRESS_STEPS = [
    {"i": 1, "phase": "act", "action": "click"},
    {"i": 2, "phase": "no_progress", "url": "https://x/timer.html", "reason": DESCRIBE_TEXT},
]


def test_the_explanation_the_loop_already_wrote_reaches_the_caller():
    out = se.explain("no_progress", NO_PROGRESS_STEPS)
    assert DESCRIBE_TEXT in out


def test_the_machine_readable_code_is_still_there():
    """The UI and callers switch on `reason`; the sentence is additional, not a
    replacement."""
    assert se.explain("no_progress", NO_PROGRESS_STEPS).startswith("no_progress:")


def test_a_successful_run_explains_nothing():
    """Inventing a failure sentence for a run that worked would be worse than
    saying nothing."""
    assert se.explain("done", [{"phase": "done", "summary": "all good"}]) == ""


def test_a_cancelled_run_explains_nothing():
    """The user's own decision needs no explaining to the model."""
    assert se.explain("cancelled", [{"phase": "cancelled", "reason": "run cancelled"}]) == ""


def test_the_repeat_branch_is_carried_too():
    text = ("the same action was repeated 5 times on the same page with no change "
            "- stopping rather than spending the rest of the budget on it")
    out = se.explain("repeating_action", [{"phase": "repeating", "reason": text}])
    assert text in out


def test_max_steps_gets_a_sentence_even_with_no_step_record():
    """max_steps breaks out of the loop without writing an explanatory record,
    and it is the reason that previously reported success (run 18)."""
    out = se.explain("max_steps", [{"i": 9, "phase": "act", "action": "click"}])
    assert out != "max_steps"
    assert "not evidence the goal was met" in out


def test_a_step_record_repeating_the_bare_code_is_not_quoted():
    """Quoting "no_progress" back as its own explanation would look like an
    answer and contain nothing."""
    out = se.explain("no_progress", [{"phase": "no_progress", "reason": "no_progress"}])
    assert out == "no_progress"


def test_the_latest_record_wins():
    steps = [{"phase": "act", "error": "an earlier transient failure"},
             {"phase": "no_progress", "reason": DESCRIBE_TEXT}]
    assert DESCRIBE_TEXT in se.explain("no_progress", steps)


def test_an_error_field_is_used_when_there_is_no_reason():
    steps = [{"phase": "observe", "error": "Target page, context or browser has been closed"}]
    out = se.explain("observe_error", steps)
    assert "browser has been closed" in out


def test_junk_does_not_raise():
    for steps in (None, [], ["not a dict"], [{}], [{"reason": None}]):
        assert isinstance(se.explain("no_progress", steps), str)
    assert se.explain(None, None) == ""
    assert se.explain("", None) == ""


def test_an_unknown_code_is_not_dressed_up_as_a_failure():
    """Only the stops that mean "did not reach the goal" get explained."""
    assert se.explain("something_new", NO_PROGRESS_STEPS) == ""


# ── the call site ───────────────────────────────────────────────────────────

def _loop_source():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "vera", "operator", "operator_loop.py"),
              encoding="utf-8") as fh:
        return fh.read()


def test_the_loop_returns_the_explanation():
    src = _loop_source()
    assert '"explanation"' in src
    # and as `error`, which is what the agentic loop shows the executor
    assert 'out.setdefault("error"' in src


def test_reason_is_not_replaced_by_the_sentence():
    """Regression guard: overwriting `reason` would break every caller that
    switches on the code."""
    src = _loop_source()
    assert '"reason": reason' in src


def test_the_operator_module_uses_absolute_imports_only():
    """A loader entry point has no parent package - a relative import here
    unregistered the whole operator subsystem once already (ce8af34)."""
    src = _loop_source()
    assert "from Vera.vera.operator import stop_explanation" in src
    assert "from . import stop_explanation" not in src
    assert "from .stop_explanation" not in src
