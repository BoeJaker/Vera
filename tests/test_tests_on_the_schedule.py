"""Owner, 18 Oct 2026: "make sure all of your tests are on the loop lab schedule ... making use of the schedule system
and not creating ad-hoc timing rules". four-minds' nightly passes no longer time themselves: each is a capability a
Loop Lab schedule entry calls, and the schedule is allowed to call it."""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.evolve import schedule_core as SC  # noqa: E402

pytestmark = pytest.mark.critical

MOVED = {  # module -> the in-code timer that is gone, and the capability its schedule entry calls
    "vera/minds/judge.py": ("decide_shadow_night", "decide.shadow.night"),
    "vera/minds/site_tap.py": ("decide_site_matrix_night", "decide.shadow.night"),
    "vera/minds/claims.py": ("output_claims_harvest", "output.claims.harvest"),
    "vera/minds/cascade.py": ("decide_cascade_plan", "decide.cascade.plan"),
    "vera/minds/tool_shortlist.py": ("caps_shortlist_index_night", "caps.shortlist.nightly"),
    "vera/minds/precedent.py": ("plan_precedent_night", "plan.precedent.index"),
    "vera/minds/calibration.py": ("decide_calibration_weekly", "decide.calibration.weekly"),
}


@pytest.mark.parametrize("path", sorted(MOVED))
def test_no_in_code_timer_and_the_schedule_may_call_it(path):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    timer, cap = MOVED[path]
    assert not re.search(r'^schedule\([^\n]*name="%s"' % re.escape(timer), src, re.M), timer
    assert "# On the Loop Lab schedule" in src
    assert not SC.cap_denied(cap), cap


def test_the_matrix_queue_carries_the_call_site_job_and_no_window_check():
    src = open(os.path.join(ROOT, "vera/minds/judge.py"), encoding="utf-8").read()
    night = src[src.index("async def _night_job"):src.index("# On the Loop Lab schedule")]
    assert "JC.in_window" not in night and '"decide.shadow.sites"' in night
    for path in ("vera/minds/tool_shortlist.py", "vera/minds/calibration.py"):
        s = open(os.path.join(ROOT, path), encoding="utf-8").read()
        assert "JC.in_window(now.hour)" not in s and "now.weekday() != 6" not in s
