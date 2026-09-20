"""A wait line must never become a second lying health indicator.

vera/agents/queue_status_core.py turns "this turn has not started yet" into a
sentence for the chat window. The temptation is to render `in_use == 0` as "no
queue" — and that is exactly the mistake that cost 2026-09-20. `in_use`, the
ollama request log and the GPU gate only count requests **Vera** made; the CPU
nodes are shared with n8n, the sandboxes and each other, and gpu-250 sat wedged
for eight hours while every one of those signals said it was free.

So the load-bearing tests here are the ones about SILENCE: when Vera cannot see
what is holding a node, the line says nothing rather than something reassuring.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.agents import queue_status_core as qs  # noqa: E402


def _job(job_type="loop_executor", age=240.0, model="qwen3.5:9b", caller="run"):
    return {"job_type": job_type, "age_s": age, "model": model, "caller": caller}


# ── the honesty property ─────────────────────────────────────────────────────

def test_nothing_known_says_nothing_at_all():
    """NOT 'no queue'. An empty in_use is not evidence of an empty node."""
    st = qs.describe_wait("gpu-250", [], resident_model="m", resident_ctx=4096,
                          requested_model="m", requested_ctx=4096,
                          runner_busy=None)
    assert st["state"] == "clear"
    assert st["text"] == ""
    assert qs.should_emit(st) is False


def test_an_unknown_runner_is_never_reported_as_free():
    """runner_busy=None means we could not ask — it must not read as False."""
    st = qs.describe_wait("cpu-247", [], resident_model="m", resident_ctx=4096,
                          requested_model="m", requested_ctx=4096,
                          runner_busy=None)
    assert "free" not in st["text"].lower()
    assert "idle" not in st["text"].lower()
    assert st["text"] == ""


def test_a_busy_runner_with_no_vera_jobs_is_named_as_such():
    """The case Vera is blind to everywhere else: someone else's work."""
    st = qs.describe_wait("cpu-246", [], resident_model="m", resident_ctx=4096,
                          requested_model="m", requested_ctx=4096,
                          runner_busy=True)
    assert st["state"] == "busy_elsewhere"
    assert "did not start" in st["text"]
    assert qs.should_emit(st) is True


# ── naming what is ahead ─────────────────────────────────────────────────────

def test_a_job_ahead_is_named_with_its_age():
    st = qs.describe_wait("gpu-250", [_job(age=240.0)])
    assert st["state"] == "queued"
    assert "loop_executor" in st["text"]
    assert "4m" in st["text"]
    assert "1 job" in st["text"]


def test_several_jobs_report_the_oldest_and_a_count():
    st = qs.describe_wait("gpu-250", [_job(age=30.0), _job("code", 300.0),
                                      _job("chat", 12.0)])
    assert "3 jobs" in st["text"]
    assert "code" in st["text"], "the oldest should lead"
    assert "+2 more" in st["text"]


def test_a_job_ahead_takes_priority_over_the_node_probe():
    """With a named job we already know the answer; don't muddy it."""
    st = qs.describe_wait("gpu-250", [_job()], runner_busy=True)
    assert st["state"] == "queued"


def test_a_pending_model_load_is_mentioned_alongside_the_queue():
    st = qs.describe_wait("gpu-250", [_job()],
                          resident_model="other", resident_ctx=4096,
                          requested_model="qwen3.5:9b", requested_ctx=12288)
    assert st["state"] == "queued"
    assert "loading the model" in st["text"]


def test_a_barely_started_job_is_not_worth_a_line():
    st = qs.describe_wait("gpu-250", [_job(age=0.2)])
    assert st["state"] == "clear"


# ── the model load ───────────────────────────────────────────────────────────

def test_loading_is_reported_when_nothing_else_is_in_the_way():
    st = qs.describe_wait("gpu-250", [], resident_model="", resident_ctx=0,
                          requested_model="qwen3.5:9b", requested_ctx=12288)
    assert st["state"] == "loading"
    assert "qwen3.5:9b" in st["text"] and "12288" in st["text"]


def test_a_matching_resident_runner_needs_no_load():
    assert qs.needs_model_load("qwen3.5:9b", 12288, "qwen3.5:9b", 12288) is False


def test_a_different_window_means_a_reload():
    """The reload that wedged gpu-250 — ollama keys a runner by (model, ctx)."""
    assert qs.needs_model_load("qwen3.5:9b", 24576, "qwen3.5:9b", 12288) is True


def test_tags_of_the_same_model_are_not_a_reload():
    """`x`, `x:latest` and `x:9b` were all one blob on gpu-250."""
    assert qs.needs_model_load("jaahas/qwen3.5-uncensored:latest", 12288,
                               "jaahas/qwen3.5-uncensored", 12288) is False


def test_nothing_resident_is_a_cold_load():
    assert qs.needs_model_load("", 0, "qwen3.5:9b", 12288) is True


def test_no_requested_model_is_not_a_load():
    assert qs.needs_model_load("x", 4096, "", 0) is False


# ── formatting + robustness ──────────────────────────────────────────────────

def test_ages_render_short():
    assert qs._age(35) == "35s"
    assert qs._age(240) == "4m"
    assert qs._age(7500) == "2h05"


def test_junk_rows_are_ignored_not_fatal():
    st = qs.describe_wait("gpu-250", [None, "nope", {}, _job()])
    assert st["state"] == "queued"
    assert len(st["ahead"]) == 1


def test_should_emit_is_false_for_empty_text():
    assert qs.should_emit(None) is False
    assert qs.should_emit({"state": "queued", "text": ""}) is False
