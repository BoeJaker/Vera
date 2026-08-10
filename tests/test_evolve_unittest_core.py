"""Unit tests for the ephemeral pytest-runner's pure core (evolve.unittest.run).

Exercise the app-free helpers directly (no docker / no app boot). Imported via the
lowercase `vera.*` path so it binds to THIS worktree, not the main checkout (see
dev-lifecycle-and-repo-hygiene §8.3 #9 / `worktree-testable-cores-pattern`)."""
from vera.evolve.evolve_unittest_core import (
    sanitize_pytest_args, build_inner_cmd, build_docker_argv, parse_pytest_output,
)


# ── sanitise ────────────────────────────────────────────────────────────────────
def test_sanitize_default_paths():
    tokens, err = sanitize_pytest_args()
    assert err == "" and tokens == ["tests"]


def test_sanitize_multiple_paths_marker_extra():
    tokens, err = sanitize_pytest_args("tests/test_a.py tests/test_b.py",
                                       markers="critical", extra="-x --maxfail=1")
    assert err == ""
    assert "tests/test_a.py" in tokens and "tests/test_b.py" in tokens
    assert tokens[tokens.index("-m") + 1] == "critical"
    assert "-x" in tokens and "--maxfail=1" in tokens


def test_sanitize_nodeid_ok():
    tokens, err = sanitize_pytest_args("tests/test_x.py::test_case")
    assert err == "" and tokens == ["tests/test_x.py::test_case"]


def test_sanitize_marker_expression_ok():
    tokens, err = sanitize_pytest_args(markers="critical and not slow")
    assert err == "" and "critical and not slow" in tokens


def test_sanitize_rejects_shell_metachars():
    for bad in ["tests; rm -rf /", "tests && curl evil", "tests|nc", "$(whoami)",
                "tests`id`", "tests > /etc/passwd"]:
        tokens, err = sanitize_pytest_args(bad)
        assert err and tokens == [], f"should reject {bad!r}"


def test_sanitize_rejects_path_traversal():
    tokens, err = sanitize_pytest_args("../../etc/passwd")
    assert err and tokens == []


def test_sanitize_rejects_bad_marker_and_extra():
    _, e1 = sanitize_pytest_args(markers="crit; rm -rf /")
    _, e2 = sanitize_pytest_args(extra="--x; evil")
    assert e1 and e2


# ── inner command ────────────────────────────────────────────────────────────────
def test_inner_cmd_runs_pytest_with_pip_fallback():
    cmd = build_inner_cmd(["tests", "-m", "critical"])
    assert "python -m pytest" in cmd
    assert "import pytest" in cmd and "pip install" in cmd   # fallback until baked in
    assert "-p no:cacheprovider" in cmd                       # no cache writes to :ro mount
    assert "tests" in cmd and "critical" in cmd


def test_inner_cmd_emits_junit_and_rc_markers():
    # deterministic result capture: JUnit XML + an explicit RC marker (the human
    # -q summary line is dropped from a non-TTY capture, so we don't rely on it)
    cmd = build_inner_cmd(["tests"])
    assert "--junitxml=/tmp/vera_junit.xml" in cmd
    assert "__VERA_JUNIT__" in cmd and "cat /tmp/vera_junit.xml" in cmd
    assert "__VERA_RC=${RC}__" in cmd   # braces so the shell doesn't read var RC__


def test_inner_cmd_quotes_tokens():
    # a token with a space must be shell-quoted so it stays one argument
    cmd = build_inner_cmd(["-m", "a and b"])
    assert "'a and b'" in cmd


# ── docker argv ──────────────────────────────────────────────────────────────────
def test_docker_argv_is_ephemeral_and_readonly():
    argv = build_docker_argv("vera:latest", "/home/x/wt", "python -m pytest")
    assert argv[:3] == ["docker", "run", "--rm"]           # throwaway
    joined = " ".join(argv)
    assert "/home/x/wt:/app/Vera:ro" in joined            # branch code, read-only
    assert "PYTHONPATH=/app:/app/Vera" in argv             # both import styles resolve
    assert "PYTHONDONTWRITEBYTECODE=1" in argv             # no .pyc pollution
    assert "vera:latest" in argv                           # the dev image
    assert argv[-3:] == ["sh", "-lc", "python -m pytest"]  # inner cmd is the last arg


def test_docker_argv_has_no_socket_or_ports():
    argv = build_docker_argv("vera:latest", "/wt", "x")
    joined = " ".join(argv)
    assert "docker.sock" not in joined     # never mount the socket into the test box
    assert "-p " not in joined             # a test run publishes no ports


# ── output parsing (JUnit XML + RC marker) ───────────────────────────────────────
def _junit(tests, failures=0, errors=0, skipped=0):
    return (f'<?xml version="1.0"?><testsuites><testsuite name="pytest" '
            f'errors="{errors}" failures="{failures}" skipped="{skipped}" '
            f'tests="{tests}" time="0.3"></testsuite></testsuites>')


def _out(rc, junit=""):
    # what the container emits: dots line, JUnit dump, RC marker
    return (".....  [100%]\n__VERA_JUNIT__\n" + junit + "\n__VERA_RC=%d__" % rc)


def test_parse_all_passed():
    r = parse_pytest_output(_out(0, _junit(12)))
    assert r["ok"] and r["passed"] == 12 and r["failed"] == 0 and r["total"] == 12 and r["rc"] == 0


def test_parse_mixed_failed():
    r = parse_pytest_output(_out(1, _junit(12, failures=1)))
    assert not r["ok"] and r["failed"] == 1 and r["passed"] == 11 and r["total"] == 12


def test_parse_errors_counted():
    r = parse_pytest_output(_out(1, _junit(2, errors=2)))
    assert not r["ok"] and r["errors"] == 2 and r["passed"] == 0


def test_parse_skipped_still_ok():
    r = parse_pytest_output(_out(0, _junit(10, skipped=3)))
    assert r["ok"] and r["skipped"] == 3 and r["passed"] == 7 and r["total"] == 10


def test_parse_ignores_testsuites_wrapper():
    # must read the inner <testsuite ...>, not the <testsuites> wrapper
    r = parse_pytest_output(_out(0, _junit(5)))
    assert r["total"] == 5 and r["passed"] == 5


def test_parse_passed_but_nonzero_rc_is_not_ok():
    # green counts but pytest exited non-zero (e.g. an internal error) → NOT ok
    r = parse_pytest_output(_out(1, _junit(5)))
    assert not r["ok"] and r["passed"] == 5 and r["rc"] == 1


def test_parse_no_tests_marks_not_ok():
    r = parse_pytest_output(_out(5, _junit(0)))
    assert not r["ok"] and r["total"] == 0


def test_parse_fallback_to_summary_line_when_no_junit():
    # pytest crashed before writing XML — fall back to the human summary line
    r = parse_pytest_output("1 failed, 4 passed in 0.2s\n__VERA_RC=1__")
    assert not r["ok"] and r["failed"] == 1 and r["passed"] == 4 and r["total"] == 5


def test_parse_nothing_parseable():
    r = parse_pytest_output("just noise, no markers")
    assert r["total"] == 0 and not r["ok"] and "no results parsed" in r["summary"]
