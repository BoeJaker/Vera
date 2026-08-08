---
name: improve-vera
description: Recursively diagnose and fix a live issue anywhere in Vera (not agentic-loop-specific) — live-test against the real running instance, root-cause from real evidence, fix, verify against the real shipped code, document, restart, re-test. Use whenever asked to debug, fix, or improve Vera itself against its live running instance, not just review its source.
---

# Improving Vera — the recursive live-debug cycle

This packages the working method that took the v7 agentic loop's
UI-verification path from completely broken to a confirmed, end-to-end
working state over one long session (`documentation/35-agentic-loop-v6-improvement-plan.md`,
`documentation/36-agentic-loop-v7-evaluation.md`). Applies to ANY Vera
subsystem, not just the agentic loop — the loop was just what that
session happened to be working on.

## 0. Before anything else — the rule that gets violated if you're not careful

**Never have more than ONE Ollama-calling test running at a time.**

Test loops, agent runs, and most live-verification tests call Ollama for
LLM inference. Two running concurrently contend for the SAME shared
GPU/Ollama capacity — this doesn't just slow both down, it muddies
timing-based findings (an elapsed_ms number becomes meaningless if
something else was hammering the same GPU at the same time) and can
make a genuinely-fixed bug look like it's still failing, or a real
regression look like normal variance.

This was violated TWICE in the session this skill is drawn from — once
early (three test loops launched at once, caught and corrected), once
late (a second test launched before confirming the first had actually
finished, caught by the user watching live). Both times it was a lapse
during a context-switch, not forgetting the rule existed. Treat it as
requiring an explicit check, not memory:

**Before launching any new Ollama-calling test, check first:**
```
redis-cli ZREVRANGE vera:loop:sessions 0 3 WITHSCORES
redis-cli HGET vera:loop:run:<most-recent-session-id> status
```
If anything relevant comes back `running`, either wait for it to finish
or explicitly decide (and say so) that it's fully diagnosed and no
longer teaching you anything — don't launch a second one "just to save
time." A background *monitoring* task (checking Redis, tailing a log)
that does NOT itself call Ollama is fine to run alongside a live test —
the constraint is specifically about concurrent Ollama/GPU consumers.

## 1. The cycle

1. **Live-test against the real running Vera.** Capabilities are plain
   HTTP endpoints — dotted name → slash path
   (`ide.fs.write` → `POST /ide/fs/write`). For LAUNCHING/monitoring a
   test loop specifically, use direct HTTP
   (`https://llm.int:8999/<path>`, self-signed cert,
   `-SkipCertificateCheck`/`-k`) and Redis, not
   `mcp__vera__dag_run`/other MCP loop-launch tools — see §1a for exactly
   why, and for what MCP tools genuinely ARE good for.
2. **Diagnose from REAL evidence, not assumption.** Pull the actual
   Redis-backed event trace (`vera:loop:events:<sid>`, a JSON-per-line
   list) and read what actually happened, not what you'd expect to
   happen. When a fix "should" work but the live trace says otherwise,
   trust the trace and keep digging — see §3 below for why.
3. **Fix the ROOT cause.** A locally-correct, individually-verified fix
   is not the same as a fix that survives the FULL real dispatch path —
   see §3.
4. **Verify against the real shipped code before trusting a fix.** See
   §2 — this has a specific, easy-to-miss trap.
