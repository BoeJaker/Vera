"""The chat stream's reply capture (found live 2026-09-28): token frames are
json.dumps'd with the default separators ('"type": "token"'), but the capture
looked only for the compact form - every reply was captured as EMPTY, so the
activity record said chars=0 and the printer's chat feed and chat insights
never fired. The capture now parses the frame.
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from Vera.vera.agents import agents as A
except Exception:                                    # pragma: no cover
    A = None

needs_app = pytest.mark.skipif(A is None, reason="app module not importable here")


@needs_app
def test_the_real_token_frame_is_captured():
    frame = A._tt_token_frame('Redis "replicas" copy\nthe primary')
    assert b'"type": "token"' in frame                    # the real, spaced form
    assert A._stream_frame_token(frame) == ("token", 'Redis "replicas" copy\nthe primary')


@needs_app
def test_compact_frames_and_multi_line_chunks_still_work():
    compact = ('data: {"type":"token","text":"a"}\n\n'
               'data: {"type":"token","text":"b"}\n\n').encode()
    assert A._stream_frame_token(compact) == ("token", "ab")


@needs_app
def test_other_frames_are_not_reply_text():
    think = ("data: " + json.dumps({"type": "thinking", "text": "hmm"}) + "\n\n").encode()
    audio = ("data: " + json.dumps({"type": "audio", "seq": 1, "pcm": "A" * 5000}) + "\n\n").encode()
    err = ("data: " + json.dumps({"type": "error", "text": "no reply"}) + "\n\n").encode()
    assert A._stream_frame_token(think) == ("", "")
    assert A._stream_frame_token(audio) == ("audio", "")
    assert A._stream_frame_token(err) == ("", "")
    assert A._stream_frame_token(b"garbage") == ("", "")


@needs_app
def test_the_endpoint_uses_the_parser():
    import inspect
    src = inspect.getsource(A.agent_chat_stream_endpoint)
    assert "_stream_frame_token(bytes(chunk))" in src
    assert "b'\"type\":\"token\"' in head" not in src
