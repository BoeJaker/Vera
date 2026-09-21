"""Pure helpers for listing a run's working directory BELOW its top level.

Why this exists (2026-09-20, census runs 54 and 55, goal `build-multifile`):
the loop's success-criterion gate asked "is there a file with one of the
extensions the criterion names (.py)?" and answered it from a TOP-LEVEL
listing of /workspace. A goal that builds a package writes
`statkit/__init__.py` and `statkit/stats.py`; the top-level listing was
`["statkit"]`, so the gate hard-failed every step - "no file in the working
directory actually has one" - while the files sat one directory down. The
loop then re-created the package directory until the wall cap, twice in a row
on two censuses, and the coder model took the blame.

Nothing here does I/O. The caller hands in `list_dir(relpath)`, an async
function that returns the entries of ONE directory as `(name, is_dir)` pairs
(or None when the directory could not be read); `walk_listing` walks it
breadth-first with hard caps so a run that wrote a node_modules tree cannot
turn a gate check into a crawl.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Iterable, List, Optional, Sequence, Tuple

DirLister = Callable[[str], Awaitable[Optional[Sequence[Tuple[str, bool]]]]]

# Directories no deliverable ever lives in; walking them only costs time.
SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", ".git", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".cache", "dist", "build", ".tox", "site-packages",
})


def is_dir_kind(kind) -> bool:
    """The sandbox's `ls` script reports 'directory'; older callers wrote
    'dir'. Both are a directory; anything else is a file."""
    return str(kind or "").strip().lower() in ("dir", "directory", "d")


def _join(rel: str, name: str) -> str:
    return f"{rel.rstrip('/')}/{name}" if rel else name


async def walk_listing(list_dir: DirLister, root: str = "", *,
                       max_depth: int = 3, max_dirs: int = 12,
                       limit: int = 200) -> Optional[List[str]]:
    """Relative paths of everything under `root`, breadth-first, directories
    with a trailing '/'. Hidden entries (dot-prefixed) are skipped, as the
    top-level listing already skips them.

    Caps: directories up to `max_depth` levels below the root are OPENED
    (their contents listed; deeper directories are named but not entered),
    at most `max_dirs` directories opened (root included), at most `limit`
    names returned.
    Returns None only when the ROOT could not be listed - a subdirectory
    that fails to list is simply left unexpanded, so one unreadable
    directory cannot hide the rest of the tree.
    """
    top = await list_dir(root)
    if top is None:
        return None
    out: List[str] = []
    queue: List[Tuple[str, int]] = []   # (relpath, depth)
    opened = 1

    def _take(rel: str, entries: Sequence[Tuple[str, bool]], depth: int) -> None:
        for name, is_dir in sorted(entries, key=lambda e: (not e[1], str(e[0]).lower())):
            name = str(name or "").strip()
            if not name or name.startswith("."):
                continue
            path = _join(rel, name)
            out.append(path + "/" if is_dir else path)
            if is_dir and depth <= max_depth and name not in SKIP_DIRS:
                queue.append((path, depth + 1))

    _take("", top, 1)
    while queue and opened < max_dirs and len(out) < limit:
        rel, depth = queue.pop(0)
        entries = await list_dir(_join(root, rel) if root else rel)
        opened += 1
        if entries is None:
            continue
        _take(rel, entries, depth)
    return out[:max(1, limit)]


def files_with_extensions(files: Iterable[str], exts: Iterable[str]) -> List[str]:
    """The entries of `files` (relative paths, dirs end in '/') whose extension
    is one of `exts` (with or without a leading dot, any case)."""
    wanted = tuple("." + str(e).lower().lstrip(".") for e in exts if str(e).strip())
    if not wanted:
        return []
    return [f for f in files
            if not str(f).endswith("/") and str(f).lower().endswith(wanted)]
