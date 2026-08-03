"""
ide_claude_sessions_capabilities.py  —  Ingest Claude Code's own conversation
transcripts into Vera
================================================================================
Claude Code (the `claude` CLI/VS Code extension) writes one JSONL file per
session to `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` on whatever
machine runs it. That's a *different* thing from the Vera MCP bridge
(vera_mcp_bridge.py) — the bridge only sees what Claude Code chooses to call
back into Vera for; this module reads what the user and Claude actually said
to each other, so Vera's memory graph / fabric / dream have that context too.

Three sources are supported:
  • local  (instance_id="")  — every ~/.claude/projects the Vera process can
    reach: its own OS account's home directory, PLUS this checkout's own
    <repo>/.claude/projects (a session sandbox or container commonly runs
    `claude` with HOME pointed at its mounted workspace, i.e. the repo root,
    so that's where its transcripts actually land — not the service
    account's real home). More roots can be added via the
    VERA_CLAUDE_PROJECTS_ROOTS env var (":" or ";" separated absolute paths,
    each already ending in ".../.claude/projects"). See _local_roots().
  • a connected VS Code client instance (ide.remote, kind=vscode-client) —
    fetched over the existing client-dispatch long-poll channel
    (claude_sessions_scan / claude_sessions_read actions added to the
    extension), so no SSH access or shared filesystem is required — this
    covers the common case of a desktop VS Code window pointed at a Vera
    project over a network share.
  • a registered SSH host (ide.remote, kind=ssh) — for a headless remote
    machine that runs `claude` but has no live VS Code extension connected.
    Scans `$HOME/.claude/projects` on that host via `find -printf` over the
    existing `exec.ssh.run`/`_ssh()` credential store (`ide_remote_capabilities.py`),
    and reads new bytes via `tail -c +N`. No shared filesystem needed —
    just the same SSH credential already used for code-server provisioning.

Only `user` / `assistant` transcript lines are recorded; bookkeeping line
types (ai-title, queue-operation, attachment, file-history-snapshot, …) and
"thinking" content blocks are skipped as noise. Each transcript is ingested
incrementally (byte-offset cursor persisted in .vera_claude_sessions_state.json,
mirroring the pattern in ide_remote_capabilities.py), so repeated calls only
record new turns.

Capabilities (group `ide.claude_sessions.*`)
──────────────────────────────────────────────
  sources        — list ingestible sources (local + alive vscode-client + ssh instances)
  scan           — list transcript files for a source
  ingest         — parse + record new lines from one transcript
  ingest_all     — scan + ingest every transcript for a source
  status         — ingestion state summary (files/bytes tracked per source)
  list_sessions  — group ingested turns into a session list (for the panel)
  history        — full ordered turn history for one session (for the panel)

A background job (schedule()) runs ingest_all for "local" and every alive
vscode-client instance every few minutes.

UI panel
─────────
  ide-claude-dispatch  — "Dispatch": browse every ingested Claude Code
                         conversation, grouped by session, with full history.
                         Served from ide_claude_sessions_panel.html via
                         GET /ide/claude_sessions/panel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from fastapi.responses import HTMLResponse

from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, register_ui, schedule,
)
from Vera.vera.fabric.data_fabric import _sqlite_conn
from Vera.vera.ide.ide_capabilities import _record, ide_git_log
from Vera.vera.ide.ide_remote_capabilities import (
    _load_instances, _get_instance, _client_dispatch, _client_alive, _ssh,
)

log = logging.getLogger("vera.ide.claude_sessions")
_HERE = Path(__file__).parent
_STATE_FILE = _HERE / ".vera_claude_sessions_state.json"


# ─────────────────────────────────────────────────────────────────────────────
# STATE  (per-source, per-file byte-offset cursor — mirrors .vera_remote_* files)
# ─────────────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("claude_sessions: could not read state: %s", e)
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("claude_sessions: could not write state: %s", e)


def _source_key(instance_id: str) -> str:
    return f"instance:{instance_id}" if instance_id else "local"


# ─────────────────────────────────────────────────────────────────────────────
# TRANSCRIPT PARSING
# ─────────────────────────────────────────────────────────────────────────────
def _extract_content_text(content) -> tuple:
    """Normalize a message.content field (str or list-of-blocks) into
    (display_text, tool_use_names). "thinking" blocks are intentionally
    skipped — internal reasoning traces are large and not conversational."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []
    parts: List[str] = []
    tool_uses: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text", "")
            if t:
                parts.append(t)
        elif btype == "tool_use":
            name = block.get("name", "?")
            tool_uses.append(name)
            parts.append(f"[tool_use: {name}]")
        elif btype == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner = " ".join(
                    b.get("text", "") for b in inner
                    if isinstance(b, dict) and b.get("type") == "text")
            snippet = str(inner)[:300]
            parts.append(f"[tool_result: {snippet}]" if snippet else "[tool_result]")
    return "\n".join(parts).strip(), tool_uses


