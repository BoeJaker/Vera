"""A deploy must not assume `$HOME` is writable.

`provision.deploy` put every component in `$HOME/.vera/edge` and built its venv
there. On an unprivileged LXC container `/root` can be owned by a uid outside
the container's mapped range — it shows as `nobody:root` mode 0700, and even
root INSIDE the container cannot traverse it. Measured 2026-09-20 on the three
ollama nodes:

    gpu-250   /root  nobody:root 0700   mkdir FAILED
    cpu-246   /root  root:100000 0700   mkdir ok
    cpu-247   /root  nobody:root 0700   mkdir FAILED

So two of the three could not receive ANY python component, and the symptom was
an opaque venv creation failure several steps after the real cause. This is the
same reason `ollama-vera.service` on those nodes sets `HOME=/`.

The fix probes for a writable directory. The rule these tests pin is that
status/stop search the SAME candidates the deploy wrote to — if they only
looked in `$HOME`, a component deployed to the fallback would report "stopped"
while still running, which is the worst answer a stop command can give.

Pure: strings in, shell out. No SSH, no app.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.provisioning import components_core as C      # noqa: E402

pytestmark = pytest.mark.critical


def test_home_is_preferred_but_is_not_the_only_option():
    """$HOME first keeps existing hosts behaving exactly as before."""
    assert C.EDGE_DIR_CANDIDATES[0] == "$HOME/.vera/edge"
    assert len(C.EDGE_DIR_CANDIDATES) > 1, "a $HOME-only list is the bug"
    assert any(not c.startswith("$HOME") for c in C.EDGE_DIR_CANDIDATES)


def test_every_fallback_is_an_absolute_path():
    """A relative fallback would land wherever the ssh session happened to be."""
    for c in C.EDGE_DIR_CANDIDATES[1:]:
        assert c.startswith("/"), c


def test_probe_tests_writability_not_just_existence():
    """`mkdir -p` succeeds on a path that already exists and is unwritable, so
    the probe must also check -w or it will pick a directory it cannot use."""
    cmd = C.edge_dir_probe_cmd()
    assert "-w" in cmd
    for c in C.EDGE_DIR_CANDIDATES:
        assert c in cmd


def test_probe_stops_at_the_first_writable_candidate():
    """Each step is guarded on D being empty, so a later candidate cannot
    overwrite an earlier successful choice."""
    cmd = C.edge_dir_probe_cmd()
    assert cmd.count('[ -z "$D" ]') == len(C.EDGE_DIR_CANDIDATES)


def test_probe_reports_failure_explicitly():
    assert "VERA_EDGE_DIR_NONE" in C.edge_dir_probe_cmd()


def test_parse_reads_the_chosen_directory():
    assert C.parse_edge_dir("noise\nVERA_EDGE_DIR=/opt/vera/edge\n") == "/opt/vera/edge"
    assert C.parse_edge_dir("VERA_EDGE_DIR=/root/.vera/edge") == "/root/.vera/edge"


def test_parse_returns_empty_when_nothing_was_writable():
    assert C.parse_edge_dir("VERA_EDGE_DIR_NONE") == ""
    assert C.parse_edge_dir("") == ""
    assert C.parse_edge_dir("some unrelated output") == ""


def test_pidfile_lookup_searches_every_candidate():
    """THE regression this guards: stop/status looking only in $HOME would
    report a running component as stopped."""
    cmd = C.pidfile_lookup_cmd("nlp_server")
    for c in C.EDGE_DIR_CANDIDATES:
        assert c in cmd, f"{c} not searched — a component there looks stopped"
    assert "nlp_server.pid" in cmd


def test_pidfile_lookup_and_probe_agree_on_the_candidate_list():
    """They must not drift apart: the deploy writes where the probe chose, and
    stop reads where the lookup searches."""
    probe = C.edge_dir_probe_cmd()
    lookup = C.pidfile_lookup_cmd("x")
    for c in C.EDGE_DIR_CANDIDATES:
        assert c in probe and c in lookup


def test_custom_candidates_are_honoured():
    cmd = C.edge_dir_probe_cmd(["/srv/a", "/srv/b"])
    assert "/srv/a" in cmd and "/srv/b" in cmd
    assert "$HOME/.vera/edge" not in cmd
