"""A capability that failed must say why, whatever it calls the field."""
import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.dag.result_failure_reason import failure_reason, BODY_FIELDS


def test_the_operator_result_from_census_16_now_explains_itself():
    """The exact shape that produced 'failed with no error detail' after 478s."""
    res = {"ok": False, "done": False, "goal": "verify inline validation",
           "reason": "step ceiling reached after 12 steps without completing the goal",
           "run_id": "op-1f2e", "screenshots": ["a.png"], "session_id": "3a3ef869",
           "step_count": 12}
    out = failure_reason(res, 0)
    assert "step ceiling reached" in out
    assert "no error detail" not in out


def test_a_shell_failure_still_leads_with_stderr():
    assert failure_reason({"rc": 1, "stdout": "partial", "stderr": "boom"}, 1) == "boom"


def test_stderr_outranks_a_reason():
    """Order matters: the specific error must not be displaced by a general one."""
    res = {"stderr": "Permission denied", "reason": "the step did not complete"}
    assert failure_reason(res, 1) == "Permission denied"


def test_an_http_failure_leads_with_its_status():
    out = failure_reason({"status": 404, "body": "not found"}, 0)
    assert out.startswith("HTTP 404")
    assert "not found" in out


def test_an_http_failure_with_no_body_still_names_the_status():
    assert failure_reason({"status_code": 500}, 0) == "HTTP 500 (no response body)"


def test_a_status_is_reported_even_when_the_reason_is_the_only_text():
    out = failure_reason({"status": 403, "reason": "forbidden by allowlist"}, 0)
    assert out == "HTTP 403: forbidden by allowlist"


def test_a_summary_is_used_only_when_nothing_else_exists():
    res = {"ok": False, "summary": "navigated to the form and typed an address"}
    assert failure_reason(res, 0) == "navigated to the form and typed an address"


def test_a_summary_never_displaces_a_real_error():
    res = {"error": "connection refused", "summary": "tried to open the page"}
    assert failure_reason(res, 0) == "connection refused"


def test_a_genuinely_empty_result_says_so_and_names_its_keys():
    out = failure_reason({"ok": False, "done": False}, 0)
    assert "no error detail" in out
    assert "done" in out and "ok" in out


def test_whitespace_only_fields_are_not_treated_as_an_explanation():
    res = {"stderr": "   ", "error": "", "reason": "the real cause"}
    assert failure_reason(res, 0) == "the real cause"


def test_empty_containers_are_skipped():
    """screenshots:[] must not win over a populated reason."""
    res = {"stdout": [], "body": {}, "reason": "the real cause"}
    assert failure_reason(res, 0) == "the real cause"


def test_a_long_body_is_clipped():
    assert len(failure_reason({"stderr": "x" * 5000}, 1)) == 600


def test_a_non_dict_is_survivable():
    assert "no error detail" in failure_reason(None, 1)


def test_unsortable_keys_do_not_crash_the_fallback():
    """A mixed-type key set must not raise while reporting a failure."""
    out = failure_reason({1: None, "ok": False}, 0)
    assert "no error detail" in out


@pytest.mark.parametrize("field", ["reason", "detail"])
def test_the_fields_this_bug_was_about_are_all_read(field):
    assert failure_reason({field: "the cause"}, 0) == "the cause"
    assert field in BODY_FIELDS
