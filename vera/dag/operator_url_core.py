"""Which of the run's own files an operator.run call meant to open.

Live 2026-10-02 (40 operator runs): every failed live-target run opened on Vera's
dashboard because the call carried no usable url. The loop's existing heal only
rewrote a `localhost:<port>/<file>` guess, or filled a missing url when the STEP
was worded as a UI check and exactly one HTML file existed. What got through:

  * no url, the file named in the goal - "Load timer_app.html from local HTTP
    server at port 8080" (154db5a7d8), "Navigate to the timer page" (61073fd42c);
  * a filesystem path as the url - "/workspace/answer.html" (3ad3229ac5).

Pure: returns the workdir-relative path to preview, or "" to leave the call alone.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

_HTML = (".html", ".htm")
#: Targets that are not a page of ours - never fill a url for these.
NON_PAGE_KINDS = frozenset({"panel", "codeserver", "vm", "novnc", "desktop"})
_WS_PREFIXES = ("/workspace/", "/mnt/data/", "/home/user/", "/app/", "./")


def _html_files(files: Iterable[str]) -> List[str]:
    return [str(f).strip().lstrip("/") for f in (files or [])
            if str(f).strip().lower().endswith(_HTML) and not str(f).endswith("/")]


def _named(text: str, rel: str) -> bool:
    """`text` names `rel` - by its basename, or by its stem as a whole word
    ("the timer page" -> timer.html). Stems under 3 characters never match."""
    t = (text or "").lower()
    base = rel.rsplit("/", 1)[-1].lower()
    if base in t:
        return True
    stem = base.rsplit(".", 1)[0]
    return len(stem) >= 3 and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(stem), t) is not None


def _match_path(path: str, files: List[str]) -> str:
    p = path.replace("\\", "/")
    if p.lower().startswith("file://"):
        p = p[7:]
    for pre in _WS_PREFIXES:
        if p.startswith(pre):
            p = p[len(pre):]
            break
    p = p.lstrip("/")
    if p in files:
        return p
    base = p.rsplit("/", 1)[-1]
    hits = [f for f in files if f.rsplit("/", 1)[-1] == base]
    if len(hits) == 1:
        return hits[0]
    return p if p.lower().endswith(_HTML) else ""


def pick_preview_file(url: str, text: str, workdir_files: Iterable[str],
                      kind: str = "", ui_verify_step: bool = False,
                      operator: bool = True) -> Tuple[str, str]:
    """(relpath, why) - the run file this call should open, or ("", "") to leave it.

    url             the call's url as given (may be empty)
    text            the call's own goal plus the step's title/goal
    workdir_files   the run's files, workdir-relative
    kind            the call's target kind; a non-page kind is never redirected
    ui_verify_step  the step is worded as a UI check (the old gate)
    operator        operator.run (needs a page to start on); False = browser.navigate
    """
    u = (url or "").strip()
    if (kind or "").strip().lower() in NON_PAGE_KINDS:
        return "", ""
    files = _html_files(workdir_files)
    if u:
        if re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I) and not u.lower().startswith("file://"):
            return "", ""                              # a real url - not ours to second-guess
        rel = _match_path(u, files)
        return (rel, "path url") if rel else ("", "")
    if not (operator or ui_verify_step):
        return "", ""
    named = [f for f in files if _named(text, f)]
    if len(named) == 1:
        return named[0], "named in goal"
    if not named and len(files) == 1 and (ui_verify_step or operator):
        return files[0], "only page"
    return "", ""
