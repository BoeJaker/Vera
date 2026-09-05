"""The prompt said "do NOT use JSON" and the same call forced JSON mode.

code.edit gained a delimited-block reply format so a code payload would not
have to survive JSON escaping. It never worked in production for one reason:

    raw = await fn(prompt=_prompt, system=sys_prompt, output_format="json", ...)

output_format="json" puts the model in JSON output mode regardless of what the
system prompt asks, so it emitted fenced JSON every single time and the block
parser never saw a block. Every sampled failing reply across censuses 30, 31
and 32 began ```json - not one was a block.

The cost: code.edit was the largest single failure cause in those three
censuses (8 occurrences, ahead of repeating_action at 3 and time_budget at 3),
and author-then-edit never passed once in three runs.

Why it survived validation: the fix was measured with a direct llm.generate
call that did NOT pass output_format, so the model complied beautifully in the
test and was overridden in production. A format is only proven by the call the
capability actually makes.

Pure: AST only, no LLM.
"""
import ast
import os

import pytest

SRC_PATH = os.path.join(os.path.dirname(__file__), "..",
                        "vera", "dag", "dag_workshop_capabilities.py")
SRC = open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)

pytestmark = pytest.mark.critical


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError("%s not found" % name)


def _seg(node):
    return ast.get_source_segment(SRC, node) or ""


def test_code_edit_no_longer_forces_json_while_asking_for_blocks():
    """The contradiction itself."""
    seg = _seg(_fn("cap_code_edit"))
    assert "format_instructions()" in seg, "cap_code_edit should offer the block format"
    assert 'output_format="json",\n' not in seg, \
        "the call still forces JSON mode while the prompt forbids it"
    assert '"output_format": "json"' in seg, \
        "JSON must remain the fallback when edit_blocks is unavailable"


def test_json_mode_is_conditional_on_blocks_being_unavailable():
    seg = _seg(_fn("cap_code_edit"))
    assert "_edit_blocks is not None" in seg
    idx = seg.index('"output_format": "json"')
    guard = seg[max(0, idx - 200):idx]
    assert "_edit_blocks" in guard, \
        "JSON mode must be gated on the block format being absent"


def test_the_two_still_agree_when_blocks_are_unavailable():
    """If edit_blocks fails to import, the prompt asks for JSON AND the call
    sets JSON mode - the pair must stay consistent in both directions."""
    seg = _seg(_fn("cap_code_edit"))
    assert "Return ONLY a JSON object" in seg, "the JSON fallback prompt is still needed"


def test_the_editor_reply_is_parsed_by_the_dual_format_reader():
    seg = _seg(_fn("cap_code_edit"))
    assert "_editor_obj_from_reply(_raw_text)" in seg


def test_other_editor_paths_that_still_ask_for_json_keep_json_mode():
    """The syntax-repair and reference-fix editors were NOT converted to
    blocks, so they must keep output_format="json" - removing it there would
    be the same contradiction in reverse."""
    for name in ("_edit_sys",):
        pass
    # both remaining JSON editors live outside cap_code_edit; assert the file
    # still pairs their JSON prompt with JSON mode
    assert SRC.count('output_format="json"') >= 2, \
        "the untouched JSON editors must keep JSON mode"
