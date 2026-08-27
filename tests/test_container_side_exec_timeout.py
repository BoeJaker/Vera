"""A blocking command must be killed INSIDE the container, and cleaned up.

Observed 2026-08-27 during the census: a step ran a hand-written
socketserver `serve_forever()` through exec.python.run. The host-side timeout
path calls `proc.kill()`, which kills the local `docker exec` CLIENT - the
server inside the container kept running, still holding port 8000 seventeen
minutes later, until it was killed by hand. Its temp script
(/tmp/vera_d010dd24.py) leaked too, because the `rm -f` after the interpreter
never ran.

Servers are deliberately NOT prohibited: starting one is legitimate. It is
bounded and cleaned up, which is what makes allowing it safe.

Imports the app module (plugin: orchestrator first), so it runs in-container.
"""
import shlex

import pytest

try:
    import Vera.vera.capability_orchestration          # noqa: F401
    from Vera.vera.remote import session_sandbox_capabilities as S
except Exception:                                      # pragma: no cover
    S = None

pytestmark = pytest.mark.skipif(S is None, reason="app module not importable here")


def test_this_module_actually_imported_the_app():
    assert S is not None and hasattr(S, "_bounded_cmd")


def test_bound_uses_container_side_timeout():
    got = S._bounded_cmd("python3 /tmp/x.py", 600)
    assert "timeout -k 5 600" in got, "the bound must be applied by timeout(1)"
    assert "python3 /tmp/x.py" in got


def test_bound_probes_for_timeout_rather_than_assuming_it():
    """A base image without coreutils must still run the command, not fail."""
    got = S._bounded_cmd("python3 /tmp/x.py", 60)
    assert "command -v timeout" in got, "must probe"
    assert '_vt=""' in got, "must fall back to running unwrapped"


def test_a_shell_command_is_wrapped_whole_not_just_its_first_word():
    """`$_vt cd /w && python3 ...` would bound `cd` only - the hang would survive."""
    inner = "sh -c " + shlex.quote("cd /workspace && python3 -m http.server")
    got = S._bounded_cmd(inner, 60)
    i_t, i_sh = got.index("$_vt"), got.index("sh -c")
    assert i_t < i_sh, "timeout must precede the shell that runs the whole command"
    assert "cd /workspace && python3 -m http.server" in got


def test_seconds_are_sane():
    """A 0/None/negative bound must floor at 1s, never become `timeout 0` (which
    GNU timeout treats as NO timeout - the exact hang this fixes)."""
    for bad in (0, -5, None):
        got = S._bounded_cmd("x", bad)
        assert 'timeout -k 5 1"' in got, "bound %r did not floor to 1s: %s" % (bad, got)
    assert 'timeout -k 5 0"' not in S._bounded_cmd("x", 0), \
        "`timeout 0` disables the timeout entirely"


def test_timeout_rc_is_the_documented_one():
    assert S._TIMEOUT_RC == 124, "GNU/busybox timeout(1) reports 124"


def test_the_builders_bound_the_interpreter_not_the_cleanup():
    """If the bound wrapped the whole line, `rm -f` would die with it and leak.

    Checked against the source: in every run_code builder the bounded payload
    must come BEFORE `rc=$?; rm -f`, never wrap it.
    """
    import inspect, re
    src = inspect.getsource(S)
    builders = re.findall(r"_payload = _bounded_cmd\(.*?\n\s*script = \((.*?)\)\n",
                          src, re.S)
    assert len(builders) >= 2, "expected both run_code builders to be bounded"
    for b in builders:
        assert "rm -f" in b, "the builder must still clean up its temp file"
        assert b.index("_payload") < b.index("rm -f"), \
            "the bound must cover the interpreter only, so cleanup still runs"
