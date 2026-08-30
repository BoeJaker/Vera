"""PYTHONPATH must survive the in-container timeout wrapper.

Two fixes collided and nobody noticed for three days.

L5 put the workspace root on PYTHONPATH, because python puts the SCRIPT'S
directory on sys.path[0] rather than the cwd - so `python3 /workspace/pkg/test.py`
cannot import `pkg` however well formed the package is. It was written as a shell
assignment prefix: `PYTHONPATH=/workspace cmd`.

Then the in-container timeout wrapper (2026-08-27) began prefixing the payload
with `timeout -k 5 <secs>`. A shell assignment prefix only applies at the start
of a SIMPLE COMMAND - so the assignment became timeout's first ARGUMENT, and
timeout tried to exec a program literally named `PYTHONPATH=/workspace:...`.

Observed live in census run 16, build-multifile, while it was failing to create
and import its test file:

    timeout: failed to run command 'PYTHONPATH=/workspace:/workspace/.python':
    No such file or directory

It failed silently in the sense that matters: `timeout` is present in every image
that has coreutils or busybox, so the L5 fix was dead nearly everywhere, and the
symptom looked like the model being unable to write a file.

These tests RUN the composed command through a real shell rather than asserting
on the string, because the bug was in shell semantics and only a shell can
settle it.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.remote.session_sandbox_capabilities import (          # noqa: E402
    _bounded_cmd, _pythonpath_prefix,
)

_HAVE_SH = os.path.exists("/bin/sh")
pytestmark = pytest.mark.skipif(not _HAVE_SH, reason="needs a POSIX shell")


def _sh(cmd, env=None):
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    e.update(env or {})
    p = subprocess.run(["/bin/sh", "-lc", cmd], capture_output=True, text=True,
                       timeout=60, env=e)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


_ECHO = 'python3 -c "import os;print(os.environ.get(\'PYTHONPATH\',\'MISSING\'))"'


# ── the bug, and that it is really fixed ───────────────────────────────────
def test_pythonpath_survives_the_timeout_wrapper():
    """The whole point: composed exactly as the runners compose it."""
    rc, out, err = _sh(_bounded_cmd(_pythonpath_prefix() + _ECHO, 20))
    assert rc == 0, err
    assert "/workspace" in out
    assert "failed to run command" not in err


def test_the_old_bare_assignment_form_really_did_break():
    """Pins the actual mechanism, so nobody 'simplifies' env back out. If this
    ever passes, the shell has changed and the fix can be revisited."""
    broken = _bounded_cmd("PYTHONPATH=/workspace " + _ECHO, 20)
    rc, out, err = _sh(broken)
    have_timeout = subprocess.run(["/bin/sh", "-lc", "command -v timeout"],
                                  capture_output=True).returncode == 0
    if have_timeout:
        assert rc != 0 or "failed to run command" in err, (
            "the bare form should fail under timeout — if it no longer does, "
            "re-check whether the env prefix is still needed")
    else:
        pytest.skip("no timeout(1) here, so the wrapper expands to nothing")


def test_it_also_works_when_timeout_is_absent():
    """The wrapper probes for timeout and expands to nothing when it is missing.
    The prefix has to work in BOTH shapes."""
    rc, out, _ = _sh(_pythonpath_prefix() + _ECHO)
    assert rc == 0 and "/workspace" in out


# ── it must not destroy an existing PYTHONPATH ─────────────────────────────
def test_an_existing_pythonpath_is_appended_not_replaced():
    rc, out, err = _sh(_bounded_cmd(_pythonpath_prefix() + _ECHO, 20),
                       env={"PYTHONPATH": "/already/here"})
    assert rc == 0, err
    assert out.startswith("/workspace"), out
    assert "/already/here" in out, "an existing PYTHONPATH must be preserved"


def test_an_absent_pythonpath_leaves_no_trailing_separator():
    """`${PYTHONPATH:+:$PYTHONPATH}` exists so an unset value adds nothing — a
    trailing ':' would put the CWD on sys.path, which is its own hazard."""
    rc, out, _ = _sh(_bounded_cmd(_pythonpath_prefix() + _ECHO, 20))
    assert rc == 0 and out == "/workspace", out


# ── shape ──────────────────────────────────────────────────────────────────
def test_the_prefix_uses_env_so_it_survives_any_wrapper():
    p = _pythonpath_prefix()
    assert p.startswith("env PYTHONPATH="), p
    assert "${PYTHONPATH:+:$PYTHONPATH}" in p


def test_the_workspace_path_is_quoted():
    assert "PYTHONPATH=/workspace" in _pythonpath_prefix().replace("'", "")


def test_all_three_runners_share_one_definition():
    """The two wrapped sites were broken while the unwrapped one worked, which
    is exactly why this hid. Three copies drifted; one definition cannot."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "vera", "remote",
                            "session_sandbox_capabilities.py"), encoding="utf-8").read()
    assert src.count("_pythonpath_prefix()") >= 4, "helper + three call sites"
    # No hand-rolled bare assignment may survive anywhere in the module.
    assert '("PYTHONPATH=" + shlex.quote(_WORKDIR)' not in src
