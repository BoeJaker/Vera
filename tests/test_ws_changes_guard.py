"""Workspace-Changes accept clobber-guard (vera/ide/ws_changes_core.py).

The security-critical invariant: ide.workspace.changes.accept must NEVER
overwrite a target that changed since the proposal was reviewed. These are pure
tests of the compare-and-swap decision + the base hasher — no app/redis boot.
"""

import os

from vera.ide import ws_changes_core as core


def test_sha256_file_roundtrip_and_missing(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    import hashlib
    assert core.sha256_file(str(f)) == hashlib.sha256(b"hello").hexdigest()
    # Absent file → None (not a crash), so an "added" file reads as base None.
    assert core.sha256_file(str(tmp_path / "nope.txt")) is None


def test_modified_file_applies_only_onto_its_reviewed_base(tmp_path):
    f = tmp_path / "code.py"
    f.write_bytes(b"v1")
    base = core.sha256_file(str(f))
    entry = {"rel": "code.py", "base_sha": base}
    # Target still == reviewed base → safe to write (NOT a conflict).
    assert core.accept_conflict(entry, core.sha256_file(str(f))) is False
    # Someone edited the target after review → MUST refuse (clobber guard).
    f.write_bytes(b"v2-edited-since-review")
    assert core.accept_conflict(entry, core.sha256_file(str(f))) is True


def test_added_file_applies_only_if_target_still_absent(tmp_path):
    # Proposal ADD: base captured as None (target didn't exist at propose time).
    entry = {"rel": "new.py", "base_sha": None}
    dest = tmp_path / "new.py"
    assert core.accept_conflict(entry, core.sha256_file(str(dest))) is False   # still absent → ok
    dest.write_bytes(b"someone-created-it-first")
    assert core.accept_conflict(entry, core.sha256_file(str(dest))) is True    # now exists → refuse


def test_legacy_proposal_without_base_sha_is_refused(tmp_path):
    # Pre-guard proposal: no base_sha key → base unverifiable → refuse, never
    # risk a clobber. (Regenerate the proposal to get a verifiable base.)
    f = tmp_path / "x.txt"
    f.write_bytes(b"anything")
    assert core.accept_conflict({"rel": "x.txt"}, core.sha256_file(str(f))) is True
    # Even against an absent target, a keyless entry is still refused.
    assert core.accept_conflict({"rel": "x.txt"}, None) is True
