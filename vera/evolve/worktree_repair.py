"""Non-destructive recovery for a fully severed Git worktree."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import uuid


_BRANCH = re.compile(r"^(?:feat|fix|test|docs|chore)/[A-Za-z0-9._/-]{1,200}$")
_HASH_LIMIT = 16 * 1024 * 1024


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True,
                          capture_output=True, timeout=120, check=False)


def _safe_name(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")


def _manifest(root: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_symlink():
            target = os.readlink(path).encode("utf-8", "surrogateescape")
            result[rel] = (len(target), "link:" + hashlib.sha256(target).hexdigest())
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        digest = ""
        if size <= _HASH_LIMIT:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[rel] = (size, digest)
    return result


def _copy_overlay(source: Path, target: Path) -> None:
    for item in source.iterdir():
        if item.name == ".git":
            continue
        destination = target / item.name
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, destination, dirs_exist_ok=True, symlinks=True)
        elif item.is_symlink():
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            destination.symlink_to(os.readlink(item))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


_GONE = re.compile(r"^Error(?: response from daemon)?: No such (?:object|container): (\S+)\s*$")
MOUNTS_FORMAT = "{{.Id}}{{range .Mounts}}\t{{.Source}}{{end}}"


def mounts_from_inspect(ids: list[str], out: str, err: str,
                        code: int) -> tuple[dict[str, set[str]] | None, str]:
    """Parse ONE `docker inspect -f MOUNTS_FORMAT <ids...>` over every container.

    Returns ({full_id: {mount sources}}, "") or (None, why). A container that
    vanished between `docker ps` and the inspect cannot mount anything, so its
    "No such object" is not a failure; any OTHER error still refuses (unknown
    ownership is never permission). Every listed id must be accounted for.
    """
    mounts: dict[str, set[str]] = {}
    for line in (out or "").splitlines():
        parts = line.strip().split("\t")
        if parts and parts[0]:
            mounts[parts[0]] = {p.strip().replace("\\", "/").rstrip("/")
                                for p in parts[1:] if p.strip()}
    gone: set[str] = set()
    for line in (err or "").splitlines():
        if not line.strip():
            continue
        m = _GONE.match(line.strip())
        if not m:
            return None, line.strip()[:300]
        gone.add(m.group(1))
    if code != 0 and not gone:
        return None, (err or "docker inspect exited %s" % code).strip()[:300]
    for cid in ids:
        if not any(full.startswith(cid) or cid.startswith(full) for full in mounts) \
                and not any(g.startswith(cid) or cid.startswith(g) for g in gone):
            return None, "container %s not accounted for by docker inspect" % cid[:12]
    return mounts, ""


def _registered(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    current = ""
    listed = _run(root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        raise RuntimeError("cannot inspect Git worktree registry")
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            current = line[9:].strip()
        elif line.startswith("branch refs/heads/") and current:
            result[line[len("branch refs/heads/"):].strip()] = current
    return result


def plan_severed_worktree_repair(repo_root: str | Path, branch: str) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    branch = str(branch or "").strip()
    if not _BRANCH.fullmatch(branch):
        raise ValueError("invalid repair branch")
    if _run(root, "rev-parse", "--show-toplevel").returncode != 0:
        raise ValueError("repo_root is not a Git repository")
    worktree = (root / ".loop-lab-worktrees" / _safe_name(branch)).resolve()
    expected_parent = (root / ".loop-lab-worktrees").resolve()
    if worktree.parent != expected_parent:
        raise ValueError("repair target escapes Loop Lab worktrees")
    ref = _run(root, "show-ref", "--verify", f"refs/heads/{branch}")
    if ref.returncode != 0:
        raise ValueError("repair branch does not exist")
    if not worktree.is_dir() or not (worktree / ".git").is_file():
        raise ValueError("repair target is not a severed worktree candidate")
    registered = _registered(root)
    if branch in registered:
        raise ValueError("branch is already registered to a worktree")
    probe = _run(worktree, "status", "--porcelain")
    detail = (probe.stderr or probe.stdout).lower()
    if probe.returncode == 0 or "not a git repository" not in detail:
        raise ValueError("target is not a confirmed severed worktree")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = (expected_parent / ".quarantine" /
                  f"{worktree.name}-{stamp}-{uuid.uuid4().hex[:8]}")
    return {"branch": branch, "worktree": str(worktree),
            "quarantine": str(quarantine), "dry_run": True, "mutated": False,
            "action": "quarantine_recreate_overlay_verify",
            "quarantine_retained": True}


def repair_severed_worktree(repo_root: str | Path, branch: str) -> dict[str, Any]:
    plan = plan_severed_worktree_repair(repo_root, branch)
    root = Path(repo_root).resolve()
    worktree = Path(plan["worktree"])
    quarantine = Path(plan["quarantine"])
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    if quarantine.exists():
        raise RuntimeError("quarantine target already exists")
    before = _manifest(worktree)
    tracked = _run(root, "ls-tree", "-r", "--name-only", branch)
    if tracked.returncode != 0:
        raise RuntimeError("cannot enumerate tracked branch files")
    tracked_paths = {line for line in tracked.stdout.splitlines() if line}
    os.replace(worktree, quarantine)
    failed_copy: Path | None = None
    try:
        added = _run(root, "worktree", "add", str(worktree), branch)
        if added.returncode != 0:
            raise RuntimeError("git worktree add failed: " + added.stderr.strip()[:300])
        _copy_overlay(quarantine, worktree)
        # Preserve tracked deletions represented by absence in the orphan.
        for rel in tracked_paths - set(before):
            candidate = worktree / rel
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate)
        after = _manifest(worktree)
        lost = sorted(set(before) - set(after))
        changed = sorted(key for key, value in before.items()
                         if key in after and after[key] != value)
        if lost or changed:
            raise RuntimeError(f"preservation verification failed: lost={lost[:5]} changed={changed[:5]}")
        healthy = _run(worktree, "rev-parse", "--is-inside-work-tree")
        if healthy.returncode != 0 or healthy.stdout.strip() != "true":
            raise RuntimeError("recreated worktree failed Git verification")
        return {**plan, "ok": True, "dry_run": False, "mutated": True,
                "preserved_files": len(before), "tracked_deletions":
                len(tracked_paths - set(before))}
    except Exception:
        if worktree.exists():
            failed_copy = quarantine.parent / (
                quarantine.name + "-failed-recreate-" + uuid.uuid4().hex[:8])
            os.replace(worktree, failed_copy)
        # Remove only the exact fresh registration created above. The path is
        # absent at this point, so Git drops its metadata without deleting the
        # preserved orphan or the retained partial recreation.
        _run(root, "worktree", "remove", "--force", str(worktree))
        os.replace(quarantine, worktree)
        raise
