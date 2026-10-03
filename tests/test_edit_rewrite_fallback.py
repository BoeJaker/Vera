"""code.edit falls back to a checked whole-file revision for a small file when anchored
edits keep missing (prod 30 Sep - 3 Oct: 36 of 57 failure parts were anchor misses)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("erc", ROOT / "vera" / "dag" / "edit_rewrite_core.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

FILE = "\n".join(["<h1 id='t'>01:00</h1>", "<script>", "let n = 60;", "function tick() {",
                  "  n--;", "  show(n);", "}", "setInterval(tick, 1000);", "</script>"]) + "\n"


def test_only_anchor_misses_qualify():
    miss = "edit 1: `find` text not present in the file (first 60 chars: 'x')"
    amb = "edit 2: `find` matches 3 places (lines 1, 4, 9) — extend it"
    assert R.anchor_failure([miss, amb])
    assert not R.anchor_failure([miss, "the edited file no longer parses — python-compile: x"])
    assert not R.anchor_failure([])
    assert R.eligible(FILE, [miss]) and not R.eligible("x" * (R.MAX_CHARS + 1), [miss])


def test_a_faithful_revision_is_accepted():
    after = FILE.replace("01:00", "01:30").replace("let n = 60;", "let n = 90;")
    assert R.accept(FILE, after) == (True, "")


def test_drops_inventions_and_no_ops_are_refused():
    assert R.accept(FILE, "")[0] is False
    assert R.accept(FILE, FILE)[0] is False
    assert "size" in R.accept(FILE, FILE[:20])[1]
    invented = "\n".join("const x%d = %d;" % (i, i) for i in range(12)) + "\n"
    ok, why = R.accept(FILE, invented)
    assert not ok and ("kept only" in why or "size" in why)


def test_prompt_carries_the_file_the_task_and_the_fence():
    p = R.prompt("timer.html", "html", FILE, "change to 90 seconds", "edit 1: not present")
    assert "let n = 60;" in p and "change to 90 seconds" in p and "```html file=timer.html" in p


def test_code_edit_uses_it_before_giving_up():
    i = SRC.index("async def cap_code_edit(")
    body = SRC[i:i + 30000]
    j = body.index("if attempt >= attempts:")
    tail = body[j:j + 4000]
    assert "_edit_rewrite_core.eligible(current" in tail
    assert "_edit_rewrite_core.accept(current" in tail
    assert '"mode": "rewrite"' in tail
