"""
evolve_unittest_core.py — pure helpers for evolve.unittest.run
==============================================================
The ephemeral-test-container runner (the real fix for dev-lifecycle-and-repo-
hygiene §8.3 #9) spins a FRESH `vera:latest` via `docker run --rm` to run a
branch's pytest suite WITHOUT touching the container that's serving HTTP. The
docker orchestration itself needs the app (`_sh`, the sandbox pool), but the pure
parts — sanitising caller-supplied pytest args, assembling the `docker run` argv +
inner shell command, and parsing pytest's summary line — are dependency-free and
live here so they are unit-testable app-free (imported via lowercase
`vera.evolve.evolve_unittest_core`; see `worktree-testable-cores-pattern`).
"""
from __future__ import annotations

import re
import shlex
from typing import Dict, List, Tuple

# What a caller may pass through to pytest. Deliberately strict — these tokens are
# interpolated into a `docker run … sh -lc "<cmd>"` string, so anything that could
# break out of the intended pytest invocation is REJECTED rather than escaped.
_PATH_RE   = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./:-]*$")   # a test path / node id
_MARKER_RE = re.compile(r"^[A-Za-z0-9_ ()-]+$")               # a `-m` marker expression
_EXTRA_RE  = re.compile(r"^[A-Za-z0-9_./=: -]*$")             # extra flags, no shell metachars


def sanitize_pytest_args(paths: str = "tests", markers: str = "",
                         extra: str = "") -> Tuple[List[str], str]:
    """Validate + tokenise the caller-supplied pytest args. Returns (tokens, error);
    on anything unsafe, error is non-empty and tokens is []."""
    tokens: List[str] = []
    for p in (paths or "tests").split():
        if ".." in p or not _PATH_RE.match(p):
            return [], f"unsafe test path: {p!r}"
        tokens.append(p)
    if not tokens:
        tokens = ["tests"]
    if markers and markers.strip():
        m = markers.strip()
        if not _MARKER_RE.match(m):
            return [], f"unsafe marker expression: {markers!r}"
        tokens += ["-m", m]
    if extra and extra.strip():
        if ".." in extra or not _EXTRA_RE.match(extra):
            return [], f"unsafe extra args: {extra!r}"
        tokens += extra.split()
    return tokens, ""


def build_inner_cmd(arg_tokens: List[str]) -> str:
    """The `sh -lc` command run INSIDE the ephemeral container: make pytest
    importable (pip-install as a fallback until it's baked into the image), run it
    with bytecode + cache writes disabled (nothing is written back to the read-only
    worktree mount), then emit a JUnit XML dump + an explicit RC marker. The XML +
    marker are what we parse — deterministic, unlike pytest's human '-q' summary line
    (which is dropped from a non-TTY capture) and unlike the container exit code
    (which would just be the final `echo`'s 0)."""
    q = " ".join(shlex.quote(t) for t in arg_tokens)
    return (
        "python -c 'import pytest' 2>/dev/null || "
        "pip install -q pytest pytest-asyncio >/dev/null 2>&1; "
        f"python -m pytest {q} --junitxml=/tmp/vera_junit.xml -p no:cacheprovider -q; "
        "RC=$?; echo __VERA_JUNIT__; cat /tmp/vera_junit.xml 2>/dev/null; "
        "echo; echo \"__VERA_RC=${RC}__\""   # ${RC} braces: $RC__ would parse as var RC__
    )


def build_docker_argv(image: str, worktree_abs: str, inner_cmd: str) -> List[str]:
    """`docker run --rm` argv for the ephemeral test container. The worktree is
    mounted READ-ONLY at /app/Vera and PYTHONPATH covers both /app (so `Vera.vera.*`
    resolves) and /app/Vera (so lowercase `vera.*` resolves) — BOTH import styles
    bind to the BRANCH code. No socket, no ports, no bytecode writes: a throwaway
    that runs pytest and exits."""
    return [
        "docker", "run", "--rm",
        "-v", f"{worktree_abs}:/app/Vera:ro",
        "-e", "PYTHONPATH=/app:/app/Vera",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "TLS_ENABLED=0",
        "-e", "VERA_IS_DEV_SANDBOX=1",
        "--add-host", "host.docker.internal:host-gateway",
        "-w", "/app/Vera",
        image,
        "sh", "-lc", inner_cmd,
    ]


def parse_pytest_output(out: str) -> Dict:
    """Parse the ephemeral runner's output. build_inner_cmd appends a JUnit XML dump
    (between __VERA_JUNIT__ and __VERA_RC=<n>__, the latter carrying pytest's REAL
    exit code) — deterministic, unlike the human '-q' summary line pytest omits from
    a non-TTY capture. Falls back to scraping the summary line if the XML is absent
    (e.g. pytest crashed before writing it). Returns pass/fail/error/skip counts,
    rc, ok, and a human summary."""
    text = out or ""
    rc = None
    m = re.search(r"__VERA_RC=(-?\d+)__", text)
    if m:
        rc = int(m.group(1))
    tests = failures = errors = skipped = 0
    found = False
    # `<testsuite\b` matches `<testsuite ` but NOT `<testsuites` (no word boundary
    # after the trailing 's'), so we read the inner suite, not the wrapper.
    for tag in re.findall(r"<testsuite\b[^>]*>", text):
        if 'tests="' in tag:
            found = True
            tests    = max(tests, _iattr(tag, "tests"))
            failures = _iattr(tag, "failures")
            errors   = _iattr(tag, "errors")
            skipped  = _iattr(tag, "skipped")
    if not found:
        # fallback: scrape pytest's human summary line
        for line in reversed(text.strip().splitlines()):
            s = line.strip().strip("=").strip()
            if re.search(r"\b(passed|failed|errors?|skipped|no tests ran)\b", s, re.I):
                failures = _count(r"(\d+) failed", s)
                errors   = _count(r"(\d+) errors?", s)
                skipped  = _count(r"(\d+) skipped", s)
                tests    = _count(r"(\d+) passed", s) + failures + errors + skipped
                found    = bool(re.search(r"\d", s))
                break
    passed = max(0, tests - failures - errors - skipped)
    ok = (failures == 0 and errors == 0 and tests > 0 and (rc is None or rc == 0))
    summary = (f"{passed} passed, {failures} failed, {errors} errors, {skipped} skipped (rc={rc})"
               if (found or tests) else f"(no results parsed; rc={rc})")
    return {"ok": ok, "passed": passed, "failed": failures, "errors": errors,
            "skipped": skipped, "total": tests, "rc": (rc if rc is not None else -1),
            "summary": summary}


def _iattr(tag: str, name: str) -> int:
    m = re.search(rf'\b{name}="(\d+)"', tag)
    return int(m.group(1)) if m else 0


def _count(pat: str, s: str) -> int:
    m = re.search(pat, s, re.I)
    return int(m.group(1)) if m else 0
