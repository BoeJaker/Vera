"""operator_capabilities.py — the ``operator.*`` capability surface.

Registers the operator's primitives (session / observe / act / read), drivers
(think / step / run), missions (mission.list / mission.run + the ``docs.*``
aliases) and testing (``operator.test.run``), plus the Operator Studio panel.

Loaded by the orchestrator from ``_module_files`` (basename import), so this
entry module uses **absolute** ``Vera.vera.operator.*`` imports; the sibling
submodules it pulls in are imported through the package and resolve their own
relative imports normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, enum_schema, register_ui,
    schedule,
)

from Vera.vera.operator import browser_engine as _be
from Vera.vera.operator import perception as _perception
from Vera.vera.operator import actions as _actions
from Vera.vera.operator import safety as _safety
from Vera.vera.operator import thinker as _thinker
from Vera.vera.operator import operator_loop as _loop
from Vera.vera.operator import operator_budget as _op_budget
from Vera.vera.operator import targets as _targets
from Vera.vera.operator import capture as _capture
from Vera.vera.operator import tours as _tours
from Vera.vera.operator import operator_trace_core as _op_trace
from Vera.vera.operator import connectors as _connectors
from Vera.vera.operator.actions import ACTIONS
from Vera.vera.operator.missions import run_mission, list_missions
from Vera.vera.operator.docs import gallery as _gallery
from Vera.vera.operator.docs import directives as _directives

log = logging.getLogger("vera.operator")


# ABSOLUTE, like every other sibling import in this file. This module is loaded
# by the orchestrator's module loader as a TOP-LEVEL module with no parent
# package, so `from . import x` raises "attempted relative import with no known
# parent package" and the whole operator subsystem fails to register - which is
# exactly what shipped on 2026-08-31 and took operator.run out of prod.
from Vera.vera.operator import operator_progress as _progress   # noqa: E402
from Vera.vera.operator import sandbox_file_target as _sfile    # noqa: E402
from Vera.vera.operator import goal_file as _goal_file          # noqa: E402
# Dual-spelled: Vera.vera.* resolves to the MAIN checkout, which does not have
# a module until it lands there - so a NEW sibling must fall back to the plain
# package or every test in this file dies at import.
try:
    from Vera.vera.operator import session_target as _sess_target   # noqa: E402
except ImportError:                                                  # pragma: no cover
    from vera.operator import session_target as _sess_target        # noqa: E402
try:
    from Vera.vera.operator import nav_pin as _nav_pin              # noqa: E402
except ImportError:                                                  # pragma: no cover
    from vera.operator import nav_pin as _nav_pin                   # noqa: E402
try:
    from Vera.vera.operator import nav_fallback as _nav_fallback    # noqa: E402
except ImportError:                                                  # pragma: no cover
    from vera.operator import nav_fallback as _nav_fallback         # noqa: E402
try:
    from Vera.vera.operator import operator_run_projection as _run_projection  # noqa: E402
except ImportError:                                                  # pragma: no cover
    from vera.operator import operator_run_projection as _run_projection       # noqa: E402
try:
    from vera.discovery_operator_readmodel import DISCOVERY_OPERATOR_LEDGER       # noqa: E402
except ImportError:                                                  # pragma: no cover
    from Vera.vera.discovery_operator_readmodel import DISCOVERY_OPERATOR_LEDGER  # noqa: E402


def _orch_base_url() -> str:
    """This orchestrator's own base URL, for building sandbox preview links.

    Same derivation the loop already uses for _v5_sandbox_preview_url, kept
    here so the operator can resolve a path without importing the loop.
    """
    scheme = "http"
    port = os.environ.get("VERA_ORCH_PORT", "").strip()
    try:
        _cfg = getattr(_orch, "cfg", None)
        if _cfg is not None:
            if getattr(_cfg, "TLS_ENABLED", False):
                scheme = "https"
            if not port:
                port = str(getattr(_cfg, "ORCHESTRATOR_PORT", "") or "")
    except Exception:
        pass
    return f"{scheme}://localhost:{port or '8999'}"

_HERE = Path(__file__).resolve().parent
_PANEL_PATH = _HERE / "operator_studio_panel.html"


def _repo_root() -> Path:
    # …/Vera/vera/operator/operator_capabilities.py → parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _default_base_url() -> str:
    c = getattr(_orch, "cfg", None)
    scheme = "https" if getattr(c, "TLS_ENABLED", False) else "http"
    port = getattr(c, "ORCHESTRATOR_PORT", 8999)
    return f"{scheme}://localhost:{port}"


def _shots_dir(session_id: str) -> str:
    return str(_repo_root() / "artifacts" / "operator" / (session_id or "misc"))


def _safe_seg(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-") or "capture"


def _operator_contract(
        canonical_task: str, *, effects: List[str],
        approval: str = "not_required",
        trust: str = "untrusted_browser_content",
        secrets: str = "not_required",
        filesystem: str = "not_required",
        network: str = "not_required",
        tenant: str = "session_scoped",
        idempotency: str = "idempotent",
        cancellation: str = "not_required",
        pagination: str = "not_applicable",
        resources: Optional[List[str]] = None,
        owner: str = "vera.operator") -> Dict[str, Any]:
    """Return a complete Capability Contract v2 declaration for Operator.

    This metadata makes the existing Operator safety boundary visible to the
    resolver, policy shadow, and audit tooling. It does not replace session
    allowlists, dry-run, destructive-action confirmation, or Redis cancellation.
    """
    return {
        "canonical_task": canonical_task,
        "lifecycle": "active",
        "effects": list(effects),
        "output_schema": {"type": "object"},
        "approval": {"status": approval},
        "trust": {"status": trust},
        "secrets": {"status": secrets},
        "filesystem": {"status": filesystem},
        "network": {"status": network},
        "tenant": {"status": tenant},
        "idempotency": {"status": idempotency},
        "cancellation": {"status": cancellation},
        "pagination": {"status": pagination},
        "resources": {"status": "declared", "classes": resources or ["cpu"]},
        "owner": owner,
    }


# ── run history (O13) ────────────────────────────────────────────────────────
# The operator emitted its events and forgot them. A finished run could not be
# re-examined at all, which is why every census goal with a browser step could
# burn its whole 25-minute cap with nothing to show for it. The agentic loop has
# kept a replay log per session for a long time; this is the same idea, same
# shape, so the two can eventually be read by one surface.
_OP_EVENTS_KEY = "vera:operator:events:%s"
_OP_RUNS_KEY = "vera:operator:runs"
_OP_EVENTS_MAX = 2000          # a long browser run is ~hundreds of steps
_OP_EVENTS_TTL = 14 * 24 * 3600
_OP_RUNS_KEEP = 500


async def _op_record(run_id: str, ev: Dict[str, Any]) -> None:
    """Persist one operator event AND emit it. Never raises: recording a run must
    never be the thing that breaks the run."""
    try:
        await emit_event(ev)
    except Exception as e:                                   # pragma: no cover
        log.debug("operator emit failed: %s", e)
    try:
        _run_projection.observe(run_id, ev)
    except Exception as e:                                   # pragma: no cover
        # The shared Run view is observational. Projection failure must never
        # alter browser execution or its authoritative Redis cancellation path.
        log.debug("operator shared Run projection failed for %s: %s", run_id, e)
    r = getattr(_orch, "REDIS", None)
    if r is None or not run_id:
        return
    try:
        rec = dict(ev)
        rec.setdefault("ts", _now_iso())
        k = _OP_EVENTS_KEY % run_id
        await r.rpush(k, json.dumps(rec, default=str))
        await r.ltrim(k, -_OP_EVENTS_MAX, -1)
        await r.expire(k, _OP_EVENTS_TTL)
        # Index by start time so `operator.runs` is ordered without reading every
        # event list, and trimmed so it cannot grow without bound.
        await r.zadd(_OP_RUNS_KEY, {run_id: time.time()})
        await r.zremrangebyrank(_OP_RUNS_KEY, 0, -(_OP_RUNS_KEEP + 1))
    except Exception as e:                                   # pragma: no cover
        log.debug("operator record failed for %s: %s", run_id, e)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Cooperative cancel. Mirrors the agentic loop's flag-first design: the flag is
# authoritative and outlives any one process, so a run started elsewhere (or
# orphaned by a restart) can still be told to stop.
_OP_CANCEL_KEY = "vera:operator:cancel:%s"
_OP_CANCEL_TTL = 6 * 3600


async def _op_set_cancel(run_id: str) -> bool:
    r = getattr(_orch, "REDIS", None)
    if r is None or not run_id:
        return False
    try:
        await r.set(_OP_CANCEL_KEY % run_id, "1", ex=_OP_CANCEL_TTL)
        return True
    except Exception as e:
        log.debug("operator cancel flag write failed for %s: %s", run_id, e)
        return False


async def _op_is_cancelled(run_id: str) -> bool:
    """Fail-OPEN: if we cannot read the flag, the run continues. A Redis blip
    must not silently kill healthy browser runs."""
    r = getattr(_orch, "REDIS", None)
    if r is None or not run_id:
        return False
    try:
        return bool(await r.get(_OP_CANCEL_KEY % run_id))
    except Exception:
        return False


async def _op_clear_cancel(run_id: str) -> None:
    r = getattr(_orch, "REDIS", None)
    if r is None or not run_id:
        return
    try:
        await r.delete(_OP_CANCEL_KEY % run_id)
    except Exception:
        pass


async def _op_events(run_id: str) -> List[Dict[str, Any]]:
    """The persisted event list for one run, or [] once it has aged out."""
    r = getattr(_orch, "REDIS", None)
    if r is None or not run_id:
        return []
    try:
        raw = await r.lrange(_OP_EVENTS_KEY % run_id, 0, _OP_EVENTS_MAX - 1)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for x in raw or []:
        try:
            d = json.loads(x.decode() if isinstance(x, (bytes, bytearray)) else x)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _gif_out_path(domain: str, name: str) -> Dict[str, str]:
    """Resolve where a GIF should be written. With a ``domain`` it lands in the
    committed docs assets (referenced from markdown); otherwise in artifacts
    (served live via /operator/artifact). Returns {path, rel, url}."""
    name = _safe_seg(name)
    if domain:
        rel = f"assets/{_safe_seg(domain)}/{name}.gif"
        path = _repo_root() / "documentation" / rel
        return {"path": str(path), "rel": rel, "url": ""}
    path = _repo_root() / "artifacts" / "operator" / "captures" / f"{name}.gif"
    return {"path": str(path), "rel": "",
            "url": f"/operator/artifact?path=captures/{name}.gif"}


def _artifact_rel(abspath: str) -> str:
    """Path under artifacts/operator suitable for the /operator/artifact route."""
    try:
        root = _repo_root() / "artifacts" / "operator"
        return str(Path(abspath).resolve().relative_to(root)).replace("\\", "/")
    except Exception:
        return ""


async def _call(name: str, **kw) -> Any:
    """In-process capability dispatch (used by think/mission plumbing)."""
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


def _target_caller(base_url: str):
    """Return an async (name, args)->result that calls a cap on the TARGET Vera
    (sandbox or live) over its /mcp/call — for seeding a tour's data."""
    async def _call_t(name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60, verify=False) as c:
                r = await c.post(base_url.rstrip("/") + "/mcp/call",
                                 json={"name": name, "arguments": args or {}})
            r.raise_for_status()
            d = r.json()
            return d.get("result", d.get("content", d)) if isinstance(d, dict) else d
        except Exception as e:
            return {"error": str(e)}
    return _call_t


