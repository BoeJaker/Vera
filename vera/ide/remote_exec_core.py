# ============================================================================
# remote_exec_core.py — pure helpers for remote Claude Code runs (§6.5 seam)
# ============================================================================
#
# Dependency-free command composition + result parsing, extracted from
# ide_remote_capabilities so the security-sensitive bits are unit-testable
# without importing the whole app:
#   • build_claude_cmd — quoting + flag gating, incl. --resume (gap 1)
#   • parse_claude_result — capture the Claude session id (gap 2), so a
#     Vera-DRIVEN run joins the same association graph as an MCP-driven one.
# ============================================================================

from __future__ import annotations

import json
import shlex
from typing import Optional, Tuple


def build_claude_cmd(workdir: str, task: str, api_key: str, stream: bool,
                     permission_mode: str = "", oauth_token: str = "",
                     model: str = "", resume_session_id: str = "",
                     default_permission_mode: str = "") -> str:
    """Compose the remote shell command that runs Claude Code headless.

    Credentials (if any) are written to a 0600 env file and sourced, so they
    are not visible in the process list. With neither api_key nor oauth_token
    the CLI runs bare and uses the host's own `claude login` credentials.
    `resume_session_id` continues an EXISTING session via --resume (gap 1).
    Everything interpolated into the shell is `shlex.quote`d.
    """
    fmt = "stream-json --verbose" if stream else "json"
    pm = permission_mode or default_permission_mode
    parts = []
    cred_var = ("ANTHROPIC_API_KEY" if api_key
                else "CLAUDE_CODE_OAUTH_TOKEN" if oauth_token else "")
    cred_val = api_key or oauth_token
    if cred_var:
        parts.append("umask 077; mkdir -p ~/.vera; "
                     f"printf 'export {cred_var}=%s\\n' {shlex.quote(cred_val)} > ~/.vera/claude.env; "
                     ". ~/.vera/claude.env;")
    parts.append(f"cd {shlex.quote(workdir)} &&")
    parts.append("claude -p " + shlex.quote(task)
                 + f" --output-format {fmt}"
                 + (f" --resume {shlex.quote(resume_session_id)}" if resume_session_id else "")
                 + (f" --permission-mode {shlex.quote(pm)}" if pm else "")
                 + (f" --model {shlex.quote(model)}" if model else ""))
    return " ".join(parts)


def parse_claude_result(raw: str) -> Tuple[str, Optional[dict], str]:
    """Pull the result object out of `claude --output-format json` stdout
    (tolerant of extra lines; the LAST JSON line wins). Returns
    (summary, result_obj, claude_session_id). Capturing the session id (gap 2)
    is what joins a Vera-driven run to the association graph."""
    result_obj = None
    for line in reversed((raw or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result_obj = json.loads(line)
                break
            except Exception:
                continue
    summary = ""
    claude_sid = ""
    if isinstance(result_obj, dict):
        summary = result_obj.get("result") or result_obj.get("text") or ""
        claude_sid = (result_obj.get("session_id")
                      or result_obj.get("sessionId") or "")
    return summary, result_obj, claude_sid
