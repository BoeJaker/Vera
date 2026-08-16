"""Critical-tier regression tests for tools/hooks/pre-push.

Guards the llm.int GitHub deploy key: once prod can push, a force-push or a
branch deletion could rewrite/destroy shared history on main / bleeding-edge.
The hook must refuse those and allow the normal fast-forward case. Self-
contained — builds a throwaway git repo so it never touches the real repo.
"""
import os
import shutil
import subprocess

import pytest

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "tools", "hooks", "pre-push")
)
ZERO = "0" * 40


def _tooling_ok():
    return (shutil.which("git") is not None
            and shutil.which("sh") is not None
            and os.path.exists(HOOK))


@pytest.fixture
def repo(tmp_path):
    if not _tooling_ok():
        pytest.skip("git / sh / the pre-push hook not available in this environment")
    d = tmp_path / "r"
    d.mkdir()
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(*a):
        subprocess.run(["git", *a], cwd=d, check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rev():
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()

    g("init", "-q")
    (d / "f").write_text("a")
    g("add", "."); g("commit", "-qm", "A")
    a_sha = rev()
    (d / "f").write_text("b")
    g("add", "."); g("commit", "-qm", "B")
    b_sha = rev()
    # a_sha is an ancestor of b_sha
    return str(d), a_sha, b_sha


def _run(repo_dir, line, env_extra=None):
    """Invoke the hook exactly as git would: remote name/url as argv, the
    per-ref line on stdin. Returns the exit code (0 = allowed, 1 = refused)."""
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(["sh", HOOK, "origin", "git@github.com:x/y.git"],
                       cwd=repo_dir, input=line, text=True,
                       capture_output=True, env=env)
    return p.returncode


def test_fast_forward_to_main_allowed(repo):
    d, a, b = repo   # push new tip b where remote has ancestor a -> fast-forward
    assert _run(d, f"refs/heads/main {b} refs/heads/main {a}\n") == 0


def test_force_rewind_to_main_refused(repo):
    d, a, b = repo   # push older a where remote has b -> non-fast-forward (force)
    assert _run(d, f"refs/heads/main {a} refs/heads/main {b}\n") == 1


def test_delete_main_refused(repo):
    d, a, b = repo   # local side all-zero = deletion
    assert _run(d, f"refs/heads/main {ZERO} refs/heads/main {b}\n") == 1


def test_force_bleeding_edge_refused(repo):
    d, a, b = repo
    assert _run(d, f"refs/heads/bleeding-edge {a} refs/heads/bleeding-edge {b}\n") == 1


def test_feature_branch_force_allowed(repo):
    d, a, b = repo   # feature branches are not protected
    assert _run(d, f"refs/heads/feat/x {a} refs/heads/feat/x {b}\n") == 0


def test_override_env_allows_force(repo):
    d, a, b = repo
    assert _run(d, f"refs/heads/main {a} refs/heads/main {b}\n",
                {"VERA_ALLOW_FORCE_PUSH": "1"}) == 0


def test_first_time_branch_create_allowed(repo):
    d, a, b = repo   # remote side all-zero = branch does not exist yet
    assert _run(d, f"refs/heads/main {b} refs/heads/main {ZERO}\n") == 0