5. **Document as you go**, not after — dated, numbered entries (this
   codebase's convention is `§2.N` for a fix, `§N` for a doc section) in
   a living plan doc, written at the moment each fix lands, not batched
   at the end.
6. **Restart Vera** to pick up the change — see §4 for how, and what to
   check first.
7. **Re-test live**, one test at a time (§0), and keep going until the
   evidence — not just the code review — says it's fixed.

## 1a. The full interface surface — don't default to only one

There are FOUR distinct ways to act on Vera, each genuinely better for
different jobs. This session mostly used only two (HTTP + SMB) out of
habit, having over-generalized one narrow restriction ("don't use MCP
for launching/watching a test loop") into avoiding MCP tools
altogether — worth naming explicitly so it isn't repeated.

1. **Direct HTTP capability calls** (`https://llm.int:8999/<path>`) —
   the right choice for launching/monitoring a TEST LOOP specifically
   (full control over polling cadence, direct Redis trace access) and
   for any one-off capability call where you want the raw response.
2. **SMB file edits** (`\\llm.int\boejaker\Vera`, Read/Edit/Write tools)
   — the right choice when YOU have already diagnosed the exact fix and
   want to apply it precisely yourself. This is what almost every fix in
   docs 35/36 used.
3. **MCP tools** — genuinely useful, actively under-used this session
   past the one real restriction:
   - `mcp__vera__dag_run` and the other `dag_agent_loop*`/loop-launch
     tools — **avoid these for testing.** Launching or watching a test
     loop through them doesn't give the same visibility as direct HTTP +
     Redis, and re-launching via `dag_run` repeatedly instead of a local
     polling script is exactly the "don't poll from the main thread"
     mistake in a different costume.
   - `mcp__vera__obs_health`/`obs_diagnostics`/`obs_redis` — clean,
     verified-working drop-ins for a manual `GET /health`-style check;
     no reason to hand-roll the HTTP call when these exist.
   - `mcp__vera__obs_events` — a GLOBAL recent-events firehose across the
     whole cluster (confirmed live: useful for "is anything broadly
     happening right now"), but it is NOT scoped to one session — for
     deep-diving a SPECIFIC test loop's own trace, `vera:loop:events:<sid>`
     via Redis is still the right tool, not a replacement.
   - `mcp__vera__code_author`/`code_edit`/`code_read` — Vera's OWN
     versioned code-authoring/editing system (coding-specialist model,
     auto-versioned in Vera's code store, diff/restore-able). This is
     the tool for OUTSOURCING an implementation to Vera itself rather
     than editing directly — meaningfully different from #2 (SMB edit),
     which is Claude applying an already-fully-diagnosed fix personally.
     Reach for `code.edit`/`code.author` specifically when the goal
     includes exercising or improving VERA'S OWN ability to make the
     change, not just getting the change made.
   - `mcp__vera__sandbox_session_exec`/`run_code`/etc. — run something
     INSIDE a specific session's own sandbox container, as opposed to
     `exec.bash.run`'s host-level execution. Use when what you're
     verifying needs to run in the SAME isolated environment a real loop
     step would run in, not the host.
4. **HTML/UI-driven verification** — actually drive Vera's own web UI
   (e.g. `operator.run` with `kind="live"`, targeting Vera's own running
   instance) rather than only calling the backend API. Use when what
   needs checking is genuinely a UI/rendering/interaction question ("does
   the panel actually show this correctly"), not just "does the backend
   return the right JSON" — the two are not the same question, and this
   session's whole operator.run investigation (docs 35 §5.14 onward) is
   itself an example of exactly this kind of check applied to code
   Vera generates, not Vera's own UI, but the same principle: sometimes
   only actually looking at rendered output catches what an API response
   can't show.

## 2. The standalone-verification trap

A script that imports only the ONE module you're testing will NOT
register capabilities defined in OTHER modules — `CAPABILITY_REGISTRY.get("operator.run")`
returns `None` if you only imported `dag_workshop_capabilities`, for
example, even though `operator.run` is a completely real, live,
correctly-registered capability in the running orchestrator. This bit
the same investigation twice before being internalized. Always import
the capability's OWN module first:

```python
import sys
sys.path.insert(0, "/home/boejaker")
import Vera.vera.operator.operator_web_capabilities  # registers operator.run
from Vera.vera.dag.dag_workshop_capabilities import _coerce_args  # what you're actually testing
```

Run verification scripts via `exec.bash.run` against the real host
Python (has the actual dependencies installed — check which interpreter
that is; it is often NOT bare `python3` on PATH, see §4), not assumed
to work from reading the code alone.

## 3. Root-cause discipline — the §2.45 cautionary tale

In the session this skill is drawn from, FIVE separate fixes were each
written, each individually verified as correct against the real shipped
code, each restarted-and-live-tested — and NONE of them actually worked,
because a sixth, unrelated, PRE-EXISTING function elsewhere in the same
file was silently reverting all of their output on every single call,
undetected through several rounds of "this is definitely fixed now."

The lesson: verifying a fix in isolation (a unit-style test of just the
function you changed) proves the function is correct. It does NOT prove
the fix survives everything that happens to its output AFTER it runs and
BEFORE the real dispatch. When a fix looks right but the live behavior
doesn't change, the right move is to trace the ENTIRE path from "where
does this value get set" to "where does it actually get used" — every
intermediate step, not just the one you already suspect — rather than
writing a sixth variant of the same fix and hoping.

**When you find a bug like this, check if it's duplicated elsewhere.**
The actual §2.45 bug (a URL-detection check with too-narrow a lookback
window) turned out to be independently copy-pasted in three different
functions across the same file, all with the same latent break. Grep
for the same pattern shape before considering the investigation closed,
and if it's genuinely duplicated, extract a shared helper instead of
patching each copy separately — a fourth copy-paste of the broken
pattern shouldn't be possible.

## 3a. "No error" is not proof a code path ran — watch for swallowed exceptions

Every `@capability`-decorated function is wrapped as `async def wrap(**kw)`
(`capability_orchestration.py`) — **keyword-only, no positional args at
all.** A helper that calls one directly as
`some_capability(positional_arg, kw=val)` raises `TypeError` on every
single call. If that call site has its own `try/except Exception` around
it (a common, reasonable-looking defensive pattern — "best-effort, don't
block the caller if this side-lookup fails"), the exception is silently
swallowed and the code returns its empty/default fallback value forever.
The response looks completely healthy: no error field, valid shape, just
an empty result — indistinguishable from "ran fine, genuinely found
nothing" unless you check the debug log or reason about the failure mode
directly.

This shipped and passed a "confirmed working live" check for an entire
session before being caught: a commit-correlation feature (`ide.git.log`
called from two different helper functions) was calling it with `path`
positional instead of `path=`. The live check that "confirmed" it hit an
empty-result case anyway (no matching commits in that window), so the
silently-broken call path and a genuinely-working-but-empty call path
were indistinguishable from the HTTP response alone.

**When verifying a fix that has its own exception handling, don't trust
an empty/default result as proof of success — either make the test case
guaranteed to produce a NON-empty result if the code path truly runs, or
temporarily remove/narrow the `except` to let a real failure surface.**
Also: when calling any `@capability`-wrapped function directly from
Python (not via HTTP, which already forces keyword-style JSON→kwargs),
always pass every argument as a keyword — positional will fail, silently
or loudly depending on what wraps the call site.

## 4. Restarting Vera

Vera runs natively (no docker) via `./build.sh run` on the `llm` host.
A tool already exists for Claude to restart it directly over SSH:
`C:\Users\User\.vera-ops\Invoke-VeraRestart.ps1` (defaults to a dry run;
pass `-Confirmed` to actually restart). Before confirming a restart,
check for other active loop sessions (§0's redis-cli check) that aren't
your own already-diagnosed test traffic — a restart kills everything
in flight.

If setting this up fresh: the real interpreter with Vera's dependencies
installed is often a specific venv (e.g. a `langchain` venv), NOT bare
`python3` on the host's PATH — confirm which one actually has the
project's dependencies (e.g. `uvicorn`) before assuming a restart
command is complete. `C:\Users\User\.vera-ops\Check-VeraLog.ps1` and
`Check-VeraPy.ps1` exist for diagnosing a failed restart (tail the
remote log / check which Python has the deps) without needing Vera
itself to be up.

## 5. Background monitoring, not tight polling

Use a `run_in_background` PowerShell (or equivalent) script that polls
Redis directly and reports once (or on an interval, reporting each
time) — never repeated `mcp__vera__dag_run`/loop-tool calls from the
main thread, and never a tight sleep-loop typed directly into the main
conversation. Wait for the background task's completion notification
rather than manually re-checking on a timer.

## 6. The Perf/Observe UI — check this before assuming a hang is your bug

Vera has a live event-loop stall/hang watchdog: `perf.stalls`
(`GET /perf/stalls`) returns captured `{kind:'stall'|'hang'|'note', ts,
stalled_ms, where, stack}` events, with `hang` rows carrying the exact
blocking call's stack trace — surfaced in the UI as the ⚡ Perf pane.
Also available: `perf.log.tail`/`perf.log.files` (recent orchestrator
log lines) and `perf.scan`/`perf.remediate` (health scan + fix).

Check this BEFORE assuming a slow/hung restart, a stuck test, or a
mysteriously long tool call is a bug in whatever you're actively
changing — it may be an unrelated, already-tracked event-loop stall
elsewhere in the process (a known category: e.g. a polling capability
firing one network call per record instead of one bulk call can starve
the loop badly enough to look like an unrelated hang). A five-second
`GET /perf/stalls?limit=20` check can save a long detour chasing the
wrong cause.

## 7. When to keep watching vs. when to stop

Don't wait out a run that's clearly re-demonstrating an
already-diagnosed, already-fixed-in-a-later-attempt problem — stop,
fix, restart, re-test instead of letting it grind to completion for no
new information.

DO take a direct, live "it looks like it's still struggling" (or
similar) from a human watching the actual UI seriously and immediately
— in the session this skill is drawn from, the two most important bugs
of the whole session (§2.45's real root cause, and the recovery-path gap
before it) were both found because a human watching live caught a
pattern — identical error, identical elapsed time, on every retry —
that was easy to miss purely from re-reading trace JSON after the fact.
A live human's "this doesn't look right" is a stronger, cheaper signal
than another round of log archaeology; treat it as an instruction to
re-open the investigation, not as something to reassure past.
