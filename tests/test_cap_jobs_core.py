"""Tests for vera/background/cap_jobs_core.py — pure, no app.

The queue runs unattended and is reachable over an unauthenticated LAN call,
so most of what matters here is what it REFUSES.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.background import cap_jobs_core as core  # noqa: E402

KNOWN = {"llm.generate", "web.fetch", "fabric.ingest", "ha.set", "sys.dev.restart",
         "background.enqueue", "evolve.pipeline.promote", "cluster.job.stop"}


class TestValidate:
    def test_a_normal_cap_is_accepted(self):
        assert core.validate_request("llm.generate", {"prompt": "x"}, known=KNOWN) == (True, "")

    def test_empty_name(self):
        ok, why = core.validate_request("", {}, known=KNOWN)
        assert not ok and "required" in why

    def test_malformed_name(self):
        ok, why = core.validate_request("not a cap!", {}, known=KNOWN)
        assert not ok and "not a capability name" in why

    def test_unknown_cap(self):
        ok, why = core.validate_request("nope.nothing", {}, known=KNOWN)
        assert not ok and "unknown" in why

    @pytest.mark.parametrize("name", [
        "sys.dev.restart", "background.enqueue", "evolve.pipeline.promote",
        "cluster.job.stop", "evolve.bleeding_edge.promote_to_main",
        "census.control.set", "jobs.purge_pending"])
    def test_dangerous_caps_are_refused(self, name):
        # A queue job must never restart the box, promote code, or touch the
        # queue that runs it - whatever the caller's intent.
        ok, why = core.validate_request(name, {}, known=KNOWN | {name})
        assert not ok and "may not run" in why

    def test_deny_is_case_insensitive(self):
        assert core.is_denied("SYS.DEV.RESTART")

    def test_arguments_must_be_an_object(self):
        ok, why = core.validate_request("web.fetch", ["x"], known=KNOWN)
        assert not ok and "object" in why

    def test_arguments_may_not_smuggle_trace_id(self):
        ok, why = core.validate_request("web.fetch", {"trace_id": "x"}, known=KNOWN)
        assert not ok and "trace_id" in why

    def test_none_arguments_is_fine(self):
        assert core.validate_request("web.fetch", None, known=KNOWN)[0]

    def test_known_none_skips_registry_check(self):
        assert core.validate_request("anything.at_all", {}, known=None)[0]


class TestTimeout:
    def test_default_when_unset(self):
        assert core.clamp_timeout(None) == core.DEFAULT_TIMEOUT_S
        assert core.clamp_timeout(0) == core.DEFAULT_TIMEOUT_S
        assert core.clamp_timeout("junk") == core.DEFAULT_TIMEOUT_S

    def test_clamped_to_the_maximum(self):
        # One bad job must not hold the queue all night.
        assert core.clamp_timeout(99999) == core.MAX_TIMEOUT_S

    def test_clamped_to_the_minimum(self):
        assert core.clamp_timeout(1) == core.MIN_TIMEOUT_S

    def test_in_range_passes_through(self):
        assert core.clamp_timeout(120) == 120.0


class TestLabelsAndKeys:
    def test_bg_label_names_cap_and_job(self):
        assert core.bg_label("llm.generate", "cap:abcdef123456xyz") == "cap:llm.generate:cap:abcdef12"

    def test_result_key(self):
        assert core.result_key("cap:1") == "vera:background:cap:cap:1"

    def test_job_title_defaults(self):
        assert core.job_title("ha.set") == "cap ha.set"
        assert core.job_title("ha.set", "lights off") == "lights off"

    def test_build_payload(self):
        p = core.build_payload("llm.generate", {"prompt": "hi"}, 60, "n8n intel-night")
        assert p == {"name": "llm.generate", "arguments": {"prompt": "hi"},
                     "timeout_s": 60.0, "submitted_by": "n8n intel-night"}

    def test_build_payload_copies_arguments(self):
        args = {"a": 1}
        p = core.build_payload("x.y", args)
        p["arguments"]["a"] = 2
        assert args["a"] == 1


class TestOutcome:
    def test_done(self):
        assert core.outcome({"text": "ok"}) == "done"
        assert core.outcome("plain") == "done"

    def test_failed(self):
        assert core.outcome({"error": "boom"}) == "failed"

    def test_timeout(self):
        assert core.outcome({"error": "timeout"}) == "timeout"

    def test_cancelled(self):
        assert core.outcome({"error": "cancelled"}) == "cancelled"
        assert core.outcome({"error": "x", "cancelled": True}) == "cancelled"


class TestSummarise:
    def test_prefers_error(self):
        assert core.summarise_result({"error": "bad", "text": "x"}).startswith("error: bad")

    def test_uses_text(self):
        assert core.summarise_result({"text": "line one\nline two"}) == "line one line two"

    def test_falls_back_to_keys(self):
        assert core.summarise_result({"b": 1, "a": 2}) == "ok: a, b"

    def test_truncates(self):
        assert len(core.summarise_result({"text": "x" * 500})) == 160
