"""Every short-answer LLM call site as an application of the candidate matrix (four-minds FM-2 / FM-12): captured
from the one generation path when the switch is on, discovered when it behaves like a decision, and tried by every
candidate on the same prompts."""
import asyncio
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import site_tap_core as SC  # noqa: E402
from vera.minds import switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical


def test_only_short_one_line_answers_outside_chat_and_the_judges_are_kept():
    assert SC.wanted("yes") and SC.wanted("  Research  ", module="researcher_api")
    assert not SC.wanted("x" * 41) and not SC.wanted("a\nb") and not SC.wanted("  ")
    assert not SC.wanted("yes", job_type="idle_decide") and not SC.wanted("met", stage="student")
    assert not SC.wanted("hi", module="agents") and not SC.wanted("ok", stage="chat")


def test_the_site_is_the_caller_stage_and_system_prompt():
    a = SC.site_key("researcher_api", "_style", "", "You classify")
    assert a.startswith("researcher_api._style||") and a != SC.site_key("researcher_api", "_style", "", "Other")
    e = SC.entry({"caller_module": "m", "caller_func": "f"}, "stage", "sys", "p" * 5000, " Yes ", "qwen3:4b")
    assert e["answer"] == "Yes" and len(e["prompt"]) == SC.MAX_PROMPT and e["site"].startswith("m.f|stage|")
    assert all(isinstance(v, str) for v in e.values())


def test_a_site_that_behaves_like_a_decision_is_discovered():
    yesno = [{"site": "s1", "answer": a, "module": "m", "func": "f", "stage": ""}
             for a in ["Yes", "no.", "yes", "No"] * 10]
    free = [{"site": "s2", "answer": f"name {i}"} for i in range(40)]             # every answer different
    few = [{"site": "s3", "answer": "yes"}] * 5                                   # too few calls
    one = [{"site": "s4", "answer": "ok"}] * 40                                   # one label: not a decision
    d = SC.discover(yesno + free + few + one)
    assert set(d) == {"s1"} and sorted(d["s1"]["labels"]) == ["no", "yes"] and d["s1"]["coverage"] == 1.0


def test_agreement_and_label_picking():
    assert SC.normalize(' "Research." ') == "research" and SC.normalize("") == ""
    assert SC.pick_label("Yes, it is", ["yes", "no"]) == "yes" and SC.pick_label("no", ["yes", "no"]) == "no"
    ag = SC.agreement([("Yes", "yes", 100), ("no", "yes", 300), ("No", "no", 200)])
    assert ag == {"n": 3, "agree": 0.667, "ms_p50": 200} and SC.agreement([]) == {"n": 0}


def test_the_switch_is_off_by_default():
    f = SB.REGISTRY["decide.site_capture"]
    assert f.default == "off" and f.census is False


def test_the_generation_path_hands_short_answers_over_and_never_changes_them():
    src = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    i = src.index("_ollama_generate_core = ollama_generate")
    assert src.index("async def ollama_generate(prompt: str") < i < src.index('os.path.join(_here, "minds/site_tap.py")')
    w = src[i:src.index("def _ollama_log_append")]
    assert "@_site_ft.wraps(_ollama_generate_core)" in w and "return out" in w and 'st.MODE.get("on")' in w
    assert "except Exception:" in w


def test_tap_writes_only_when_on(monkeypatch):
    from vera.minds import site_tap as T
    seen = []

    class _R:
        async def xadd(self, key, fields, **kw):
            seen.append((key, fields))
    monkeypatch.setattr(T._orch, "REDIS", _R(), raising=False)
    caller = {"caller_module": "researcher_api", "caller_func": "_style"}

    async def go(on):
        monkeypatch.setitem(T.MODE, "on", on)
        T.tap(caller, "", "", "sys", "prompt", "research", "qwen3:4b")
        T.tap(caller, "", "idle_decide", "sys", "prompt", "research", "qwen3:4b")
        await asyncio.gather(*list(T._PENDING))
    asyncio.run(go(False))
    assert seen == []
    asyncio.run(go(True))
    assert len(seen) == 1 and seen[0][0] == SC.STREAM and seen[0][1]["answer"] == "research"
