"""A page a browser will verify has to be one a browser can read.

Census 47 lost two of its three goals to this, and neither was an operator
problem - proven by re-running the operator against a corrected file, where it
passed first try in 29s against 767s of failures on the authored one.

`author-then-edit` - timer.html shipped

    <div id="timer">60</div>          <- state typed into the markup
    let countdown = 90;               <- the edit was correct
    ...                               <- updateDisplay() never called on load

so the page loaded showing `60` forever and the operator's criterion ("loads
showing 01:30") was unsatisfiable. Both code.edit calls returned ok=True. Only a
browser could see the disagreement, and it cost 767s.

`long-horizon` - the to-do app created its complete-checkbox as

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = completed;
    checkbox.onchange = () => toggleTaskCompleted(li);

with no id+label, aria-label or title. The operator reads the ACCESSIBILITY
TREE, where an unnamed checkbox has no name at all - it saw `Task A Delete`,
found nothing to click to complete a task, repeated one action five times and
stopped. The feature was implemented and invisible.

Both rules now live in code.author's system prompt. This pins them there: a
prompt rule that silently stops being sent is the failure mode
`loop_prompt_rules` exists for, and these two were added because nothing in the
guidance mentioned either (checked before writing them - they are additions, not
a contradiction of an existing rule).
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.critical


def _author_prompt_source() -> str:
    """The literal source of code.author's system prompt.

    Read as TEXT rather than by importing: the composed prompt is built inside a
    long capability body that needs the whole app, and what matters here is that
    the rule is in the string the model is sent.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "vera", "dag", "dag_workshop_capabilities.py")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    start = body.index("FINISH the behaviour")
    end = body.index("This call authors EXACTLY ONE file", start)
    seg = body[start:end]
    # Join adjacent string literals so this reads the PROMPT the model gets, not
    # the source layout. Without it a phrase that happens to wrap across two
    # source lines is invisible to a substring check - which is a property of
    # the formatting, not of the rule.
    return re.sub(r'"\s*\n\s*"', "", seg)


def test_the_render_on_load_rule_is_present():
    src = _author_prompt_source()
    assert "RENDER STATE ON LOAD" in src
    assert "never typed into the markup as a literal" in src


def test_the_naming_rule_defines_what_a_control_IS():
    """The first version said "give every control a real label" and had no
    effect: the model does not necessarily agree that a bare createElement
    input IS a control needing one. So the rule now DEFINES the set and gives a
    test the model can apply to any element."""
    src = _author_prompt_source()
    assert "A control is ANYTHING you attach a click/change/input handler to" in src
    assert "THE TEST: does the element contain visible TEXT of" in src


def test_the_naming_rule_gives_the_literal_line_to_write():
    """A1 landed because it named a concrete action in the model's own idiom
    ("call the render function at the end"). A2 did not, because "give it a
    label" is not an action - it is a property someone else has to achieve. So
    it now shows the exact setAttribute call, in the createElement idiom the
    model actually writes."""
    src = _author_prompt_source()
    assert "cb.setAttribute('aria-label'" in src
    assert "document.createElement('input')" in src


def test_it_says_an_unnamed_control_is_ABSENT_not_merely_unlabelled():
    """The consequence is what motivates the work. 'Unlabelled' sounds
    cosmetic; the checker cannot see the element at all."""
    src = _author_prompt_source()
    assert "ABSENT from what the checker reads" in src


def test_an_element_that_already_has_text_is_explicitly_exempt():
    """Without this the rule reads as 'add aria-label to everything', which is
    noise on <button>Delete</button> and invites the model to ignore it."""
    src = _author_prompt_source()
    assert "<button>Delete</button>" in src
    assert "already named and needs nothing" in src


def test_both_rules_carry_the_concrete_failure_they_came_from():
    """A rule with no example is one the model can satisfy in the letter and
    miss in the spirit. Each carries the artifact that actually broke."""
    src = _author_prompt_source()
    assert "<div id='timer'>60</div>" in src        # author-then-edit
    assert "type/checked/onchange" in src           # long-horizon


def test_the_rules_sit_with_the_other_finish_the_behaviour_guidance():
    """Both are 'the file must actually work' rules and belong beside the
    dead-control rule, not in a separate block a future edit can drop."""
    src = _author_prompt_source()
    assert src.index("DEAD control") < src.index("BROWSER-OBSERVABLE")


def test_the_rule_says_what_to_DO_not_only_what_to_avoid():
    """'Do not hardcode' leaves the model with no action. The instruction has to
    name the fix - call the render function at the end of the script."""
    src = _author_prompt_source()
    assert "Call the render/update function once at the end" in src
    assert "set the name ON THE SAME LINES you build the element" in src
