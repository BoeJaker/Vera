"""Delegate a task to Vera - the pure half (user, 2026-09-28).

"A tool for you to hand over tasks or agent workload to Vera for completion":
Claude hands Vera a brief the way it briefs one of its own agents - a rough
plan, suggested actions, commands, capabilities - and a Vera agentic loop
carries it out in its own worktree, grepping files, keeping the boards up to
date; on the GPU, or GPU + CPU nodes at max effort. It starts as a
CODE-REPORTING agent (finding and reporting on code details and structure);
editing is a later mode, only once its reports are confirmed good.

What makes it safe to run unattended:
  * its own DETACHED worktree of a ref (never prod's checkout), removed after;
  * a session cap guard that admits only the delegate's JAILED read tools
    (evolve.delegate.fs.*, which force the job's worktree as root and refuse
    any path that escapes it - the raw ide.code.* caps read any absolute host
    path) plus, when a board item was given, board.comment pinned to it;
  * async: a job id comes back at once (the MCP bridge times out at 120 s).

Pure: text and rules in, text and rules out. delegate_capabilities runs it.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence

MODES = ("report",)               # "edit" arrives only once reports are confirmed good
EFFORTS = ("standard", "max")
SESSION_PREFIX = "delegate:"
WORKTREE_PREFIX = "delegate-"
DEFAULT_MAX_STEPS = 12
MAX_BRIEF_CHARS = 8000
MAX_REPORT_CHARS = 60000

FS_CAPS = ("evolve.delegate.fs.grep", "evolve.delegate.fs.list",
           "evolve.delegate.fs.read", "evolve.delegate.fs.outline")
BOARD_CAP = "board.comment"
BOARD_FROM = "vera-delegate"


def new_job_id() -> str:
    return "dg" + uuid.uuid4().hex[:10]


def session_for(job_id: str) -> str:
    return SESSION_PREFIX + job_id


def worktree_name(job_id: str) -> str:
    return WORKTREE_PREFIX + job_id


def _list(v: Any) -> List[str]:
    if isinstance(v, (list, tuple)):
        items = v
    else:
        items = re.split(r"[\n,]+", str(v or ""))
    return [str(x).strip() for x in items if str(x).strip()]


def guard_spec(job_id: str, board_item: str = "") -> Dict[str, Any]:
    """The session cap guard for a report-mode job: the jailed read tools,
    each pinned to this job, and board.comment pinned to its item."""
    allow = list(FS_CAPS)
    pins: Dict[str, Dict[str, Any]] = {"evolve.delegate.fs.*": {"job": job_id}}
    if board_item:
        allow.append(BOARD_CAP)
        pins[BOARD_CAP] = {"id": board_item, "frm": BOARD_FROM}
    return {"label": "delegate-report:%s" % job_id, "allow": allow, "pin_args": pins}


def compose_goal(*, title: str, brief: str, plan: Any = "", suggest_caps: Any = "",
                 suggest_commands: Any = "", ref: str = "bleeding-edge", repo: str = "vera",
                 paths: Any = "", board_item: str = "") -> str:
    """The loop's goal: the handover as Claude wrote it, framed as a READ-ONLY
    code-reporting job with the tools it actually has and the report it owes."""
    steps = _list(plan)
    caps = _list(suggest_caps)
    cmds = _list(suggest_commands)
    scope = _list(paths)
    parts = [
        "DELEGATED TASK (from a coding agent): %s" % (title or "code report"),
        "",
        "You are a CODE-REPORTING agent working in a read-only checkout of %s @ %s. "
        "You cannot edit anything. Find and report on the code: its structure, the "
        "details asked for, with exact file paths and line numbers." % (repo, ref),
        "",
        "BRIEF:",
        str(brief or "")[:MAX_BRIEF_CHARS],
    ]
    if steps:
        parts += ["", "ROUGH PLAN (adapt it to what you find):"]
        parts += ["  %d. %s" % (i + 1, s) for i, s in enumerate(steps)]
    if scope:
        parts += ["", "START IN THESE PATHS: " + ", ".join(scope)]
    parts += ["", "YOUR TOOLS (all paths are relative to the checkout root):",
              "  evolve.delegate.fs.grep(pattern, include?, exclude?, is_regex?) - search the code",
              "  evolve.delegate.fs.list(root?, include?) - list files",
              "  evolve.delegate.fs.read(path, start?, end?) - read lines of a file",
              "  evolve.delegate.fs.outline(path) - a file's classes/functions with line numbers"]
    if board_item:
        parts.append("  board.comment(kind='progress', body=...) - post progress on board item %s"
                     % board_item)
    if caps:
        parts += ["", "SUGGESTED BY THE REQUESTER (use what you have; these are hints): "
                  + ", ".join(caps)]
    if cmds:
        parts += ["", "SUGGESTED COMMANDS / SEARCHES (run the equivalent with your tools):"]
        parts += ["  - %s" % c for c in cmds]
    parts += ["", "DELIVER a markdown REPORT with these sections:",
              "  ## Summary - the answer in a few sentences",
              "  ## Findings - each a concrete fact with `path:line`",
              "  ## Structure - how the relevant code fits together",
              "  ## Open questions - what you could not settle, and where to look next",
              "Never state anything you did not read in the code."]
    return "\n".join(parts)


def jail_path(root: str, path: str) -> Optional[str]:
    """The absolute path of `path` inside `root`, or None if it escapes it
    (absolute paths are taken relative to the root; '..' may not climb out;
    symlinks are resolved before the check)."""
    if not root:
        return None
    base = os.path.realpath(root)
    rel = str(path or "").strip().lstrip("/\\") or "."
    full = os.path.realpath(os.path.join(base, rel))
    if full == base or full.startswith(base + os.sep):
        return full
    return None


def relativise(root: str, text: str) -> str:
    """Tool output with the worktree root stripped, so the report cites
    repo-relative paths."""
    base = os.path.realpath(root or "")
    if not base:
        return text
    return str(text or "").replace(base + os.sep, "").replace(base, ".")


def report_from(result: Any) -> str:
    """The job's report from the loop's return: the deliverable, else the
    final answer."""
    r = result if isinstance(result, dict) else {}
    for k in ("deliverable", "final", "summary", "handover_output"):
        v = r.get(k)
        if isinstance(v, str) and v.strip():
            return v[:MAX_REPORT_CHARS]
    return ""


def terminal(status: str) -> bool:
    return status in ("done", "error", "cancelled")
