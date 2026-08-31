"""Point the operator at a file a loop step just wrote.

Census run 18, `author-then-edit`: two `operator.run` calls spent 1262 of the
goal's 1500 seconds and both ended at `max_steps`. The operator traces show why
- it was never on the right page. Its own thoughts:

    "I'm on the VERA orchestrator harness page at localhost:8999/"

while the goal concerned `timer.html`. It spent fifteen steps clicking Vera's
own dashboard ("Auto 5s", "Events") hunting for a countdown timer that lives in
a file nobody gave it a URL for. The outcomes across twelve recorded runs split
cleanly on exactly that:

    goal named a page  ->  done (9 steps), done (6 steps)
    goal named none    ->  max_steps x6, repeating_action, too_many_errors

The machinery to fix this already exists and works. `/remote/sandbox/preview/
{session}/{path}` serves a file straight out of a session sandbox, live - I
wrote a file into a sandbox and fetched it over HTTP before writing this. What
was missing is that using it required the executor to copy a constructed URL out
of a prompt block, and when that did not happen there was no fallback: the
operator opened the sandbox's Vera UI and guessed.

So let the caller name the FILE. `operator.run(goal=..., path="timer.html")`
resolves the preview URL itself. A path is something a step already knows - it
just wrote the file - whereas a URL is something it has to be told and then
reproduce correctly.

Pure: no I/O. The caller supplies the base URL and the session id.
"""

from __future__ import annotations

from typing import Optional


def is_safe_relpath(path: str) -> bool:
    """A path may only address a file INSIDE the sandbox working directory.

    Mirrors the preview route's own check, so a path this module accepts is one
    that route will serve rather than 400.
    """
    rel = str(path or "").strip().lstrip("/")
    if not rel:
        return False
    parts = rel.split("/")
    if ".." in parts:
        return False
    return not any(p in ("", ".") for p in parts)


def preview_url(base_url: str, session_id: str, path: str) -> Optional[str]:
    """The live URL serving `path` out of `session_id`'s sandbox, or None.

    None whenever the inputs cannot make a URL the preview route would honour -
    the caller then falls back to its existing target resolution rather than
    navigating somewhere wrong, which is the failure this exists to end.
    """
    base = str(base_url or "").strip().rstrip("/")
    sid = str(session_id or "").strip()
    rel = str(path or "").strip().lstrip("/")
    if not base or not sid or not is_safe_relpath(rel):
        return None
    return f"{base}/remote/sandbox/preview/{sid}/{rel}"


def looks_like_url(value: str) -> bool:
    """True when the caller passed a URL where a path was expected.

    Models conflate the two constantly, and silently treating a URL as a
    relative path produces a 404 that reads like a missing file.
    """
    v = str(value or "").strip().lower()
    return v.startswith(("http://", "https://", "file://", "//"))


def resolve(url: str, path: str, *, base_url: str, session_id: str) -> dict:
    """Work out what the operator should open.

    Returns ``{url, source, note}``. An explicit `url` always wins - naming a
    URL is unambiguous, and overriding it would be the same class of bug as
    dropping it (see resolve_target's 2026-08-27 note).
    """
    explicit = str(url or "").strip()
    if explicit:
        return {"url": explicit, "source": "url", "note": ""}

    raw = str(path or "").strip()
    if not raw:
        return {"url": "", "source": "", "note": ""}

    if looks_like_url(raw):
        # Charitable, and safe: it IS a URL, just passed in the wrong argument.
        return {"url": raw, "source": "path-was-a-url",
                "note": "`path` looked like a URL and was used as one"}

    resolved = preview_url(base_url, session_id, raw)
    if not resolved:
        return {"url": "", "source": "",
                "note": (f"could not turn path {raw!r} into a sandbox preview URL "
                         f"(session={session_id or 'none'}) - falling back to the "
                         "default target")}
    return {"url": resolved, "source": "sandbox-path",
            "note": f"opening {raw} from sandbox {session_id}"}
