"""What an exec.<lang>.run call with BOTH `code` and `path` means. Pure.

The per-language exec caps say "provide EITHER code OR path", and the agentic
loop's model does neither: it routinely hands over the script's full source in
`code` AND the file it wants that script to be in `path`, in one call. Inside a
session sandbox that call ran the PATH - which did not exist yet - and the
interpreter answered "can't open file", while the source sat unused in `code`.
Nothing about the retry changed, so the retry failed the same way.

Read from the census archive (board loop-o43), the same shape in both runs:

    run 47  research-web  cycles 5-9, 12, 15   code=yes  path=/workspace/extract_mdn_webgpu.py   -> can't open file
    run 48  research-web  cycle 24             code=yes  path=/workspace/parse_chrome_webgpu.py   -> can't open file

Seven of run 47's eight exec failures and run 48's one had this shape; both
goals wall-capped with no artifact. The model had not "forgotten" to write the
file - it delivered the content and the destination together and the contract
threw the content away.

`decide` names the outcome the router should take. The interesting case is
MATERIALISE: the destination does not exist and real code arrived, so write the
code to the path and run it - which is both what the call meant and the only
outcome that leaves the named artifact behind for the steps that follow.
"""

from __future__ import annotations

import re
from typing import Any

#: "python /art/app.py" - the invocation string a model sometimes passes as
#: `code` when it means "run this file". That is not source, so it is never
#: written anywhere. Mirrors exec_capabilities._INVOCATION_RE.
_INVOCATION_RE = re.compile(
    r'^\s*(?:python3?|node|nodejs|ruby|php|perl|lua|deno|bash|sh)\s+'
    r'(["\']?)([^"\']+\.[A-Za-z0-9]+)\1\s*$')
_BARE_PATH_RE = re.compile(r'^["\']?[~/.][^\n]*\.[A-Za-z0-9]+["\']?$')

RUN_PATH = "run_path"            # run the existing file; inline code, if any, ignored
RUN_INLINE = "run_inline"        # no path: run the snippet from a temp file
MATERIALISE = "materialise"      # write `code` to `path` (absent), then run it
NOTHING = "nothing"              # neither code nor path


def is_real_code(code: Any) -> bool:
    """Source, as opposed to an invocation string or a bare file path."""
    s = str(code or "").strip()
    if not s:
        return False
    if "\n" in s:
        return True
    if _INVOCATION_RE.match(s) or _BARE_PATH_RE.match(s):
        return False
    return True


def decide(code: Any, path: Any, path_exists: bool) -> str:
    """The router's decision for one call. `path_exists` is the container's
    answer for `path`; it is ignored when there is no path."""
    p = str(path or "").strip().strip('"\'')
    if not p:
        return RUN_INLINE if str(code or "").strip() else NOTHING
    if path_exists:
        return RUN_PATH
    return MATERIALISE if is_real_code(code) else RUN_PATH


def materialised_note(path: str) -> str:
    """Appended to the run's result so the next step knows the file now exists
    and does not author it again."""
    return ("%s did not exist; the inline code from this call was written there "
            "and then run. The file now exists - edit it rather than re-sending "
            "the whole script." % path)
