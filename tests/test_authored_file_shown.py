"""An authored file is shown to the executor, not read back (plan item 17b).

run70-73 (24 Sep 2026): 42 times the executor read back a file that
code.author/code.edit had just written and a parser had verified - one full
executor turn each - because the result it saw was metadata JSON cut at the
preview budget. Source pin: the register block now rewrites the preview from
the registry copy of the content and re-records it as the call's output.
"""
import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_the_authored_file_block_shows_the_content():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    i = src.index("Register a code.author/code.edit write the moment it lands")
    block = src[i:i + 9000]
    assert "Its content " in block and "is shown here IN FULL - do NOT read it back with sandbox.session.fs.read" in block
    assert "_v5_head_tail(_cbody, _V5_GEN_INSTEP_MAX)" in block
    assert "outputs[tool] = preview" in block
    assert "success_sigs[_call_sig] = preview" in block
    assert "_budget = max(_budget, len(preview))" in block
    assert block.index('_cbody = ""') < block.index("if _cfs:")      # defined on every path
    ast.parse(src)
