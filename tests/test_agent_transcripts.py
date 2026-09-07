"""Codex writes transcripts too, and nothing read them.

"the swarm page is displaying the codex agents operational are but im not sure
its displaying yours ... claude code sessions have been ingested but it looks
like it missing quite allot and it needs codex compatibility too"

The ingestion was Claude-only in both its roots and its parser, while codex
agents hold sandboxes in the same estate (`owner: codex` in evolve.sandbox.list).

Every fixture here follows the shape of REAL files inspected on the host
(2026-09-07), not a guess at the format:

    ~/.codex/sessions/2026/08/23/rollout-2026-08-23T04-34-48-<uuid>.jsonl
    {"timestamp":…, "type":"session_meta",  "payload":{id, cwd, originator, …}}
    {"timestamp":…, "type":"event_msg",     "payload":{"type":"user_message", …}}
    {"timestamp":…, "type":"response_item", "payload":{"type":"message", role, content}}

The sharp one is `test_the_injected_developer_preamble_is_not_a_turn`: in the
real file the FIRST response_item message had role "developer" and its text was
the injected skills preamble. Reading the conversation from response_item would
have made that every codex session's title.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.ide import agent_transcripts as AT                # noqa: E402

UUID = "01a02caf-615f-72a3-9ecc-ac02f8f54bae"
ROLLOUT = "rollout-2026-08-23T04-34-48-%s.jsonl" % UUID


def _l(obj):
    return json.dumps(obj)


CODEX_LINES = [
    _l({"timestamp": "2026-08-23T04:34:48Z", "type": "session_meta",
        "payload": {"session_id": UUID, "id": UUID, "cwd": "\\\\llm.int\\boejaker\\Vera",
                    "originator": "codex_vscode", "cli_version": "0.148.0-alpha.15"}}),
    _l({"timestamp": "2026-08-23T04:34:49Z", "type": "event_msg",
        "payload": {"type": "task_started"}}),
    _l({"timestamp": "2026-08-23T04:34:50Z", "type": "response_item",
        "payload": {"type": "message", "role": "developer",
                    "content": [{"type": "input_text",
                                 "text": "<skills_instructions>## Skills…"}]}}),
    _l({"timestamp": "2026-08-23T04:34:51Z", "type": "event_msg",
        "payload": {"type": "user_message", "message": "add a workflow adapter"}}),
    _l({"timestamp": "2026-08-23T04:35:10Z", "type": "response_item",
        "payload": {"type": "reasoning", "summary": []}}),
    _l({"timestamp": "2026-08-23T04:35:20Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total": 812}}}),
    _l({"timestamp": "2026-08-23T04:35:30Z", "type": "event_msg",
        "payload": {"type": "agent_message", "message": "Adapter landed."}}),
]

CLAUDE_LINES = [
    _l({"type": "user", "sessionId": "22e34f10-b10e-48db-88c3-570c5326daaa",
        "cwd": "/home/boejaker/Vera", "timestamp": "2026-09-07T18:00:00Z",
        "message": {"role": "user", "content": "fix the gate"}}),
    _l({"type": "assistant", "sessionId": "22e34f10-b10e-48db-88c3-570c5326daaa",
        "timestamp": "2026-09-07T18:01:00Z",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "Done."}]}}),
    _l({"type": "summary", "summary": "not a turn"}),
]


# ── telling them apart ──────────────────────────────────────────────────────
def test_a_rollout_filename_is_recognised_as_codex():
    assert AT.detect_kind("/home/x/.codex/sessions/2026/08/23/" + ROLLOUT) == AT.CODEX


def test_a_codex_path_is_recognised_even_with_windows_separators():
    assert AT.detect_kind(r"C:\Users\User\.codex\sessions\2026\08\23\x.jsonl") == AT.CODEX


def test_a_claude_transcript_is_recognised_as_claude():
    assert AT.detect_kind("/home/x/.claude/projects/enc/%s.jsonl" % UUID) == AT.CLAUDE


def test_detection_never_opens_the_file():
    """A 75MB rollout must not be read just to find out whose it is — the
    largest real file on the host was 75MB."""
    assert AT.detect_kind("/nonexistent/" + ROLLOUT) == AT.CODEX


def test_the_session_id_can_come_from_the_filename():
    assert AT.session_id_from_path(ROLLOUT) == UUID
    assert AT.session_id_from_path("%s.jsonl" % UUID) == UUID
    assert AT.session_id_from_path("notes.jsonl") == ""


# ── reading codex ───────────────────────────────────────────────────────────
def test_a_codex_transcript_yields_the_conversation():
    t = AT.read_transcript(CODEX_LINES, kind=AT.CODEX, path=ROLLOUT)
    assert [x["role"] for x in t["turns"]] == ["user", "assistant"]
    assert t["turns"][0]["text"] == "add a workflow adapter"
    assert t["turns"][1]["text"] == "Adapter landed."


def test_codex_session_metadata_is_picked_up():
    t = AT.read_transcript(CODEX_LINES, kind=AT.CODEX, path=ROLLOUT)
    assert t["session_id"] == UUID
    assert t["cwd"].endswith("Vera")
    assert t["originator"] == "codex_vscode"
    assert t["agent"] == "codex"


def test_the_injected_developer_preamble_is_not_a_turn():
    """In the real file the first response_item message had role 'developer'
    and its text was the injected skills preamble. Reading the conversation
    from response_item would have made that every session's title."""
    t = AT.read_transcript(CODEX_LINES, kind=AT.CODEX, path=ROLLOUT)
    assert not any("skills_instructions" in x["text"] for x in t["turns"])
    assert not any(x["role"] == "developer" for x in t["turns"])


