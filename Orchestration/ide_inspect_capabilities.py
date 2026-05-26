"""
ide_inspect_capabilities.py  —  Vera Source Self-Inspection Mode
==================================================================
Lets Vera inspect, analyse, and improve its OWN source code — but only
ever through a snapshot copy. Hard safety guarantees:

  1. The original source directory is resolved at module import time from
     capability_orchestration.__file__ — this is the ground truth.
  2. Every snapshot is written to  <PROJECT_ROOT>/__vera_inspect__/<stamp>/
     which is a totally separate tree from the live source.
  3. A path guard registered in the request path of the core fs write caps
     refuses writes into the source tree — irrespective of how the agent
     constructs paths.
  4. Promotion (writing a snapshot back to source) is an EXPLICIT opt-in
     operation that takes a confirmation token and produces a git-friendly
     side-by-side patch for review. It is NOT available via tool_dispatch.

Because a snapshot is just a normal directory, every IDE coding capability
(grep, edit_lines, replace, outline, read_lines, list_files, git.*)
works against it unchanged. The IDE UI simply opens the snapshot path as
its project root and the whole agent experience becomes "inspect mode".

Capabilities registered
────────────────────────
  ide.inspect.source_info       — describe the live source tree
  ide.inspect.snapshot          — create a fresh snapshot of the source tree
  ide.inspect.list_snapshots    — list existing snapshots
  ide.inspect.diff_snapshot     — unified diff: snapshot vs live source
  ide.inspect.delete_snapshot   — remove a snapshot directory
  ide.inspect.promote_snapshot  — explicit write-back (requires confirm token)

  ide.inspect.scaffold_capability  — generate a new @capability from a spec
  ide.inspect.scaffold_ui_panel    — generate a new register_ui() panel
  ide.inspect.review_file          — LLM-driven code review of one snapshot file
  ide.inspect.plan_improvement     — ask the thinker to plan a cross-file change

UI
───
  ide-inspect-panel  — registered as a side tool card inside the IDE panel
                       (the IDE panel's "Inspect" tab loads this via iframe).
                       You can also open it standalone at /ide/inspect/panel.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import HTMLResponse

from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    CAPABILITY_REGISTRY, OLLAMA_INSTANCES, OLLAMA_MODEL,
    capability, emit_event, now_iso, ollama_generate, pick_instance,
    register_ui,
)

import httpx as _httpx_inspect


# ─────────────────────────────────────────────────────────────────────────────
# Local generation helper that supports the full Ollama `options` payload.
# The shared ollama_generate() in capability_orchestration is a convenience
# wrapper (no temperature/num_ctx control), so for inspect-mode generations
# we call the Ollama HTTP API directly to get the temperature + context-size
# knobs we actually need.
# ─────────────────────────────────────────────────────────────────────────────

async def _ollama_with_options(
    prompt:      str,
    system:      str = "",
    model:       str = "",
    instance_id: str = "",
    prefer_gpu:  bool = False,
    options:     Optional[Dict[str, Any]] = None,
    timeout:     float = 180.0,
) -> str:
    import time as _time
    from Vera.Orchestration.capability_orchestration import (
        emit_event, _ollama_log_append, now_iso,
    )

    chosen = instance_id or pick_instance(prefer_gpu=prefer_gpu) or ""
    if not chosen:
        return ""
    inst = OLLAMA_INSTANCES.get(chosen, {})
    url  = inst.get("url", "")
    if not url:
        return ""
    use_model = model or OLLAMA_MODEL
    body: Dict[str, Any] = {
        "model":   use_model,
        "prompt":  prompt,
        "stream":  False,
    }
    if system:
        body["system"] = system
    if options:
        body["options"] = options

    # ── Log the Ollama request ───────────────────────────────────────────────
    # Identify the actual caller (one frame above _ollama_with_options)
    import traceback as _tb
    _caller_func = ""
    try:
        stack = _tb.extract_stack(limit=4)
        for frame in reversed(stack[:-1]):
            if os.path.basename(frame.filename) != os.path.basename(__file__):
                _caller_func = frame.name
                break
            if frame.name != "_ollama_with_options":
                _caller_func = frame.name
                break
        if not _caller_func:
            _caller_func = stack[-2].name if len(stack) >= 2 else "unknown"
    except Exception:
        _caller_func = "unknown"

    _req_id = str(uuid.uuid4())[:12]
    _t0 = _time.time()
    _prompt_preview = (prompt or "")[:120].replace("\n", " ")
    log.info("ollama_req [%s] model=%s inst=%s caller=ide_inspect:%s prompt=%s",
             _req_id, use_model, chosen, _caller_func, _prompt_preview)
    try:
        await emit_event({
            "type": "ollama.request", "req_id": _req_id,
            "model": use_model, "instance_id": chosen, "instance_url": url,
            "caller_file": "ide_inspect_capabilities.py", "caller_func": _caller_func,
            "caller_module": "ide_inspect_capabilities",
            "cap_name": f"ide.inspect.{_caller_func}",
            "prompt_preview": _prompt_preview, "json_mode": False,
            "prefer_gpu": prefer_gpu, "streaming": False,
        })
    except Exception:
        pass

    inst["in_use"] = inst.get("in_use", 0) + 1
    try:
        async with _httpx_inspect.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{url}/api/generate", json=body)
            r.raise_for_status()
            result = (r.json() or {}).get("response", "") or ""
            _elapsed = round(_time.time() - _t0, 2)
            log.info("ollama_done [%s] %.2fs caller=ide_inspect:%s", _req_id, _elapsed, _caller_func)
            _ollama_log_append({
                "req_id": _req_id, "model": use_model, "instance": chosen,
                "caller_file": "ide_inspect_capabilities.py", "caller_func": _caller_func,
                "prompt_preview": _prompt_preview, "ts": now_iso(),
                "status": "done", "elapsed_s": _elapsed,
            })
            try:
                await emit_event({
                    "type": "ollama.request_done", "req_id": _req_id,
                    "model": use_model, "instance_id": chosen,
                    "caller_file": "ide_inspect_capabilities.py", "caller_func": _caller_func,
                    "elapsed_s": _elapsed,
                })
            except Exception:
                pass
            return result
    except Exception as e:
        _elapsed = round(_time.time() - _t0, 2)
        log.warning("inspect _ollama_with_options [%s] FAILED after %.2fs: %s", chosen, _elapsed, e)
        _ollama_log_append({
            "req_id": _req_id, "model": use_model, "instance": chosen,
            "caller_file": "ide_inspect_capabilities.py", "caller_func": _caller_func,
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "error", "elapsed_s": _elapsed, "error": str(e)[:200],
        })
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": use_model, "instance_id": chosen,
                "caller_file": "ide_inspect_capabilities.py", "caller_func": _caller_func,
                "elapsed_s": _elapsed, "error": str(e)[:200],
            })
        except Exception:
            pass
        # Last-resort fallback to the shared helper (which now also logs)
        try:
            return await ollama_generate(prompt=prompt, system=system,
                                          model=model or OLLAMA_MODEL,
                                          instance_id=chosen,
                                          prefer_gpu=prefer_gpu) or ""
        except Exception:
            return ""
    finally:
        inst["in_use"] = max(0, inst.get("in_use", 1) - 1)

# Reuse helpers from the main IDE module
from Vera.Orchestration.ide_capabilities import (   # type: ignore
    PROJECT_ROOT,
    _record,
    _ide_get_session_id,
    IDE_AGENT_THINKER,
    IDE_AGENT_WRITER,
    IDE_AGENT_ANALYSER,
)

log = logging.getLogger("vera.ide.inspect")


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE TREE DISCOVERY (ground truth — resolved once at import time)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_source_root() -> Path:
    """Resolve the live Vera source directory. Prefers capability_orchestration's
    file location (the canonical module), with sensible fallbacks."""
    try:
        import Vera.Orchestration.capability_orchestration as _orch
        p = Path(_orch.__file__).resolve().parent
        if p.exists():
            return p
    except Exception as e:
        log.warning("inspect: could not resolve via capability_orchestration: %s", e)
    # Fallback: assume we're next to ide_capabilities.py
    try:
        from Vera.Orchestration import ide_capabilities as _ic
        p = Path(_ic.__file__).resolve().parent
        if p.exists():
            return p
    except Exception:
        pass
    # Last resort: the directory of this file
    return Path(__file__).resolve().parent


SOURCE_ROOT: Path = _resolve_source_root()
log.info("inspect: SOURCE_ROOT = %s", SOURCE_ROOT)


# Snapshots live under  PROJECT_ROOT/__vera_inspect__/  — never inside SOURCE_ROOT
SNAPSHOT_ROOT: Path = Path(PROJECT_ROOT) / "__vera_inspect__"
try:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
except Exception as e:
    log.warning("inspect: could not create SNAPSHOT_ROOT %s: %s", SNAPSHOT_ROOT, e)


# Files to ignore when copying a snapshot (caches, secrets, state)
_IGNORE_PATTERNS = {
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".vera_workspaces.json",        # per-instance workspace list
    ".vera_ide_whitelist.json",     # agent tool whitelist
    "*.pyc", "*.pyo", "*.sqlite3", "*.db",
}


def _ignore_fn(src_dir: str, names: List[str]) -> List[str]:
    ignored = []
    for n in names:
        if n in _IGNORE_PATTERNS:
            ignored.append(n); continue
        if n.startswith(".") and n not in (".env.example", ".gitignore", ".dockerignore"):
            ignored.append(n); continue
        for pat in _IGNORE_PATTERNS:
            if "*" in pat and Path(n).match(pat):
                ignored.append(n); break
    return ignored


# ─────────────────────────────────────────────────────────────────────────────
# PATH GUARD — refuse writes into the live source tree
# ─────────────────────────────────────────────────────────────────────────────
# Wraps selected write-capable capabilities so that if any caller — agent,
# scaffold, manual UI action — attempts to modify a file inside SOURCE_ROOT,
# the call is rejected. Promotion goes through its own explicit capability
# that bypasses the guard.

_PROTECTED = SOURCE_ROOT.resolve()


def _is_under_source(path_str: str) -> bool:
    try:
        p = Path(path_str).resolve()
        p.relative_to(_PROTECTED)
        return True
    except Exception:
        return False


# Capabilities whose write paths we intercept. When any of these are called
# with a `path` under SOURCE_ROOT the call is denied.
_GUARDED_WRITE_CAPS = [
    "ide.fs.write",
    "ide.fs.delete",
    "ide.code.edit_lines",
    "ide.code.insert_at",
    "ide.code.replace",
]

_INSTALLED_GUARDS = False


def _install_path_guards():
    """Wrap guarded capabilities with a source-protection check. Idempotent."""
    global _INSTALLED_GUARDS
    if _INSTALLED_GUARDS:
        return
    for cap_name in _GUARDED_WRITE_CAPS:
        entry = CAPABILITY_REGISTRY.get(cap_name)
        if not entry:
            continue
        original = entry["func"]

        async def _guarded(*, __orig=original, __cap=cap_name, **kwargs):
            path = kwargs.get("path", "")
            if path and _is_under_source(path):
                log.warning("inspect guard: BLOCKED %s path=%s", __cap, path)
                await emit_event({
                    "type": "ide.inspect.guard_block",
                    "capability": __cap, "path": path, "ts": now_iso(),
                })
                return {
                    "error": (
                        "Write denied: path is inside the live Vera source tree "
                        f"({_PROTECTED}). Source is read-only outside inspect mode. "
                        "Work on a snapshot under PROJECT_ROOT/__vera_inspect__/, "
                        "then use ide.inspect.promote_snapshot to publish changes."
                    ),
                    "ok": False,
                    "path": path,
                    "source_protected": True,
                }
            return await __orig(**kwargs)

        entry["func"] = _guarded
    _INSTALLED_GUARDS = True
    log.info("inspect: installed path guards on %s", _GUARDED_WRITE_CAPS)


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.source_info
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.inspect.source_info",
    http_method="GET", http_path="/ide/inspect/source_info",
    http_tags=["ide", "inspect"],
    memory="off", silent=True,
    description="Describe the live Vera source tree that inspect mode targets. "
                "Output: {source_root, snapshot_root, files: int, "
                "modules: [{name, lines, bytes}], capabilities_registered}.",
)
async def ide_inspect_source_info(trace_id=None):
    try:
        modules = []
        file_count = 0
        total_bytes = 0
        for p in sorted(SOURCE_ROOT.glob("*.py")):
            if p.name.startswith(".") or p.name.startswith("_"):
                continue
            try:
                st = p.stat()
                lines = sum(1 for _ in p.open("rb"))
                modules.append({
                    "name":  p.name,
                    "lines": lines,
                    "bytes": st.st_size,
                    "mtime": st.st_mtime,
                })
                total_bytes += st.st_size
                file_count += 1
            except Exception:
                pass
        # Also count HTML panel files
        panel_files = []
        for p in sorted(SOURCE_ROOT.glob("*.html")):
            try:
                panel_files.append({
                    "name":  p.name,
                    "bytes": p.stat().st_size,
                })
            except Exception:
                pass
        return {
            "source_root":            str(SOURCE_ROOT),
            "snapshot_root":          str(SNAPSHOT_ROOT),
            "py_file_count":          file_count,
            "py_total_bytes":         total_bytes,
            "modules":                modules,
            "panel_files":            panel_files,
            "capabilities_registered": len(CAPABILITY_REGISTRY),
            "protected":              True,
        }
    except Exception as e:
        return {"error": str(e), "source_root": str(SOURCE_ROOT)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.snapshot
# ─────────────────────────────────────────────────────────────────────────────

def _snapshot_id(label: str = "") -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    lbl = re.sub(r"[^a-zA-Z0-9_-]+", "-", (label or "").strip())[:40]
    return f"{stamp}-{lbl}" if lbl else stamp


@capability(
    "ide.inspect.snapshot",
    http_method="POST", http_path="/ide/inspect/snapshot",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Create a fresh snapshot copy of the live Vera source tree. "
                "Input: label (str, optional human-readable tag), "
                "session_id (str). "
                "Output: {snapshot_id, path, file_count, bytes, source_root, created_at}.",
)
async def ide_inspect_snapshot(
    label:      str = "",
    session_id: str = "",
    trace_id=None,
):
    try:
        SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        sid = _snapshot_id(label)
        dst = SNAPSHOT_ROOT / sid
        if dst.exists():
            # extremely unlikely (seconds resolution) but just in case
            sid = sid + "-" + uuid.uuid4().hex[:6]
            dst = SNAPSHOT_ROOT / sid

        # Copy tree using ignore patterns
        shutil.copytree(str(SOURCE_ROOT), str(dst), ignore=_ignore_fn)

        # Write metadata
        meta = {
            "snapshot_id":  sid,
            "source_root":  str(SOURCE_ROOT),
            "created_at":   now_iso(),
            "label":        label,
            "session_id":   session_id or _ide_get_session_id(),
            "source_hash":  _tree_hash(SOURCE_ROOT),
        }
        (dst / ".vera_inspect.json").write_text(json.dumps(meta, indent=2))

        # Count files + bytes
        file_count, total_bytes = 0, 0
        for p in dst.rglob("*"):
            if p.is_file():
                file_count += 1
                try: total_bytes += p.stat().st_size
                except Exception: pass

        sess = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sess, category="ide.inspect.snapshot",
            text=f"[inspect] snapshot {sid} — {file_count} files",
            full_text=(f"Snapshot: {sid}\nPath: {dst}\n"
                       f"Source: {SOURCE_ROOT}\nLabel: {label}\n"
                       f"Files: {file_count}  Bytes: {total_bytes}"),
            tags=["ide", "inspect", "snapshot"],
            importance=0.7, source_type="tool", record_type="event",
            capability_name="ide.inspect.snapshot",
            broadcast_type="ide.inspect.snapshot",
            fabric_dataset="ide.inspect_snapshots",
            metadata={"snapshot_id": sid, "path": str(dst),
                      "file_count": file_count, "bytes": total_bytes,
                      "label": label},
            fabric_data={"snapshot_id": sid, "path": str(dst),
                         "source_root": str(SOURCE_ROOT),
                         "label": label, "file_count": file_count,
                         "bytes": total_bytes, "created_at": now_iso()},
        ))

        return {
            "ok":           True,
            "snapshot_id":  sid,
            "path":         str(dst),
            "source_root":  str(SOURCE_ROOT),
            "file_count":   file_count,
            "bytes":        total_bytes,
            "label":        label,
            "created_at":   meta["created_at"],
        }
    except Exception as e:
        log.exception("ide.inspect.snapshot failed")
        return {"ok": False, "error": str(e)}


def _tree_hash(root: Path) -> str:
    """Cheap fingerprint of a source tree (mtime + size of .py/.html files)."""
    h = hashlib.sha1()
    try:
        for p in sorted(root.rglob("*")):
            if not p.is_file(): continue
            if p.suffix not in (".py", ".html"): continue
            if any(part in _IGNORE_PATTERNS for part in p.parts): continue
            try:
                st = p.stat()
                h.update(f"{p.relative_to(root)}:{st.st_size}:{int(st.st_mtime)}\n".encode())
            except Exception:
                pass
    except Exception:
        pass
    return h.hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.list_snapshots
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.inspect.list_snapshots",
    http_method="GET", http_path="/ide/inspect/snapshots",
    http_tags=["ide", "inspect"],
    memory="off", silent=True,
    description="List all source snapshots. "
                "Output: {snapshots: [{id, path, created_at, label, file_count, "
                "source_hash, is_fresh}], snapshot_root, current_source_hash}.",
)
async def ide_inspect_list_snapshots(trace_id=None):
    try:
        cur_hash = _tree_hash(SOURCE_ROOT)
        out = []
        if not SNAPSHOT_ROOT.exists():
            return {"snapshots": [], "snapshot_root": str(SNAPSHOT_ROOT),
                    "current_source_hash": cur_hash}
        for d in sorted(SNAPSHOT_ROOT.iterdir(), reverse=True):
            if not d.is_dir(): continue
            meta_f = d / ".vera_inspect.json"
            meta = {}
            if meta_f.exists():
                try: meta = json.loads(meta_f.read_text())
                except Exception: meta = {}
            file_count = sum(1 for p in d.rglob("*") if p.is_file())
            total_bytes = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
            out.append({
                "id":          d.name,
                "path":        str(d),
                "created_at":  meta.get("created_at", ""),
                "label":       meta.get("label", ""),
                "source_hash": meta.get("source_hash", ""),
                "file_count":  file_count,
                "bytes":       total_bytes,
                "is_fresh":    meta.get("source_hash") == cur_hash,
            })
        return {
            "snapshots":           out,
            "snapshot_root":       str(SNAPSHOT_ROOT),
            "current_source_hash": cur_hash,
            "count":               len(out),
        }
    except Exception as e:
        return {"error": str(e), "snapshots": []}


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.diff_snapshot
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.inspect.diff_snapshot",
    http_method="POST", http_path="/ide/inspect/diff",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Unified diff between a snapshot and the live source tree. "
                "Limits per-file diff size. "
                "Input: snapshot_id (str!), max_chars_per_file (int, default 20000). "
                "Output: {snapshot_id, modified: [path], added: [path], "
                "removed: [path], diffs: {path: unified_diff}}.",
)
async def ide_inspect_diff_snapshot(
    snapshot_id:        str,
    max_chars_per_file: int = 20000,
    trace_id=None,
):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists() or not snap.is_dir():
            return {"error": f"Snapshot not found: {snapshot_id}"}

        modified, added, removed, diffs = [], [], [], {}

        # Files currently in snapshot
        snap_files = {}
        for p in snap.rglob("*"):
            if not p.is_file(): continue
            if p.name == ".vera_inspect.json": continue
            if any(part in _IGNORE_PATTERNS for part in p.parts): continue
            try:
                rel = str(p.relative_to(snap))
                snap_files[rel] = p
            except Exception:
                pass

        # Files currently in source
        src_files = {}
        for p in SOURCE_ROOT.rglob("*"):
            if not p.is_file(): continue
            if any(part in _IGNORE_PATTERNS for part in p.parts): continue
            try:
                rel = str(p.relative_to(SOURCE_ROOT))
                src_files[rel] = p
            except Exception:
                pass

        # Added in snapshot (not in live source)
        for rel in sorted(set(snap_files) - set(src_files)):
            added.append(rel)
            sp = snap_files[rel]
            try:
                snap_text = sp.read_text(encoding="utf-8", errors="replace")
                udiff = "".join(difflib.unified_diff(
                    [], snap_text.splitlines(keepends=True),
                    fromfile=f"source/{rel} (missing)",
                    tofile=f"snapshot/{rel}",
                ))
                diffs[rel] = udiff[:max_chars_per_file]
            except Exception as e:
                diffs[rel] = f"[could not diff: {e}]"

        # Removed in snapshot (in live source but not snapshot)
        for rel in sorted(set(src_files) - set(snap_files)):
            removed.append(rel)
            sp = src_files[rel]
            try:
                src_text = sp.read_text(encoding="utf-8", errors="replace")
                udiff = "".join(difflib.unified_diff(
                    src_text.splitlines(keepends=True), [],
                    fromfile=f"source/{rel}",
                    tofile=f"snapshot/{rel} (missing)",
                ))
                diffs[rel] = udiff[:max_chars_per_file]
            except Exception as e:
                diffs[rel] = f"[could not diff: {e}]"

        # Modified
        for rel in sorted(set(snap_files) & set(src_files)):
            try:
                snap_text = snap_files[rel].read_text(encoding="utf-8", errors="replace")
                src_text  = src_files[rel].read_text(encoding="utf-8", errors="replace")
                if snap_text == src_text:
                    continue
                modified.append(rel)
                udiff = "".join(difflib.unified_diff(
                    src_text.splitlines(keepends=True),
                    snap_text.splitlines(keepends=True),
                    fromfile=f"source/{rel}",
                    tofile=f"snapshot/{rel}",
                ))
                diffs[rel] = udiff[:max_chars_per_file]
            except Exception as e:
                diffs[rel] = f"[could not diff: {e}]"

        return {
            "snapshot_id": snapshot_id,
            "modified":    modified,
            "added":       added,
            "removed":     removed,
            "diffs":       diffs,
            "total_changes": len(modified) + len(added) + len(removed),
        }
    except Exception as e:
        log.exception("diff_snapshot failed")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.delete_snapshot
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.inspect.delete_snapshot",
    http_method="POST", http_path="/ide/inspect/delete_snapshot",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Delete a snapshot directory. "
                "Input: snapshot_id (str!). "
                "Output: {ok, snapshot_id, deleted}.",
)
async def ide_inspect_delete_snapshot(snapshot_id: str, trace_id=None):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}
        # Extra safety: refuse if we'd delete anything outside SNAPSHOT_ROOT
        resolved = snap.resolve()
        try:
            resolved.relative_to(SNAPSHOT_ROOT.resolve())
        except ValueError:
            return {"ok": False, "error": "Refusing to delete outside snapshot_root"}
        shutil.rmtree(str(snap))
        await emit_event({"type": "ide.inspect.deleted",
                          "snapshot_id": snapshot_id, "ts": now_iso()})
        return {"ok": True, "snapshot_id": snapshot_id, "deleted": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.promote_snapshot — explicit write-back with confirm token
# ─────────────────────────────────────────────────────────────────────────────
# Generates a time-limited token. The agent can call promote with dry_run=True
# to get the token + the exact change plan; then the user (or the UI after
# showing a diff) invokes promote again WITH the token to actually copy files
# back. Token-bound, single-use, fifteen-minute TTL.

_PROMOTE_TOKENS: Dict[str, Dict[str, Any]] = {}


@capability(
    "ide.inspect.promote_snapshot",
    http_method="POST", http_path="/ide/inspect/promote",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Copy changed files from a snapshot back to the live source. "
                "TWO-STEP: (1) call with dry_run=True to get a plan + token, "
                "(2) call again with confirm_token to actually write. "
                "The token expires in 15 minutes and is single-use. "
                "Input: snapshot_id (str!), dry_run (bool), confirm_token (str), "
                "files (JSON list of rel paths — empty = all changed), "
                "session_id (str). "
                "Output: {ok, plan, token (on dry_run), written, skipped, errors}.",
)
async def ide_inspect_promote_snapshot(
    snapshot_id:    str,
    dry_run:        bool = True,
    confirm_token:  str  = "",
    files:          str  = "",
    session_id:     str  = "",
    trace_id=None,
):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists() or not snap.is_dir():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}

        # Resolve diff
        diff_result = await ide_inspect_diff_snapshot(snapshot_id=snapshot_id,
                                                      max_chars_per_file=1000)
        if diff_result.get("error"):
            return {"ok": False, "error": diff_result["error"]}

        all_changed = (diff_result.get("modified", []) +
                       diff_result.get("added", []) +
                       diff_result.get("removed", []))

        try:
            requested = json.loads(files) if files else []
        except Exception:
            requested = [s.strip() for s in (files or "").split(",") if s.strip()]

        targets = [f for f in all_changed if (not requested or f in requested)]
        if not targets:
            return {"ok": True, "plan": [], "written": [], "skipped": [], "errors": {},
                    "message": "No changes to promote."}

        plan = []
        for rel in targets:
            snap_p = snap / rel
            src_p  = SOURCE_ROOT / rel
            kind = ("added"    if not src_p.exists()  else
                    "removed"  if not snap_p.exists() else
                    "modified")
            plan.append({"path": rel, "kind": kind})

        # DRY RUN — mint a token and return
        if dry_run or not confirm_token:
            tok = uuid.uuid4().hex
            _PROMOTE_TOKENS[tok] = {
                "snapshot_id": snapshot_id,
                "targets":     targets,
                "expires_at":  time.time() + 900,   # 15 min
                "created_by":  session_id or _ide_get_session_id(),
            }
            await emit_event({
                "type": "ide.inspect.promote_prepared",
                "snapshot_id": snapshot_id,
                "targets":     len(targets),
                "ts":          now_iso(),
            })
            return {
                "ok":           True,
                "dry_run":      True,
                "snapshot_id":  snapshot_id,
                "plan":         plan,
                "token":        tok,
                "token_ttl_sec": 900,
                "message": ("Review plan, then call again with confirm_token "
                            "set to apply. Token expires in 15 minutes."),
            }

        # COMMIT — validate token
        info = _PROMOTE_TOKENS.pop(confirm_token, None)
        if not info:
            return {"ok": False, "error": "Invalid or expired confirm_token"}
        if info["snapshot_id"] != snapshot_id:
            return {"ok": False, "error": "Token does not match this snapshot"}
        if time.time() > info["expires_at"]:
            return {"ok": False, "error": "Token expired"}

        written, skipped, errors = [], [], {}
        for rel in info["targets"]:
            snap_p = snap / rel
            src_p  = SOURCE_ROOT / rel
            try:
                # Resolve and double-check containment
                src_resolved = src_p.resolve()
                src_resolved.relative_to(SOURCE_ROOT.resolve())
            except Exception:
                errors[rel] = "path escapes source root"
                continue
            try:
                if not snap_p.exists():
                    # Removed in snapshot → delete in source
                    if src_p.exists():
                        src_p.unlink()
                        written.append(rel + " (removed)")
                    else:
                        skipped.append(rel)
                else:
                    src_p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(snap_p), str(src_p))
                    written.append(rel)
            except Exception as e:
                errors[rel] = str(e)

        sess = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sess, category="ide.inspect.promote",
            text=f"[inspect] promote {snapshot_id} — {len(written)} files",
            full_text=(f"Snapshot: {snapshot_id}\n"
                       f"Written: {len(written)}\nErrors: {len(errors)}\n"
                       f"Files: {json.dumps(written)[:1500]}"),
            tags=["ide", "inspect", "promote"],
            importance=0.9, source_type="human", record_type="event",
            capability_name="ide.inspect.promote_snapshot",
            broadcast_type="ide.inspect.promoted",
            fabric_dataset="ide.inspect_promotions",
            metadata={"snapshot_id": snapshot_id, "written": len(written),
                      "errors": len(errors)},
            fabric_data={"snapshot_id": snapshot_id, "written": written,
                         "skipped": skipped, "errors": errors,
                         "at": now_iso()},
        ))
        return {"ok": True, "snapshot_id": snapshot_id,
                "written": written, "skipped": skipped, "errors": errors,
                "plan": plan}
    except Exception as e:
        log.exception("promote_snapshot failed")
        return {"ok": False, "error": str(e)}


# Clean expired promote tokens every few minutes
async def _sweep_promote_tokens():
    while True:
        try:
            now = time.time()
            expired = [k for k, v in _PROMOTE_TOKENS.items() if now > v["expires_at"]]
            for k in expired:
                _PROMOTE_TOKENS.pop(k, None)
        except Exception:
            pass
        await asyncio.sleep(300)


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.review_file — LLM-driven review of one snapshot file
# ─────────────────────────────────────────────────────────────────────────────

_REVIEW_SYSTEM = (
    "You are Vera's source-inspection reviewer — a senior Python / web engineer "
    "reviewing the Vera platform's own code. Vera is a distributed capability "
    "framework on FastAPI: every function is a @capability, there's a DAG engine, "
    "a polyglot data fabric (Postgres/Redis/Chroma/FAISS/Neo4j), a UI-panel "
    "registry, and an LLM cluster (Ollama). "
    "Produce concise, actionable review notes. Prefer surgical fixes over rewrites. "
    "Output STRICT JSON with this shape: "
    '{"summary": str, "strengths": [str], '
    '"issues": [{"severity":"low|medium|high","line":int|null,"title":str,"detail":str,"suggestion":str}], '
    '"opportunities": [{"title":str,"detail":str}]}'
)


@capability(
    "ide.inspect.review_file",
    http_method="POST", http_path="/ide/inspect/review_file",
    http_tags=["ide", "inspect"],
    memory="off",
    description="LLM code review of one snapshot file. "
                "Uses the 'analyser' agent by default. "
                "Input: snapshot_id (str!), path (str! — relative to snapshot), "
                "agent (thinker|writer|analyser, default analyser), "
                "session_id (str). "
                "Output: {ok, summary, strengths, issues, opportunities, raw}.",
)
async def ide_inspect_review_file(
    snapshot_id: str,
    path:        str,
    agent:       str = "analyser",
    session_id:  str = "",
    trace_id=None,
):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}
        fp = (snap / path).resolve()
        try:
            fp.relative_to(snap.resolve())
        except ValueError:
            return {"ok": False, "error": "path escapes snapshot"}
        if not fp.exists() or not fp.is_file():
            return {"ok": False, "error": f"File not found: {path}"}

        content = fp.read_text(encoding="utf-8", errors="replace")
        # For big files, numbered excerpt + structural summary.
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:>5}  {l}" for i, l in enumerate(lines[:1500]))
        if len(lines) > 1500:
            numbered += f"\n     … (truncated, {len(lines)} total lines)"

        user_prompt = (
            f"FILE: {path}\nLINES: {len(lines)}\n\n"
            "Review the following code and respond with the JSON schema in the "
            "system prompt. Cite line numbers (the left column) where relevant.\n\n"
            f"```\n{numbered}\n```"
        )

        iid = pick_instance(prefer_gpu=(agent == "thinker"))
        inst = OLLAMA_INSTANCES.get(iid or "", {})
        model = OLLAMA_MODEL
        full = (_REVIEW_SYSTEM + "\n\n" + user_prompt)

        raw = await _ollama_with_options(
            prompt=full, model=model, instance_id=iid or "",
            prefer_gpu=(agent == "thinker"),
            options={"temperature": 0.15, "num_ctx": 16384},
        )
        raw = (raw or "").strip()

        # Parse JSON — tolerant of prose prefix/fence
        parsed = None
        m = re.search(r"\{[\s\S]*\}\s*$", raw)
        if m:
            try: parsed = json.loads(m.group(0))
            except Exception:
                try: parsed = json.loads(re.sub(r"```(?:json)?|```", "", raw).strip())
                except Exception: parsed = None

        sess = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sess, category="ide.inspect.review",
            text=f"[review] {path} — {len((parsed or {}).get('issues', []))} issues",
            full_text=f"Snapshot: {snapshot_id}\nPath: {path}\n\n{raw[:5000]}",
            tags=["ide", "inspect", "review", agent],
            importance=0.75, source_type="ai", record_type="observation",
            capability_name="ide.inspect.review_file",
            broadcast_type="ide.inspect.review",
            fabric_dataset="ide.inspect_reviews",
            metadata={"snapshot_id": snapshot_id, "path": path, "agent": agent,
                      "issue_count": len((parsed or {}).get("issues", []))},
            fabric_data={"snapshot_id": snapshot_id, "path": path,
                         "agent": agent, "review": parsed or {"raw": raw[:5000]}},
        ))

        if parsed:
            return {"ok": True, "snapshot_id": snapshot_id, "path": path,
                    "agent": agent, **parsed, "raw": raw[:2000]}
        return {"ok": True, "snapshot_id": snapshot_id, "path": path,
                "agent": agent, "summary": raw[:500],
                "issues": [], "opportunities": [], "strengths": [],
                "raw": raw[:5000], "parse_failed": True}
    except Exception as e:
        log.exception("review_file failed")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.plan_improvement — cross-file change plan via the thinker
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ide.inspect.plan_improvement",
    http_method="POST", http_path="/ide/inspect/plan",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Ask the thinker agent to plan a cross-file improvement against "
                "a snapshot. Uses the snapshot's outline (functions/classes) as "
                "context. Input: snapshot_id (str!), goal (str!), "
                "files (JSON list — optional hint of files to focus on), "
                "session_id (str). "
                "Output: {ok, plan: {overview, steps: [{file, action, rationale}]}, "
                "raw}.",
)
async def ide_inspect_plan_improvement(
    snapshot_id: str,
    goal:        str,
    files:       str = "",
    session_id:  str = "",
    trace_id=None,
):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}

        # Collect an outline of the snapshot
        outline_lines = []
        for p in sorted(snap.rglob("*.py")):
            if any(part in _IGNORE_PATTERNS for part in p.parts): continue
            rel = str(p.relative_to(snap))
            outline_lines.append(f"=== {rel} ===")
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if re.match(r"^\s*(class |def |async def |@capability\()", line):
                        outline_lines.append(f"  {i:>4}: {line.strip()[:120]}")
            except Exception:
                pass
            if len(outline_lines) > 1200:
                outline_lines.append("  … (outline truncated)")
                break

        outline = "\n".join(outline_lines)[:40000]

        try:
            requested = json.loads(files) if files else []
        except Exception:
            requested = [s.strip() for s in (files or "").split(",") if s.strip()]
        file_hint = ("\n\nUser asked you to focus on: " + ", ".join(requested)) if requested else ""

        system = (
            "You are Vera's Thinker — a senior architect planning a change to the "
            "Vera codebase. You produce STRICT JSON plans only. "
            "Every @capability lives under Vera.Orchestration/*.py. UI panels are "
            "HTML files registered via register_ui(). Prefer surgical edits over "
            "rewrites. Never invent files. "
            'Schema: {"overview": str, "risk": "low|medium|high", '
            '"steps": [{"file": str, "action": "edit|add|remove", '
            '"rationale": str, "detail": str}]}'
        )
        user = (
            f"GOAL: {goal}{file_hint}\n\n"
            f"SOURCE OUTLINE:\n{outline}\n\n"
            "Plan the minimum set of edits to achieve the goal."
        )

        iid = pick_instance(prefer_gpu=True)
        raw = await _ollama_with_options(
            prompt=system + "\n\n" + user, model=OLLAMA_MODEL,
            instance_id=iid or "", prefer_gpu=True,
            options={"temperature": 0.25, "num_ctx": 32768},
        )

        parsed = None
        m = re.search(r"\{[\s\S]*\}\s*$", raw or "")
        if m:
            try: parsed = json.loads(m.group(0))
            except Exception: parsed = None

        sess = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sess, category="ide.inspect.plan",
            text=f"[plan] {goal[:80]}",
            full_text=f"Goal: {goal}\nSnapshot: {snapshot_id}\n\n{raw[:8000]}",
            tags=["ide", "inspect", "plan"],
            importance=0.8, source_type="ai", record_type="message",
            capability_name="ide.inspect.plan_improvement",
            broadcast_type="ide.inspect.plan",
            fabric_dataset="ide.inspect_plans",
            metadata={"snapshot_id": snapshot_id, "goal": goal[:200]},
            fabric_data={"snapshot_id": snapshot_id, "goal": goal,
                         "plan": parsed or {"raw": raw[:5000]}},
        ))
        if parsed:
            return {"ok": True, "snapshot_id": snapshot_id, "goal": goal,
                    "plan": parsed, "raw": (raw or "")[:2000]}
        return {"ok": True, "snapshot_id": snapshot_id, "goal": goal,
                "plan": {"overview": (raw or "")[:600], "steps": []},
                "raw": (raw or "")[:5000], "parse_failed": True}
    except Exception as e:
        log.exception("plan_improvement failed")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.scaffold_capability — generate a new @capability from a spec
# ─────────────────────────────────────────────────────────────────────────────

_CAP_EXEMPLAR = '''\
@capability(
    "group.verb_noun",
    http_method="POST", http_path="/group/verb_noun", http_tags=["group"],
    memory="off",
    description="One-line summary. Input: a (type!), b (type). Output: {x, y}.",
)
async def group_verb_noun(
    a: str,
    b: int = 0,
    session_id: str = "",
    trace_id=None,
):
    try:
        # ... implementation ...
        return {"ok": True, "x": ..., "y": ...}
    except Exception as e:
        return {"ok": False, "error": str(e)}
'''


@capability(
    "ide.inspect.scaffold_capability",
    http_method="POST", http_path="/ide/inspect/scaffold_capability",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Generate a new @capability function skeleton using the LLM and "
                "write it to a new file inside a snapshot. "
                "Input: snapshot_id (str!), cap_name (str! — e.g. 'math.fizzbuzz'), "
                "summary (str!), inputs_hint (str — natural-language hint), "
                "output_hint (str), module_file (str — filename in snapshot; "
                "if omitted, one is derived from cap_name's group prefix), "
                "session_id (str). "
                "Output: {ok, snapshot_id, cap_name, module_file, code, written}.",
)
async def ide_inspect_scaffold_capability(
    snapshot_id: str,
    cap_name:    str,
    summary:     str,
    inputs_hint: str = "",
    output_hint: str = "",
    module_file: str = "",
    session_id:  str = "",
    trace_id=None,
):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}

        # Validate cap_name
        if not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", cap_name):
            return {"ok": False,
                    "error": "cap_name must look like 'group.verb_noun' (lowercase)"}

        group = cap_name.split(".", 1)[0]
        func_name = cap_name.replace(".", "_")
        http_path = "/" + cap_name.replace(".", "/")

        # Pick a target file — either caller-specified or a new group module
        if module_file:
            target = snap / module_file
        else:
            target = snap / f"{group}_extra_capabilities.py"

        # Try to resolve inside snap
        try:
            target.resolve().relative_to(snap.resolve())
        except ValueError:
            return {"ok": False, "error": "module_file escapes snapshot"}

        # Prompt the LLM with a strong exemplar
        system = (
            "You are Vera's Writer agent generating a single Python file that "
            "registers a new @capability for the Vera framework. "
            "Rules:\n"
            "- The file must start with the standard Vera imports: "
            "  `from Vera.Orchestration.capability_orchestration import capability, emit_event, now_iso`\n"
            "- Use the EXACT @capability decorator pattern from the exemplar.\n"
            "- The function must be `async def`, take kwargs only (no positional args),\n"
            "  include a `session_id: str = \"\"` and `trace_id=None` parameter.\n"
            "- Return a dict with at minimum `{\"ok\": bool}`; use `{\"ok\": False, \"error\": str(e)}` on failure.\n"
            "- Produce ONLY valid Python source — no markdown, no commentary."
        )
        user = (
            f"Exemplar pattern:\n\n```python\n{_CAP_EXEMPLAR}\n```\n\n"
            f"Generate a NEW capability with:\n"
            f"- cap_name:   {cap_name}\n"
            f"- function:   {func_name}\n"
            f"- http_path:  {http_path}\n"
            f"- summary:    {summary}\n"
            f"- inputs:     {inputs_hint or '(decide from the summary)'}\n"
            f"- output:     {output_hint or '{ok, ...}'}\n\n"
            "Include a short, realistic implementation — do not leave it as a TODO."
        )

        iid = pick_instance(prefer_gpu=False)
        raw = await _ollama_with_options(
            prompt=system + "\n\n" + user, model=OLLAMA_MODEL,
            instance_id=iid or "", prefer_gpu=False,
            options={"temperature": 0.2, "num_ctx": 16384},
        )
        code = _strip_fences(raw or "")
        if "from Vera.Orchestration" not in code and "import" not in code.split("\n", 5)[0]:
            # Prepend minimal imports if model forgot
            code = (
                "from __future__ import annotations\n"
                "from Vera.Orchestration.capability_orchestration import capability\n\n"
                + code
            )

        # Write — either append to existing module or create new one
        written_mode = "created"
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="replace")
            if f'"{cap_name}"' in existing:
                return {"ok": False,
                        "error": f"Capability {cap_name} already exists in {target.name}"}
            # Append new function (trim leading imports if already present)
            code_body = _strip_duplicate_imports(code, existing)
            new_text = existing.rstrip() + "\n\n\n" + code_body.strip() + "\n"
            target.write_text(new_text, encoding="utf-8")
            written_mode = "appended"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")

        # Optionally register the file with the module loader hint
        loader_hint = (
            "(To load this module on next start, either rename it to match one of "
            "the files in capability_orchestration._module_files, or set the "
            f"VERA_MODULES env var to include '{target}'.)"
        )

        sess = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sess, category="ide.inspect.scaffold_cap",
            text=f"[scaffold_cap] {cap_name} → {target.name}",
            full_text=(f"Snapshot: {snapshot_id}\nCap: {cap_name}\n"
                       f"Target: {target}\nMode: {written_mode}\n\n"
                       f"{code[:3000]}"),
            tags=["ide", "inspect", "scaffold", "capability"],
            importance=0.75, source_type="ai", record_type="observation",
            capability_name="ide.inspect.scaffold_capability",
            broadcast_type="ide.inspect.scaffold",
            fabric_dataset="ide.inspect_scaffolds",
            metadata={"snapshot_id": snapshot_id, "cap_name": cap_name,
                      "module_file": target.name, "mode": written_mode},
            fabric_data={"snapshot_id": snapshot_id, "cap_name": cap_name,
                         "module_file": str(target), "code": code[:20000]},
        ))
        return {
            "ok":           True,
            "snapshot_id":  snapshot_id,
            "cap_name":     cap_name,
            "module_file":  str(target.relative_to(snap)),
            "code":         code,
            "written":      written_mode,
            "loader_hint":  loader_hint,
        }
    except Exception as e:
        log.exception("scaffold_capability failed")
        return {"ok": False, "error": str(e)}


def _strip_fences(txt: str) -> str:
    txt = txt.strip()
    if txt.startswith("```"):
        # remove leading fence line
        nl = txt.find("\n")
        if nl != -1:
            txt = txt[nl + 1:]
    if txt.endswith("```"):
        txt = txt[: -3].rstrip()
    return txt


def _strip_duplicate_imports(new_code: str, existing: str) -> str:
    """If new_code starts with import lines that already exist in `existing`, drop them."""
    lines = new_code.splitlines()
    out_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("from __future__"):
            out_start = i + 1; continue
        if s.startswith(("import ", "from ")):
            if s in existing:
                out_start = i + 1; continue
            # import is new — stop trimming
            break
        break
    return "\n".join(lines[out_start:])


# ─────────────────────────────────────────────────────────────────────────────
# ide.inspect.scaffold_ui_panel — generate a new HTML panel + register_ui()
# ─────────────────────────────────────────────────────────────────────────────

_PANEL_EXEMPLAR_HTML = '''\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Panel</title>
<style>
  :root { --bg0:#0d0f12; --bg1:#141821; --bg2:#1b2131;
          --text0:#e8eaed; --text1:#b8bcc4; --text2:#8b919c;
          --border:#2a303d; --accent:#6ea5ff; --ok:#22c55e; --err:#ef4444;
          --radius:6px; --mono:'JetBrains Mono', monospace; --sans:system-ui,sans-serif; }
  body { margin:0; background:var(--bg0); color:var(--text0); font-family:var(--sans); font-size:13px; }
  .bar  { padding:10px 14px; border-bottom:1px solid var(--border); display:flex; gap:8px; align-items:center; }
  .main { padding:14px; }
  button { background:var(--bg2); color:var(--text0); border:1px solid var(--border);
           border-radius:var(--radius); padding:6px 12px; cursor:pointer; font-size:12px; }
  button:hover { border-color:var(--accent); }
</style></head>
<body>
  <div class="bar"><strong>Panel Title</strong><span style="flex:1"></span>
    <button id="btn-refresh">Refresh</button>
  </div>
  <div class="main" id="main">Loading…</div>
<script>
const API = window.location.origin;
async function refresh() {
  const r = await fetch(API + '/your/capability/path');
  const j = await r.json();
  document.getElementById('main').textContent = JSON.stringify(j, null, 2);
}
document.getElementById('btn-refresh').onclick = refresh;
refresh();
</script></body></html>
'''


@capability(
    "ide.inspect.scaffold_ui_panel",
    http_method="POST", http_path="/ide/inspect/scaffold_ui_panel",
    http_tags=["ide", "inspect"],
    memory="off",
    description="Generate a new UI panel (HTML file + register_ui() call) "
                "inside a snapshot. The panel is registered via the same "
                "capability that serves the HTML. "
                "Input: snapshot_id (str!), panel_id (str! — e.g. 'fizzbuzz-ui'), "
                "label (str!), icon (str), description (str — what the panel "
                "should do), capabilities_used (JSON list of cap names), "
                "session_id (str). "
                "Output: {ok, html_file, module_file, register_call}.",
)
async def ide_inspect_scaffold_ui_panel(
    snapshot_id:       str,
    panel_id:          str,
    label:             str,
    icon:              str = "",
    description:       str = "",
    capabilities_used: str = "[]",
    session_id:        str = "",
    trace_id=None,
):
    try:
        snap = SNAPSHOT_ROOT / snapshot_id
        if not snap.exists():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}
        if not re.match(r"^[a-z][a-z0-9-_]*$", panel_id):
            return {"ok": False,
                    "error": "panel_id must be lowercase-hyphenated, e.g. 'my-panel'"}

        try:
            caps_used = json.loads(capabilities_used) if capabilities_used else []
        except Exception:
            caps_used = [s.strip() for s in (capabilities_used or "").split(",") if s.strip()]

        # Generate panel HTML via the LLM using the exemplar
        sys_prompt = (
            "You are generating a single self-contained HTML file for a Vera UI "
            "panel. It must include its own inline <style> and <script>. It must "
            "use the CSS variable set from the exemplar so it matches the rest "
            "of the Vera UI theme. Output ONLY the raw HTML — no markdown."
        )
        usr_prompt = (
            f"Exemplar (copy structure, adapt content):\n\n{_PANEL_EXEMPLAR_HTML}\n\n"
            f"Generate a new panel with:\n"
            f"- panel_id:    {panel_id}\n"
            f"- label:       {label}\n"
            f"- icon:        {icon}\n"
            f"- purpose:     {description or 'a helpful tool'}\n"
            f"- uses caps:   {', '.join(caps_used) if caps_used else '(pick appropriate capability endpoints)'}\n"
        )
        iid = pick_instance(prefer_gpu=False)
        raw = await _ollama_with_options(
            prompt=sys_prompt + "\n\n" + usr_prompt, model=OLLAMA_MODEL,
            instance_id=iid or "", prefer_gpu=False,
            options={"temperature": 0.3, "num_ctx": 16384},
        )
        html = _strip_fences(raw or "")
        if "<html" not in html.lower():
            html = _PANEL_EXEMPLAR_HTML.replace("Panel Title", label)

        # Write the HTML file + a tiny loader module
        html_file = snap / f"{panel_id}_panel.html"
        html_file.write_text(html, encoding="utf-8")

        module_file = snap / f"{panel_id.replace('-', '_')}_panel_capabilities.py"

        module_src = (
            "\"\"\"Auto-scaffolded UI panel for Vera.\"\"\"\n"
            "from pathlib import Path\n"
            "from fastapi.responses import HTMLResponse\n"
            "from Vera.Orchestration.capability_orchestration import capability, register_ui\n\n"
            f"_HTML_FILE = Path(__file__).parent / '{panel_id}_panel.html'\n\n"
            "@capability(\n"
            f"    '{panel_id}.panel.html',\n"
            f"    http_method='GET', http_path='/{panel_id}/panel',\n"
            f"    http_tags=['{panel_id}', 'ui'],\n"
            "    memory='off', silent=True,\n"
            f"    description='Serve the {label} panel HTML.',\n"
            ")\n"
            f"async def {panel_id.replace('-', '_')}_panel_html(trace_id=None):\n"
            "    try:\n"
            "        return HTMLResponse(_HTML_FILE.read_text(encoding='utf-8'))\n"
            "    except Exception as e:\n"
            "        return HTMLResponse('<h3>Panel missing: ' + str(e) + '</h3>', status_code=500)\n\n"
            "register_ui(\n"
            f"    '{panel_id}',\n"
            f"    {label!r},\n"
            f"    {icon!r},\n"
            "    \"\"\"<div style=\\\"height:100%;display:flex;flex-direction:column;\\\">\n"
            f"  <iframe src=\\\"/{panel_id}/panel\\\" style=\\\"flex:1;border:none;width:100%;height:100%;\\\"></iframe>\n"
            "</div>\"\"\",\n"
            "    \"\",\n"
            f"    ui_caps={caps_used!r},\n"
            "    mode='tab',\n"
            "    tab_order=90,\n"
            ")\n"
        )
        module_file.write_text(module_src, encoding="utf-8")

        sess = session_id or _ide_get_session_id()
        asyncio.ensure_future(_record(
            session_id=sess, category="ide.inspect.scaffold_panel",
            text=f"[scaffold_panel] {panel_id} ({label})",
            full_text=(f"Snapshot: {snapshot_id}\nPanel: {panel_id}\nLabel: {label}\n"
                       f"HTML: {html_file.name}\nModule: {module_file.name}"),
            tags=["ide", "inspect", "scaffold", "panel"],
            importance=0.75, source_type="ai", record_type="observation",
            capability_name="ide.inspect.scaffold_ui_panel",
            broadcast_type="ide.inspect.scaffold",
            fabric_dataset="ide.inspect_scaffolds",
            metadata={"snapshot_id": snapshot_id, "panel_id": panel_id,
                      "label": label},
            fabric_data={"snapshot_id": snapshot_id, "panel_id": panel_id,
                         "label": label, "html_file": str(html_file),
                         "module_file": str(module_file)},
        ))
        return {
            "ok":           True,
            "snapshot_id":  snapshot_id,
            "panel_id":     panel_id,
            "html_file":    str(html_file.relative_to(snap)),
            "module_file":  str(module_file.relative_to(snap)),
            "preview":      html[:1500],
        }
    except Exception as e:
        log.exception("scaffold_ui_panel failed")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# INSPECT PANEL — served at BOTH /ide/inspect/panel and /inspect/panel.
# HTML lives in ide_inspect_panel.html next to this file so it can be iterated
# without editing Python. An inline fallback is kept for disaster recovery.
# ─────────────────────────────────────────────────────────────────────────────

_INSPECT_PANEL_FILE = Path(__file__).parent / "ide_inspect_panel.html"

_INSPECT_PANEL_FALLBACK = (
    "<!DOCTYPE html><html><body style=\"background:#0d0f12;color:#e8eaed;"
    "font-family:system-ui;padding:32px;\">"
    "<h2 style=\"color:#ef4444;\">Source Inspection panel missing</h2>"
    "<p>Expected HTML file at:<br><code style=\"color:#00d4ff;\">" +
    str(_INSPECT_PANEL_FILE) + "</code></p>"
    "<p>Place <code>ide_inspect_panel.html</code> next to "
    "<code>ide_inspect_capabilities.py</code> and refresh.</p>"
    "</body></html>"
)


def _load_inspect_panel_html() -> str:
    """Load the inspect panel HTML from disk on every request so edits pick up
    without a server restart. Falls back to the stub if the file is missing."""
    try:
        return _INSPECT_PANEL_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("inspect panel HTML: %s — using fallback", e)
        return _INSPECT_PANEL_FALLBACK


@capability(
    "ide.inspect.panel_html",
    http_method="GET", http_path="/ide/inspect/panel",
    http_tags=["ide", "inspect", "ui"],
    memory="off", silent=True,
    description="Serve the Source Inspection panel HTML (text/html).",
)
async def ide_inspect_panel_html(trace_id=None):
    return HTMLResponse(_load_inspect_panel_html())


# Secondary route alias — users who mounted the panel at /inspect/panel get
# the same content. Registered directly on APP (not as a @capability, so it
# doesn't double-up in the MCP tool list).
try:
    from Vera.Orchestration.capability_orchestration import APP as _APP

    @_APP.get("/inspect/panel", include_in_schema=False)
    async def _inspect_panel_alias():
        return HTMLResponse(_load_inspect_panel_html())

    log.info("inspect: also serving panel at /inspect/panel")
except Exception as e:
    log.debug("inspect: could not register /inspect/panel alias: %s", e)


# NOTE: We intentionally do NOT call register_ui() here any more.
# The inspect panel is surfaced via a bottom-drawer in the IDE panel
# itself (see ide_panel.html), NOT as a right-side tab. If you want a
# standalone top-level harness tab as well, uncomment the block below.
#
register_ui(
    "ide-inspect-panel", "Source Inspection", "🔍",
    """<div style="height:100%;display:flex;flex-direction:column;">
      <iframe src="/inspect/panel"
              style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12);"
              allow="clipboard-read; clipboard-write"></iframe>
    </div>""",
    "", ui_caps=[...], mode="inject"
)


# ─────────────────────────────────────────────────────────────────────────────
# Install path guards on module load. Guards only wrap capabilities that
# already exist — since ide_capabilities and ide_code_capabilities load
# before this file, they'll be wrapped here. For any that don't exist at
# import time (e.g. code capabilities module not installed), we retry once
# after a short delay.
# ─────────────────────────────────────────────────────────────────────────────

async def _deferred_guard_install():
    await asyncio.sleep(2.0)
    _install_path_guards()
    # Sweep promote tokens in the background
    asyncio.ensure_future(_sweep_promote_tokens())


# Install immediately against whatever is already registered
_install_path_guards()

# Schedule a deferred retry + the token sweeper. Use the orchestrator's
# `schedule` helper if the event loop isn't running yet at import time.
try:
    from Vera.Orchestration.capability_orchestration import schedule as _schedule
    async def _once(): await _deferred_guard_install()
    _schedule(_once, interval=999999, name="ide_inspect_guard_install")
except Exception:
    try:
        asyncio.get_event_loop().create_task(_deferred_guard_install())
    except RuntimeError:
        pass  # loop not yet running; guards already applied above


log.info(
    "ide_inspect_capabilities loaded: source=%s  snapshots=%s",
    SOURCE_ROOT, SNAPSHOT_ROOT
)