def _parse_turns(raw_lines: List[str]) -> List[dict]:
    """Parse raw JSONL lines into normalized conversational turns."""
    turns = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        rtype = row.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = row.get("message") or {}
        role = msg.get("role") or rtype
        text, tool_uses = _extract_content_text(msg.get("content"))
        if not text:
            continue
        turns.append({
            "uuid": row.get("uuid", ""),
            "role": role,
            "text": text,
            "tool_uses": tool_uses,
            "ts": row.get("timestamp", ""),
            "session_id": row.get("sessionId", ""),
            "cwd": row.get("cwd", ""),
            "git_branch": row.get("gitBranch", ""),
            "is_error": bool(row.get("isApiErrorMessage")),
        })
    return turns


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL FILESYSTEM SOURCE
# ─────────────────────────────────────────────────────────────────────────────
# `~/.claude/projects` alone assumes Claude Code ran with the same HOME as
# whatever OS account runs the Vera process — true for an interactive SSH
# login, false for a service/container account (root, appuser, a session
# sandbox with HOME pointed at its mounted workspace, ...). Every local root
# is scanned and files are tagged "<label>::<rel-path>" so ingest can resolve
# each one back to the right root. `vera-repo` always covers this checkout —
# e.g. a sandbox that mounts the repo and runs `claude` with HOME set to it.
_REPO_ROOT = _HERE.resolve().parents[1]


def _local_roots() -> Dict[str, Path]:
    roots: Dict[str, Path] = {
        "home":      Path(os.path.expanduser("~")) / ".claude" / "projects",
        "vera-repo": _REPO_ROOT / ".claude" / "projects",
    }
    extra = os.environ.get("VERA_CLAUDE_PROJECTS_ROOTS", "")
    for i, p in enumerate(x.strip() for x in extra.replace(";", ":").split(":")):
        if p:
            roots[f"extra{i}"] = Path(p)
    return roots


def _local_scan() -> List[dict]:
    files = []
    for label, root in _local_roots().items():
        if not root.exists():
            continue
        for p in root.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            sub = str(p.relative_to(root)).replace(os.sep, "/")
            files.append({
                "rel": f"{label}::{sub}",
                "size": st.st_size,
                "mtime": st.st_mtime * 1000.0,
            })
    return files


def _split_local_rel(rel: str) -> tuple:
    """"<label>::<sub-path>" -> (label, sub_path). Falls back to the "home"
    root for rel values recorded before multi-root support existed."""
    if "::" in rel:
        label, _, sub = rel.partition("::")
        return label, sub
    return "home", rel


