"""The agent registry, as capabilities.

`registry_core` owns the record shape and every decision worth testing; this
module owns persistence and the HTTP/MCP surface and as little else as it can
get away with.

Storage mirrors `skills.py` exactly - SQLite primary (always works, survives a
Redis flush), Redis as a secondary cache - because a second storage idiom in
the same estate is a second thing to debug at 2am, and this one is proven.

WHY THIS EXISTS AT ALL: a Loop Lab census row records what ran. It cannot say
who ran it, under which skill, with which tool, or what throwaway scripts got
written along the way, because none of those things exist anywhere Vera can
name. Seeding this registry with the operator's OWN toolkit is what closes
that gap - see `_BUILTIN_ENTRIES`, which is today's census programme written
down as data rather than as a story in a chat log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (   # noqa: F401
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, register_ui, schedule,
)

# The namespace trap: a module under vera/ is loaded by bare filename, so the
# dotted package spelling is not guaranteed to resolve the same way twice.
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.registry import registry_core as RC
except ImportError:                      # pragma: no cover
    from vera.registry import registry_core as RC   # type: ignore

log = logging.getLogger("vera.registry")

#: id -> entry. The SQLite table is the truth; this is the read path.
ENTRIES: Dict[str, Dict[str, Any]] = {}

_SQLITE_PATH = Path(__file__).parent / "vera_registry.db"


def _redis():
    return _orch.REDIS


def _sqlite_init():
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, data TEXT, updated_at TEXT
        );
    """)
    conn.commit()
    return conn


try:
    _c = _sqlite_init(); _c.close()
    log.debug("registry: SQLite ready at %s", _SQLITE_PATH)
except Exception as _e:                                    # pragma: no cover
    log.warning("registry: SQLite init failed: %s", _e)


def _sqlite_save(record: dict):
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        conn.execute("INSERT OR REPLACE INTO entries (id, kind, name, data, updated_at) "
                     "VALUES (?,?,?,?,?)",
                     (record["id"], record.get("kind", ""), record.get("name", ""),
                      json.dumps(record), record.get("updated_at", now_iso())))
        conn.commit(); conn.close()
    except Exception as e:                                 # pragma: no cover
        log.warning("registry sqlite_save: %s", e)


def _sqlite_delete(item_id: str):
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        conn.execute("DELETE FROM entries WHERE id=?", (item_id,))
        conn.commit(); conn.close()
    except Exception as e:                                 # pragma: no cover
        log.warning("registry sqlite_delete: %s", e)


def _sqlite_load_all() -> List[dict]:
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        rows = conn.execute("SELECT data FROM entries").fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]
    except Exception as e:                                 # pragma: no cover
        log.warning("registry sqlite_load_all: %s", e)
        return []


async def _save(record: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sqlite_save, record)
    r = _redis()
    if r:
        try:
            await r.set("vera:registry:%s" % record["id"], json.dumps(record))
        except Exception as e:                             # pragma: no cover
            log.debug("registry redis save %s: %s", record["id"], e)


async def _drop(item_id: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sqlite_delete, item_id)
    r = _redis()
    if r:
        try:
            await r.delete("vera:registry:%s" % item_id)
        except Exception as e:                             # pragma: no cover
            log.debug("registry redis delete %s: %s", item_id, e)


# â”€â”€ builtins â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Today's census programme, written down as data. These are seeded once and are
# then ordinary entries - editable, deletable, and NOT re-seeded over an edit
# (see _startup_load), because a builtin that overwrites a human's correction
# every restart is worse than no builtin.

