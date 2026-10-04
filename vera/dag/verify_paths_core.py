"""Which file a step's success check really means, and which files a verdict leans on. Pure.

Two verifier failures from the 4 Oct 2026 census:

* A criterion naming a path from ANOTHER environment. The planner wrote "the report is
  saved to /tmp/redis_valkey_report.md"; the loop cannot write there (its own guard calls
  /tmp/..., /mnt/..., /w/... "an absolute path from a DIFFERENT environment") so it wrote
  ./redis_valkey_report.md, and the hard gate failed the step on the path alone - the run
  then spent its wall cap retrying a finished report. `foreign_alias` maps such a path to
  the same file name in the working directory.

* A "met" verdict resting on a file that never existed: the judge passed a step because the
  run journal mentioned /w/scratch/qdrant_docs.txt. `cited_paths` is the explicit paths a
  verdict's reason names, so the verifier can probe them before trusting it.
"""

from __future__ import annotations

import posixpath
import re
from typing import Iterable, List

#: Where the loop's files actually live, as the sandbox names them.
WORKDIR_PREFIXES = ("/workspace/", "/workspace")

_PATHLIKE = re.compile(r"(?<![\w:/.])(/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)")


def foreign_alias(path: str) -> str:
    """For an absolute path OUTSIDE the working directory, the bare file name the loop
    would have written instead; '' for a relative path, a /workspace path, or a path with
    no file name."""
    p = str(path or "").strip()
    if not p.startswith("/") or p.startswith(WORKDIR_PREFIXES):
        return ""
    base = posixpath.basename(p.rstrip("/"))
    return base if ("." in base and base not in (".", "..")) else ""


def cited_paths(reason: str, limit: int = 6) -> List[str]:
    """Explicit slashed paths a verdict's reason names (URLs excluded): the files the
    judge leans on. Bare names are left out on purpose - 'report.md' may well be a real
    file in a subfolder, and a wrong 'missing' would fail a good step."""
    out: List[str] = []
    s = str(reason or "")
    for m in _PATHLIKE.finditer(s):
        start = m.start(1)
        if "://" in s[max(0, start - 12):start + 3]:
            continue
        p = m.group(1).rstrip(".,;:)'\"")
        if "." not in posixpath.basename(p):
            continue
        if p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def missing_after_alias(paths: Iterable[str], exist: dict) -> List[str]:
    """The paths that are absent under their own name AND under their foreign alias.
    A path whose probe was not decided (absent from `exist`) is not reported missing."""
    out = []
    for p in paths:
        if exist.get(p) is not False:
            continue
        alias = foreign_alias(p)
        if alias and exist.get(alias) is True:
            continue
        out.append(p)
    return out
