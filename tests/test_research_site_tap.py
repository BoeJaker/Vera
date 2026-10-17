"""Research's short answers reach the call-site matrix too (four-minds FM-2 / FM-12; the rest of RF-50): research
generates through its own stream, so the shared path's tap never saw its decisions."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import site_tap_core as SC  # noqa: E402

pytestmark = pytest.mark.critical


def test_collect_ollama_hands_its_answer_to_the_tap_before_returning():
    src = open(os.path.join(ROOT, "vera", "research", "researcher_api.py"), encoding="utf-8").read()
    body = src[src.index("async def collect_ollama("):src.index("async def list_models(")]
    assert "_site_tap(prompt, system, result, inst)\n    return result" in body
    tap = src[src.index("def _site_tap("):src.index("async def list_models(")]
    assert 'st.MODE.get("on")' in tap and "inspect.stack(context=0)" in tap and "except Exception:" in tap
    assert 'st.tap(caller, "research", "research"' in tap


def test_research_answers_are_wanted_by_the_tap():
    assert SC.wanted("academic", stage="research", job_type="research", module="researcher_api")
    assert SC.site_key("researcher_api", "_decide_style", "research", "s").startswith("researcher_api._decide_style|research|")