def _policy_for_session(s, *, allowlist: Optional[List[str]] = None,
                        dry_run: Optional[bool] = None,
                        allow_destructive: Optional[bool] = None,
                        confirm: Optional[bool] = None) -> "_safety.SafetyPolicy":
    """Build the effective SafetyPolicy for a session: the policy stored at
    connect (allowlist / allow_destructive / dry_run) UNIONed with any per-call
    overrides. This is why a host allowlisted once at connect stays permitted for
    every later act and run on that session."""
    sp = getattr(s, "policy", {}) or {}
    kind = (getattr(s, "target", {}) or {}).get("kind", "url")
    merged = list(sp.get("allowlist", []) or [])
    for h in (allowlist or []):
        h = (h or "").strip()
        if h and h not in merged:
            merged.append(h)
    return _safety.SafetyPolicy.for_target(
        kind, getattr(s, "base_url", ""), allowlist=merged,
        dry_run=(dry_run if dry_run is not None else sp.get("dry_run")),
        allow_destructive=(allow_destructive if allow_destructive is not None
                           else sp.get("allow_destructive")),
        confirm=(confirm if confirm is not None else None))


async def _open_session(url: str = "", kind: str = "", base_url: str = "",
                        session_id: str = "", width: int = 1440, height: int = 900,
                        branch: str = "", panel_id: str = "", cs_id: str = "",
                        source: str = "", ref: str = "",
                        allowlist: Optional[List[str]] = None,
                        allow_destructive: Optional[bool] = None,
                        dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """Resolve a target, boot a browser session, navigate to its start page.
    Returns {ok, session_id, resolved, summary} or {error}."""
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    if source:
        # A registered connectable (integration/ollama/node/docker/proxmox).
        target: Dict[str, Any] = {"source": source, "ref": ref}
    else:
        target = {"kind": kind or ("url" if url else "live")}
        if url:
            target["url"] = url
        if base_url:
            target["base_url"] = base_url
        if branch:
            target["branch"] = branch
        if panel_id:
            target["panel_id"] = panel_id
        if cs_id:
            target["id"] = cs_id
    resolved = await _targets.ensure_target(target, _call, _default_base_url())
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready"), "resolved": resolved}
    # A caller who NAMED a url meant to land on it. If resolution produced no
    # start_url, the session would open on about:blank and the operator would be
    # asked to pursue its goal on an empty page - which it does, by wandering
    # (2026-08-27: it tried two public timer sites, was allowlist-blocked, then
    # drove Vera's own UI). Fail loudly here instead: one clear error beats
    # fifteen cycles of plausible-looking nonsense. Deliberately NOT raised when
    # no url was supplied - "start_url may be empty (caller navigates)" is a
    # legitimate contract for open-then-act flows.
    if url and not str(resolved.get("start_url") or "").strip():
        return {"error": ("target resolved with no start_url, so the browser would open "
                          "on a blank page: url=%r was dropped by kind=%r. Pass the url "
                          "WITHOUT kind, or pass kind with the base_url/id it needs."
                          % (url, kind or "")),
                "resolved": resolved}
    try:
        sess = await _be.start_session(
            session_id=session_id, base_url=resolved["base_url"],
            viewport={"width": int(width), "height": int(height)}, target=resolved)
    except Exception as e:
        return {"error": f"session start failed: {e}"}
    # Persist the operating policy on the session so every subsequent act/run
    # honours it (allowlist a site ONCE at connect, not on every action).
    pol: Dict[str, Any] = dict(sess.policy or {})
    if allowlist is not None:
        pol["allowlist"] = [h.strip() for h in allowlist if h and h.strip()]
    if allow_destructive is not None:
        pol["allow_destructive"] = bool(allow_destructive)
    if dry_run is not None:
        pol["dry_run"] = bool(dry_run)
    sess.policy = pol
    if resolved.get("start_url"):
        try:
            await sess.page.goto(resolved["start_url"], wait_until="domcontentloaded",
                                 timeout=30000)
            try:
                await sess.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            sess.meta["last_url"] = sess.page.url
        except Exception as e:
            # Was previously a silently-swallowed log.warning — the session
            # would then enter run_loop() sitting on about:blank with NO
            # indication anything went wrong, and the model's own first
            # "observe" sees a genuinely blank page with no error attached.
            # Confirmed live (§5.22, Test U): the model then GUESSES its
            # own goto target (a bare relative filename) instead of retrying
            # the URL it was actually given, and that guess trips the
            # allowlist check — a confusing failure mode with no trace back
            # to the real cause. If the caller's own start page can't be
            # reached, that is itself the actionable problem; own the
            # session and close it rather than leak a half-started one.
            log.warning("operator: initial navigation to %s failed: %s",
                       resolved["start_url"], e)
            try:
                await _be.close_session(sess.session_id)
            except Exception:
                pass
            return {"error": f"could not load the start page ({resolved['start_url']}): {e}"}
    return {"ok": True, "session_id": sess.session_id, "resolved": resolved,
            "summary": sess.summary()}


# ─────────────────────────────────────────────────────────────────────────────
#  DISCOVERY EVIDENCE — bounded payload-free operator read model
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.discovery.evidence", memory="off", silent=True,
            http_method="GET", http_path="/operator/discovery/evidence",
            http_tags=["operator"],
            contract=_operator_contract(
                "discovery.evidence.inspect", effects=["read"],
                trust="bounded_payload_free_evidence", tenant="global_aggregate",
                pagination="bounded_limit", resources=["cpu", "memory"]),
            description="Read bounded payload-free discovery route and context benchmark evidence. "
                        "Inputs: limit (1..50). Output has no query/result payload and no control authority.")
async def cap_operator_discovery_evidence(limit: int = 20, trace_id=None) -> Dict:
    return {"ok": True, **DISCOVERY_OPERATOR_LEDGER.snapshot(int(limit))}


@capability("operator.discovery.benchmark.record", memory="off",
            http_method="POST", http_path="/operator/discovery/benchmark/record",
            http_tags=["operator"],
            contract=_operator_contract(
                "discovery.benchmark.record", effects=["write"],
                trust="validated_benchmark_evidence", tenant="global_aggregate",
                resources=["cpu", "memory"]),
            description="Record an already-computed offline context benchmark comparison in the "
                        "bounded operator read model. Does not run providers, models, or sources. "
                        "Inputs: comparison (object from compare_context_benchmark).")
async def cap_operator_discovery_benchmark_record(
        comparison: Dict = None, trace_id=None) -> Dict:
    value = DISCOVERY_OPERATOR_LEDGER.record_benchmark(comparison or {})
    return {"ok": True, "benchmark": value}


# ─────────────────────────────────────────────────────────────────────────────
#  SESSION
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.session.start", memory="on",
            http_method="POST", http_path="/operator/session/start", http_tags=["operator"],
            contract=_operator_contract(
                "browser.session.start", effects=["execute", "network"],
                approval="session_policy", network="target_selected_by_caller",
                secrets="browser_session_may_contain_sensitive_content",
                idempotency="non_idempotent", cancellation="bounded_timeout",
                resources=["cpu", "network", "browser"]),
            description="Open a browser session on a target and navigate to it. "
                        "Inputs: url (any web page) OR kind (url|live|sandbox|panel|"
                        "codeserver|vm) + base_url + panel_id/id, session_id (reuse), "
                        "width, height, branch (sandbox), and the session's operating "
                        "policy: allowlist (external hosts you permit acting on), "
                        "allow_destructive (permit state-changing acts), dry_run. The "
                        "policy is stored on the session so every later act/run honours "
                        "it. Output: {session_id, resolved, summary}.",
            schema=enum_schema(kind=["url", "live", "sandbox", "panel", "codeserver", "vm"]))
