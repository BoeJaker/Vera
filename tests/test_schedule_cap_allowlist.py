"""DEEP-21 D21-2 (compute-roles, 11 Oct 2026): `cap` schedules run only allowed families (read / measure /
report / train); release, exec, sandbox, secrets and autonomous caps are refused at save and at start."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import schedule_core as sc  # noqa: E402

pytestmark = pytest.mark.critical


@pytest.mark.parametrize("name", ["evolve.release.prod", "exec.bash.run", "exec.python.run",
                                  "evolve.sandbox.exec", "autonomous.run", "secrets.get",
                                  "board.dispatch", "ide.fs.write", "unknown.thing", ""])
def test_refused(name):
    assert sc.cap_denied(name) and not sc.cap_allowed(name)


@pytest.mark.parametrize("name", ["decide.shadow.score", "worldview.train", "chat.corpus.run",
                                  "evolve.suite.start", "obs.provenance", "intel.sweep", "census.runs"])
def test_allowed_families_including_every_live_schedule(name):
    assert sc.cap_allowed(name)


def test_deny_list_wins_inside_an_allowed_family():
    assert sc.cap_denied("census.control.set")
    with pytest.raises(ValueError):
        sc.normalize({"kind": "cap", "target": {"name": "exec.bash.run"}})
