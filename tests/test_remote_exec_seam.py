"""Regression tests for the execution-target seam (swarm §6.5 gaps 1+2):
Claude session continuity (--resume) and session-id capture.

Command composition is security-sensitive (quoting, flag gating), so the pure
core (remote_exec_core) is pinned here — no app import needed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.ide.remote_exec_core import build_claude_cmd, parse_claude_result  # noqa: E402


def test_resume_flag_gated():
    with_resume = build_claude_cmd("/w", "do a thing", "", False,
                                   resume_session_id="sess-abc")
    assert "--resume sess-abc" in with_resume
    without = build_claude_cmd("/w", "do a thing", "", False)
    assert "--resume" not in without


def test_task_is_shell_quoted():
    # a task with shell metacharacters must be single-quoted, never raw
    cmd = build_claude_cmd("/w", "rm -rf $HOME; echo pwned", "", False)
    assert "'rm -rf $HOME; echo pwned'" in cmd


def test_resume_id_is_quoted():
    cmd = build_claude_cmd("/w", "t", "", False, resume_session_id="a b;c")
    assert "--resume 'a b;c'" in cmd


def test_default_permission_mode_applied():
    cmd = build_claude_cmd("/w", "t", "", False, default_permission_mode="acceptEdits")
    assert "--permission-mode acceptEdits" in cmd
    # explicit overrides the default
    cmd2 = build_claude_cmd("/w", "t", "", False, permission_mode="plan",
                            default_permission_mode="acceptEdits")
    assert "--permission-mode plan" in cmd2 and "acceptEdits" not in cmd2


def test_api_key_beats_oauth_and_is_written_to_env_file():
    cmd = build_claude_cmd("/w", "t", "KEY123", False, oauth_token="OAUTH")
    assert "ANTHROPIC_API_KEY" in cmd and "CLAUDE_CODE_OAUTH_TOKEN" not in cmd


def test_parse_captures_session_id():
    summary, obj, sid = parse_claude_result(
        'log noise\n{"result":"done","session_id":"sess-9"}')
    assert summary == "done" and sid == "sess-9" and obj["result"] == "done"


def test_parse_tolerates_no_json():
    summary, obj, sid = parse_claude_result("just prose, no json object")
    assert summary == "" and obj is None and sid == ""


def test_parse_prefers_last_json_line():
    summary, obj, sid = parse_claude_result(
        '{"result":"early","session_id":"a"}\n{"result":"final","session_id":"b"}')
    assert summary == "final" and sid == "b"