def test_bookkeeping_events_are_not_turns():
    """task_started, token_count and reasoning are not things anyone said."""
    t = AT.read_transcript(CODEX_LINES, kind=AT.CODEX, path=ROLLOUT)
    assert len(t["turns"]) == 2


def test_a_bookkeeping_event_CARRYING_TEXT_is_still_not_a_turn():
    """The role map has to be an allowlist, not a default.

    Written after a mutation survived: making the map fall back to "assistant"
    for unknown event types still passed, because every bookkeeping event in
    the fixture happened to carry no text and the empty-text guard caught it.
    A task_complete that summarises itself would have walked straight in.
    """
    lines = CODEX_LINES + [
        _l({"timestamp": "2026-08-23T04:36:00Z", "type": "event_msg",
            "payload": {"type": "task_complete", "message": "Task finished."}}),
        _l({"timestamp": "2026-08-23T04:36:01Z", "type": "event_msg",
            "payload": {"type": "thread_settings_applied", "message": "model set"}}),
    ]
    t = AT.read_transcript(lines, kind=AT.CODEX, path=ROLLOUT)
    assert len(t["turns"]) == 2
    assert not any("Task finished" in x["text"] for x in t["turns"])


def test_a_later_session_meta_does_not_blank_what_an_earlier_one_set():
    """Real rollouts carry MANY session_meta lines — 115 in the file inspected
    on the host, not one. A later one with an absent cwd must not erase the
    cwd the first one established, or the session loses its project."""
    lines = CODEX_LINES + [
        _l({"timestamp": "2026-08-23T05:00:00Z", "type": "session_meta",
            "payload": {"id": UUID}}),
    ]
    t = AT.read_transcript(lines, kind=AT.CODEX, path=ROLLOUT)
    assert t["cwd"].endswith("Vera")
    assert t["originator"] == "codex_vscode"


# ── reading claude, unchanged ───────────────────────────────────────────────
def test_a_claude_transcript_still_reads():
    t = AT.read_transcript(CLAUDE_LINES, kind=AT.CLAUDE)
    assert [x["role"] for x in t["turns"]] == ["user", "assistant"]
    assert t["turns"][1]["text"] == "Done."
    assert t["session_id"].startswith("22e34f10")


def test_claude_non_message_lines_are_skipped():
    t = AT.read_transcript(CLAUDE_LINES, kind=AT.CLAUDE)
    assert len(t["turns"]) == 2