_BUILTIN_ENTRIES: List[Dict[str, Any]] = [
    {
        "kind": "os", "name": "Claude Code",
        "version": "opus-5",
        "summary": "Agent harness driving Vera from outside over /mcp/call; "
                   "attributes its work as controller=claude_code.",
        "body": (
            "WHAT IT IS\n"
            "An agent harness running outside Vera (on a Windows host, over SMB "
            "and HTTPS). It edits files, runs shell commands on the Vera host "
            "through evolve.sandbox.exec, and calls Vera capabilities.\n\n"
            "HOW IT REACHES VERA\n"
            "Its MCP tool list is fixed at session start, so a capability "
            "deployed mid-session will NOT appear as a tool. Any cap is still "
            "reachable over HTTP:\n\n"
            "    POST https://llm.int:8999/mcp/call\n"
            "    {\"name\":\"<cap.name>\",\"arguments\":{...},\n"
            "     \"caller_kind\":\"claude\",\"session_id\":\"<session uuid>\"}\n\n"
            "The payload comes back under `content`. caller_kind:\"claude\" "
            "stamps the pipeline/commit as controller=claude_code and links it "
            "to the chat; a bare curl tags `user` instead.\n\n"
            "WHERE ITS TRANSCRIPTS LIVE\n"
            "~/.claude/projects/*.jsonl on the operator's machine. Vera ingests "
            "them via ide.claude_sessions.* - that is where the prompts and "
            "conversation behind a census row can be read back, and it is why a "
            "census row's session id is worth recording.\n\n"
            "CONSTRAINTS THAT SHAPE ITS WORK HERE\n"
            "- All code lands via bleeding-edge; main only on explicit "
            "go-ahead.\n"
            "- Never edits prod's live checkout.\n"
            "- One GPU call at a time: the ollama gate is capacity 1.\n"
            "- Git author is always the human; no AI attribution trailer (a "
            "commit-msg hook rejects it)."),
        "source": {"origin": "claude-code"},
        "interop": {"cap": "ide.claude_sessions.list", "protocol": "mcp"},
        "tags": ["harness", "operator", "how-to"],
    },
    {
        "kind": "tool", "name": "/loop",
        "summary": "Self-paced recurring prompt: runs a task, then schedules its "
                   "own next wake-up rather than polling on a fixed interval.",
        "body": (
            "WHAT IT DOES\n"
            "Takes `[interval] <prompt>`. With an interval it schedules a cron "
            "job and runs the prompt immediately. WITHOUT one it enters DYNAMIC "
            "mode: it runs the task now, then decides for itself when the next "
            "iteration is worth running and schedules a single wake-up.\n\n"
            "WHY DYNAMIC MODE IS THE ONE THAT MATTERS HERE\n"
            "A census goal takes minutes to half an hour and its duration is "
            "UNBOUNDED - an LLM call can run seconds or tens of minutes. A "
            "fixed poll either wastes wake-ups on a run that has not moved, or "
            "sleeps through the finish. Dynamic mode picks the delay from what "
            "is actually being waited on.\n\n"
            "HOW TO DRIVE A LONG PROGRAMME WITH IT\n"
            "1. Put the WHOLE state in the loop prompt - what is running, what "
            "was confirmed, what to do next, and the traps. Each firing is a "
            "fresh turn; anything not in the prompt is gone.\n"
            "2. Do the NEXT step and nothing else, then reschedule.\n"
            "3. Say what you are waiting on in the `reason` - the operator "
            "reads that to understand the cadence.\n"
            "4. Fallback heartbeat 1200-1800s when something else (a monitor, a "
            "task notification) is the real wake signal. Idle ticks more "
            "frequent than the task needs are pure overhead.\n"
            "5. Stop with ScheduleWakeup(stop:true) when the task is done or "
            "cannot progress. Re-arming is a per-turn choice, not a default.\n\n"
            "TRAP\n"
            "Do NOT schedule a short wake-up to poll work the harness already "
            "tracks - you are re-invoked when it finishes. Poll only external "
            "state nothing will notify you about."),
        "source": {"origin": "claude-code", "path": "bundled:loop"},
        "tags": ["loop", "scheduling", "operator", "how-to"],
    },
    {
        "kind": "skill", "name": "improve-vera-sandboxed",
        "summary": "Land changes through the sandboxed, adversarially-reviewed, "
                   "gated pipeline rather than editing prod's checkout.",
        "body": (
            "THE PIPELINE, IN ORDER\n"
            "  evolve.pipeline.begin(title, spawn=true, session_id)\n"
            "      one call: typed branch off bleeding-edge, worktree, dev\n"
            "      container, and the CI/CD record\n"
            "  edit ONLY inside the returned worktree\n"
            "  evolve.sandbox.exec(where='worktree', branch=..., cmd='git ...')\n"
            "      git over SMB does NOT work; commit through the host\n"
            "  review your OWN diff as a skeptic before landing\n"
            "  evolve.unittest.run(markers='critical')\n"
            "  evolve.pipeline.adopt(branch, to='bleeding-edge')\n"
            "  evolve.pipeline.promote(id, to='bleeding-edge')\n"
            "  evolve.bleeding_edge.promote_to_main(confirm=true)   [ASK FIRST]\n"
            "  sys.dev.restart(confirm=true)\n\n"
            "NON-NEGOTIABLES\n"
            "- bleeding-edge is always a superset of main. main only ever "
            "advances by promoting bleeding-edge as a whole.\n"
            "- One branch, ONE concern. A mixed branch cannot be reviewed, "
            "promoted or reverted as a unit.\n"
            "- Claim a hand-made worktree (evolve.worktree.claim) or the "
            "cleanup sweep deletes it under you.\n"
            "- Never run repo-wide worktree/branch commands; other agents share "
            "this repo.\n"
            "- Internal plans go in <git-common-dir>/vera-work/shared-planning/, "
            "never in documentation/.\n\n"
            "SECTION 11 is the census-driven improvement loop: the harness, the "
            "noise floor, prove-the-code-path-ran, artifacts-not-counters, "
            "archiving, the self-paced loop, and provenance."),
        "source": {"origin": "claude-code",
                   "path": ".claude/skills/improve-vera-sandboxed/SKILL.md"},
        "interop": {"cap": "evolve.pipeline.begin"},
        "tags": ["pipeline", "review", "census", "how-to"],
    },
    {
        "kind": "skill", "name": "Using this registry",
        "summary": "How to record a skill, tool, loop, technique or harness "
                   "here - and what a usable entry has to contain.",
        "body": (
            "WHY BOTHER\n"
            "A census row records what ran. Without this it cannot say who ran "
            "it, under which skill, with which tool, or what throwaway scripts "
            "were written on the way - so a run becomes an orphan the moment "
            "its chat log scrolls away.\n\n"
            "THE FIVE KINDS\n"
            "  skill      instructions an agent FOLLOWS (a SKILL.md, a prompt)\n"
            "  tool       something an agent INVOKES (a slash command, an MCP\n"
            "             tool, a script)\n"
            "  loop       a repeating operating pattern (/loop, a census\n"
            "             programme, a Vera loop profile)\n"
            "  technique  a way of working that is not itself executable\n"
            "  os         a whole harness that hosts agents\n\n"
            "HOW TO ADD ONE\n"
            "  registry.upsert {entry: {\n"
            "     kind, name, summary,\n"
            "     body,                       <- THE FULL CONTENT\n"
            "     source: {origin, path},\n"
            "     owner:  {agent, session},\n"
            "     tags: [], helpers: [{name, purpose, path}] }}\n\n"
            "THE RULE THAT MAKES IT WORTH HAVING\n"
            "`summary` is one line saying what the thing IS. `body` is the "
            "ACTUAL CONTENT - the technique in full, the prompt in full, the "
            "operating procedure in full. An entry whose body is a paraphrase "
            "is a title, and a registry of titles is what the estate already "
            "had. If someone cannot APPLY the thing from what you wrote, you "
            "have not recorded it.\n\n"
            "HELPER SCRIPTS\n"
            "Record every throwaway script with what it was FOR. A helper with "
            "no purpose is dropped on write - the file alone never says why it "
            "existed, and a half-recorded helper reads as coverage.\n\n"
            "WHAT IT REFUSES\n"
            "No summary, no kind, or no source.origin. Pass force=true only "
            "when you have read the refusal and disagree with it.\n\n"
            "PROJECTING INTO VERA'S OWN SKILLS\n"
            "registry.sync_skill projects an entry into skills.* so chat and "
            "loops can attach it. It is idempotent - it joins on a "
            "registry:<id> tag, so a re-sync updates rather than duplicating - "
            "and the provenance tags survive, so a round trip cannot launder a "
            "Claude Code skill into a Vera-native one."),
        "source": {"origin": "claude-code"},
        "interop": {"cap": "registry.upsert"},
        "tags": ["registry", "how-to", "meta"],
    },
    {
        "kind": "loop", "name": "census programme",
        "summary": "Back-to-back Loop Lab tests with no improvement or review "
                   "step, run against one commit to measure the loop itself.",
        "body": (
            "WHAT A CENSUS IS\n"
            "A fixed set of goals with declared checks, run back-to-back "
            "against ONE commit. It is a measuring instrument, not a feature.\n\n"
            "HOW TO RUN ONE (from the UI, since the flattening)\n"
            "  Loop Lab -> Census -> 'Templates & run': pick, Seed, Run.\n"
            "  evolve.census.templates            what exists\n"
            "  evolve.census.template.seed name=  template -> suite tasks\n"
            "  evolve.suite.run tag=census-<name> run it\n"
            "  evolve.suites tag=census-<name>    that template's timeline\n"
            "A template SAVED but not SEEDED shows in the picker and runs "
            "NOTHING - check evolve.tasks(tag=...) is non-empty.\n"
            "ALWAYS pass a tag: untagged runs every enabled task.\n\n"
            "GOAL == TASK == ONE RUNNABLE TEST\n"
            "A seeded goal is an ordinary benchmark task, and "
            "evolve.task.run(id=...) runs exactly one and returns its checks. "
            "Anything true of a suite task is true of a census goal.\n\n"
            "PARITY, WHICH IS THE WHOLE VALUE\n"
            "`planning` is the only profile whose engine is v7, and every "
            "historical number came from a bare v7 call. Only default, "
            "model-compare and the exec/code/prose/data families share a "
            "comparable history. A different profile is a different ENGINE.\n"
            "`default` is the baseline: DO NOT EDIT IT.\n"
            "Archive a non-default template as census.<template>-run<N>.jsonl, "
            "never census.runNN.\n\n"
            "JUDGING A RUN\n"
            "From ARTIFACTS, never from scores. quality N/N does not prove an "
            "artifact is correct - run 47 scored 4/4 on a file with a hardcoded "
            "60. Read the file out of the session sandbox."),
        "source": {"origin": "claude-code",
                   "path": "/home/boejaker/loop-census/run_census.py"},
        "interop": {"cap": "evolve.suite.run"},
        "tags": ["census", "benchmark", "loop", "how-to"],
        "helpers": [
            {"name": "helpers.json",
             "purpose": "the manifest of throwaway scripts an operator wrote, "
                        "stamped onto every census row so a run can be traced "
                        "to the tooling that drove it",
             "path": "/home/boejaker/loop-census/helpers.json"},
        ],
    },
    {
        "kind": "tool", "name": "run_census.py",
        "summary": "The loop census runner: one template's goals back to back "
                   "against v7, one JSONL row per goal.",
        "body": (
            "USAGE\n"
            "  python3 -B run_census.py --template <name> [--model M] [--list]\n"
            "        [--operator claude] [--skill improve-vera-sandboxed]\n"
            "        [--tool /loop] [--session-id <uuid>]\n"
            "  (or the VERA_CENSUS_* environment variables)\n\n"
            "WHAT IT DOES\n"
            "Reads templates/<name>.json, runs each goal through "
            "dag.agent_loop_v7 with the goal and nothing else - which is how "
            "every historical census number was produced - waiting for a free "
            "box before each goal so it never competes with a person or another "
            "agent. Writes census.jsonl (one row per goal) and census.log.\n\n"
            "WALL CAPS resolve most-specific-first:\n"
            "  goal['wall_cap_s'] -> template['wall_cap_s'] -> WALL_CAP_S (1800)\n"
            "and THE CAP THAT APPLIED IS RECORDED ON EVERY ROW. Without that, "
            "1805s is a timeout under one template and a healthy finish under "
            "another, and a cross-template comparison silently measures the "
            "harness instead of the loop.\n\n"
            "PROVENANCE is stamped on every row: caller_session, operator, "
            "skill, tool, and the helper manifest. Blank fields are left blank "
            "rather than guessed - an unattributed run should look "
            "unattributed, not be credited to whoever edited the harness last.\n\n"
            "AFTERWARDS\n"
            "Archive as census.<template>-run<N>.jsonl. NEVER census.runNN "
            "unless it is the default template - that numbering is the only "
            "comparison baseline there is.\n\n"
            "CAVEAT\n"
            "This file lives OUTSIDE the git repo, so it is not pipeline-gated "
            "and has no tests. Prefer the seeded suite path "
            "(evolve.suite.run tag=census-<name>) for anything new."),
        "source": {"origin": "claude-code",
                   "path": "/home/boejaker/loop-census/run_census.py"},
        "interop": {"cap": "dag.agent_loop_v7"},
        "tags": ["census", "harness", "tool", "how-to"],
    },
    {
        "kind": "tool", "name": "run_operator_census.py",
        "summary": "The operator census runner: browser/operator goals, minutes "
                   "each instead of 1800s.",
        "body": (
            "USAGE\n"
            "  python3 -B run_operator_census.py --template <name> [--list]\n"
            "        [--operator ...] [--skill ...] [--tool ...] [--session-id ...]\n\n"
            "WHAT IT IS FOR\n"
            "The cheap counterpart to the loop census. Every goal in "
            "operator-templates/regressions.json reproduces a failure the LOOP "
            "census actually paid for, in minutes rather than 1800s a time:\n"
            "  target-with-no-url            O44 - operator.run given no url "
            "drove Vera's own dashboard for 504s while the target file sat in "
            "the workspace\n"
            "  page-answers-in-text-only     census 49 - the answer was on the "
            "page as text and the run kept clicking instead of reading it\n"
            "  value-changes-without-returning  census 48 / O45 / O46\n"
            "  preview-served-file           census 50 - the preview could not "
            "serve a file the run had just written\n"
            "  unreachable-target-fails-fast a CONTROL: it must fail FAST, "
            "which is why it carries its own 300s cap\n\n"
            "RUN IT FIRST when touching the operator - it finds browser-layer "
            "defects before a loop run inherits them.\n\n"
            "SCHEMA NOTE\n"
            "Its goals use kind/panel_id/max_steps and carry no `checks`, so "
            "the census template store rightly refuses them - a goal with no "
            "assertable check inflates the denominator and reads as coverage. "
            "The `operator-family` template in the store is a REWRITE of these "
            "goals into the census schema; it is not the same artifact.\n\n"
            "CAVEAT\n"
            "Outside the git repo: not gated, no tests."),
        "source": {"origin": "claude-code",
                   "path": "/home/boejaker/loop-census/run_operator_census.py"},
        "interop": {"cap": "operator.run"},
        "tags": ["census", "operator", "harness", "tool", "how-to"],
    },
    {
        "kind": "technique", "name": "prove the code path ran",
        "summary": "Before crediting a fix for a better run, show mechanically "
                   "that the changed code executed at all.",
        "body": (
            "THE RULE\n"
            "A better number after a change is not evidence the change caused "
            "it. Before claiming anything, show the changed code EXECUTED - a "
            "log line it emits, a counter it writes, a field only it sets, or a "
            "trace entry only it produces.\n\n"
            "WHY IT EARNS ITS KEEP\n"
            "FIVE would-be improvement claims were withdrawn in a single "
            "session. Every one was caught by this rule and by nothing else:\n"
            "  - two commits looked vindicated by better censuses and had NEVER "
            "executed. One was gated on a session id that appears nowhere in "
            "the calling module; the other guards a case every real call "
            "already avoids (every operator.run gets an explicit url).\n"
            "  - one 'improvement' (2 goals, 18%) sat inside the measured noise "
            "floor and was never evidence.\n"
            "  - one attribution matched a stop message to the WRONG guard: two "
            "repeating-action guards exist and were conflated.\n"
            "  - one scope overclaim: grep -c on ONE vera.log while saying 'the "
            "whole census'. vera.log rotates.\n\n"
            "HOW TO APPLY IT\n"
            "1. Name the observable the change produces.\n"
            "2. Find it in a real run's output.\n"
            "3. Only then discuss whether the numbers moved.\n"
            "If you cannot name an observable, the change is not yet "
            "falsifiable - add one before landing it."),
        "source": {"origin": "claude-code"},
        "tags": ["evidence", "census", "discipline", "technique"],
    },
    {
        "kind": "technique", "name": "the noise floor",
        "summary": "Two censuses of the SAME commit differed by one capped goal "
                   "and 12.6% wall, so a result smaller than that is not a result.",
        "body": (
            "THE MEASUREMENT\n"
            "Censuses 49 and 50 ran the SAME COMMIT, deliberately, to measure "
            "the instrument:\n\n"
            "    done/capped   11/1  vs  10/2\n"
            "    total wall    9873s vs  8632s      (12.6% apart)\n"
            "    one goal flipped pass<->cap with NO code change\n"
            "    median per-goal time ratio 1.44x, worst 1.71x\n\n"
            "SO: plus or minus one capped goal, and plus or minus 13% wall, is "
            "NOISE.\n\n"
            "WHAT FOLLOWS, AND IT BINDS\n"
            "- No single pair of censuses can establish a change of that size.\n"
            "- 'Best run yet' is not a result.\n"
            "- The earlier 48->49 improvement (2 goals, 18%) barely clears the "
            "floor and was withdrawn as evidence.\n\n"
            "WHAT TO DO INSTEAD\n"
            "Prefer MECHANICAL claims, which are falsifiable in one run: show "
            "the code path executed, and show the specific behaviour it was "
            "written to change. A statistical claim about the loop needs more "
            "runs than anyone has time for; a mechanical one needs one."),
        "source": {"origin": "claude-code"},
        "tags": ["evidence", "census", "measurement", "technique"],
    },
    {
        "kind": "technique", "name": "one branch, one concern",
        "summary": "A branch carrying unrelated changes cannot be reviewed, "
                   "promoted or reverted as a unit - and gets binned wholesale.",
        "body": (
            "WHAT HAPPENED\n"
            "A single branch accumulated five unrelated things: an agent "
            "registry, census per-goal wall caps, census run provenance, a "
            "loop-profile fix, and a planner change. When the planner part "
            "turned out to be the wrong approach, there was no way to drop just "
            "that - the whole branch was binned and the surviving work re-cut "
            "into four single-purpose branches.\n\n"
            "WHY IT MATTERS BEYOND TIDINESS\n"
            "A mixed branch makes the gate result meaningless: 'the branch is "
            "green' says nothing about which change the green belongs to. And "
            "when one part is wrong the only options are surgery, or throwing "
            "away good work with the bad.\n\n"
            "HOW TO APPLY IT\n"
            "- Before the FIRST edit of a new piece of work, ask whether it is "
            "the same concern as what is already on this branch. If not, cut a "
            "new one. Cutting is cheap; unpicking is not.\n"
            "- A fix noticed in passing is its OWN branch, even at two lines. "
            "Especially then - small unrelated commits are the ones that "
            "quietly ride along.\n"
            "- Verify before landing: git diff --name-only bleeding-edge..<br> "
            "should read as one coherent change, and no file should appear on "
            "two of your branches."),
        "source": {"origin": "claude-code"},
        "tags": ["discipline", "git", "technique"],
    },
    {
        "kind": "technique", "name": "add a style, do not amend the system",
        "summary": "'Add an optional mode' means a new self-contained component "
                   "beside the existing one, not a flag threaded through it.",
        "body": (
            "WHAT HAPPENED\n"
            "Asked for an optional 'detailed planner' MODE, the work added a "
            "planner_mode parameter to cap_dag_agent_loop_v6, a branch at the "
            "plan call site, a role on LOOP_ROUTING_PROFILE and an edit to the "
            "minimal prompt body. All of it was thrown away.\n\n"
            "TWO SEPARATE FAILURES, AND BOTH ARE THE LESSON\n"
            "1. A critical system was changed without the plan being put to the "
            "user first. dag_workshop_capabilities.py IS the agentic loop; work "
            "on it gets proposed and agreed BEFORE it is written.\n"
            "2. It amended when the instruction was to ADD. That puts the new "
            "idea's risk onto the working system's blast radius - exactly what "
            "an 'optional mode' is supposed to avoid.\n\n"
            "NOTE WHAT IS *NOT* THE LESSON\n"
            "The idea itself was never assessed on its merits and may well be "
            "sound. Do not read this as a verdict on the approach.\n\n"
            "HOW TO APPLY IT\n"
            "- New module, its own capability, its own routing profile if it "
            "needs one. DELETING the files must return the estate to exactly "
            "what it was.\n"
            "- Test the additive property mechanically:\n"
            "      git diff --stat <base>..HEAD -- <the existing system's files>\n"
            "      # must be EMPTY\n"
            "- A registry of styles beats a branch in the caller: the next "
            "style is another entry, not another `if`.\n"
            "- Inject the dependency (pass the generate/execute function in) so "
            "the new component is testable without the system it sits beside, "
            "and can never import it back.\n"
            "- If the new thing genuinely cannot work without one hook, SAY SO "
            "AND ASK before writing that hook."),
        "source": {"origin": "claude-code"},
        "tags": ["discipline", "design", "technique"],
    },
]


