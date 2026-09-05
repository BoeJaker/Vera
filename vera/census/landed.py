"""What actually landed between two censuses.

The panel's "fixes per run" reads board items labelled census:fixed:<run>. The
labels exist for run10-prefixes and `current` and stop there - not because
anything broke, but because the convention stopped being followed. Every fix
landed during censuses 28-34 should have carried one and none did. A record
that depends on somebody remembering to label it will always end up like that,
so this derives the same answer from the repository, which cannot forget.

Attribution is by TIME WINDOW: a commit belongs to the first census that
finished after it. That is not the whole truth, and the untruth is worth naming
because it bit the first version of this file.

A commit's timestamp is when it was CREATED, not when prod started running it.
Measured 2026-09-05: fix/an-anchor-that-differs-only-in-indentation was
committed at 13:26, census 33 ran 10:07-13:45, and prod only restarted onto
that code at 13:57. By commit time alone the fix is credited to a census it
could not possibly have affected.

So a commit made while a run was in flight is flagged `during_run`. Those are
the ones a reader must not trust: the code existed but the loop was still
executing the previous build of it, unless a restart happened mid-run, which
is not something this module can see. Commits that predate the run's start are
unambiguous and carry no flag.

Run start is derived as `ended_at - sum(wall_s)`, which slightly underestimates
the true start (it ignores the gaps between goals) and therefore flags slightly
FEWER commits than it might. It errs toward calling a commit clean, so a flag
that does appear is worth believing.

Pure: parses git output and file times the caller supplies. No subprocess here,
so the whole thing is testable without a repository.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

#: The shape evolve.pipeline.promote writes: "Loop Lab: merge <branch> (pipeline <id>)"
_MERGE_RE = re.compile(r"^Loop Lab: merge (?P<branch>\S+) \(pipeline (?P<pipeline>\w+)\)")

#: Field separator used in the git --pretty format below.
SEP = "\x1f"
#: The exact format `landed` expects. Kept next to the parser so the two cannot
#: drift - the caller passes this to git.
GIT_FORMAT = SEP.join(["%H", "%ct", "%s"])


def parse_log(text: str) -> List[Dict[str, Any]]:
    """git log output -> commits, newest first preserved as given.

    A line that does not split into three fields is skipped rather than
    guessed at: a half-parsed commit attributed to the wrong run is worse than
    a missing one.
    """
    out: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(SEP)
        if len(parts) < 3:
            continue
        sha, ts, subject = parts[0], parts[1], SEP.join(parts[2:])
        try:
            when = int(ts)
        except (TypeError, ValueError):
            continue
        m = _MERGE_RE.match(subject)
        out.append({
            "sha": sha[:10],
            "ts": when,
            "subject": subject,
            "branch": m.group("branch") if m else "",
            "pipeline": m.group("pipeline") if m else "",
            "is_merge": bool(m),
        })
    return out


def assign(commits: Sequence[Dict[str, Any]],
           runs: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group commits by the census they landed before.

    `runs` is [{run_id, ended_at}] in any order; ended_at is epoch seconds.
    Returns {run_id: {window_from, window_to, commits[], count, merges}}.

    The earliest run in `runs` reports NOTHING and keeps window_from=None: it
    has no predecessor, so its window is genuinely unknown, and claiming every
    commit back to the repository's first would be a confident wrong answer.
    Every later run's window opens at the previous run's end.
    """
    ordered = sorted(
        [r for r in (runs or []) if isinstance(r, dict) and r.get("ended_at")],
        key=lambda r: float(r["ended_at"]))
    out: Dict[str, Dict[str, Any]] = {}
    prev_end: Optional[float] = None
    for r in ordered:
        end = float(r["ended_at"])
        rid = str(r.get("run_id") or "")
        started = r.get("started_at")
        picked = []
        for c in (commits or []):
            if not isinstance(c, dict) or c.get("ts") is None:
                continue
            ts = float(c["ts"])
            # The FIRST run in the list has no earlier boundary, so its window
            # is unknown - claiming everything back to the repository's first
            # commit would be a confident wrong answer. It reports nothing and
            # window_from stays None to say why.
            if prev_end is None or ts <= prev_end or ts > end:
                continue
            item = dict(c)
            # Committed while the run was already executing: the code existed
            # but the loop was running the previous build of it.
            item["during_run"] = bool(started) and ts > float(started)
            picked.append(item)
        picked.sort(key=lambda c: -float(c["ts"]))
        out[rid] = {
            "window_from": prev_end,
            "window_to": end,
            "started_at": started,
            "commits": picked,
            "count": len(picked),
            "merges": sum(1 for c in picked if c.get("is_merge")),
            "during_run": sum(1 for c in picked if c.get("during_run")),
        }
        prev_end = end
    return out


def summarise(entry: Optional[Dict[str, Any]]) -> str:
    """One line for a run: what changed before it, in words.

    Names the branches rather than counting commits, because "4 commits" says
    nothing about whether the run should have improved.
    """
    e = entry or {}
    commits = e.get("commits") or []
    if not commits:
        return "nothing landed since the previous census"
    branches = []
    for c in commits:
        b = str(c.get("branch") or "").strip()
        if b and b not in branches:
            branches.append(b)
    if not branches:
        return "%d commit(s), none of them a pipeline merge" % len(commits)
    shown = ", ".join(branches[:4])
    more = "" if len(branches) <= 4 else " and %d more" % (len(branches) - 4)
    mid = int(e.get("during_run") or 0)
    tail = ("" if not mid else
            " (%d committed while this run was already in flight, so it was "
            "probably still running the previous build)" % mid)
    return "%d change(s) landed: %s%s%s" % (len(branches), shown, more, tail)
