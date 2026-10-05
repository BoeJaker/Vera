"""A call that can write files drops the cached content of every registered file it names.

The run's file registry serves a re-read from cache ("served from this run's file registry
(unchanged since it was last read/written)"), refreshed only where the loop itself knows a
write happened. Probe 3 Oct (7c5bf52c): a write the registry did not hear about (prose.author)
left it serving the old report.md, and the verifier failed a finished report twice. Scripts
are the same blind spot: exec.python.run rewriting data.csv after it was read. A call that can
write and NAMES a registered file now forgets that file's cached content, so the next read goes
to disk. It can only cost a read; it never serves anything stale. Pure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

WRITE_PREFIXES = ("exec.",)
WRITE_TOOLS = frozenset({"sandbox.session.fs.write", "code.save", "ide.fs.write", "file.write",
                         "fs.write", "artifact.write"})


def can_write(tool: str) -> bool:
    t = str(tool or "")
    return t.startswith(WRITE_PREFIXES) or t in WRITE_TOOLS


def forget_named(artifacts: Dict[str, Dict[str, Any]], tool: str, args: Any) -> List[str]:
    """Drop cached content of registered files `args` names, when `tool` can write.
    Returns the keys forgotten."""
    if not can_write(tool) or not artifacts:
        return []
    try:
        blob = json.dumps(args, default=str)
    except Exception:
        blob = str(args)
    out: List[str] = []
    for key, rec in list(artifacts.items()):
        if not key or not isinstance(rec, dict) or not rec.get("content"):
            continue
        if key in blob:
            rec["content"] = ""
            out.append(key)
    return out
