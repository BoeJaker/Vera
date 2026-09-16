"""
stall_trace_core.py — pick the frame that names the blocking call.

Pure, dependency-free, importable without the app (see tests/test_stall_trace_core.py).

The event-loop stall dumper (capability_orchestration._start_stall_stack_dumper)
captures the main thread's stack and has to reduce it to one short "where" for
the Perf UI table and for perf.scan's "Blocking call(s): ..." line.

The original rule was "deepest frame that is not under /site-packages/". That
reads well but is wrong in practice, because the standard library is not under
site-packages either: a stall inside `json.loads` over a large payload reports
`decoder.py:353`, a stall interrupted by a GC weakref callback reports
`_weakrefset.py:39`, and a loop blocked in native code with no application
frame at all reports `runners.py:118` (the bottom of asyncio's own stack). All
three were observed on prod on 2026-09-16, and all three hid the frame that
actually mattered — session_sandbox_capabilities.py:1418 — which was sitting
directly above `json.loads` in two of those same stacks.

So the rule here prefers, in order:
  1. the deepest frame inside the Vera package        (the actionable one)
  2. the deepest frame that is neither stdlib nor site-packages (vendored/app)
  3. the deepest non-site-packages frame              (the old behaviour)
and reports stdlib-only stacks honestly rather than dressing them up as a
location in the code: a stack with no application frame means the loop was not
executing Python at all, which is a different diagnosis (GIL/CPU starvation)
and should not be labelled with whatever stdlib line the sampler happened to
land on.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# `  File "/path/to/x.py", line 123, in func`
_FRAME_RE = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)')

# A stdlib path looks like /usr/lib/python3.11/... or .../lib/python3.11/...
_STDLIB_RE = re.compile(r"/(?:lib|lib64)/python3[.\d]*/")

#: Returned when the stack contains no Python frame worth naming.
NO_APP_FRAME = "(no app frame - loop blocked in native code)"


def parse_frames(stack: str) -> List[Tuple[str, str]]:
    """[(path, lineno)] in the order they appear (outermost first)."""
    out: List[Tuple[str, str]] = []
    for ln in (stack or "").splitlines():
        m = _FRAME_RE.match(ln)
        if m:
            out.append((m.group("path"), m.group("line")))
    return out


def _is_site_packages(path: str) -> bool:
    return "/site-packages/" in path or "\\site-packages\\" in path


def _is_stdlib(path: str) -> bool:
    if _STDLIB_RE.search(path):
        return True
    # `<frozen runpy>` and friends carry no directory at all.
    return path.startswith("<") and path.endswith(">")


def _short(path: str, lineno: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{name}:{lineno}"


def _loop_entry_index(frames: List[Tuple[str, str]]) -> int:
    """Index of the last asyncio runner frame, or -1.

    Everything at or above this is process bootstrap, not work the loop is
    doing. It matters because the bootstrap frame IS a Vera frame — every
    stack contains `capability_orchestration.py:<module>` calling
    `uvicorn.run(...)` — so "deepest Vera frame" alone would label a loop
    blocked in native code with the line that started the server four hours
    earlier. Only frames BELOW run_until_complete are the loop's current work.
    """
    last = -1
    for i, (path, _ln) in enumerate(frames):
        if "/asyncio/runners.py" in path or "/asyncio/base_events.py" in path:
            last = i
    return last


def stall_where(stack: str, package_dir: Optional[str] = None) -> str:
    """Reduce a formatted traceback to one short `file.py:line`.

    `package_dir` is the absolute path of the Vera package (pass
    `str(Path(__file__).resolve().parent)` from inside it). It is matched as a
    path prefix, so it works whether prod runs from /home/boejaker/Vera/vera or
    a container runs from /app/Vera/vera. When it is None or matches nothing,
    the caller still gets the best available frame via the later passes.
    """
    frames = parse_frames(stack)
    if not frames:
        return ""

    pkg = (package_dir or "").replace("\\", "/").rstrip("/")
    norm = [(p.replace("\\", "/"), ln) for p, ln in frames]

    # Consider only what the loop is actually running (see _loop_entry_index).
    # With no runner frame at all (a worker-thread stack) take the whole thing.
    entry = _loop_entry_index(norm)
    body = norm[entry + 1:] if entry >= 0 else norm

    # 1. Deepest frame inside the Vera package — the line someone can act on.
    if pkg:
        for path, lineno in reversed(body):
            if path.startswith(pkg + "/"):
                return _short(path, lineno)

    # 2. Deepest frame that is neither stdlib nor a third-party package. Covers
    #    a Vera checkout whose path we could not derive, and vendored code.
    for path, lineno in reversed(body):
        if not _is_stdlib(path) and not _is_site_packages(path):
            return _short(path, lineno)

    # 3. No application frame below the loop entry at all. That is itself the
    #    finding — the main thread was not executing Python, which is a
    #    different diagnosis (GIL/CPU starvation) from a blocking call. Say so
    #    rather than naming whatever stdlib line the sampler landed on.
    return NO_APP_FRAME
