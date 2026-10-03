"""The stale-display check also follows the value change the TASK names (probe 3 Oct,
e364ef40: `- 1` became `- 90 / 60`, the page still said `Seconds Remaining: 60`)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("svc2", ROOT / "vera" / "dag" / "stale_value_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

BEFORE = """<div id="timer">Seconds Remaining: 60</div>
<script>
  const timeLeft = parseInt(d.textContent.split(': ')[1]) - 1;
</script>"""
AFTER = BEFORE.replace(") - 1;", ") - 90 / 60;")
TASK = "Change the initial timer duration in timer.html from 60 seconds to 90 seconds."


def test_task_pairs():
    assert C.task_pairs(TASK) == [(60, 90)]
    assert C.task_pairs("change the countdown from 60 to 90") == [(60, 90)]
    assert C.task_pairs("make it faster") == [] and C.task_pairs("") == []


def test_the_probe_case_is_caught_through_the_task():
    assert C.stale_display_values(BEFORE, AFTER) == []                 # the diff alone misses it
    f = C.stale_display_values(BEFORE, AFTER, task=TASK)
    assert f and f[0]["old"] == 60 and f[0]["line"] == 1 and "closing tag" in f[0]["form"]


def test_a_complete_edit_raises_nothing():
    done = BEFORE.replace("Remaining: 60<", "Remaining: 90<")
    assert C.stale_display_values(BEFORE, done, task=TASK) == []


def test_code_edit_passes_its_task():
    assert 'stale_display_values(current, res["content"], task=task)' in SRC