def _shell_dquote(s: str) -> str:
    """Escape a string for safe interpolation inside a DOUBLE-quoted shell
    string (not full shlex.quote — that would also escape '$', breaking the
    intentional $HOME expansion around it)."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


async def _read_new_bytes(instance_id: str, rel: str, offset: int) -> Optional[str]:
    """Fetch bytes of `rel` from `offset` to EOF for the given source."""
    if not instance_id:
        label, sub = _split_local_rel(rel)
        root = _local_roots().get(label)
        if root is None:
            log.warning("claude_sessions: unknown local root %r in %r", label, rel)
            return None
        full = root / sub
        try:
            with open(full, "rb") as f:
                f.seek(offset)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except OSError as e:
            log.warning("claude_sessions: local read failed for %s: %s", rel, e)
            return None
    inst = _get_instance(instance_id)
    if inst and inst.get("kind") == "ssh":
        host_id = inst.get("host_id", "")
        if not host_id:
            log.warning("claude_sessions: ssh instance %s has no host_id", instance_id)
            return None
        # tail -c +N is 1-indexed (byte N onward); offset is a 0-indexed
        # byte count already consumed, so +1 to land on the first new byte.
        cmd = f'tail -c +{offset + 1} -- "$HOME/.claude/projects/{_shell_dquote(rel)}"'
        res = await _ssh(host_id, cmd, timeout=30)
        if not res.get("ok"):
            log.warning("claude_sessions: ssh read failed for %s: %s", rel,
                       res.get("error") or res.get("stderr"))
            return None
        return res.get("stdout", "")
    out = await _client_dispatch(instance_id, "claude_sessions_read",
                                  {"path": rel, "offset": offset}, wait=30)
    if not out.get("ok", True):
        log.warning("claude_sessions: client read failed for %s: %s", rel, out.get("error"))
        return None
    return (out.get("result") or {}).get("content", "")


async def _ingest_file(instance_id: str, rel: str, state: dict) -> int:
    """Ingest new lines from one transcript. Returns count of new turns recorded."""
    key = _source_key(instance_id)
    src_state = state.setdefault(key, {})
    file_state = src_state.setdefault(rel, {"offset": 0})
    offset = int(file_state.get("offset", 0))

    text = await _read_new_bytes(instance_id, rel, offset)
    if not text:
        return 0

    new_offset = offset + len(text.encode("utf-8"))
    turns = _parse_turns(text.splitlines())
    local_root_label, display_rel = _split_local_rel(rel) if not instance_id else ("", rel)
    project_dir = display_rel.split("/", 1)[0] if "/" in display_rel else ""
    session_uuid = ""
    recorded = 0
    for turn in turns:
        session_uuid = turn.get("session_id") or session_uuid
        role = turn["role"]
        text_body = turn["text"]
        vera_session_id = f"claude-cc:{session_uuid or project_dir}"
        await _record(
            session_id=vera_session_id,
            category="ide.claude_session_user" if role == "user" else "ide.claude_session_assistant",
            text=f"[Claude Code · {project_dir}] {text_body[:180]}",
            full_text=text_body,
            tags=["claude_code", "external_session", role] + ([project_dir] if project_dir else []),
            importance=0.55 if role == "user" else 0.6,
            source_type="human" if role == "user" else "ai",
            record_type="message",
            capability_name="ide.claude_sessions.ingest",
            broadcast_type="ide.claude_session_turn",
            fabric_dataset="ide.claude_sessions",
            metadata={
                "instance_id": instance_id, "project_dir": project_dir,
                "claude_session_id": session_uuid, "cwd": turn.get("cwd", ""),
                "git_branch": turn.get("git_branch", ""),
                "tool_uses": turn.get("tool_uses", []),
                "local_root": local_root_label,
            },
            fabric_data={
                "instance_id": instance_id, "project_dir": project_dir,
                "claude_session_id": session_uuid, "role": role,
                "text": text_body[:20000], "ts": turn.get("ts", ""),
            },
            dedup_key=f"ccsess:{rel}:{turn.get('uuid') or new_offset}",
        )
        recorded += 1

    file_state["offset"] = new_offset
    file_state["updated_at"] = now_iso()
    return recorded


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "ide.claude_sessions.sources",
    http_method="GET", http_path="/ide/claude_sessions/sources", http_tags=["ide", "claude_sessions"],
    memory="off", silent=True,
    description="List ingestible Claude Code transcript sources: the Vera "
                "host's own local ~/.claude/projects (instance_id='') plus "
                "every connected vscode-client instance plus every "
                "registered ssh instance (alive = a fresh, capped SSH probe). "
                "Output: {sources: [{instance_id, label, kind, alive}]}.",
)
async def cap_claude_sessions_sources(trace_id=None) -> dict:
    sources = [{"instance_id": "", "label": "local (Vera host)",
                "kind": "local", "alive": True}]
    for inst in _load_instances():
        if inst.get("kind") == "vscode-client":
            sources.append({
                "instance_id": inst.get("id", ""),
                "label": inst.get("label", ""),
                "kind": "vscode-client",
                "alive": _client_alive(inst.get("id", "")),
            })
        elif inst.get("kind") == "ssh" and inst.get("host_id"):
            # Cheap, capped liveness probe (not tight-polled — called on
            # panel load/refresh, not on an interval) rather than trusting
            # a possibly-stale stored status field.
            alive = False
            try:
                res = await _ssh(inst["host_id"], "true", timeout=5)
                alive = bool(res.get("ok"))
            except Exception:
                alive = False
            sources.append({
                "instance_id": inst.get("id", ""),
                "label": inst.get("label", ""),
                "kind": "ssh",
                "alive": alive,
            })
    return {"sources": sources}


@capability(
    "ide.claude_sessions.scan",
    http_method="GET", http_path="/ide/claude_sessions/scan", http_tags=["ide", "claude_sessions"],
    memory="off", silent=True,
    description="List Claude Code transcript files (~/.claude/projects/**/*.jsonl) "
                "for a source. Input: instance_id (str — empty/omitted = every "
                "local root the Vera process can reach, see _local_roots() "
                "(its own home dir + this checkout's own .claude/projects, plus "
                "VERA_CLAUDE_PROJECTS_ROOTS); a vscode-client instance id to scan "
                "a connected VS Code window instead; an ssh instance id to scan "
                "$HOME/.claude/projects on that registered host over SSH). For "
                "local sources rel is '<root-label>::<path>' (root-label e.g. "
                "home, vera-repo); for ssh sources rel is the path relative to "
                "$HOME/.claude/projects. Output: {source, files: [{rel, size, mtime}]}.",
)
async def cap_claude_sessions_scan(instance_id: str = "", trace_id=None) -> dict:
    if not instance_id:
        return {"source": "local", "files": _local_scan()}
    inst = _get_instance(instance_id)
    if not inst:
        return {"error": f"instance not found: {instance_id}"}
    if inst.get("kind") == "ssh":
        host_id = inst.get("host_id", "")
        if not host_id:
            return {"error": f"ssh instance {instance_id} has no host_id"}
        # %s/%T@/%P: size, mtime (epoch, fractional), path relative to the
        # find root — same shape _local_scan() builds from os.stat() so both
        # sources parse identically downstream.
        cmd = ('ROOT="$HOME/.claude/projects"; '
               '[ -d "$ROOT" ] && find "$ROOT" -name "*.jsonl" '
               '-printf "%s\\t%T@\\t%P\\n" 2>/dev/null || true')
        res = await _ssh(host_id, cmd, timeout=30)
        if not res.get("ok"):
            return {"error": f"ssh scan failed: {res.get('error') or res.get('stderr', '')}"}
        files = []
        for line in (res.get("stdout") or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            size_s, mtime_s, rel = parts
            try:
                files.append({"rel": rel, "size": int(size_s), "mtime": float(mtime_s) * 1000.0})
            except ValueError:
                continue
        return {"source": instance_id, "files": files}
    if inst.get("kind") != "vscode-client":
        return {"error": f"scan only supports local, ssh, or vscode-client sources "
                          f"(instance kind={inst.get('kind')!r} not yet wired up)"}
    out = await _client_dispatch(instance_id, "claude_sessions_scan", {}, wait=30)
    if not out.get("ok", True):
        return {"error": out.get("error", "client scan failed")}
    result = out.get("result") or {}
    return {"source": instance_id, "files": result.get("files", [])}


@capability(
    "ide.claude_sessions.ingest",
    http_method="POST", http_path="/ide/claude_sessions/ingest", http_tags=["ide", "claude_sessions"],
    memory="off",
    description="Ingest new lines from one Claude Code transcript file into "
                "Vera's memory graph + fabric (dataset ide.claude_sessions), "
                "tailing from the last recorded byte offset. "
                "Input: rel (str! — path from scan(), e.g. "
                "'<project-dir>/<session-uuid>.jsonl'), instance_id (str — "
                "empty = local). Output: {ok, turns_recorded}.",
)
async def cap_claude_sessions_ingest(rel: str = "", instance_id: str = "", trace_id=None) -> dict:
    if not rel:
        return {"error": "rel is required"}
    state = _load_state()
    try:
        n = await _ingest_file(instance_id, rel, state)
    finally:
        _save_state(state)
    return {"ok": True, "turns_recorded": n}


@capability(
    "ide.claude_sessions.ingest_all",
    http_method="POST", http_path="/ide/claude_sessions/ingest_all", http_tags=["ide", "claude_sessions"],
    memory="off",
    description="Scan + ingest every Claude Code transcript for a source. "
                "Input: instance_id (str — empty = the Vera host's own local "
                "~/.claude/projects; a vscode-client instance id otherwise). "
                "Output: {ok, files_scanned, files_updated, turns_recorded}.",
)
async def cap_claude_sessions_ingest_all(instance_id: str = "", trace_id=None) -> dict:
    scan = await cap_claude_sessions_scan(instance_id=instance_id)
    if scan.get("error"):
        return {"error": scan["error"]}
    files = scan.get("files", [])
    state = _load_state()
    key = _source_key(instance_id)
    src_state = state.get(key, {})
    updated = 0
    total_turns = 0
    for f in files:
        rel = f["rel"]
        known = src_state.get(rel, {})
        if "offset" in known and f.get("size", 0) <= known["offset"]:
            continue  # nothing new
        n = await _ingest_file(instance_id, rel, state)
        if n:
            updated += 1
            total_turns += n
    _save_state(state)
    if updated:
        await emit_event({"type": "ide.claude_sessions.ingest_all", "source": key,
                          "files_scanned": len(files), "files_updated": updated,
                          "turns_recorded": total_turns})
    return {"ok": True, "files_scanned": len(files), "files_updated": updated,
            "turns_recorded": total_turns}


def _query_records_sync(limit: int) -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT id, data, created_at FROM fabric_records "
            "WHERE dataset_id=? ORDER BY created_at DESC LIMIT ?",
            ("ide.claude_sessions", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def _query_records(limit: int) -> List[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _query_records_sync, limit)


@capability(
    "ide.claude_sessions.list_sessions",
    http_method="GET", http_path="/ide/claude_sessions/list_sessions", http_tags=["ide", "claude_sessions"],
    memory="off", silent=True,
    description="Group ingested Claude Code turns into a session list for the "
                "Dispatch panel, most-recently-active first. Each session is "
                "correlated against this repo's git log for its own time "
                "window, so a session that produced commits shows them "
                "directly — the same join key Loop Lab's evolve.* runs use. "
                "Input: scan_limit (int, default 3000 — how many recent turns "
                "to scan before grouping). "
                "Output: {sessions: [{claude_session_id, project_dir, "
                "instance_id, turns, first_ts, last_ts, last_role, "
                "last_preview, commits: [{hash, author, date, ts, message}]}]}.",
)
async def cap_claude_sessions_list_sessions(scan_limit: int = 3000, trace_id=None) -> dict:
    scan_limit = max(1, min(20000, scan_limit))
    rows = await _query_records(scan_limit)
    sessions: Dict[str, dict] = {}
    for row in rows:
        try:
            data = json.loads(row.get("data") or "{}")
        except Exception:
            continue
        sid = data.get("claude_session_id") or "unknown"
        ts = data.get("ts") or row.get("created_at") or ""
        s = sessions.get(sid)
        if s is None:
            # Rows are scanned newest-first, so the first row seen per
            # session is already its most recent turn.
            sessions[sid] = s = {
                "claude_session_id": sid,
                "project_dir": data.get("project_dir", ""),
                "instance_id": data.get("instance_id", ""),
                "turns": 0,
                "first_ts": ts, "last_ts": ts,
                "last_role": data.get("role", ""),
                "last_preview": (data.get("text") or "")[:180],
            }
        s["turns"] += 1
        if ts and ts < s["first_ts"]:
            s["first_ts"] = ts
    out = sorted(sessions.values(), key=lambda s: s["last_ts"], reverse=True)
    # Correlation: which commit(s) to THIS repo landed during each session's
    # own time window — the shared join key with Loop Lab's evolve.* runs
    # (see ide.git.log's since/until support). A session with no commits in
    # its window (read-only work, or work on a different repo/checkout) just
    # gets an empty list — this never blocks the session list from loading.
    for s in out:
        try:
            log_res = await ide_git_log(path=str(_REPO_ROOT), since=s["first_ts"], until=s["last_ts"])
            s["commits"] = log_res.get("commits", [])
        except Exception as e:
            log.debug("claude_sessions: commit correlation failed for %s: %s",
                     s.get("claude_session_id"), e)
            s["commits"] = []
    return {"sessions": out}


@capability(
    "ide.claude_sessions.history",
    http_method="GET", http_path="/ide/claude_sessions/history", http_tags=["ide", "claude_sessions"],
    memory="off", silent=True,
    description="Full ordered turn history for one ingested Claude Code "
                "session, for the Dispatch panel. "
                "Input: claude_session_id (str!), scan_limit (int, default "
                "5000 — how many recent dataset rows to scan for matches). "
                "Output: {claude_session_id, turns: [{role, text, ts}]}.",
)
async def cap_claude_sessions_history(
    claude_session_id: str = "", scan_limit: int = 5000, trace_id=None,
) -> dict:
    if not claude_session_id:
        return {"error": "claude_session_id is required"}
    scan_limit = max(1, min(50000, scan_limit))
    rows = await _query_records(scan_limit)
    turns = []
    for row in rows:
        try:
            data = json.loads(row.get("data") or "{}")
        except Exception:
            continue
        if data.get("claude_session_id") != claude_session_id:
            continue
        turns.append({
            "role": data.get("role", ""),
            "text": data.get("text", ""),
            "ts": data.get("ts") or row.get("created_at") or "",
        })
    turns.sort(key=lambda t: t["ts"])
    return {"claude_session_id": claude_session_id, "turns": turns}


@capability(
    "ide.claude_sessions.status",
    http_method="GET", http_path="/ide/claude_sessions/status", http_tags=["ide", "claude_sessions"],
    memory="off", silent=True,
    description="Ingestion state summary: files tracked and bytes consumed "
                "per source. Output: {sources: {source_key: {files, bytes}}}.",
)
async def cap_claude_sessions_status(trace_id=None) -> dict:
    state = _load_state()
    out = {}
    for key, files in state.items():
        out[key] = {"files": len(files),
                    "bytes": sum(f.get("offset", 0) for f in files.values())}
    return {"sources": out}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED AUTO-INGESTION
# ─────────────────────────────────────────────────────────────────────────────
_SCHEDULE_INTERVAL_S = int(os.environ.get("VERA_CLAUDE_SESSIONS_INGEST_INTERVAL", "300"))


async def _scheduled_ingest_all():
    try:
        await cap_claude_sessions_ingest_all(instance_id="")
    except Exception as e:
        log.warning("claude_sessions: scheduled local ingest failed: %s", e)
    for inst in _load_instances():
        iid = inst.get("id", "")
        if inst.get("kind") == "vscode-client" and _client_alive(iid):
            try:
                await cap_claude_sessions_ingest_all(instance_id=iid)
            except Exception as e:
                log.warning("claude_sessions: scheduled ingest failed for %s: %s", iid, e)


schedule(_scheduled_ingest_all, _SCHEDULE_INTERVAL_S, name="ide.claude_sessions.autoingest")


# ─────────────────────────────────────────────────────────────────────────────
# UI PANEL — "Dispatch": every ingested Claude Code conversation, browsable
# ─────────────────────────────────────────────────────────────────────────────
_PANEL_PATH = _HERE / "ide_claude_sessions_panel.html"


@capability(
    "ide.claude_sessions.panel_html",
    http_method="GET", http_path="/ide/claude_sessions/panel", http_tags=["ide", "claude_sessions", "ui"],
    memory="off", silent=True,
    description="Serve the Dispatch panel HTML (text/html).",
)
async def cap_claude_sessions_panel_html(trace_id=None):
    try:
        html = _PANEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef4444;"
                "font-family:monospace;padding:40px'><h2>ide_claude_sessions_panel.html not found</h2>"
                f"<p>Expected: {_PANEL_PATH}</p></body></html>")
    return HTMLResponse(html)


register_ui(
    "ide-claude-dispatch",
    "Dispatch",
    "🗂",
    """<div id="ide-claude-dispatch-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/ide/claude_sessions/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "ide.claude_sessions.sources", "ide.claude_sessions.scan",
        "ide.claude_sessions.ingest", "ide.claude_sessions.ingest_all",
        "ide.claude_sessions.status", "ide.claude_sessions.list_sessions",
        "ide.claude_sessions.history",
    ],
    # Merged into the IDE tab (vscode_panel.html embeds /ide/claude_sessions/panel
    # as its "🗂 Dispatch" view). mode="element" keeps it listed for custom tabs /
    # solo pop-out without rendering a second top-level tab — same convention as
    # ide-remote-panel's "Remotes & Queue" merge.
    mode="element",
    tab_order=52,
)
