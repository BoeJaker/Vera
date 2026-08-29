"""Prod must write a log, out of the tree, without blocking the event loop.

Vera is started by hand, so its stdout is whatever terminal launched it — on the
live instance `/proc/<pid>/fd/1` is a `/dev/pts` and `vera_start.log` has been
stale since 2026-08-13. Every incident on 2026-08-29 had to be diagnosed through
endpoints and sockets instead of a log.

The three ways this fix could itself become a problem, each pinned here:

  * a log written INSIDE the repo dirties the tracked tree, and a dirty tree
    blocks every promote;
  * a file handler that is not moved off the event loop reintroduces the stall
    class `_offload_blocking_log_handlers` was written to kill;
  * a logging misconfiguration that raises would stop the whole instance.

Pure: no Redis, no app import - so it runs anywhere, including the host venv.
"""
import logging
import logging.handlers
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.log_setup import (          # noqa: E402
    DEFAULT_BACKUPS, DEFAULT_MAX_BYTES, MAX_BACKUPS, MAX_MAX_BYTES,
    MIN_MAX_BYTES, OFFLOAD_ATTR, resolve_log_config, should_offload,
)

DEFAULT_DIR = "/var/tmp/vera-state/logs"


# ── defaults ────────────────────────────────────────────────────────────────
def test_the_default_log_lands_out_of_tree_under_the_state_dir():
    cfg = resolve_log_config({}, default_dir=DEFAULT_DIR)
    assert cfg["enabled"] is True
    assert cfg["path"] == os.path.join(DEFAULT_DIR, "vera.log")
    assert cfg["max_bytes"] == DEFAULT_MAX_BYTES
    assert cfg["backups"] == DEFAULT_BACKUPS
    assert cfg["level"] == logging.INFO


def test_rotation_is_bounded_by_default():
    """A log that never rotates is how vera_start.log reached 14 MB and stopped
    being useful. Both bounds must be real."""
    cfg = resolve_log_config({}, default_dir=DEFAULT_DIR)
    assert cfg["max_bytes"] > 0 and cfg["backups"] > 0


# ── explicit configuration ──────────────────────────────────────────────────
def test_an_explicit_path_wins():
    cfg = resolve_log_config({"VERA_LOG_FILE": "/srv/logs/v.log"}, default_dir=DEFAULT_DIR)
    assert cfg["path"] == "/srv/logs/v.log"


def test_a_blank_or_whitespace_path_falls_back_to_the_default():
    for raw in ("", "   "):
        cfg = resolve_log_config({"VERA_LOG_FILE": raw}, default_dir=DEFAULT_DIR)
        assert cfg["path"] == os.path.join(DEFAULT_DIR, "vera.log")


def test_file_logging_can_be_turned_off():
    for raw in ("0", "false", "no", "off", "", "FALSE"):
        assert resolve_log_config({"VERA_LOG_FILE_ENABLED": raw},
                                  default_dir=DEFAULT_DIR)["enabled"] is False
    for raw in ("1", "true", "yes", "on"):
        assert resolve_log_config({"VERA_LOG_FILE_ENABLED": raw},
                                  default_dir=DEFAULT_DIR)["enabled"] is True


def test_level_is_configurable_and_survives_nonsense():
    assert resolve_log_config({"VERA_LOG_FILE_LEVEL": "warning"},
                              default_dir=DEFAULT_DIR)["level"] == logging.WARNING
    assert resolve_log_config({"VERA_LOG_FILE_LEVEL": "DEBUG"},
                              default_dir=DEFAULT_DIR)["level"] == logging.DEBUG
    # An unknown name must not become the string "Level BANANAS".
    got = resolve_log_config({"VERA_LOG_FILE_LEVEL": "BANANAS"},
                             default_dir=DEFAULT_DIR)["level"]
    assert got == logging.INFO and isinstance(got, int)


# ── a bad value costs a default, never the instance ─────────────────────────
def test_unparseable_sizes_fall_back_instead_of_raising():
    cfg = resolve_log_config({"VERA_LOG_MAX_BYTES": "loads",
                              "VERA_LOG_BACKUPS": ""}, default_dir=DEFAULT_DIR)
    assert cfg["max_bytes"] == DEFAULT_MAX_BYTES
    assert cfg["backups"] == DEFAULT_BACKUPS


def test_sizes_are_clamped_to_something_survivable():
    huge = resolve_log_config({"VERA_LOG_MAX_BYTES": str(10 ** 15),
                               "VERA_LOG_BACKUPS": "9999"}, default_dir=DEFAULT_DIR)
    assert huge["max_bytes"] == MAX_MAX_BYTES
    assert huge["backups"] == MAX_BACKUPS
    tiny = resolve_log_config({"VERA_LOG_MAX_BYTES": "-5",
                               "VERA_LOG_BACKUPS": "-1"}, default_dir=DEFAULT_DIR)
    assert tiny["max_bytes"] == MIN_MAX_BYTES
    assert tiny["backups"] == 0          # 0 backups is legitimate, -1 is not


# ── the off-loop sweep must pick ours up, and still leave others alone ──────
def test_our_file_handler_is_swept_off_the_event_loop():
    """A file handler is a StreamHandler subclass writing to a disk that can
    stall. If the sweep misses it, the stall class comes straight back."""
    h = logging.handlers.RotatingFileHandler(os.devnull, delay=True)
    assert should_offload(h) is False          # untagged: not ours, left alone
    setattr(h, OFFLOAD_ATTR, True)
    assert should_offload(h) is True
    h.close()


def test_the_plain_console_handlers_are_still_swept():
    assert should_offload(logging.StreamHandler()) is True


def test_foreign_handlers_are_still_left_alone():
    """The original `type(h) is` test existed to avoid touching handlers Vera
    did not install. Widening it to a blanket isinstance would swallow those."""
    import queue
    assert should_offload(logging.handlers.QueueHandler(queue.SimpleQueue())) is False
    assert should_offload(logging.NullHandler()) is False

    class SomeoneElsesConsole(logging.StreamHandler):
        pass
    assert should_offload(SomeoneElsesConsole()) is False
