"""evolve.pipeline.lint - what is wrong on a change's changed lines (looplab-control §9 K6, 18 Oct 2026).

Reads the pipeline's own diff (evolve.pipeline.diff) and the files as the change leaves them, and hands both to
diff_lint_core. External checkers run when installed (node for JavaScript and the panels' inline scripts, ruff for
Python); a checker that is not installed is reported as absent, never as clean. Nothing here writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

try:
    from Vera.vera.evolve import diff_lint_core as core
except ImportError:                                        # pragma: no cover
    from vera.evolve import diff_lint_core as core         # type: ignore

log = logging.getLogger("vera.evolve.diff_lint")
_ROOT = Path(__file__).resolve().parents[2]                # the Vera checkout this process runs from
MAX_FILE_BYTES = 2_000_000


async def _call(cap_name: str, /, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {cap_name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        log.debug("lint: %s failed: %s", cap_name, e)
        return {"error": f"{cap_name}: {e}"}


async def _run(argv: List[str], stdin: str = "", cwd: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, cwd=cwd)
        out, err = await asyncio.wait_for(p.communicate(stdin.encode("utf-8", "replace")), timeout)
        return {"rc": p.returncode, "out": out.decode("utf-8", "replace"), "err": err.decode("utf-8", "replace")}
    except Exception as e:
        return {"rc": None, "out": "", "err": str(e)}


async def _content(path: str, worktree: str, branch: str) -> Optional[str]:
    """The file as the change leaves it: from the live worktree, else from the branch in git."""
    if worktree and Path(worktree, path).is_file():
        try:
            p = Path(worktree, path)
            return p.read_text(encoding="utf-8", errors="replace") if p.stat().st_size <= MAX_FILE_BYTES else None
        except OSError:
            return None
    if branch:
        r = await _run(["git", "-C", str(_ROOT), "show", f"{branch}:{path}"], timeout=20)
        return r["out"] if r["rc"] == 0 and len(r["out"]) <= MAX_FILE_BYTES else None
    return None


@capability("evolve.pipeline.lint", memory="off", silent=True,
            http_method="GET", http_path="/evolve/pipeline/lint", http_tags=["evolve"],
            description="READ-ONLY. What is wrong on the lines a code pipeline's change touched: line checks (NUL "
                        "bytes, conflict markers, trailing whitespace, tab indents, long lines, debugger), Python "
                        "syntax (ast) of each changed .py file, node --check of each changed .js file and of an HTML "
                        "file's inline scripts, and ruff when it is installed. A checker that cannot run is listed "
                        "in `sources` as absent - never counted as clean. Inputs: id (str! pipeline id), max_files "
                        "(int=60). Output: {ok, pipeline, branch, findings[{path, line, code, message, source, "
                        "severity}], files[{path, findings}], sources{check: ok|absent|skipped}, summary}.")
async def cap_pipeline_lint(id: str = "", max_files: int = 60, trace_id=None) -> Dict[str, Any]:
    if not str(id or "").strip():
        return {"ok": False, "error": "id required"}
    got = await _call("evolve.pipeline.get", id=id)
    rec = (got or {}).get("pipeline") if isinstance(got, dict) else None
    if not rec:
        return {"ok": False, "error": (got or {}).get("error") or "pipeline not found"}
    d = await _call("evolve.pipeline.diff", id=id, max_bytes=600000)
    if not isinstance(d, dict) or d.get("error"):
        return {"ok": False, "error": (d or {}).get("error") or "no diff"}
    worktree, branch = str(rec.get("worktree") or ""), str(rec.get("branch") or "")
    if worktree and not Path(worktree).exists():
        worktree = ""
    node, ruff = shutil.which("node"), shutil.which("ruff")
    sources = {"line-checks": "ok", "python-ast": "ok", "node": "ok" if node else "absent", "ruff": "ok" if ruff else "absent"}
    findings: List[Dict[str, Any]] = []
    files_out = []
    parsed = [f for f in core.parse_unified(d.get("diff") or "") if not f["deleted"] and not f["binary"]]
    for f in parsed[:max(1, min(int(max_files or 60), 200))]:
        path, added = f["path"], f["added"]
        mine = core.line_checks(path, added)
        src = await _content(path, worktree, branch) if path.endswith((".py", ".js", ".cjs", ".mjs", ".html")) else None
        if src is not None:
            if path.endswith(".py"):
                mine += core.py_syntax(path, src)
                if ruff:
                    r = await _run([ruff, "check", "--output-format", "json", "--stdin-filename", path, "-"], src)
                    try:
                        mine += core.ruff_findings(path, json.loads(r["out"] or "[]"))
                    except ValueError:
                        sources["ruff"] = "skipped"
            elif node and path.endswith((".js", ".cjs", ".mjs")):
                r = await _run([node, "--check", "-"], src)
                mine += core.node_finding(path, r["err"] if r["rc"] else "", joined=False)
            elif node and path.endswith(".html"):
                js = core.inline_scripts(src)
                if js.strip():
                    r = await _run([node, "--check", "-"], js)
                    mine += core.node_finding(path, r["err"] if r["rc"] else "", joined=True)
        mine = core.on_changed(mine, added)
        findings += mine
        files_out.append({"path": path, "added": len(added), "findings": len(mine)})
    if d.get("truncated"):
        sources["diff"] = "truncated"
    return {"ok": True, "pipeline": id, "branch": branch, "findings": findings, "files": files_out,
            "sources": sources, "summary": core.summary(findings, sources)}
