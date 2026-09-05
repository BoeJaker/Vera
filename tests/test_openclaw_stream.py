"""Reading an OpenClaw answer off the wire.

Every frame below is a real one, captured from gateway 2026.4.29 while it
answered "pong" (2026-09-05). The bridge previously read `payload.delta` and
`payload.done`, which this gateway has never sent — so a prompt returned
nothing, forever, and `chat.send` was refused outright for want of an
`idempotencyKey`.

The trap this pins down: the SAME text arrives twice, as `agent` increments and
again as `chat` snapshots. A reader that appends from both emits "pongpong".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.openclaw import openclaw_device_core as dev  # noqa: E402

RUN = "8e27a342511340768959cf471944d8d1"
KEY = "agent:main:vera-bridge"

AGENT_DELTA = {"runId": RUN, "stream": "assistant",
               "data": {"text": "pong", "delta": "pong"},
               "sessionKey": KEY, "seq": 2, "ts": 1788634060582}
AGENT_LIFECYCLE = {"runId": RUN, "stream": "lifecycle",
                   "data": {"phase": "end", "livenessState": "working",
                            "endedAt": 1788634060620},
                   "sessionKey": KEY, "seq": 3, "ts": 1788634060620}
CHAT_DELTA = {"runId": RUN, "sessionKey": KEY, "seq": 2, "state": "delta",
              "message": {"role": "assistant",
                          "content": [{"type": "text", "text": "pong"}],
                          "timestamp": 1788634060583}}
CHAT_FINAL = {"runId": RUN, "sessionKey": KEY, "seq": 3, "state": "final",
              "message": {"role": "assistant",
                          "content": [{"type": "text", "text": "pong"}],
                          "timestamp": 1788634060621}}


# ── chat.send params ─────────────────────────────────────────────────────────

def test_chat_send_always_carries_an_idempotency_key():
    params = dev.build_chat_send_params(
        session_key="vera-bridge", message="hi", idempotency_key="k1")
    assert params["idempotencyKey"] == "k1"
    assert set(params) == {"sessionKey", "message", "idempotencyKey"}


def test_optional_fields_are_omitted_rather_than_sent_empty():
    params = dev.build_chat_send_params(
        session_key="s", message="m", idempotency_key="k",
        agent_id="", thinking="")
    assert "agentId" not in params and "thinking" not in params


def test_agent_and_thinking_are_included_when_asked_for():
    params = dev.build_chat_send_params(
        session_key="s", message="m", idempotency_key="k",
        agent_id="main", thinking="low")
    assert params["agentId"] == "main" and params["thinking"] == "low"


def test_a_rejected_property_can_be_named_and_dropped():
    message = ("invalid chat.send params: must have required property "
               "'idempotencyKey'; at root: unexpected property 'agentId'")
    assert dev.unexpected_properties(message) == ["agentId"]
    assert dev.unexpected_properties("something else entirely") == []


# ── deltas ───────────────────────────────────────────────────────────────────

def test_the_increment_comes_from_the_agent_assistant_stream():
    assert dev.stream_delta("agent", AGENT_DELTA) == "pong"


def test_chat_frames_are_never_a_delta_source():
    """Both families carry the same text; consuming both doubles every token."""
    assert dev.stream_delta("chat", CHAT_DELTA) == ""


def test_lifecycle_frames_carry_no_text():
    assert dev.stream_delta("agent", AGENT_LIFECYCLE) == ""


def test_a_delta_falls_back_to_the_cumulative_text_field():
    frame = {"stream": "assistant", "data": {"text": "pong"}}
    assert dev.stream_delta("agent", frame) == "pong"


def test_malformed_payloads_do_not_raise():
    for junk in (None, {}, {"stream": "assistant"}, {"stream": "assistant", "data": 7}):
        assert dev.stream_delta("agent", junk) == ""


# ── the final answer ─────────────────────────────────────────────────────────

def test_the_final_chat_frame_is_the_answer():
    assert dev.final_answer("chat", CHAT_FINAL) == ("final", "pong")


def test_a_delta_frame_is_not_terminal():
    assert dev.final_answer("chat", CHAT_DELTA) is None
    assert dev.final_answer("agent", AGENT_LIFECYCLE) is None


def test_aborted_and_errored_runs_are_terminal_too():
    for state in ("aborted", "error"):
        frame = dict(CHAT_FINAL, state=state)
        assert dev.final_answer("chat", frame)[0] == state


def test_text_is_joined_across_content_blocks():
    message = {"role": "assistant",
               "content": [{"type": "text", "text": "po"},
                           {"type": "image", "url": "x"},
                           {"type": "text", "text": "ng"}]}
    assert dev.message_text(message) == "pong"


def test_a_plain_string_message_still_reads():
    assert dev.message_text("pong") == "pong"
    assert dev.message_text({"content": "pong"}) == "pong"
    assert dev.message_text(None) == ""


# ── session keys ─────────────────────────────────────────────────────────────

def test_the_caller_sees_the_key_it_asked_for():
    """The gateway namespaces `vera-bridge` into `agent:main:vera-bridge`;
    a subscriber matching on what it sent must still match."""
    assert dev.resolve_session_key(KEY, "vera-bridge") == "vera-bridge"


def test_an_unrelated_key_is_reported_as_is():
    assert dev.resolve_session_key("agent:main:other", "vera-bridge") == \
        "agent:main:other"


def test_a_missing_key_falls_back_to_the_configured_one():
    assert dev.resolve_session_key("", "vera-bridge") == "vera-bridge"


# ── the whole exchange ───────────────────────────────────────────────────────

def test_one_answer_is_read_exactly_once():
    """Replay the captured run through both halves of the reader's logic."""
    streamed, final = [], None
    for event, payload in (("session.message", {}),
                           ("agent", AGENT_DELTA),
                           ("chat", CHAT_DELTA),
                           ("session.message", {}),
                           ("agent", AGENT_LIFECYCLE),
                           ("chat", CHAT_FINAL)):
        if event not in dev.STREAM_EVENTS:
            continue
        delta = dev.stream_delta(event, payload)
        if delta:
            streamed.append(delta)
        terminal = dev.final_answer(event, payload)
        if terminal:
            final = terminal
    assert "".join(streamed) == "pong"      # not "pongpong"
    assert final == ("final", "pong")