async def _startup_load():
    """SQLite is the truth; builtins fill only the gaps.

    A builtin is seeded when its id is ABSENT, never over an existing row. The
    entries below are opinions about how to work, and an opinion that silently
    reinstates itself over a human's correction on every restart is worse than
    no opinion at all.
    """
    try:
        for rec in _sqlite_load_all():
            e = RC.normalise(rec)
            if e["id"]:
                ENTRIES[e["id"]] = e
        seeded = 0
        for raw in _BUILTIN_ENTRIES:
            e = RC.normalise(raw, now=now_iso())
            if e["id"] in ENTRIES:
                continue
            e["owner"] = {"agent": "claude", "session": ""}
            ENTRIES[e["id"]] = e
            await _save(e)
            seeded += 1
        log.info("registry: %d entries loaded (%d builtins seeded)",
                 len(ENTRIES), seeded)
    except Exception as e:                                 # pragma: no cover
        log.warning("registry startup load failed: %s", e)


# â”€â”€ capabilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@capability("registry.list", memory="off", silent=True,
            http_method="GET", http_path="/registry", http_tags=["registry"],
            description="List agent-registry entries - the skills, tools, loops, "
                        "techniques and LLM operating systems an agent drives Vera "
                        "WITH. Query: kind (skill|tool|loop|technique|os), owner "
                        "(agent name), tag, q (search), limit. "
                        "Output: {entries:[...], count, kinds}.",
            contract=_orch._inspection_contract("registry.list", effects=["read"]))