async def cap_session_start(url: str = "", kind: str = "", base_url: str = "",
                            session_id: str = "", width: int = 1440, height: int = 900,
                            branch: str = "", panel_id: str = "", id: str = "",
                            source: str = "", ref: str = "",
                            allowlist: Optional[List[str]] = None,
                            allow_destructive: Optional[bool] = None,
                            dry_run: Optional[bool] = None,
                            trace_id=None) -> Dict[str, Any]:
    res = await _open_session(url=url, kind=kind, base_url=base_url,
                              session_id=session_id, width=width, height=height,
                              branch=branch, panel_id=panel_id, cs_id=id,
                              source=source, ref=ref,
                              allowlist=allowlist, allow_destructive=allow_destructive,
                              dry_run=dry_run)
    if res.get("ok"):
        await emit_event({"type": "operator.session", "stage": "start",
                          "session_id": res["session_id"],
                          "base_url": res["resolved"].get("base_url")})
    return res


@capability("operator.session.status", memory="off", silent=True,
            http_method="GET", http_path="/operator/session/status", http_tags=["operator"],
            contract=_operator_contract(
                "browser.session.inspect", effects=["read"],
                trust="session_metadata_only", pagination="bounded_in_memory"),
            description="Status of one session (session_id) or all sessions. "
                        "Output: {session_id,url,refs,steps,alive,...} or {sessions:[...]}.")
async def cap_session_status(session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if session_id:
        s = _be.get_session(session_id)
        return s.summary() if s else {"error": f"no such session: {session_id}"}
    return {"sessions": _be.list_sessions(), "count": len(_be.list_sessions())}


@capability("operator.session.close", memory="on",
            http_method="POST", http_path="/operator/session/close", http_tags=["operator"],
            contract=_operator_contract(
                "browser.session.close", effects=["execute"],
                trust="session_identifier", idempotency="idempotent",
                resources=["cpu", "browser"]),
            description="Close a browser session and free its page/context. "
                        "Input: session_id (str!). Output: {ok}.")
async def cap_session_close(session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not session_id:
        return {"error": "session_id required"}
    ok = await _be.close_session(session_id)
    return {"ok": ok}


# ─────────────────────────────────────────────────────────────────────────────
#  CONNECTIONS  —  drive anything registered across Vera's infrastructure
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.connect.list", memory="off", silent=True,
            http_method="GET", http_path="/operator/connect/list", http_tags=["operator"],
            contract=_operator_contract(
                "browser.connection.list", effects=["read"],
                trust="registry_metadata_only", tenant="request_scoped",
                pagination="bounded_registry", resources=["cpu"]),
            description="List everything the operator can connect to across Vera's "
                        "registries — Integrations Hub apps, Ollama instances, "
                        "worker/nodes, Docker containers (published ports), Proxmox "
                        "VM consoles — by calling each subsystem's existing list cap. "
                        "Inputs: sources (csv subset of integration,ollama,node,docker,"
                        "proxmox; blank=all). Output: {connectables:[{source,ref,label,"
                        "url,type(web|api|ssh|vnc),driveable,group,detail}], count, groups}.")
async def cap_connect_list(sources: str = "", trace_id=None) -> Dict[str, Any]:
    srcs = [s.strip() for s in sources.split(",") if s.strip()] or None
    return await _connectors.list_connectables(_call, sources=srcs)


@capability("operator.connect", memory="on",
            http_method="POST", http_path="/operator/connect", http_tags=["operator"],
            contract=_operator_contract(
                "browser.connection.open",
                effects=["execute", "network", "model", "external_side_effect"],
                approval="session_policy", network="registered_target_only",
                secrets="browser_session_may_contain_sensitive_content",
                idempotency="non_idempotent", cancellation="cooperative_between_steps",
                filesystem="conditional_screenshot_artifacts",
                resources=["cpu", "network", "browser", "model"]),
            description="Open a browser session on a REGISTERED connectable (from "
                        "operator.connect.list) and, optionally, drive it. Inputs: "
                        "source (integration|ollama|node|docker|proxmox), ref (str! — "
                        "the connectable's ref), session_id (reuse), goal (str — if "
                        "given, runs the observe→think→act loop toward it), provider, "
                        "max_steps. Output: session summary, or the run result when a "
                        "goal is given. Web UIs drive fully; API/SSH endpoints open for "
                        "reference (control them via their own caps).")
async def cap_connect(source: str = "", ref: str = "", goal: str = "",
                      session_id: str = "", provider: str = "ollama",
                      max_steps: int = 15, keep_open: bool = True,
                      trace_id=None) -> Dict[str, Any]:
    if not source or not ref:
        return {"error": "source and ref are required (see operator.connect.list)"}
    start = await _open_session(source=source, ref=ref, session_id=session_id)
    if start.get("error"):
        return start
    sid = start["session_id"]
    s = _be.get_session(sid)
    await emit_event({"type": "operator.session", "stage": "connect",
                      "session_id": sid, "source": source, "ref": ref,
                      "driveable": (s.target or {}).get("driveable", True) if s else True})
    if not goal:
        return start
    resolved = (s.target or {}) if s else {}
    policy = _policy_for_session(s)
    run_id = uuid.uuid4().hex[:10]

    async def _on_step(rec: Dict[str, Any]):
        shot = rec.get("screenshot", "")
        await _op_record(run_id, {
            "type": "operator.step", "run_id": run_id, "i": rec.get("i"),
            "phase": rec.get("phase"), "action": rec.get("action"),
            "thought": rec.get("thought", "")[:200], "reason": rec.get("reason", ""),
            "error": rec.get("error", ""),
            "screenshot": f"/operator/artifact?path={_artifact_rel(shot)}" if shot else ""})

    await _op_record(run_id, {"type": "operator.run", "stage": "start", "run_id": run_id,
                              "goal": goal[:200], "target": resolved.get("kind"),
                              "source": source, "ref": ref, "session_id": sid})
    await _op_clear_cancel(run_id)
    # finally, not the happy path: run_loop raises on error, on its wall cap and
    # on CANCELLATION (that is what should_cancel drives). Closing after it
    # meant every such run left its browser context+page resident in the shared
    # Chromium until the process exited.
    try:
        result = await _loop.run_loop(goal, s, call_cap=_call, policy=policy, provider=provider,
                                      max_steps=int(max_steps), canvas=resolved.get("canvas", False),
                                      shots_dir=_shots_dir(sid) + f"/run-{run_id}", on_step=_on_step,
                                      should_cancel=lambda: _op_is_cancelled(run_id))
    finally:
        if not keep_open:
            await _be.close_session(sid)
    result.update({"run_id": run_id, "source": source, "ref": ref,
                   "session_id": sid if keep_open else ""})
    await _op_record(run_id, {"type": "operator.run", "stage": "done", "run_id": run_id,
                              "reason": result.get("reason"), "steps": result.get("step_count")})
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  OBSERVE / READ
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.observe", memory="off", silent=True,
            http_method="POST", http_path="/operator/observe", http_tags=["operator"],
            contract=_operator_contract(
                "browser.page.observe", effects=["read", "filesystem"],
                secrets="browser_session_may_contain_sensitive_content",
                filesystem="conditional_screenshot_artifact",
                idempotency="snapshot_at_call_time", resources=["cpu", "browser"]),
            description="Hybrid observation of the session's current page: a "
                        "screenshot PLUS interactive elements with stable refs "
                        "(e1,e2,…) + visible text. Inputs: session_id (str!), "
                        "max_elements (120), full_page (bool), save_screenshot (bool). "
                        "Output: {url,title,elements:[{ref,role,name,bbox}],text,"
                        "screenshot_url}.")
