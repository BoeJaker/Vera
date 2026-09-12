"""pxstore.store.attach -- the pure parts that decide HOW a store gets attached.

Marked critical: both behaviours below broke a real cutover on 2026-09-12.
  * the token-authenticated API always refuses a bind-mount mpN (HTTP 403), so
    without a shell fallback the capability can never succeed;
  * the old default path /root/.ollama/models is untraversable in unprivileged
    containers, which took two Ollama nodes down.
"""
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vera.proxmox.pxstore_attach_core import (  # noqa: E402
    DEFAULT_CT_PATH, is_token_bindmount_refusal, mp_value, pct_set_command,
)

pytestmark = pytest.mark.critical


# ── default path ──────────────────────────────────────────────────────────────
def test_default_path_is_not_under_root():
    assert DEFAULT_CT_PATH == "/.ollama/models"
    assert not DEFAULT_CT_PATH.startswith("/root")


# ── mp_value ──────────────────────────────────────────────────────────────────
def test_mp_value_read_only_by_default():
    assert mp_value("/tank_sdh/vera-store/models/ollama") == \
        "/tank_sdh/vera-store/models/ollama,mp=/.ollama/models,ro=1"


def test_mp_value_writable_when_asked():
    assert mp_value("/s", "/x", ro=False) == "/s,mp=/x"


def test_mp_value_strips_trailing_slash_so_already_attached_matching_works():
    assert mp_value("/s/models/", "/x").startswith("/s/models,")


@pytest.mark.parametrize("host, ct", [("relative/path", "/x"), ("/abs", "relative")])
def test_mp_value_rejects_relative_paths(host, ct):
    with pytest.raises(ValueError):
        mp_value(host, ct)


# ── 403 detection ─────────────────────────────────────────────────────────────
def test_403_is_the_token_refusal():
    assert is_token_bindmount_refusal('HTTP 403: {"data":null}')


@pytest.mark.parametrize("err", ["HTTP 500: boom", "HTTP 401: nope", "", None,
                                 "connection refused", "wrapped HTTP 403 later"])
def test_other_errors_do_not_trigger_the_shell_fallback(err):
    # A fallback on a real failure would only hide it.
    assert not is_token_bindmount_refusal(err)


# ── pct set command ───────────────────────────────────────────────────────────
def test_pct_set_command_shape():
    cmd = pct_set_command(131, "mp0", "/s,mp=/.ollama/models,ro=1")
    assert shlex.split(cmd) == ["pct", "set", "131", "-mp0", "/s,mp=/.ollama/models,ro=1"]


def test_hostile_value_stays_one_shell_word():
    nasty = "/s,mp=/x'; rm -rf / #"
    words = shlex.split(pct_set_command(131, "mp0", nasty))
    assert words[-1] == nasty
    assert "rm" not in words


@pytest.mark.parametrize("vmid", ["131; reboot", "abc", None, 0, -5])
def test_rejects_non_integer_or_nonpositive_vmid(vmid):
    with pytest.raises(ValueError):
        pct_set_command(vmid, "mp0", "/s,mp=/x")


@pytest.mark.parametrize("key", ["mp", "mpX", "rootfs", "mp0; id", ""])
def test_rejects_anything_but_mpN(key):
    with pytest.raises(ValueError):
        pct_set_command(131, key, "/s,mp=/x")