async def registry_list(kind: str = "", owner: str = "", tag: str = "",
                        q: str = "", limit: int = 100, trace_id=None):
    found = RC.search(list(ENTRIES.values()), q, kind=kind, owner=owner,
                      tag=tag, limit=limit)
    return {"entries": found, "count": len(found), "kinds": list(RC.KINDS)}


@capability("registry.get", memory="off", silent=True,
            http_method="GET", http_path="/registry/entry", http_tags=["registry"],
            description="One registry entry by id, with its interoperability "
                        "projection. Inputs: id (str!). "
                        "Output: {ok, entry, interop}.",
            contract=_orch._inspection_contract("registry.get", effects=["read"]))
async def registry_get(id: str = "", trace_id=None):
    e = ENTRIES.get(str(id or "").strip())
    if not e:
        return {"ok": False, "error": "no entry %r" % id,
                "known": sorted(ENTRIES)[:50]}
    return {"ok": True, "entry": e, "interop": RC.to_interop(e)}


@capability("registry.upsert", memory="off",
            http_method="POST", http_path="/registry/upsert", http_tags=["registry"],
            description="Create or update a registry entry. Pass the record: "
                        "{kind!, name!, summary!, body, source:{origin!,path,repo,"
                        "commit}, owner:{agent,session}, interop:{cap,mcp_tool,"
                        "protocol}, tags[], applies_to[], helpers:[{name!,purpose!,"
                        "path}]}. REFUSES an entry that says nothing about itself "
                        "(no summary, no kind, no origin) - pass force=true to "
                        "store it anyway. An update merges: fields it does not "
                        "mention are kept, and created_at is never overwritten. "
                        "Output: {ok, entry, problems[], created}.")
