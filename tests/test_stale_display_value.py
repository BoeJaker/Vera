"""An edit that changes a value is not done while the page still shows the old one
(census 3 Oct, author-then-edit: 60 -> 90 changed in code, display left at 01:00)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("svc", ROOT / "vera" / "dag" / "stale_value_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

BEFORE = """<title>60 Second Countdown Timer</title>
<div id="display">01:00</div>
<script>
const countdownTime = 60 * 1000;
function fmt(t) { const m = Math.floor(t / 60); const s = t % 60; return m + ':' + s; }
</script>"""
AFTER_HALF = BEFORE.replace("60 * 1000", "90 * 1000").replace("<title>60 Second", "<title>90 Second")
AFTER_FULL = AFTER_HALF.replace(">01:00<", ">01:30<")


def test_the_live_case_is_caught():
    f = C.stale_display_values(BEFORE, AFTER_HALF)
    assert f and f[0]["old"] == 60 and f[0]["new"] == 90 and f[0]["form"] == "01:00" and f[0]["line"] == 2
    msg = C.describe(f)
    assert msg.startswith(C.MARKER) and "01:00" in msg and "60 -> 90" in msg


def test_a_complete_edit_and_the_formulas_raise_nothing():
    assert C.stale_display_values(BEFORE, AFTER_FULL) == []          # t / 60 and % 60 are not displays


def test_text_forms_and_untouched_lines_only():
    b = "<p>Wait 60 seconds</p>\nconst T = 60;\n"
    assert C.stale_display_values(b, b.replace("const T = 60", "const T = 90"))[0]["form"] == "60 seconds"
    b2 = "<span>60</span>\nlet n = 60;"
    assert C.stale_display_values(b2, b2.replace("let n = 60", "let n = 90"))[0]["form"] == ">60<"
    # the old value inside the changed line itself is the edit, not a leftover
    assert C.stale_display_values("x = 60  # 60 seconds", "x = 90  # 60 seconds") == []


def test_no_number_change_no_findings():
    assert C.stale_display_values("a\nb", "a\nc") == []
    assert C.stale_display_values("v = 1\n<b>1</b>", "v = 2\n<b>1</b>") == []    # 0/1 ignored
    assert C.describe([]) == ""


def test_the_edit_reports_it_and_the_verifier_does_not_pass_it():
    i = SRC.index("async def cap_code_edit(")
    body = SRC[i:i + 20000]
    assert "_stale_value_core.stale_display_values(current, res[\"content\"], task=task)" in body
    assert '**({"warning": _stale_msg} if _stale_msg else {})' in body
    v = SRC[SRC.index("async def _v6_verify_step("):]
    v = v[:v.index("verified its syntax deterministically")]
    assert "_stale_value_core.MARKER" in v
