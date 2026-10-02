"""The operator may finish a check that fails (edge mirror 2026-10-02, run
fa706e575d: a frozen countdown watched until the 480 s budget ran out)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.operator import thinker as T  # noqa: E402


class _Obs:
    url = "https://localhost:8999/remote/sandbox/preview/s/timer.html"
    title = "Timer"
    text = "03:00 Start Pause Reset"
    elements = []

    def to_prompt(self, *a, **k):
        return "URL: %s\nTEXT: %s" % (self.url, self.text)


def test_the_system_prompt_says_a_failed_check_is_finished():
    s = T._SYSTEM
    assert "a check that FAILS is a finished check" in s
    assert "did NOT behave as asked" in s
    assert s.rstrip().endswith('"Respond with EXACTLY ONE JSON object and nothing else."'[1:-1])


def test_the_done_slot_admits_a_negative_answer():
    try:
        p = T.build_prompt("confirm the countdown decreases", _Obs(), [])
    except Exception:                       # observation shape differs - read the source
        src = (ROOT / "vera" / "operator" / "thinker.py").read_text(encoding="utf-8")
        assert "verified, or seen NOT to work" in src
        return
    assert "verified, or seen NOT to work" in p["user"]
    assert '"done": false' not in p["user"]