async def registry_upsert(entry: Optional[dict] = None, force: bool = False,
                          trace_id=None):
    entry = entry if isinstance(entry, dict) else {}
    probs = RC.problems(entry)
    if probs and not force:
        return {"ok": False, "problems": probs,
                "hint": "pass force=true to store it anyway"}
    eid = str(entry.get("id") or "").strip() or RC.entry_id(
        str(entry.get("kind") or ""), str(entry.get("name") or ""))
    existing = ENTRIES.get(eid)
    rec = (RC.merge(existing, entry, now=now_iso()) if existing
           else RC.normalise(dict(entry, id=eid), now=now_iso()))
    ENTRIES[rec["id"]] = rec
    await _save(rec)
    await emit_event({"type": "registry.upsert", "id": rec["id"], "kind": rec["kind"],
                      "name": rec["name"], "created": existing is None})
    return {"ok": True, "entry": rec, "problems": probs, "created": existing is None}


@capability("registry.delete", memory="off",
            http_method="POST", http_path="/registry/delete", http_tags=["registry"],
            description="Remove a registry entry. Inputs: id (str!). "
                        "Output: {ok, id}.")
async def registry_delete(id: str = "", trace_id=None):
    eid = str(id or "").strip()
    if eid not in ENTRIES:
        return {"ok": False, "error": "no entry %r" % eid}
    ENTRIES.pop(eid, None)
    await _drop(eid)
    await emit_event({"type": "registry.delete", "id": eid})
    return {"ok": True, "id": eid}


