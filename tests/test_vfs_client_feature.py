"""Tests for the Foundry `vfs-client` feature script builder.

These guard shell-generation logic that runs AS ROOT on a freshly provisioned
guest, from operator-supplied ctx. A quoting regression here is a root command
injection on every machine the estate provisions, which is why they are marked
`critical` and run in the merge gate.

Imported lowercase with the worktree on sys.path so pytest binds to THIS
worktree's copy rather than the main checkout -- see
`worktree-testable-cores-pattern`.
"""
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vera.foundry.features_core import (  # noqa: E402
    FEATURES, VFS_DEFAULT_HOST, VFS_SHARE_PATHS, _q, feature_script,
)

pytestmark = pytest.mark.critical


def build(**ctx):
    return feature_script("vfs-client", ctx)


# ── registration ────────────────────────────────────────────────────────────
def test_feature_is_registered_and_dispatches():
    assert "vfs-client" in FEATURES
    assert build() != ""
    # underscore spelling is accepted too, so a caller using the python-ish
    # form does not silently get an empty script
    assert feature_script("vfs_client", {}) != ""


def test_unknown_feature_still_returns_empty():
    assert feature_script("no-such-feature", {}) == ""


# ── defaults ────────────────────────────────────────────────────────────────
def test_defaults_to_nfs_and_the_known_fabric_host():
    s = build()
    assert VFS_DEFAULT_HOST in s
    assert "nfs4" in s
    assert "vers=4.2" in s
    # NFS must not drag in a credentials file
    assert "/etc/vfs-credentials" not in s


def test_default_shares_are_mounted_under_mnt_vfs():
    s = build()
    for share in ("home", "sync", "media"):
        assert "/mnt/vfs/" + share in s


def test_unknown_share_names_are_dropped_not_mounted():
    s = build(shares=["home", "definitely-not-a-share"])
    assert "/mnt/vfs/home" in s
    assert "definitely-not-a-share" not in s


def test_no_known_shares_produces_a_script_that_says_so():
    s = build(shares=["nope"])
    assert "no known shares requested" in s
    assert "mount -a" not in s


def test_shares_accept_a_comma_string():
    s = build(shares="home, backup")
    assert "/mnt/vfs/home" in s
    assert "/mnt/vfs/backup" in s


# ── estate is read-only ─────────────────────────────────────────────────────
def test_estate_share_is_mounted_read_only():
    s = build(shares=["estate"])
    fstab = [ln for ln in s.splitlines() if "/mnt/vfs/estate" in ln and "printf" in ln]
    assert fstab, "expected an fstab line for the estate share"
    assert ",ro" in fstab[0]


def test_writable_shares_are_not_marked_read_only():
    s = build(shares=["home"])
    fstab = [ln for ln in s.splitlines() if "/mnt/vfs/home" in ln and "printf" in ln]
    assert fstab
    assert ",ro'" not in fstab[0]


# ── CIFS path ───────────────────────────────────────────────────────────────
def test_cifs_requires_credentials_and_refuses_without_them():
    s = build(transport="cifs", shares=["home"])
    assert "needs smb_user and smb_pass" in s
    assert "exit 1" in s
    assert "mount -a" not in s


def test_cifs_uses_smb311_with_sealing_and_a_locked_down_credentials_file():
    s = build(transport="cifs", shares=["home"], smb_user="u", smb_pass="p")
    assert "vers=3.1.1" in s
    assert "seal" in s
    assert "chmod 600 /etc/vfs-credentials" in s
    # the superseded, anonymous default of the generic file-client feature
    assert "vers=3.0" not in s
    assert "guest" not in s


def test_unknown_transport_falls_back_to_nfs_rather_than_failing():
    s = build(transport="carrier-pigeon", shares=["home"])
    assert "nfs4" in s


# ── quoting: the security-relevant part ─────────────────────────────────────
def test_q_wraps_and_escapes_single_quotes():
    assert _q("plain") == "'plain'"
    # the POSIX idiom: close, escaped quote, reopen
    assert _q("it's") == "'it'\\''s'"


def test_a_password_with_shell_metacharacters_cannot_break_out():
    # Substring checks prove nothing here -- the payload legitimately appears
    # inside the quoted word. Parse the line the way a shell would instead and
    # assert the payload is ONE argument, not an argument plus a command.
    nasty = "p'; rm -rf / #"
    s = build(transport="cifs", shares=["home"], smb_user="u", smb_pass=nasty)
    line = next(ln for ln in s.splitlines() if "/etc/vfs-credentials" in ln
                and ln.startswith("printf"))
    words = shlex.split(line.split(">", 1)[0])
    assert nasty in words, "the password must survive as a single shell word"
    assert "rm" not in words, "the payload must not become its own command"


def test_a_hostile_host_value_is_quoted_into_the_fstab_line():
    hostile = "1.2.3.4'; touch /pwned; '"
    s = build(vfs_host=hostile, shares=["home"])
    line = next(ln for ln in s.splitlines()
                if ln.startswith("printf") and "/mnt/vfs/home" in ln)
    words = shlex.split(line.split(">>", 1)[0])
    assert hostile + ":" + VFS_SHARE_PATHS["home"] in words
    assert "touch" not in words


# ── idempotence ─────────────────────────────────────────────────────────────
def test_existing_fstab_entry_is_removed_before_being_rewritten():
    s = build(shares=["home"])
    order = s.index("sed -i"), s.index("printf '%s %s %s %s")
    assert order[0] < order[1], "must delete the old fstab line before appending"


def test_mount_is_bounded_and_nonfatal_so_a_dead_server_cannot_wedge_provisioning():
    s = build()
    assert "timeout 60 mount -a" in s
    assert "nofail" in s


def test_script_is_a_shell_script_with_the_os_adapter():
    s = build()
    assert s.startswith("#!/bin/sh")
    assert "pkg_install" in s
