"""One definition of "does this path already start at the workspace root".

The session workspace root is IMPLICIT: everything is rooted at /workspace, so a
caller handing back `/workspace/x` or `workspace/x` - very often the loop's own
model echoing an `fs_path` from a previous result - must not be re-joined to
/workspace/workspace/x.

Two places decided this independently and disagreed, which is how a shadow file
appears. The READ side (`read_artifact_file`, `artifact_file_exists`) collapsed
`^/?workspace/` unconditionally. The WRITE side (`_code_workspace_path`)
collapsed it only when no `repo` argument was given. So a call carrying BOTH an
absolute path and a repo read the real file and wrote somewhere else, and
reported success.

OBSERVED LIVE, census run 21, goal build-multifile, step 3 (2026-08-31):

    cycle 12  code.edit(path='/workspace/statkit/stats.py', repo='statkit')
              -> ok, fs_path=/workspace/workspace/statkit/stats.py, version 1
    cycle 13  code.edit(path='/workspace/statkit/stats.py')
              -> ok, fs_path=/workspace/statkit/stats.py,           version 2
    cycle 14  code.edit(path='/workspace/statkit/stats.py', repo='/workspace/statkit')

leaving the container holding both

     910 bytes  /workspace/statkit/stats.py            <- what the tests import
    1231 bytes  /workspace/workspace/statkit/stats.py  <- the shadow

with divergent `calculate_mode` bodies: the real file still raising ValueError,
the shadow carrying the fix the model believed it had applied. The step's repair
loop kept reading the same unchanged test failure.

`_code_workspace_path`'s own docstring records this exact failure being fixed on
2026-08-27, in this exact goal. It was fixed for the no-repo case only, and the
executor passes `repo` about half the time - inconsistently, for the same file.

THE RULE, and why `repo` was allowed to suppress the collapse in the first
place: inside a real repository, `workspace/` can be a genuine top-level
directory, and collapsing it there would corrupt the path. That reasoning holds
for a RELATIVE path only. An ABSOLUTE `/workspace/...` names the session
workspace root itself - no repo-relative path is absolute - so it collapses
whatever `repo` says, which is exactly how every read already resolves it.
"""

from __future__ import annotations

import re

# The single definition. Both the read and the write side use this, so they
# cannot drift apart again.
WORKSPACE_PREFIX_RE = re.compile(r"^/?workspace/")

# An absolute path under the workspace root. Checked on the RAW input, before
# any normalisation strips the leading slash and loses the distinction.
ABSOLUTE_WORKSPACE_RE = re.compile(r"^/workspace/")


def collapse_workspace_prefix(path: str) -> str:
    """Strip one redundant leading `workspace/` or `/workspace/`."""
    return WORKSPACE_PREFIX_RE.sub("", str(path or "").replace("\\", "/"))


def is_absolute_workspace(path: str) -> bool:
    """True when the RAW path is rooted at the session workspace itself."""
    return bool(ABSOLUTE_WORKSPACE_RE.match(str(path or "").replace("\\", "/")))


def should_collapse(path: str, repo: str = "") -> bool:
    """Whether `path` carries a redundant workspace prefix that must come off.

    Without a repo, any leading `workspace/` is redundant. With one, only an
    ABSOLUTE `/workspace/...` is - a relative `workspace/...` may be a real
    directory in that repo.
    """
    raw = str(path or "").replace("\\", "/")
    if not WORKSPACE_PREFIX_RE.match(raw):
        return False
    return (not repo) or is_absolute_workspace(raw)