@capability("registry.interop", memory="off", silent=True,
            http_method="GET", http_path="/registry/interop", http_tags=["registry"],
            description="Every entry projected onto the interoperability "
                        "boundaries (contract / resolution / policy / evidence), "
                        "schema vera.agent-registry-entry/v1. This is the view the "
                        "Agent Bridge summary reads. Query: kind. "
                        "Output: {schema, entries:[...], count, external_only}.",
            contract=_orch._inspection_contract("registry.interop", effects=["read"]))
async def registry_interop(kind: str = "", trace_id=None):
    rows = [RC.to_interop(e) for e in RC.search(list(ENTRIES.values()), "",
                                                kind=kind, limit=500)]
    return {"schema": "vera.agent-registry-entry/v1", "entries": rows,
            "count": len(rows),
            # Named, not implied: an entry nothing inside Vera can invoke is an
            # external technique, not a broken registration.
            "external_only": sum(1 for r in rows if r["resolution"]["external_only"])}


@capability("registry.sync_skill", memory="off",
            http_method="POST", http_path="/registry/sync_skill",
            http_tags=["registry"],
            description="Project a registry entry into Vera's own skills library "
                        "so chat and loops can attach it, keeping the provenance "
                        "tags that say where it really came from. Idempotent: "
                        "re-syncing updates the same skill rather than making a "
                        "second one. Inputs: id (str!). "
                        "Output: {ok, skill_id, created}.")
