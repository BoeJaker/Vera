"""Prod must write a log it can be read back from.

Vera is started by hand (`python -m Vera.vera.capability_orchestration`), so its
stdout is whatever terminal happened to launch it — `/proc/<pid>/fd/1` on the
live instance points at `/dev/pts/8`, and `vera_start.log` has been stale since
2026-08-13. The consequence is not theoretical: every incident in the
2026-08-29 session had to be diagnosed through HTTP endpoints and sockets
because there was no log to read, including a module that was believed to be
failing its startup import with the error swallowed.

Fixing this at the launcher would only work for launches that use the launcher.
Doing it *in process* works however Vera is started, which is the point.

Three things this module is careful about, each of which has burned Vera before:

  * **The log must not land inside the repo.** A machine writer whose target is
    inside the checkout dirties the tracked tree, and a dirty tree blocks every
    promote (dev-lifecycle §8.2 #7). The default is out-of-tree under the state
    root, and an explicitly configured in-tree path is REFUSED rather than
    honoured. That is also why the old in-tree `vera_start.log` is not reused.

  * **The file handler must not block the event loop.** A blocked console
    already caused a captured 1.5s stall inside `stream.write`, which is why
    `_offload_blocking_log_handlers` exists. A file handler is a
    `StreamHandler` subclass writing to a disk that can stall (full disk, NFS,
    a snapshotting filesystem), so it must go behind the same
    QueueHandler/QueueListener pair. Handlers are tagged with `OFFLOAD_ATTR`
    so that machinery can select ours precisely, without touching foreign
    handlers it was deliberately written to leave alone.

  * **Logging setup must never stop startup.** Every value falls back to a
    working default rather than raising: a fat-fingered `VERA_LOG_MAX_BYTES`
    should cost you a non-default rotation size, not the instance.

Pure: no app imports, no I/O. It decides the CONFIG; the caller builds the
handler.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping, Optional

# Handlers carrying this attribute are ours, and are safe for
# `_offload_blocking_log_handlers` to move behind a queue.
OFFLOAD_ATTR = "_vera_offloadable"

DEFAULT_FILENAME = "vera.log"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024        # 32 MB per file
DEFAULT_BACKUPS = 5                          # ~192 MB ceiling, all in
DEFAULT_LEVEL = "INFO"

# Bounds, so a mis-set env cannot wedge the disk or make rotation meaningless.
MIN_MAX_BYTES, MAX_MAX_BYTES = 64 * 1024, 2 * 1024 * 1024 * 1024
MIN_BACKUPS, MAX_BACKUPS = 0, 50

_FALSEY = {"0", "false", "no", "off", ""}


def _flag(raw: Optional[str], default: bool = True) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in _FALSEY


def _bounded_int(raw: Optional[str], default: int, lo: int, hi: int) -> int:
    """An unparseable or out-of-range value costs you the default, never a crash."""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _level(raw: Optional[str]) -> int:
    name = str(raw or DEFAULT_LEVEL).strip().upper()
    got = logging.getLevelName(name)
    # getLevelName returns the string "Level X" for anything it doesn't know.
    return got if isinstance(got, int) else logging.INFO


def resolve_log_config(env: Mapping[str, str], *, default_dir: Any) -> dict:
    """Decide where and how the file log is written.

    `default_dir` is the out-of-tree directory to use when `VERA_LOG_FILE` is
    unset (the caller passes `state_paths.state_dir("logs")`). Returns a dict
    with `enabled`, `path`, `max_bytes`, `backups` and `level`.
    """
    path = str(env.get("VERA_LOG_FILE", "") or "").strip()
    if not path:
        path = os.path.join(str(default_dir), DEFAULT_FILENAME)
    return {
        "enabled": _flag(env.get("VERA_LOG_FILE_ENABLED"), True),
        "path": path,
        "max_bytes": _bounded_int(env.get("VERA_LOG_MAX_BYTES"),
                                  DEFAULT_MAX_BYTES, MIN_MAX_BYTES, MAX_MAX_BYTES),
        "backups": _bounded_int(env.get("VERA_LOG_BACKUPS"),
                                DEFAULT_BACKUPS, MIN_BACKUPS, MAX_BACKUPS),
        "level": _level(env.get("VERA_LOG_FILE_LEVEL")),
    }


def already_installed(handlers, path: str) -> bool:
    """True if one of ours is already writing to `path`.

    `capability_orchestration` is imported more than once under different module
    names — Vera is a namespace package, so `Vera.vera.capability_orchestration`
    and `vera.capability_orchestration` are separate module objects that each run
    the module body. Observed live: three copies, so the root logger collected
    three file handlers and every line was written to the log three times.

    The root logger is the shared thing here, so asking IT what is already
    attached is the honest check — a module-level "did I run" flag would be a
    per-copy answer to a process-wide question.
    """
    target = os.path.abspath(str(path))
    for h in handlers or []:
        if not getattr(h, OFFLOAD_ATTR, False):
            continue
        existing = getattr(h, "baseFilename", "")
        if existing and os.path.abspath(str(existing)) == target:
            return True
    return False


def should_offload(handler: logging.Handler) -> bool:
    """True for handlers whose writes must be moved off the event loop.

    Exactly the plain console handlers the original sweep targeted, PLUS the
    ones we tagged. Deliberately still not a blanket `isinstance` check: foreign
    handlers (a third-party RotatingFileHandler, a QueueHandler already in
    place) are left alone, which is what the original `type(h) is` test was
    protecting.
    """
    return type(handler) is logging.StreamHandler or bool(
        getattr(handler, OFFLOAD_ATTR, False))


# ── secrets must not reach the log file ──────────────────────────────────────
# Telegram puts the bot token in the URL PATH, and httpx logs every request at
# INFO ("HTTP Request: POST https://api.telegram.org/bot<token>/getUpdates").
# Prod's own log therefore held a live credential in plaintext on every poll -
# ~3,000 lines a day - and anything that ships a log (a paste, a bug report,
# `logs/` in a backup) ships the token with it. Silencing httpx would cost the
# request logging that diagnoses routing, so the token is masked instead, as
# late as possible: one filter on the handlers, so every logger is covered
# whatever it passes as msg or args.
_SECRET_PATTERNS = (
    # Telegram: /bot<digits>:<35-ish urlsafe chars>/method
    re.compile(r"(/bot)(\d{5,})(:)([A-Za-z0-9_\-]{20,})"),
    # Anything that spells its own secret out in a query string or header dump.
    re.compile(r"((?:api_?key|access_?token|auth_?token|password|secret)"
               r"[\"'\s:=]{1,4})([A-Za-z0-9_\-.]{12,})", re.I),
)


def redact(text: str) -> str:
    """Mask credentials in a log line. Keeps enough to correlate (the bot id,
    the parameter name) and drops the part that authenticates."""
    out = str(text)
    if "/bot" in out:
        out = _SECRET_PATTERNS[0].sub(r"\1\2\3<redacted>", out)
    if any(w in out.lower() for w in ("key", "token", "password", "secret")):
        out = _SECRET_PATTERNS[1].sub(r"\1<redacted>", out)
    return out


def _mask_arg(a):
    """Redact one format argument. httpx logs the request URL as an httpx.URL
    OBJECT, not a str - `'HTTP Request: %s %s ...', request.method, request.url`
    - so a str-only check let the token through and prod kept writing it
    (2026-09-19). Anything whose string form carries a secret is replaced by
    the redacted string; everything else is returned untouched, so typed args
    (the %d status code, uvicorn's unpacked tuple) keep their types."""
    if isinstance(a, str):
        return redact(a)
    if a is None or isinstance(a, (int, float, bool, bytes)):
        return a
    try:
        s = str(a)
    except Exception:
        return a
    r = redact(s)
    return r if r != s else a


class RedactingFilter(logging.Filter):
    """Masks secrets in the record's message AND its args.

    A filter, not a formatter: the record passes through every handler, and the
    args are where httpx keeps the URL. Returning True always - this never drops
    a line, it only rewrites one. Never raises: a logging filter that throws
    takes out the log call that used it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and ("/bot" in record.msg or ":" in record.msg):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _mask_arg(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(_mask_arg(a) for a in record.args)
        except Exception:
            pass
        return True


def install_redaction() -> bool:
    """Redact at the point every LogRecord is CREATED, whatever handler it ends
    up in. Idempotent.

    A handler filter was not enough: the first cut put RedactingFilter on the
    handlers present when log_setup's file handler was built, and prod kept
    writing tokens - perf_capabilities opens a second RotatingFileHandler on the
    in-tree logs/vera.log LATER in startup, and that one never saw the filter
    (2026-09-19). Handlers come and go; the record factory is the one choke
    point every logger shares. Also wrapped: the QueueHandler offload copies
    records, so this runs before any copy is made.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, "_vera_redacting", False):
        return False
    _filter = RedactingFilter()

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        _filter.filter(record)
        return record

    factory._vera_redacting = True          # type: ignore[attr-defined]
    factory._vera_wrapped = current         # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
    return True
