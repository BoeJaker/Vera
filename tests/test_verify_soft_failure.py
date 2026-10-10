"""An ok call whose output reports a failure is flagged to the verifier; an empty error field is not a report."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.dag import verify_evidence_core as VE  # noqa: E402

pytestmark = pytest.mark.critical

# the prose.author result of census step dec_1b0fd35c (2026-10-10), flagged as an error before
AUTHOR_OK = ('{"ok": true, "path": "README.md", "fs_path": "/workspace/README.md", "version": 1, "bytes": 4584, '
             '"lang": "markdown", "chars": 4578, "truncated": false, "grounded": true, "ungrounded_refs": [], '
             '"error": "", "note": "Grounded against the real working-directory listing."}')


def test_an_empty_error_field_is_not_a_failure():
    assert VE.soft_failure(AUTHOR_OK) == ""
    for empty in ('"error": null', '"errors": []', '"failed": 0', '"stderr_error": ""', '"fatal": false',
                  '"exception": {}', '"Error":""'):
        assert VE.soft_failure('{"ok": true, ' + empty + '}') == "", empty


def test_a_reported_failure_still_is():
    assert "Error: file not found" in VE.soft_failure('{"ok": true, "stdout": "x"}\nError: file not found')
    assert VE.soft_failure('{"ok": true, "error": "permission denied"}')            # a non-empty error field
    assert VE.soft_failure('{"ok": true, "failed": 2, "passed": 5}')                 # a count above zero
    assert "Traceback" in VE.soft_failure("ok\nTraceback (most recent call last):\n  File x")
    assert VE.soft_failure("") == "" and VE.soft_failure(None) == ""
    assert len(VE.soft_failure("error " + "x" * 500)) == 160


def test_the_verifier_uses_it():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    body = src[src.index("    def _soft_failed(h) -> str:"):][:400]
    assert "_verify_evidence.soft_failure(h.get(\"preview\"))" in body