async def registry_sync_skill(id: str = "", trace_id=None):
    e = ENTRIES.get(str(id or "").strip())
    if not e:
        return {"ok": False, "error": "no entry %r" % id}
    payload = RC.to_vera_skill(e)
    create = CAPABILITY_REGISTRY.get("skills.create")
    update = CAPABILITY_REGISTRY.get("skills.update")
    if not create:
        return {"ok": False, "error": "skills.* is not loaded on this instance"}
    # skills.create mints its OWN uuid id and takes tags as a comma-separated
    # STRING, so the entry's id cannot be the skill's. The `registry:<id>` tag
    # is the join key instead, and finding it is what makes a re-sync an update
    # rather than a duplicate.
    marker = "registry:%s" % e["id"]
    sk = sys.modules.get("skills")
    existing_id = ""
    for sid_, rec in (getattr(sk, "SKILLS", {}) or {}).items() if sk else []:
        if marker in (rec.get("tags") or []):
            existing_id = sid_
            break
    args = {"name": payload["name"], "content": payload["content"],
            "description": payload["description"], "type": payload["type"],
            "tags": ",".join(payload["tags"]), "enabled": True}
    if existing_id and update:
        await update["func"](id=existing_id, **args)
        return {"ok": True, "skill_id": existing_id, "created": False}
    res = await create["func"](**args)
    new_id = (res or {}).get("id") or (res or {}).get("skill", {}).get("id", "")
    return {"ok": True, "skill_id": new_id, "created": True}


@capability("registry.import_skill", memory="off",
            http_method="POST", http_path="/registry/import_skill",
            http_tags=["registry"],
            description="Read a Vera skill back into the registry. A skill that "
                        "originally came from an external agent keeps that origin "
                        "- a round trip must not launder it into a Vera-native "
                        "one. Inputs: skill_id (str!). Output: {ok, entry}.")
async def registry_import_skill(skill_id: str = "", trace_id=None):
    sk = sys.modules.get("skills")
    rec = (getattr(sk, "SKILLS", {}) or {}).get(str(skill_id or "").strip()) if sk else None
    if not rec:
        return {"ok": False, "error": "no skill %r" % skill_id}
    e = RC.from_vera_skill(rec)
    e["id"] = e["id"] or RC.entry_id(e["kind"], e["name"])
    e = RC.normalise(e, now=now_iso())
    ENTRIES[e["id"]] = e
    await _save(e)
    return {"ok": True, "entry": e}


# Same startup shape skills.py uses. NOT `schedule(...)` - that takes a required
# interval, and getting it wrong here raises at import time, which does not fail
# one capability, it removes the module.
try:
    # Through the orchestrator, so a node worker skips it unless it is
    # on the worker allow-list (worker_placement_core).
    import Vera.vera.capability_orchestration as _co_start
    _co_start.start_at_import(_startup_load, "registry_startup_load")
except Exception:
    pass


# â”€â”€ panel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_PANEL_HTML = """
<div class="reg-wrap">
  <div class="reg-bar">
    <input id="reg-q" placeholder="search skills, tools, loops, techniquesâ€¦"
           oninput="regLoad()">
    <select id="reg-kind" onchange="regLoad()">
      <option value="">all kinds</option>
      <option value="skill">skills</option>
      <option value="tool">tools</option>
      <option value="loop">loops</option>
      <option value="technique">techniques</option>
      <option value="os">operating systems</option>
    </select>
    <span id="reg-count" class="reg-hint"></span>
  </div>
  <div id="reg-list" class="reg-list"></div>
  <div id="reg-detail" class="reg-detail"></div>