def test_a_claude_system_line_WITH_a_message_is_still_not_a_turn():
    """Same lesson as the codex bookkeeping case: the type check has to be an
    allowlist. A summary line with no message was filtered by the empty-text
    guard rather than by the type check, which hid the difference."""
    lines = CLAUDE_LINES + [
        _l({"type": "system", "timestamp": "2026-09-07T18:02:00Z",
            "message": {"role": "system", "content": "injected reminder"}}),
    ]
    t = AT.read_transcript(lines, kind=AT.CLAUDE)
    assert len(t["turns"]) == 2
    assert not any("injected reminder" in x["text"] for x in t["turns"])


# ── robustness: these files are read while an agent is writing them ─────────
def test_a_truncated_final_line_is_skipped_not_fatal():
    lines = CODEX_LINES + ['{"timestamp": "2026-08-23T04:36:00Z", "type": "eve']
    t = AT.read_transcript(lines, kind=AT.CODEX, path=ROLLOUT)
    assert len(t["turns"]) == 2


def test_blank_lines_and_junk_are_ignored():
    t = AT.read_transcript(["", "  ", "null", "[]", "3"] + CODEX_LINES,
                           kind=AT.CODEX, path=ROLLOUT)
    assert len(t["turns"]) == 2


def test_an_empty_message_is_not_a_turn():
    lines = [_l({"timestamp": "t", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "   "}})]
    assert AT.read_transcript(lines, kind=AT.CODEX)["turns"] == []


def test_reading_can_be_bounded():
    t = AT.read_transcript(CODEX_LINES, kind=AT.CODEX, path=ROLLOUT, max_turns=1)
    assert len(t["turns"]) == 1


def test_an_empty_transcript_is_not_an_error():
    t = AT.read_transcript([], kind=AT.CODEX, path=ROLLOUT)
    assert t["turns"] == [] and t["session_id"] == UUID


# ── routing a codex session to the checkout it worked on ────────────────────
def test_a_codex_session_files_under_the_SAME_project_as_claude_sessions():
    """The real folder Claude Code created for this checkout, on this host, is
    `--llm-int-boejaker-Vera`. A codex rollout whose cwd is that same checkout
    has to land there too, or the two agents' work on one repo shows up as two
    unrelated projects.

    Two leading dashes for the UNC path's two leading separators: one dash per
    CHARACTER, never a collapsed run. Collapsing gave `-llm-int-boejaker-Vera`
    — a project that does not exist.
    """
    assert AT.encode_cwd(r"\\llm.int\boejaker\Vera") == "--llm-int-boejaker-Vera"


def test_a_posix_checkout_encodes_with_one_leading_dash():
    assert AT.encode_cwd("/home/boejaker/Vera") == "-home-boejaker-Vera"


def test_codex_takes_its_project_from_cwd_not_the_path():
    """Codex files under a DATE. Using the first path segment would file every
    codex session on this host under the project "2026"."""
    assert AT.project_dir_for(AT.CODEX, "2026/08/23/" + ROLLOUT,
                              r"\\llm.int\boejaker\Vera") == "--llm-int-boejaker-Vera"
    assert AT.project_dir_for(AT.CODEX, "2026/08/23/" + ROLLOUT, "") == ""


def test_claude_still_takes_its_project_from_the_path():
    assert AT.project_dir_for(
        AT.CLAUDE, "--llm-int-boejaker-Vera/%s.jsonl" % UUID) == "--llm-int-boejaker-Vera"
    assert AT.project_dir_for(AT.CLAUDE, "loose.jsonl") == ""


def test_the_cwd_read_from_a_real_transcript_routes_it():
    """End to end on the fixture: metadata cwd -> project dir."""
    t = AT.read_transcript(CODEX_LINES, kind=AT.CODEX, path=ROLLOUT)
    assert AT.project_dir_for(AT.CODEX, "2026/08/23/" + ROLLOUT,
                              t["cwd"]) == "--llm-int-boejaker-Vera"


# ── content shapes ──────────────────────────────────────────────────────────
def test_message_content_is_flattened_whatever_shape_it_arrives_in():
    assert AT._text_of("plain") == "plain"
    assert AT._text_of({"text": "dict"}) == "dict"
    assert AT._text_of([{"type": "text", "text": "a"}, {"text": "b"}]) == "a\nb"
    assert AT._text_of([{"type": "image"}]) == ""
    assert AT._text_of(None) == ""