async def cap_observe(session_id: str = "", max_elements: int = 120,
                      full_page: bool = False, save_screenshot: bool = True,
                      trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session (operator.session.start first)"}
    shot = ""
    if save_screenshot:
        d = _shots_dir(session_id)
        os.makedirs(d, exist_ok=True)
        shot = os.path.join(d, f"obs-{int(time.time()*1000)}.png")
    obs = await _perception.observe_page(s.page, screenshot_path=shot,
                                         max_elements=int(max_elements),
                                         full_page=bool(full_page))
    s.ref_map = obs.ref_map()
    s.meta["last_url"] = obs.url
    out = obs.to_dict()
    out["screenshot_url"] = f"/operator/artifact?path={_artifact_rel(shot)}" if shot else ""
    return out


@capability("operator.read", memory="off", silent=True,
            http_method="POST", http_path="/operator/read", http_tags=["operator"],
            contract=_operator_contract(
                "browser.page.read", effects=["read"],
                secrets="browser_session_may_contain_sensitive_content",
                idempotency="snapshot_at_call_time", resources=["cpu", "browser"]),
            description="Read text from the current page (whole body, or a CSS "
                        "selector). Inputs: session_id (str!), selector (str). "
                        "Output: {text, chars}.")
async def cap_read(session_id: str = "", selector: str = "", trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session"}
    try:
        if selector:
            txt = await s.page.locator(selector).first.inner_text(timeout=6000)
        else:
            txt = await s.page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception as e:
        return {"error": str(e)}
    txt = txt or ""
    return {"ok": True, "text": txt[:8000], "chars": len(txt)}


@capability("operator.screenshot", memory="off", silent=True,
            http_method="POST", http_path="/operator/screenshot", http_tags=["operator"],
            contract=_operator_contract(
                "browser.page.capture", effects=["read", "filesystem"],
                secrets="browser_session_may_contain_sensitive_content",
                filesystem="writes_operator_artifact",
                idempotency="non_idempotent", resources=["cpu", "browser"]),
            description="Capture a screenshot of the session's current page. "
                        "Inputs: session_id (str!), full_page (bool). "
                        "Output: {screenshot, screenshot_url}.")
async def cap_screenshot(session_id: str = "", full_page: bool = False,
                         trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session"}
    d = _shots_dir(session_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"shot-{int(time.time()*1000)}.png")
    try:
        await s.page.screenshot(path=path, full_page=bool(full_page), type="png")
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "screenshot": path,
            "screenshot_url": f"/operator/artifact?path={_artifact_rel(path)}"}


# ─────────────────────────────────────────────────────────────────────────────
#  ACT
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.act", memory="on",
            http_method="POST", http_path="/operator/act", http_tags=["operator"],
            contract=_operator_contract(
                "browser.action.perform",
                effects=["execute", "network", "filesystem", "external_side_effect"],
                approval="session_policy_and_destructive_confirmation",
                secrets="browser_session_may_contain_sensitive_content",
                network="session_allowlist", filesystem="conditional_screenshot_artifact",
                idempotency="non_idempotent", cancellation="action_timeout",
                resources=["cpu", "network", "browser"]),
            description="Perform one action on the session's page. Inputs: "
                        "session_id (str!), action (click|type|press|scroll|goto|"
                        "select|hover|wait|nav|screenshot), and per-action args: "
                        "ref (element ref from observe) | x,y (pixels); text, key, "
                        "url, value, label, direction, dx, dy, ms, selector, clear, "
                        "submit; confirm/allow_destructive for the safety gate. "
                        "Output: {ok, action, ...} or {blocked, reason} / {dry_run}.",
            schema=enum_schema(action=list(ACTIONS.keys())))
async def cap_act(session_id: str = "", action: str = "", ref: str = "",
                  x: Optional[float] = None, y: Optional[float] = None,
                  text: str = "", key: str = "", url: str = "", value: str = "",
                  label: str = "", direction: str = "", dx: int = 0, dy: int = 400,
                  ms: int = 1000, selector: str = "", clear: bool = True,
                  submit: bool = False, confirm: bool = False,
                  allow_destructive: Optional[bool] = None,
                  allowlist: Optional[List[str]] = None, trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s:
        return {"error": f"no such session: {session_id}"}
    if not action:
        return {"error": "action required"}
    args: Dict[str, Any] = {}
    if ref:
        args["ref"] = ref
    if x is not None:
        args["x"] = x
    if y is not None:
        args["y"] = y
    if text != "":
        args["text"] = text
    if key:
        args["key"] = key
    if url:
        args["url"] = url
    if value != "":
        args["value"] = value
    if label != "":
        args["label"] = label
    if direction:
        args["direction"] = direction
    if selector:
        args["selector"] = selector
    if action == "scroll":
        args["dx"], args["dy"] = int(dx), int(dy)
    if action == "wait":
        args["ms"] = int(ms)
    if action == "type":
        args["clear"], args["submit"] = bool(clear), bool(submit)

    policy = _policy_for_session(s, allowlist=allowlist, confirm=(confirm or None),
                                 allow_destructive=allow_destructive)
    gate = _safety.evaluate(policy, s.meta.get("last_url", s.base_url), action, args)
    if not gate["allowed"]:
        return {"blocked": True, "reason": gate["reason"], "action": action}
    if gate["dry_run"]:
        return {"ok": True, "dry_run": True, "note": gate["reason"],
                "action": action, "args": args}
    res = await _actions.perform(s, action, args)
    await emit_event({"type": "operator.act", "session_id": session_id,
                      "action": action, "ok": bool(res.get("ok")),
                      "error": res.get("error", "")})
    return res


# ─────────────────────────────────────────────────────────────────────────────
#  THINK / STEP / RUN
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.think", memory="off",
            http_method="POST", http_path="/operator/think", http_tags=["operator"],
            contract=_operator_contract(
                "browser.action.propose", effects=["read", "model"],
                secrets="browser_session_may_contain_sensitive_content",
                network="internal_model_or_configured_provider",
                idempotency="non_idempotent", cancellation="provider_timeout",
                resources=["cpu", "browser", "model"]),
            description="Observe once and let the LLM pick the next action WITHOUT "
                        "performing it. Inputs: session_id (str!), goal (str!), "
                        "provider (ollama|anthropic:model|openai:model|<id>), model. "
                        "Output: {observation, decision:{thought,action,args,done}}.")
async def cap_think(session_id: str = "", goal: str = "", provider: str = "ollama",
                    model: str = "", trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session"}
    if not goal:
        return {"error": "goal required"}
    obs = await _perception.observe_page(s.page)
    s.ref_map = obs.ref_map()
    decision = await _thinker.decide(goal, obs, s.history, _call, provider=provider,
                                     model=model, canvas=(s.target or {}).get("canvas", False))
    return {"observation": {"url": obs.url, "title": obs.title,
                            "elements": len(obs.elements)}, "decision": decision}


@capability("operator.step", memory="on",
            http_method="POST", http_path="/operator/step", http_tags=["operator"],
            contract=_operator_contract(
                "browser.run.step",
                effects=["execute", "network", "filesystem", "model", "external_side_effect"],
                approval="session_policy_and_destructive_confirmation",
                secrets="browser_session_may_contain_sensitive_content",
                network="session_allowlist", filesystem="writes_operator_artifacts",
                idempotency="non_idempotent", cancellation="between_actions_best_effort",
                resources=["cpu", "network", "browser", "model"]),
            description="Run ONE observe→think→act tick against a session. Inputs: "
                        "session_id (str!), goal (str!), provider, model. "
                        "Output: {steps:[one record], done, reason}.")
async def cap_step(session_id: str = "", goal: str = "", provider: str = "ollama",
                   model: str = "", trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s:
        return {"error": "no such session"}
    policy = _policy_for_session(s)
    return await _loop.run_loop(goal, s, call_cap=_call, policy=policy,
                                provider=provider, model=model, max_steps=1,
                                canvas=(s.target or {}).get("canvas", False),
                                shots_dir=_shots_dir(session_id))


@capability("operator.run", memory="on",
            http_method="POST", http_path="/operator/run", http_tags=["operator"],
            contract=_operator_contract(
                "browser.run.execute",
                effects=["execute", "network", "filesystem", "model", "external_side_effect"],
                approval="session_policy_and_destructive_confirmation",
                secrets="browser_session_may_contain_sensitive_content",
                network="session_allowlist", filesystem="writes_operator_artifacts",
                idempotency="non_idempotent", cancellation="cooperative_between_steps",
                resources=["cpu", "network", "browser", "model", "redis"]),
            description="Drive a REAL browser session to a goal via observe→think→act — a "
                        "general-purpose web operator, not just a verification tool. WHEN TO "
                        "USE: any goal that means actually operating a real page — click a "
                        "button/link, fill a form and submit it, navigate a site's own "
                        "structure to find something (menus, search boxes, pagination), read/"
                        "extract real content off a rendered page (including JS-rendered "
                        "content a plain HTTP fetch never sees), OR verify a webpage's visible "
                        "UI actually changed after an interaction (a status text flipping, a "
                        "page navigating, an element appearing) — that's the tool for checking "
                        "a page/interface really works, not just that its file exists, but it "
                        "is one of several jobs this cap does, not the only one. For a pure "
                        "text/data lookup that doesn't need real navigation prefer the lighter "
                        "web.research (query-driven) or web.crawl (known-site, link-following) "
                        "— reach for operator.run when the page itself needs to be DRIVEN, not "
                        "just read. "
                        "Inputs: goal (str!), url OR kind+base_url (target), provider, "
                        "model, max_steps (15), session_id (reuse), allowlist (extra "
                        "hosts), dry_run, allow_destructive, keep_open, branch. "
                        "Output: {done, reason, steps:[...], run_id, screenshots}.",
            schema=enum_schema(kind=["url", "live", "sandbox", "panel", "codeserver", "vm"]))
async def cap_run(goal: str = "", url: str = "", kind: str = "", base_url: str = "",
                  provider: str = "ollama", model: str = "", max_steps: int = 15,
                  max_seconds: float = 0,
                  session_id: str = "", allowlist: Optional[List[str]] = None,
                  dry_run: Optional[bool] = None, allow_destructive: Optional[bool] = None,
                  keep_open: bool = False, branch: str = "", panel_id: str = "",
                  id: str = "", record_gif: bool = False, gif_duration_ms: int = 900,
                  path: str = "", sandbox_session: str = "",
                  progress_tolerance: int = 0, think: Optional[bool] = None,
                  trace_id=None) -> Dict[str, Any]:
    if not (goal or "").strip():
        return {"error": "goal required"}
    # Point the operator at a FILE a step just wrote. A path is something the
    # caller already knows; a URL is something it has to be told and reproduce.
    # Census run 18: two runs spent 21 minutes clicking Vera's own dashboard
    # hunting for a timer that lived in timer.html, because no URL was passed.
    _target_note = ""
    # True only when the target was picked out of the workspace rather than
    # given or named. It changes what may be ENFORCED, not where we start.
    _target_inferred = False
    # Neither a url nor a path was given - the planner just wrote a sentence.
    # Before falling back to the orchestrator root (never the right place to
    # verify a file this run wrote), see whether the goal NAMES the file.
    # Census 29: three runs landed on https://localhost:8999/ and spent their
    # budget clicking Vera's own dashboard.
    if not url and not path:
        _named = _goal_file.filename_in_goal(goal)
        if _named:
            path = _named
            log.info("operator.run target from the goal text: %s", _named)
        else:
            # The sentence names no file either. goal_file's docstring stops
            # here and says why: "that needs the run's artifact registry, which
            # the capability boundary does not have." It does now -- the
            # missing-path hint already reaches artifact_list_files across the
            # same boundary. Ask what this run actually WROTE rather than
            # falling back to the orchestrator root.
            #
            # 2026-09-09 author-then-edit re-test, third operator.run: goal
            # "Verify 90-second countdown behavior", no url, no path, no
            # filename. 504s spent observing Vera's own dashboard while
            # timer.html sat in the workspace -- and the two operator.run calls
            # before it had both been handed the correct preview URL.
            #
            # NOT gated on a session id being passed in. The agentic loop calls
            # operator.run with a goal and nothing else -- `sandbox_session`
            # appears nowhere in dag_workshop_capabilities -- so requiring one
            # here would make this branch dead code in exactly the case it
            # exists for. artifact_list_files falls back to _trigger_session_id()
            # ("set by chat.stream / the agentic loop ... the safety net"),
            # which is how exec's own missing-path hint reaches the workspace.
            #
            # Best-effort throughout: it returns None when the listing cannot be
            # determined, and any failure leaves the target exactly as it was.
            # The old behaviour is the fallback, not an error.
            try:
                from Vera.vera.execution import exec_capabilities as _exec
            except ImportError:                                  # pragma: no cover
                from vera.execution import exec_capabilities as _exec
            try:
                _wrote = _nav_fallback.sole_page(
                    await _exec.artifact_list_files(
                        session_id=str(sandbox_session or session_id or "").strip()))
                if _wrote:
                    path = _wrote
                    _target_inferred = True
                    log.info("operator.run target from the run's own files: %s",
                             _nav_fallback.target_note(_wrote))
            except Exception as e:                               # pragma: no cover
                log.debug("operator.run: workspace target probe skipped: %s", e)
    if path and not url:
        _sbx = str(sandbox_session or session_id or "").strip()
        _res = _sfile.resolve(url, path, base_url=_orch_base_url(), session_id=_sbx)
        if _res.get("url"):
            url = _res["url"]
            _target_note = _res.get("note") or ""
            log.info("operator.run target from path: %s", _target_note)
        elif _res.get("note"):
            log.warning("operator.run: %s", _res["note"])
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    own = False
    s = _be.get_session(session_id) if session_id else None
    if not s:
        start = await _open_session(url=url, kind=kind, base_url=base_url, branch=branch,
                                    panel_id=panel_id, cs_id=id, allowlist=allowlist,
                                    allow_destructive=allow_destructive, dry_run=dry_run)
        if start.get("error"):
            return start
        s = _be.get_session(start["session_id"])
        own = True
    elif url:
        # A REUSED session is still showing the previous run's page - nothing
        # above navigated it, because only _open_session does that. Census 30
        # run ce4b0a4725 was handed the correct preview url and spent all
        # twelve steps on the dashboard trying to reach it. See session_target.
        _cur = ""
        try:
            _cur = str(getattr(getattr(s, "page", None), "url", "") or "")
        except Exception:
            _cur = ""
        if _sess_target.needs_navigation(_cur, url):
            try:
                _nav = await _actions.perform(s, "goto", {"url": url})
                if _nav.get("error"):
                    log.warning("operator.run: reused session could not reach %s: %s",
                                url, _nav["error"])
                else:
                    log.info("operator.run: moved reused session %s -> %s", _cur, url)
            except Exception as e:
                log.warning("operator.run: navigating reused session failed: %s", e)
    resolved = s.target or {}
    # Session policy (set at connect) UNIONed with any per-run overrides.
    policy = _policy_for_session(s, allowlist=allowlist, dry_run=dry_run,
                                 allow_destructive=allow_destructive)
    run_id = uuid.uuid4().hex[:10]
    shots = _shots_dir(s.session_id) + f"/run-{run_id}"

    async def _on_step(rec: Dict[str, Any]):
        shot = rec.get("screenshot", "")
        await _op_record(run_id, {
            "type": "operator.step", "run_id": run_id,
            "i": rec.get("i"), "phase": rec.get("phase"),
            "action": rec.get("action"),
            # ARGS, not just the verb. Without them "click 11x" is ambiguous
            # between eleven clicks on one element (thrash) and eleven on
            # different ones (progress) — and that ambiguity is exactly what made
            # run 15's author-then-edit failure hard to read.
            "args": rec.get("args") or {},
            "url": rec.get("url", ""),
            # The page's own text, forwarded so the PERSISTED trace carries it
            # too. run_loop has recorded it on every act step since the
            # observation-as-finding change, but this emitter dropped it, so
            # operator.trace showed `seen` empty on every step.
            "seen": rec.get("seen", ""),
            "thought": rec.get("thought", "")[:200],
            "reason": rec.get("reason", ""), "error": rec.get("error", ""),
            "screenshot": f"/operator/artifact?path={_artifact_rel(shot)}" if shot else ""})

    await _op_record(run_id, {"type": "operator.run", "stage": "start", "run_id": run_id,
                              "goal": goal[:200], "target": resolved.get("kind"),
                              "session_id": s.session_id})
    # A stale flag from a previous run of the same id would cancel this one
    # instantly; ids are random, but clearing is cheap and removes the class.
    await _op_clear_cancel(run_id)
    # See the matching note in cap_connect: this close belongs in a finally,
    # because a cancelled or timed-out run is exactly the case that leaked.
    try:
        result = await _loop.run_loop(
            goal, s, call_cap=_call, policy=policy, provider=provider, model=model,
            max_steps=int(max_steps),
            # 0 = use the loop's own default budget; a caller with a tighter goal
            # allowance can hand it a smaller one.
            # Floored: a caller may tighten the budget but not below the point where
            # the run cannot finish anything. Census 24 saw a model ask for 95s and
            # the run die at 103s after five steps. 0 still means "use the default".
            **_op_budget.budget_kwargs(max_seconds),
            canvas=resolved.get("canvas", False),
            shots_dir=shots, on_step=_on_step,
            progress_tolerance=(int(progress_tolerance) if progress_tolerance
                                else _progress.DEFAULT_TOLERANCE),
            think=think,
            # Pin the run to the file it was aimed at, when that is what it was
            # aimed at. `url` here is whatever survived resolution above - explicit,
            # path-derived or read out of the goal text - and nav_pin.is_pinnable
            # accepts only a sandbox preview URL, so a goal that legitimately
            # browses a site is never pinned. See nav_pin for the run this fixes.
            #
            # An INFERRED target is deliberately not pinned. The pin holds a run to
            # a target it was TOLD to use; a page picked out of the workspace was
            # never told, and pinning it would trap a goal that really did mean to
            # go to the open web on a local file it never asked for - worse than
            # the dashboard this fallback exists to avoid. Starting in the right
            # place is the whole benefit; enforcing it is not ours to claim.
            pin_url=_nav_fallback.pin_for(url, _target_inferred, _nav_pin.is_pinnable),
            should_cancel=lambda: _op_is_cancelled(run_id))
        # Assemble the per-step screenshots into a GIF of the whole run (the frames
        # already exist — this is nearly free).
        if record_gif and result.get("screenshots"):
            gif_path = os.path.join(shots, "run.gif")
            ga = _capture.assemble_gif(result["screenshots"], gif_path,
                                       duration_ms=int(gif_duration_ms))
            if ga.get("ok"):
                result["gif"] = f"/operator/artifact?path={_artifact_rel(gif_path)}"
                result["gif_path"] = gif_path
                result["gif_frames"] = ga.get("frames")
            else:
                result["gif_error"] = ga.get("error")
    finally:
        if own and not keep_open:
            await _be.close_session(s.session_id)
    result.update({"run_id": run_id, "goal": goal, "target": resolved.get("kind"),
                   "session_id": s.session_id if (keep_open or not own) else ""})
    await _op_record(run_id, {"type": "operator.run", "stage": "done", "run_id": run_id,
                              "reason": result.get("reason"), "steps": result.get("step_count"),
                              "gif": result.get("gif", "")})
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  MISSIONS  (+ docs aliases)
# ─────────────────────────────────────────────────────────────────────────────
def _mission_ctx() -> Dict[str, Any]:
    return {"call_cap": _call, "emit": emit_event, "repo_root": str(_repo_root()),
            "default_base_url": _default_base_url()}


@capability("operator.mission.list", memory="off", silent=True,
            http_method="GET", http_path="/operator/mission/list", http_tags=["operator"],
            contract=_operator_contract(
                "browser.mission.list", effects=["read"],
                trust="internal_mission_registry", tenant="global_read_only"),
            description="List available operator missions. Output: {missions:{name:desc}}.")
async def cap_mission_list(trace_id=None) -> Dict[str, Any]:
    return {"missions": list_missions()}


@capability("operator.mission.run", memory="on",
            http_method="POST", http_path="/operator/mission/run", http_tags=["operator"],
            contract=_operator_contract(
                "browser.mission.execute",
                effects=["execute", "network", "filesystem", "external_side_effect"],
                approval="target_and_write_policy", network="selected_target",
                filesystem="conditional_repository_and_artifact_writes",
                idempotency="mission_defined", cancellation="between_steps_best_effort",
                resources=["cpu", "network", "browser"]),
            description="Run a named operator mission. Inputs: mission (str!, e.g. "
                        "'documentation'), target (sandbox|live|{...}), domains "
                        "(list of doc slugs, empty=all), base_url, capture (bool), "
                        "write_docs (bool). Output depends on the mission.")
async def cap_mission_run(mission: str = "", target: str = "sandbox",
                          domains: Optional[List[str]] = None, base_url: str = "",
                          capture: bool = True, write_docs: bool = True,
                          trace_id=None) -> Dict[str, Any]:
    if not mission:
        return {"error": "mission required"}
    params = {"target": target, "domains": domains or [], "base_url": base_url,
              "capture": bool(capture), "write_docs": bool(write_docs)}
    return await run_mission(mission, params, _mission_ctx())


@capability("docs.build", memory="on",
            http_method="POST", http_path="/docs/build", http_tags=["docs", "operator"],
            contract=_operator_contract(
                "documentation.capture.build",
                effects=["execute", "network", "filesystem"],
                approval="target_and_write_policy", network="selected_vera_target",
                filesystem="writes_documentation_assets_and_managed_blocks",
                idempotency="replace_managed_outputs", cancellation="between_panels_best_effort",
                resources=["cpu", "network", "browser"], owner="vera.documentation"),
            description="Build/refresh Vera's documentation: screenshot every UI "
                        "panel (seeded) on a target Vera and regenerate the doc "
                        "auto-blocks + gallery. Alias for the 'documentation' "
                        "mission. Inputs: target (sandbox|live), domains (list, "
                        "empty=all), panels (list of panel ids for SELECTIVE "
                        "re-capture — leaves the rest untouched), settle_ms (extra "
                        "wait per panel, default 1400), full_page, base_url, capture, "
                        "write_docs. Output: {screenshots, domains, capabilities, ...}.")
async def cap_docs_build(target: str = "sandbox", domains: Optional[List[str]] = None,
                         panels: Optional[List[str]] = None, settle_ms: int = 1400,
                         full_page: bool = False, base_url: str = "",
                         capture: bool = True, write_docs: bool = True,
                         trace_id=None) -> Dict[str, Any]:
    params = {"target": target, "domains": domains or [], "panels": panels or [],
              "settle_ms": int(settle_ms), "full_page": bool(full_page),
              "base_url": base_url, "capture": bool(capture),
              "write_docs": bool(write_docs)}
    return await run_mission("documentation", params, _mission_ctx())


@capability("docs.assets", memory="off", silent=True,
            http_method="GET", http_path="/docs/assets", http_tags=["docs", "operator"],
            contract=_operator_contract(
                "documentation.assets.list", effects=["read", "filesystem"],
                trust="repository_metadata", filesystem="read_documentation_assets",
                tenant="repository_scoped", pagination="bounded_repository_scan",
                owner="vera.documentation"),
            description="List captured documentation images for the gallery. Scans "
                        "documentation/assets/ on DISK (the source of truth, so a "
                        "stale/empty manifest never hides real images) and enriches "
                        "each with manifest metadata (label/via) when available. "
                        "Output: {domains:[{slug,title,doc,panels:[{id,label,shot,url,"
                        "via,mode,kind}]}], count}.")
async def cap_docs_assets(trace_id=None) -> Dict[str, Any]:
    from Vera.vera.operator.docs import domain_map as _dm
    assets = _repo_root() / "documentation" / "assets"
    if not assets.exists():
        return {"domains": [], "count": 0, "note": "no documentation/assets yet"}
    # Manifest is metadata only (labels, via); disk decides what exists.
    meta: Dict[str, Dict[str, Any]] = {}
    man = assets / "manifest.json"
    if man.exists():
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
            for slug, info in (data.get("domains") or {}).items():
                for p in info.get("panels", []):
                    meta[f"{slug}/{p.get('id')}"] = p
        except Exception:
            pass
    title_of = {d["slug"]: d["title"] for d in _dm.DOMAINS}
    doc_of = {d["slug"]: d["doc"] for d in _dm.DOMAINS}
    out = []
    total = 0
    for sub in sorted(p for p in assets.iterdir() if p.is_dir()):
        slug = sub.name
        pnls = []
        for f in sorted(sub.glob("*.png")) + sorted(sub.glob("*.gif")):
            stem = f.stem
            rel = f"assets/{slug}/{f.name}"
            m = meta.get(f"{slug}/{stem}", {})
            pnls.append({"id": stem, "label": m.get("label", stem), "shot": rel,
                         "url": f"/docs/asset?path={rel}", "via": m.get("via", ""),
                         "mode": m.get("mode", ""), "kind": f.suffix.lstrip(".")})
            total += 1
        if pnls:
            out.append({"slug": slug, "title": title_of.get(slug, slug),
                        "doc": doc_of.get(slug, ""), "panels": pnls})
    return {"domains": out, "count": total}


@capability("docs.capture", memory="on",
            http_method="POST", http_path="/docs/capture", http_tags=["docs", "operator"],
            contract=_operator_contract(
                "documentation.directive.capture",
                effects=["execute", "network", "filesystem"],
                approval="target_and_write_policy", network="selected_vera_target",
                filesystem="writes_documentation_assets_and_managed_blocks",
                idempotency="replace_managed_outputs", cancellation="between_captures_best_effort",
                resources=["cpu", "network", "browser"], owner="vera.documentation"),
            description="Fulfil <!-- VERA:CAPTURE panel=... steps=... gif=... --> "
                        "directives in the docs: navigate to each panel, run the "
                        "deterministic steps, capture a still/GIF, and insert it in a "
                        "managed block after the directive (idempotent, preserves "
                        "prose). Inputs: doc (one 'NN-slug.md', blank=all docs with "
                        "directives), target (sandbox|live), base_url, branch. "
                        "Output: {ok, captures, docs:{file:n}}.")
async def cap_docs_capture(doc: str = "", target: str = "sandbox", base_url: str = "",
                           branch: str = "", trace_id=None) -> Dict[str, Any]:
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    docs_dir = _repo_root() / "documentation"
    if doc:
        f0 = docs_dir / doc
        if not f0.exists():
            return {"error": f"doc not found: {doc}"}
        files = [f0]
    else:
        files = sorted(docs_dir.glob("*.md"))
    # Only bother with docs that actually contain a directive.
    files = [f for f in files if "VERA:CAPTURE" in f.read_text(encoding="utf-8")]
    if not files:
        return {"ok": True, "captures": 0, "docs": {},
                "note": "no <!-- VERA:CAPTURE --> directives found"}

    tgt: Dict[str, Any] = {"kind": target} if isinstance(target, str) else dict(target)
    if branch:
        tgt["branch"] = branch
    resolved = await _targets.ensure_target(tgt, _call, _default_base_url())
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready")}
    base = base_url or resolved["base_url"]
    try:
        sess = await _be.start_session(base_url=base, target=resolved)
    except Exception as e:
        return {"error": f"session start failed: {e}"}
    caller = _target_caller(base)

    total = 0
    per_doc: Dict[str, int] = {}
    try:
        for f in files:
            md = f.read_text(encoding="utf-8")
            slug = _safe_seg(re.sub(r"^\d+-", "", f.stem))
            out_dir = str(docs_dir / "assets" / slug)
            rel_base = f"assets/{slug}"
            made = 0
            for d in _directives.parse_directives(md):
                attrs = d["attrs"]
                name = _safe_seg(attrs.get("name") or f"capture{made}")
                steps = _directives.directive_steps(attrs)
                await emit_event({"type": "operator.docs.progress", "stage": "capture",
                                  "message": f"[{f.name}] {name}"})
                res = await _tours.run_tour(sess, steps, out_dir=out_dir,
                                            rel_base=rel_base, call_target=caller)
                assets = (res.get("gifs") or []) + (res.get("shots") or [])
                asset = next((a for a in assets if a["name"] == name),
                             assets[-1] if assets else None)
                if not asset:
                    continue
                is_gif = str(asset["path"]).endswith(".gif")
                img = _directives.image_markdown(name, asset["rel"], gif=is_gif)
                # Re-read + re-locate the directive so growing inserts stay valid.
                md = f.read_text(encoding="utf-8")
                pos = md.find(d["raw"])
                after = (pos + len(d["raw"])) if pos >= 0 else None
                md = _directives.upsert_capture(md, name, img, after_pos=after)
                f.write_text(md, encoding="utf-8")
                made += 1
                total += 1
            per_doc[f.name] = made
    finally:
        await _be.close_session(sess.session_id)
    return {"ok": True, "captures": total, "docs": per_doc,
            "target": resolved.get("kind"), "base_url": base}


@capability("docs.gallery", memory="on",
            http_method="POST", http_path="/docs/gallery", http_tags=["docs", "operator"],
            contract=_operator_contract(
                "documentation.gallery.rebuild", effects=["read", "write", "filesystem"],
                trust="repository_metadata", filesystem="reads_manifest_writes_gallery",
                tenant="repository_scoped", idempotency="replace_generated_output",
                owner="vera.documentation"),
            description="Rebuild documentation/GALLERY.md from the last "
                        "capture manifest (no screenshots taken). Output: {ok, domains}.")
async def cap_docs_gallery(trace_id=None) -> Dict[str, Any]:
    docs = _repo_root() / "documentation"
    man = docs / "assets" / "manifest.json"
    if not man.exists():
        return {"error": "no manifest — run docs.build first"}
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"manifest unreadable: {e}"}
    entries = []
    for slug, info in (data.get("domains") or {}).items():
        panels = info.get("panels", [])
        entries.append({"slug": slug, "title": info.get("title", slug),
                        "doc": info.get("doc", ""),
                        "cover_rel": panels[0]["shot"] if panels else "",
                        "shot_count": len(panels), "cap_count": info.get("cap_count", 0)})
    gal = _gallery.build_gallery(entries, generated_at=data.get("generated_at", ""),
                                 total_caps=sum(e["cap_count"] for e in entries))
    (docs / _gallery.OUTPUT_FILE).write_text(gal, encoding="utf-8")
    return {"ok": True, "domains": len(entries),
            "path": f"documentation/{_gallery.OUTPUT_FILE}"}


# ─────────────────────────────────────────────────────────────────────────────
#  CAPTURE  —  GIF / time-lapse
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.capture.start", memory="on",
            http_method="POST", http_path="/operator/capture/start", http_tags=["operator"],
            contract=_operator_contract(
                "browser.capture.start", effects=["execute", "filesystem"],
                filesystem="writes_operator_capture_frames", idempotency="non_idempotent",
                cancellation="explicit_stop_or_frame_limit", resources=["cpu", "browser"]),
            description="Start a time-lapse: screenshot the session's page every "
                        "interval_ms while a long task runs (a dream cycle, a "
                        "backtest, a loop). Stop with operator.capture.stop to get "
                        "a GIF. Inputs: session_id (str!), interval_ms (1000), "
                        "max_frames (180), name. Output: {capture_id, interval_ms}.")
async def cap_capture_start(session_id: str = "", interval_ms: int = 1000,
                            max_frames: int = 180, name: str = "",
                            trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session (operator.session.start first)"}
    cid = _safe_seg(name) if name else ""
    frames_dir = str(_repo_root() / "artifacts" / "operator" / "captures" /
                     (cid or f"cap-{uuid.uuid4().hex[:8]}") / "frames")

    def _get_page():
        cur = _be.get_session(session_id)
        return cur.page if cur else None

    cap = _capture.start_capture(session_id, frames_dir, _get_page,
                                 interval_ms=int(interval_ms),
                                 max_frames=int(max_frames), capture_id=cid)
    await emit_event({"type": "operator.capture", "stage": "start",
                      "capture_id": cap.capture_id, "session_id": session_id})
    return {"ok": True, "capture_id": cap.capture_id, "interval_ms": int(interval_ms),
            "pillow": _capture.pil_available()}


@capability("operator.capture.status", memory="off", silent=True,
            http_method="GET", http_path="/operator/capture/status", http_tags=["operator"],
            contract=_operator_contract(
                "browser.capture.inspect", effects=["read"],
                trust="capture_metadata_only", pagination="bounded_in_memory"),
            description="Status of one capture (capture_id) or all. Output: "
                        "{capture_id, frames, running, ...} or {captures:[...]}.")
async def cap_capture_status(capture_id: str = "", trace_id=None) -> Dict[str, Any]:
    if capture_id:
        c = _capture.get_capture(capture_id)
        return c.summary() if c else {"error": f"no such capture: {capture_id}"}
    return {"captures": _capture.list_captures()}


@capability("operator.capture.stop", memory="on",
            http_method="POST", http_path="/operator/capture/stop", http_tags=["operator"],
            contract=_operator_contract(
                "browser.capture.stop", effects=["execute", "filesystem"],
                filesystem="writes_gif_and_removes_temporary_frames",
                idempotency="idempotent_after_success", cancellation="bounded_encoding",
                resources=["cpu", "browser"]),
            description="Stop a time-lapse and assemble its frames into a GIF. "
                        "Inputs: capture_id (str!), domain (docs domain slug → the "
                        "GIF lands in documentation/assets/<domain>/<name>.gif; blank "
                        "→ artifacts, served live), name, duration_ms (per frame, "
                        "800), max_width (900). Output: {ok, path, rel, url, frames, "
                        "bytes}.")
async def cap_capture_stop(capture_id: str = "", domain: str = "", name: str = "",
                           duration_ms: int = 800, max_width: int = 900,
                           trace_id=None) -> Dict[str, Any]:
    if not capture_id:
        return {"error": "capture_id required"}
    cap = await _capture.stop_capture(capture_id)
    if not cap:
        return {"error": f"no such capture: {capture_id}"}
    if not cap.frames:
        return {"error": "capture produced no frames", "capture_id": capture_id}
    out = _gif_out_path(domain, name or capture_id)
    res = _capture.assemble_gif(cap.frames, out["path"], duration_ms=int(duration_ms),
                                max_width=int(max_width))
    if res.get("error"):
        return res
    await emit_event({"type": "operator.capture", "stage": "gif",
                      "capture_id": capture_id, "frames": res.get("frames"),
                      "url": out.get("url", ""), "rel": out.get("rel", "")})
    return {"ok": True, "capture_id": capture_id, "frames": res["frames"],
            "bytes": res["bytes"], "path": out["path"], "rel": out["rel"],
            "url": out["url"]}


# ─────────────────────────────────────────────────────────────────────────────
#  TOURS  —  deterministic scripted walkthroughs (stills + GIFs), no LLM
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.tour.list", memory="off", silent=True,
            http_method="GET", http_path="/operator/tour/list", http_tags=["operator"],
            contract=_operator_contract(
                "browser.tour.list", effects=["read"],
                trust="internal_tour_registry", tenant="global_read_only"),
            description="List available scripted tours. Output: {tours:[slug,...]}.")
async def cap_tour_list(trace_id=None) -> Dict[str, Any]:
    return {"tours": _tours.list_tours()}


@capability("operator.tour.run", memory="on",
            http_method="POST", http_path="/operator/tour/run", http_tags=["operator"],
            contract=_operator_contract(
                "browser.tour.execute", effects=["execute", "network", "filesystem"],
                approval="target_and_write_policy", network="selected_vera_target",
                filesystem="writes_documentation_assets",
                idempotency="replace_named_outputs", cancellation="between_steps_best_effort",
                resources=["cpu", "network", "browser"], owner="vera.documentation"),
            description="Run a deterministic scripted tour of a domain's UI and "
                        "capture stills + GIF clips into documentation/assets/<slug>/. "
                        "Reproducible 'in-action' docs without the LLM. Inputs: slug "
                        "(str! — see operator.tour.list), target (sandbox|live), "
                        "base_url, branch (sandbox). Output: {shots:[...], gifs:[...], "
                        "errors, slug}.")
async def cap_tour_run(slug: str = "", target: str = "sandbox", base_url: str = "",
                       branch: str = "", trace_id=None) -> Dict[str, Any]:
    tour = _tours.get_tour(slug)
    if not tour:
        return {"error": f"unknown tour '{slug}'. Available: {_tours.list_tours()}"}
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    tgt: Dict[str, Any] = {"kind": target} if isinstance(target, str) else dict(target)
    if branch:
        tgt["branch"] = branch
    resolved = await _targets.ensure_target(tgt, _call, _default_base_url())
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready"), "target": resolved}
    base = base_url or resolved["base_url"]
    try:
        sess = await _be.start_session(base_url=base, target=resolved)
    except Exception as e:
        return {"error": f"session start failed: {e}"}

    caller = _target_caller(base)
    # Seed representative data so the tour renders populated.
    if tour.get("seed"):
        from Vera.vera.operator.missions import seeds as _seeds
        try:
            await _seeds.run_seed(tour["seed"], caller)
        except Exception as e:
            log.debug("tour seed failed: %s", e)

    out_dir = str(_repo_root() / "documentation" / "assets" / _safe_seg(slug))
    rel_base = f"assets/{_safe_seg(slug)}"

    async def _emit(**k):
        await emit_event({"type": "operator.tour", "slug": slug, **k})

    await emit_event({"type": "operator.tour", "stage": "start", "slug": slug})
    try:
        res = await _tours.run_tour(sess, tour["steps"], out_dir=out_dir,
                                    rel_base=rel_base, call_target=caller, emit=_emit)
    finally:
        await _be.close_session(sess.session_id)
    res.update({"slug": slug, "title": tour.get("title"),
                "target": resolved.get("kind"), "base_url": base})
    await emit_event({"type": "operator.tour", "stage": "done", "slug": slug,
                      "shots": len(res.get("shots", [])), "gifs": len(res.get("gifs", []))})
    return res


# ─────────────────────────────────────────────────────────────────────────────
#  TESTING
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.test.run", memory="on",
            http_method="POST", http_path="/operator/test/run", http_tags=["operator"],
            contract=_operator_contract(
                "test.pytest.execute", effects=["execute", "filesystem"],
                approval="repository_execution_policy", trust="repository_test_code",
                filesystem="test_process_may_read_and_write_repository",
                tenant="repository_scoped", idempotency="test_suite_defined",
                cancellation="hard_timeout", resources=["cpu"], owner="vera.testing"),
            description="Run the project's pytest unit suite. Inputs: path (default "
                        "'tests'), k (pytest -k expression). Output: {ok, code, out}.")
