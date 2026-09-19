"""A bot token must never reach the log.

Telegram takes the token in the URL path, and httpx logs every request at INFO,
so prod's own vera.log held a live credential ~3,000 times a day (2026-09-13).
The token is not in git (logs/ is ignored) but a log is a thing people paste.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera import log_setup  # noqa: E402

pytestmark = pytest.mark.critical

# Token SHAPE only, assembled here so no credential-looking literal is stored.
FAKE = "1234567890:" + ("A" * 35)
URL = "https://api.telegram.org/bot" + FAKE + "/getUpdates"


def test_the_token_is_masked_but_the_bot_id_survives():
    out = log_setup.redact("HTTP Request: POST " + URL + ' "HTTP/1.1 200 OK"')
    assert ("A" * 35) not in out
    assert "/bot1234567890:<redacted>" in out, out
    assert "getUpdates" in out and "200 OK" in out


def test_a_line_without_a_secret_is_untouched():
    line = "ollama_req [abc] model=qwen3.5:9b inst=gpu-250 job=chat rule=profile:chat"
    assert log_setup.redact(line) == line


def test_named_secrets_are_masked_too():
    assert "hunter2hunter2" not in log_setup.redact("api_key=hunter2hunter2xyz")
    assert "<redacted>" in log_setup.redact('"access_token": "abcdefghijklmnop"')


def _record(msg, *args):
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


def test_the_filter_masks_the_message_and_the_args():
    f = log_setup.RedactingFilter()
    rec = _record("HTTP Request: %s %s", "POST", URL)
    assert f.filter(rec) is True
    assert ("A" * 35) not in rec.getMessage()
    assert "/bot1234567890:<redacted>" in rec.getMessage()


def test_the_filter_never_drops_or_raises_on_odd_records():
    f = log_setup.RedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, {"not": "a string"}, None, None)
    assert f.filter(rec) is True
    rec2 = _record("count=%d", 3)
    assert f.filter(rec2) is True
    assert rec2.getMessage() == "count=3"
