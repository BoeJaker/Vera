"""DEEP-21 D21-3 (compute-roles, 11 Oct 2026): census liveness parsed UTC with mktime - timezone, which is
LOCAL time. On prod (Europe/London, BST) every age came out an hour too old, so a census whose active
file was 1 h old read as stale. Runs under TZ=Europe/London in a subprocess so the bug would show."""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.critical

PROBE = r'''
import sys, time, calendar
sys.path.insert(0, %r)
from vera.census import control as C
from vera.evolve import work_core as W
now = calendar.timegm((2026, 7, 15, 12, 0, 0, 0, 0, 0))          # a July (BST) noon, UTC
upd = time.strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ", time.gmtime(now - 5400))   # 90 min ago
v = C.active_view({"state": "running", "updated_at": upd, "pid": 0}, now=now)
print("AGE", int(v.get("age_s") or -1), "LIVE", v.get("live"))
'''


def test_active_view_age_is_utc_under_bst():
    env = dict(os.environ, TZ="Europe/London")
    out = subprocess.run([sys.executable, "-c", PROBE % str(ROOT)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("AGE")][-1]
    age = int(line.split()[1])
    assert 5390 <= age <= 5410, line          # 90 min, not 150 (mktime read it as BST)


def test_no_mktime_left_for_utc_text():
    for f in ("vera/census/control.py", "vera/evolve/work_core.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "time.mktime(" not in src, f