</div>
<style>
 .reg-wrap{padding:10px;font:13px/1.5 system-ui,sans-serif}
 .reg-bar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
 .reg-bar input{flex:1;min-width:180px;padding:6px 8px}
 .reg-hint{opacity:.6;font-size:12px}
 .reg-list{display:grid;gap:6px}
 .reg-card{border:1px solid rgba(128,128,128,.3);border-radius:6px;padding:8px 10px;cursor:pointer}
 .reg-card:hover{border-color:rgba(128,128,128,.65)}
 .reg-k{display:inline-block;font-size:11px;padding:1px 6px;border-radius:10px;
        border:1px solid rgba(128,128,128,.4);margin-right:6px;opacity:.85}
 .reg-name{font-weight:600}
 .reg-sum{opacity:.8;margin-top:2px}
 .reg-meta{font-size:11px;opacity:.6;margin-top:4px}
 .reg-detail{margin-top:12px;white-space:pre-wrap}
 .reg-detail h4{margin:.6em 0 .2em}
 .reg-empty{opacity:.6;padding:14px}
</style>
"""

_PANEL_JS = """
async function regLoad(){
  const q=(document.getElementById('reg-q')||{}).value||'';
  const k=(document.getElementById('reg-kind')||{}).value||'';
  const r=await fetch('/registry?q='+encodeURIComponent(q)+'&kind='+encodeURIComponent(k))
              .then(x=>x.json()).catch(()=>({entries:[]}));
  const es=r.entries||[];
  const c=document.getElementById('reg-count');
  if(c) c.textContent=es.length+' of '+((r.count!=null)?r.count:es.length)+' entries';
  const el=document.getElementById('reg-list');
  if(!el) return;
  if(!es.length){ el.innerHTML='<div class="reg-empty">Nothing registered matches that.</div>'; return }
  el.innerHTML=es.map(e=>{
    const h=(e.helpers||[]).length;
    const own=(e.owner&&e.owner.agent)||'unattributed';
    const org=(e.source&&e.source.origin)||'unknown';
    return '<div class="reg-card" onclick="regOpen(\\''+encodeURIComponent(e.id)+'\\')">'
      +'<span class="reg-k">'+regEsc(e.kind||'?')+'</span>'
      +'<span class="reg-name">'+regEsc(e.name||e.id)+'</span>'
      +'<div class="reg-sum">'+regEsc(e.summary||'')+'</div>'
      +'<div class="reg-meta">'+regEsc(own)+' Â· '+regEsc(org)
      +(h?(' Â· '+h+' helper'+(h===1?'':'s')):'')
      +((e.tags||[]).length?(' Â· '+regEsc((e.tags||[]).join(', '))):'')+'</div></div>';
  }).join('');
}
function regEsc(s){return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function regOpen(id){
  const r=await fetch('/registry/entry?id='+id).then(x=>x.json()).catch(()=>null);
  const el=document.getElementById('reg-detail'); if(!el) return;
  if(!r||!r.ok){ el.innerHTML='<div class="reg-empty">Could not load that entry.</div>'; return }
  const e=r.entry, io=r.interop||{};
  let h='<h4>'+regEsc(e.name)+' <span class="reg-k">'+regEsc(e.kind)+'</span></h4>';
  h+='<div class="reg-sum">'+regEsc(e.summary)+'</div>';
  if(e.body) h+='<h4>Detail</h4><div>'+regEsc(e.body)+'</div>';
  if((e.helpers||[]).length){
    h+='<h4>Helper scripts</h4>';
    h+=e.helpers.map(x=>'Â· '+regEsc(x.name)+' â€” '+regEsc(x.purpose||'(no purpose recorded)')
        +(x.path?('\\n  '+regEsc(x.path)):'')).join('\\n');
  }
  h+='<h4>Reach</h4>';
  // Named, not implied: an entry Vera cannot invoke is an external technique,
  // not a broken registration.
  h+=(io.resolution&&io.resolution.external_only)
      ? 'external only â€” nothing inside Vera invokes this'
      : regEsc(((io.resolution||{}).reachable_as||[]).join(', '));
  h+='\\n\\nowner: '+regEsc(((io.policy||{}).owner)||'unattributed')
    +'\\norigin: '+regEsc(((io.policy||{}).origin)||'unknown');
  if(e.source&&e.source.path) h+='\\nsource: '+regEsc(e.source.path);
  el.innerHTML=h;
}
regLoad();
"""

@APP.get("/ui/panels/registry-panel", include_in_schema=False)
async def _registry_panel():
    """The registry's own page, served for the Agents/Skills/Ontologies shell.

    A standalone document rather than injected markup because that shell mounts
    its sections as IFRAMES - the same way agents, skills and ontologies are
    already served. Registering this as a top-level tab was wrong: the registry
    IS a skills surface, and it belongs beside the two stores it projects into
    and out of, not in a fourth place a reader has to know about separately.
    """
    from fastapi.responses import HTMLResponse
    p = Path(__file__).parent / "registry_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:#c96b6b'>registry_panel.html not found</p>")


# mode="element": registered and listed so it is discoverable, but NOT a
# top-level tab - it is rendered inside the Agents/Skills/Ontologies panel.
register_ui("agent-registry", "Registry", "▥", _PANEL_HTML, js=_PANEL_JS,
            ui_caps=["registry.list", "registry.get", "registry.interop",
                     "registry.upsert", "registry.delete", "registry.sync_skill",
                     "registry.import_skill"],
            mode="element", tab_order=62)