async def cap_test_run(path: str = "tests", k: str = "", trace_id=None) -> Dict[str, Any]:
    root = _repo_root()
    cmd = [sys.executable, "-m", "pytest", path or "tests", "-q"]
    if k:
        cmd += ["-k", k]

    def _run():
        try:
            p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                               timeout=600)
            return {"code": p.returncode, "out": (p.stdout or "")[-6000:],
                    "err": (p.stderr or "")[-2000:]}
        except Exception as e:
            return {"code": -1, "out": "", "err": str(e)}

    res = await asyncio.get_event_loop().run_in_executor(None, _run)
    res["ok"] = res.get("code") == 0
    return res


# ─────────────────────────────────────────────────────────────────────────────
#  RAW ROUTES: artifact serving + Operator Studio panel
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/operator/artifact", include_in_schema=False)
async def _operator_artifact(path: str = ""):
    from fastapi.responses import FileResponse, JSONResponse
    root = (_repo_root() / "artifacts" / "operator").resolve()
    target = (root / (path or "").lstrip("/\\")).resolve()
    if root != target and root not in target.parents:
        return JSONResponse({"error": "path escapes artifact root"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = target.suffix.lower()
    mt = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".webm": "video/webm", ".mp4": "video/mp4"}.get(ext, "image/png")
    return FileResponse(str(target), media_type=mt)


@APP.get("/docs/asset", include_in_schema=False)
async def _docs_asset(path: str = ""):
    """Serve a committed documentation screenshot/GIF (documentation/assets/…)
    so the Operator Studio gallery can display them. Path-jailed to that dir."""
    from fastapi.responses import FileResponse, JSONResponse
    root = (_repo_root() / "documentation").resolve()
    rel = (path or "").lstrip("/\\")
    target = (root / rel).resolve()
    assets = (root / "assets").resolve()
    if assets != target and assets not in target.parents:
        return JSONResponse({"error": "path escapes docs assets"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = target.suffix.lower()
    mt = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
    return FileResponse(str(target), media_type=mt)


@APP.get("/operator/panel", include_in_schema=False)
async def _operator_panel():
    from fastapi.responses import HTMLResponse
    if _PANEL_PATH.exists():
        return HTMLResponse(_PANEL_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:#c96b6b'>operator_studio_panel.html not found</p>",
                        status_code=404)


# ─────────────────────────────────────────────────────────────────────────────
#  TRACE  (O13) — read a browser run back, the way the agentic loop can be read
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.trace", memory="off", silent=True,
            http_method="GET", http_path="/operator/trace", http_tags=["operator", "obs"],
            contract=_operator_contract(
                "browser.run.trace", effects=["read"],
                trust="persisted_operator_events", tenant="run_scoped",
                pagination="bounded_event_history", resources=["cpu", "redis"]),
            description=(
                "READ-ONLY diagnostic digest of ONE operator (browser) run — the "
                "operator's answer to workshop.agent_loop.trace. Returns what it "
                "was asked to do, each observe/think/act step with its action, "
                "thought, error and screenshot, phase coverage, and warnings for "
                "the failure shapes that matter: the SAME action attempted 3+ "
                "times (the browser thrash signature), steps that errored, and a "
                "run that stopped because it ran out of STEPS rather than because "
                "it finished. That last one is flagged next to the run's own "
                "`reason`, because an operator run reporting success after hitting "
                "its step ceiling is a real observed failure, not a hypothetical. "
                "Offers no verdict of its own. Inputs: run_id (str!). Output: "
                "{run, steps[], counters, warnings[], duration_s}."),
            )
async def cap_operator_trace(run_id: str = "", trace_id=None) -> Dict[str, Any]:
    rid = (run_id or "").strip()
    if not rid:
        return {"error": "run_id is required"}
    events = await _op_events(rid)
    if not events:
        return {"error": f"no recorded events for run '{rid}'",
                "note": "runs are kept for 14 days; runs from before operator "
                        "recording landed were never persisted at all"}
    return {"run_id": rid, **_op_trace.digest_events(events)}


@capability("operator.cancel", memory="on",
            http_method="POST", http_path="/operator/cancel", http_tags=["operator"],
            contract=_operator_contract(
                "browser.run.cancel", effects=["write", "execute"],
                trust="run_identifier", tenant="run_scoped", idempotency="idempotent",
                cancellation="cooperative_between_steps", resources=["cpu", "redis"]),
            description=(
                "STOP a running operator (browser) run. Sets a cooperative cancel "
                "flag the run checks BEFORE each step, so it stops without buying "
                "one more browser action and one more LLM call. The agentic loop "
                "has had this for a long time; the operator did not, which is how "
                "a cancelled census run left an operator generation running 1211s "
                "against the shared GPU.\n"
                "HONEST LIMIT: this stops the run issuing further work. It cannot "
                "abort the single generation already in flight — that ends on its "
                "own timeout — so a slot can stay busy briefly after cancelling. "
                "The flag is authoritative and outlives the process, so a run "
                "orphaned by a restart can still be told to stop. Inputs: run_id "
                "(str!). Output: {ok, run_id, flag_set}."),
            )
async def cap_operator_cancel(run_id: str = "", trace_id=None) -> Dict[str, Any]:
    rid = (run_id or "").strip()
    if not rid:
        return {"ok": False, "error": "run_id is required"}
    ok = await _op_set_cancel(rid)
    await _op_record(rid, {"type": "operator.run", "stage": "cancel_requested",
                           "run_id": rid})
    return {"ok": bool(ok), "run_id": rid, "flag_set": bool(ok),
            "note": ("the run stops before its next step; a generation already in "
                     "flight still ends on its own timeout")}


@capability("operator.runs", memory="off", silent=True,
            http_method="GET", http_path="/operator/runs", http_tags=["operator", "obs"],
            contract=_operator_contract(
                "browser.run.list", effects=["read"],
                trust="persisted_operator_events", tenant="global_aggregate",
                pagination="bounded_limit", resources=["cpu", "redis"]),
            description=(
                "LIST recent operator (browser) runs, newest first — goal, target, "
                "steps, errors, repeated actions, whether it hit its step ceiling, "
                "duration, and whether it COMPLETED at all. An incomplete run is "
                "worth looking at: it means the run never wrote its `done` event, "
                "so it was cancelled or its process died holding it. Inputs: limit "
                "(int=30). Output: {runs[], count}."),
            )
async def cap_operator_runs(limit: int = 30, trace_id=None) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"runs": [], "count": 0, "error": "redis unavailable"}
    n = max(1, min(200, int(limit or 30)))
    try:
        ids = await r.zrevrange(_OP_RUNS_KEY, 0, n - 1)
    except Exception as e:
        return {"runs": [], "count": 0, "error": f"{type(e).__name__}: {e}"}
    out: List[Dict[str, Any]] = []
    for raw in ids or []:
        rid = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        events = await _op_events(rid)
        if events:
            out.append(_op_trace.summarise_run(rid, events))
    return {"runs": out, "count": len(out)}


register_ui(
    "operator-studio", "Operator", "⦿",
    """<div id="operator-mount" style="height:100%;display:flex;flex-direction:column;">
        <iframe src="/operator/panel"
                style="flex:1;border:none;width:100%;height:100%"></iframe>
    </div>""",
    "",
    ui_caps=["operator.session.start", "operator.session.status",
             "operator.session.close", "operator.observe", "operator.read",
             "operator.screenshot", "operator.act", "operator.think",
             "operator.step", "operator.run", "operator.mission.list",
             "operator.mission.run", "docs.build", "docs.gallery",
             "operator.test.run", "operator.trace", "operator.runs"],
    # "element", not "tab": the Operator is reached through the Automations
    # hub, which embeds /operator/panel as its own sub-tab. It stays registered
    # (test_ui_panels asserts that, and the dashboard-widget loader, custom
    # tabs and solo popout all read the registry) but no longer claims a
    # top-level tab.
    mode="element", tab_order=73,
)

async def _operator_session_sweep() -> None:
    """Scheduler tick: close abandoned operator sessions and, once none are
    left, the shared browser.

    `keep_open=True` sessions (and every operator.session.start) are handed to a
    caller that is trusted to close them, and nothing collected the ones that
    never were — there was no reaper and no cap on browser_engine._SESSIONS. A
    live context+page is a real slice of a shared Chromium, which was observed
    on prod at 8.4GB RSS. Both thresholds are env-tunable; 0 disables.
    """
    try:
        idle_s = float(os.getenv("VERA_OPERATOR_SESSION_IDLE_S",
                                 _be.DEFAULT_SESSION_IDLE_S) or 0)
        if idle_s <= 0:
            return
        linger_s = float(os.getenv("VERA_OPERATOR_BROWSER_LINGER_S",
                                   _be.DEFAULT_BROWSER_LINGER_S) or 0)
        res = await _be.sweep_idle(idle_s=idle_s, linger_s=linger_s)
        if res.get("closed_sessions") or res.get("browser_closed"):
            await emit_event({"type": "operator.session.swept",
                              "closed": len(res.get("closed_sessions") or []),
                              "browser_closed": bool(res.get("browser_closed")),
                              "live_sessions": res.get("live_sessions", 0)})
    except Exception as e:
        log.debug("operator session sweep failed: %s", e)


try:
    schedule(_operator_session_sweep, 300, name="operator_session_sweep")
except Exception as _e:
    log.debug("could not register operator session sweep: %s", _e)


log.info("operator: capabilities registered (playwright=%s)", _be.playwright_available())
