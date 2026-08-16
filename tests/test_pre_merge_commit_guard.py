"""Critical-tier regression tests for tools/hooks/pre-merge-commit (M3.6 part 2).

The pre-commit hook blocks direct commits to `main` but EXEMPTS merge commits (the
gate landing), leaving one hole: a raw `git merge <branch>` run by hand on the live
`main` checkout. This hook must block that, allow it under the sanctioned
VERA_ALLOW_MAIN_COMMIT=1 deploy override (what the pipeline's own merge sets), and
never touch non-main branches. Self-contained — a throwaway repo, real merges, so
it exercises the hook exactly the way git invokes it.
"""
import os
import shutil
import subprocess

import pytest

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "tools", "hooks", "pre-merge-commit")
)

_IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _tooling_ok():
    return (shutil.which("git") is not None
            and shutil.which("sh") is not None
            and os.path.exists(HOOK))


@pytest.fixture
def repo(tmp_path):
    if not _tooling_ok():
        pytest.skip("git / sh / the pre-merge-commit hook not available in this environment")
    d = tmp_path / "r"
    d.mkdir()
    env = {**os.environ, **_IDENT}

    def g(*a):
        return subprocess.run(["git", *a], cwd=d, env=env,
                              capture_output=True, text=True)

    g("init", "-q")
    # version-safe way to put the unborn branch on `main` (no -b flag needed)
    g("symbolic-ref", "HEAD", "refs/heads/main")
    (d / "f").write_text("a"); g("add", "."); g("commit", "-qm", "A")
    # feature branch diverges from A
    g("checkout", "-q", "-b", "feat/x")
    (d / "g").write_text("c"); g("add", "."); g("commit", "-qm", "C")
    # main advances too, so a merge is a real (non-fast-forward) merge commit
    g("checkout", "-q", "main")
    (d / "h").write_text("d"); g("add", "."); g("commit", "-qm", "D")
    # install ONLY this hook (avoid pulling in pre-commit's secret-scan etc.)
    hooks = d / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    dst = hooks / "pre-merge-commit"
    shutil.copy(HOOK, dst)
    os.chmod(dst, 0o755)
    return str(d)


def _merge(repo_dir, target, source, env_extra=None):
    env = {**os.environ, **_IDENT}
    if env_extra:
        env.update(env_extra)
    subprocess.run(["git", "checkout", "-q", target], cwd=repo_dir, env=env,
                   capture_output=True, text=True)
    p = subprocess.run(["git", "merge", "--no-ff", "-m", "m", source],
                       cwd=repo_dir, env=env, capture_output=True, text=True)
    return p.returncode


def test_hand_merge_onto_main_is_blocked(repo):
    assert _merge(repo, "main", "feat/x") != 0


def test_merge_onto_main_allowed_with_sanctioned_override(repo):
    assert _merge(repo, "main", "feat/x", {"VERA_ALLOW_MAIN_COMMIT": "1"}) == 0


def test_merge_onto_a_feature_branch_is_allowed(repo):
    # target is feat/x, not main -> the hook must not interfere
    assert _merge(repo, "feat/x", "main") == 0
