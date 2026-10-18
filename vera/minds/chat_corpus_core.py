"""The chat corpus: hand-written chat turns run weekly through the real chat UI, judged by deterministic checks.

four-minds ROADMAP §5.4 (FM-0.7's traffic-independent run): chat sees ~2 turns a day, far below the 30/day the
shadow evals need, so nothing measured it. The owner approved 30 cases (shared-planning/four-minds/CHAT-CORPUS-v0.md,
2026-10-10); tools/chat-corpus/run_chat_corpus.py types each one into /chat_panel with Playwright and hands what it
saw to this module, which says whether each check held.

Evidence for one case (the runner collects it, this module only reads it):

    stream      the raw SSE text of the turn's /agents/chat/stream response (tokens, phase frames, errors)
    loop        the agent-loop requests the page made (a [[loop]] hand-off) - intercepted, never started
    calls       the /mcp/call requests the page made after the send, each {name, would_call}
    canvas      the session canvas document after the turn (canvas.get)
    elapsed_ms, client_ttft_ms, error

Checks (all deterministic):

    route         chat | loop - a loop request was made, or the reply carries the [[loop marker
    acquire       none | web | memory - from the turn's phase frames (stage acquire, id web / memory)
    mentions      every term appears in the reply (case-insensitive; "a|b" = either)
    not_mentions  none of them appears
    canvas        the canvas shows an item from this domain (surface_core's domains, read from the item keys)
    ttft_ms       time to the first token within the budget for the turn's kind - ADVISORY until the first run sets
                  the budgets (the corpus says so); reported, never a fail

A case with a fixture (a memory record seeded before the run) is skipped until fixture seeding exists.

Effects: a page call whose capability changes something (surface_core.is_read says no, or its namespace runs work)
is answered with a stub and recorded as "would call"; loop hand-offs are answered with a stub stream. A run never
books a reminder or starts a loop. Pure: no I/O, no app imports beyond the two pure cores it reads.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.canvas import surface_core as SC
    from Vera.vera.agents import turn_phases_core as TP
except Exception:                        # worktree / app-free import
    from vera.canvas import surface_core as SC          # type: ignore
    from vera.agents import turn_phases_core as TP      # type: ignore

ROUTES = ("chat", "loop")
ACQUIRES = ("none", "web", "memory", "any")          # any: not checked
CHECKS = ("route", "acquire", "mentions", "not_mentions", "canvas", "tools_any", "tools_none", "cites", "ttft_ms")
#: a numbered source citation in a reply: [3], [E3], [1, 2]
CITE_RE = r"\[\s*[Ee]?\d{1,3}(?:\s*[,;]\s*[Ee]?\d{1,3})*\s*\]"
#: placeholders until the first run sets them (CHAT-CORPUS-v0: plain 6 s, triggered 12 s)
TTFT_BUDGET_MS = {"plain": 6000, "triggered": 12000}
SESSION_PREFIX = "chat-corpus-"
LOOP_MARKER = "[[loop"
#: page calls into these namespaces start or change work whatever their verb - never run from a corpus turn
STUB_PREFIXES = ("dag.", "workshop.", "loops.", "evolve.", "sys.", "exec.", "census.", "background.", "board.",
                 "telegram.", "delivery.", "notify.", "ide.", "provision.", "autoenroll.", "secrets.", "build.")
#: the page's loop hand-off (chat_panel _dispatchServerLoop / the loop pane's message box)
LOOP_PATHS = ("/workshop/agent_loop/stream", "/workshop/agent_loop/message")


# ── cases ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def load_cases(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The corpus' cases, checked: ids unique, route / acquire / canvas known. ValueError names the first bad one."""
    cases = list((doc or {}).get("cases") or [])
    if not cases:
        raise ValueError("the corpus has no cases")
    seen = set()
    domains = set(SC.DOMAIN)
    out = []
    for c in cases:
        cid = c.get("id")
        if not isinstance(cid, int) or cid in seen:
            raise ValueError(f"case id {cid!r} is missing or repeated")
        seen.add(cid)
        if not str(c.get("text") or "").strip():
            raise ValueError(f"case {cid} has no text")
        if c.get("route") not in ROUTES:
            raise ValueError(f"case {cid}: route {c.get('route')!r} is not one of {ROUTES}")
        if c.get("acquire", "none") not in ACQUIRES:
            raise ValueError(f"case {cid}: acquire {c.get('acquire')!r} is not one of {ACQUIRES}")
        if c.get("canvas") and c["canvas"] not in domains:
            raise ValueError(f"case {cid}: canvas domain {c['canvas']!r} is not one of {sorted(domains)}")
        for k in ("mentions", "not_mentions"):
            if not isinstance(c.get(k, []), list) or not all(str(t).strip() for t in c.get(k, [])):
                raise ValueError(f"case {cid}: {k} must be a list of terms")
        for k in ("tools_any", "tools_none"):
            if not isinstance(c.get(k, []), list) or not all(str(t).strip() for t in c.get(k, [])):
                raise ValueError(f"case {cid}: {k} must be a list of capability names ('*' = any)")
        minds = c.get("minds") or {}
        if not isinstance(minds, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in minds.items()):
            raise ValueError(f"case {cid}: minds must map switch ids to modes")
        out.append({"id": cid, "text": str(c["text"]), "route": c["route"], "acquire": c.get("acquire", "none"),
                    "mentions": [str(t) for t in c.get("mentions", [])],
                    "not_mentions": [str(t) for t in c.get("not_mentions", [])],
                    "canvas": str(c.get("canvas") or ""), "fixture": str(c.get("fixture") or ""),
                    # info-seeking eval (cases_info_v1): which tools the turn should / must not call, and whether
                    # the reply cites numbered sources - optional, so the v0 corpus judges exactly as before
                    "tools_any": [str(t) for t in c.get("tools_any", [])],
                    "tools_none": [str(t) for t in c.get("tools_none", [])],
                    "cites": bool(c.get("cites")), "label": str(c.get("label") or ""),
                    "kind": str(c.get("kind") or ""),
                    # switchboard modes for this case's chat request (the corpus' arms, §5.4), e.g. memory on for the
                    # fixture cases; recorded with the result
                    "minds": dict(c.get("minds") or {})})
    return out


def select(cases: Sequence[Dict[str, Any]], only: str = "") -> List[Dict[str, Any]]:
    """`only` = "1,2,27" or "11-15" or a mix; empty = every case."""
    if not str(only or "").strip():
        return list(cases)
    want = set()
    for part in str(only).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            want.update(range(int(a), int(b) + 1))
        elif part:
            want.add(int(part))
    return [c for c in cases if c["id"] in want]


def session_id(run_id: str, case_id: int) -> str:
    return f"{SESSION_PREFIX}{run_id}-{case_id}"


def fixture_session_id(run_id: str, case_id: int) -> str:
    """The memory session a case's fixture is written to (a `chat-corpus-` id, so only the corpus' own are touched)."""
    return f"{SESSION_PREFIX}{run_id}-{case_id}-fixture"


def fixture_turn(fixture: str) -> Dict[str, str]:
    """The fixture as one remembered exchange (memory.record_turn arguments): what was said, and its acknowledgement."""
    text = " ".join(str(fixture or "").split())
    return {"human_text": f"Please remember this for later: {text}.",
            "ai_text": f"Noted - I'll remember that {text}."}


def canvas_id(sid: str) -> str:
    """The session canvas' id, as the chat page derives it (chat_panel: 'cv_session_' + the cleaned session id)."""
    clean = "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in str(sid))
    return "cv_session_" + clean[:64]


def ours(sid: str) -> bool:
    """Only the corpus' own sessions may be deleted after a run."""
    return str(sid or "").startswith(SESSION_PREFIX) and len(str(sid)) > len(SESSION_PREFIX)


# ── what the page may run ────────────────────────────────────────────────────────────────────────────────────────

def effect(name: str) -> bool:
    """True when a page call must be stubbed ("would call"): it changes something, or starts work."""
    n = str(name or "").strip()
    if not n:
        return True
    return n.startswith(STUB_PREFIXES) or not SC.is_read(n)


def stub_result(name: str) -> Dict[str, Any]:
    return {"ok": True, "would_call": True, "stub": "chat-corpus",
            "note": f"{name} was not called: a chat corpus run records effects instead of making them"}


def is_loop_request(method: str, url: str) -> bool:
    path = str(url or "").split("?", 1)[0]
    return str(method or "").upper() == "POST" and any(path.endswith(p) for p in LOOP_PATHS)


# ── reading the turn ─────────────────────────────────────────────────────────────────────────────────────────────

def parse_sse(text: str) -> Dict[str, Any]:
    """The turn's stream: reply (token text joined), frames (the phase frames), types (frame counts), error."""
    reply: List[str] = []
    frames: List[Dict[str, Any]] = []
    types: Dict[str, int] = {}
    error = ""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = str(obj.get("type") or "")
        types[kind] = types.get(kind, 0) + 1
        if kind == "token":
            reply.append(str(obj.get("text") or ""))
        elif kind == "phase":
            frames.append(obj)
        elif kind == "error" and not error:
            error = str(obj.get("error") or obj.get("message") or obj.get("text") or "error")[:300]
    return {"reply": "".join(reply), "frames": frames, "types": types, "error": error}


def acquired(frames: Iterable[Dict[str, Any]]) -> List[str]:
    """Which context the turn went for before the model: web and/or memory (the turn record's 'triggered' rule)."""
    got = {f.get("id") for f in frames or []
           if f.get("stage") == "acquire" and f.get("id") in ("web", "memory")
           and f.get("state") in ("start", "done", "late", "fail")}
    return sorted(g for g in got if g)


def _panel_domain(panel: str) -> str:
    p = str(panel or "").lower()
    for rx, dom in getattr(SC, "_PANEL_DOMAIN", []):
        if rx.search(p):
            return dom
    return ""


def item_domain(block: Dict[str, Any]) -> str:
    """One canvas item's domain: from its key (turn.prepare keys reads 'enrich:<cap>[:h]' and panels
    'pre:panel:<id>'), else from the capability or panel its content names."""
    key = str((block or {}).get("key") or "")
    content = (block or {}).get("content")
    content = content if isinstance(content, dict) else {}
    if key.startswith("enrich:"):
        return SC.domain_of(key.split(":")[1])
    if key.startswith("pre:panel:"):
        return _panel_domain(key[len("pre:panel:"):])
    cap = content.get("cap") or content.get("capability") or ""
    if cap:
        return SC.domain_of(str(cap))
    if content.get("panel"):
        return _panel_domain(str(content["panel"]))
    return ""


def canvas_domains(doc: Optional[Dict[str, Any]]) -> List[str]:
    blocks = (doc or {}).get("blocks") or []
    return sorted({d for d in (item_domain(b) for b in blocks if isinstance(b, dict)) if d})


def _has(reply_l: str, term: str) -> bool:
    return any(alt.strip().lower() in reply_l for alt in str(term).split("|") if alt.strip())


# ── judging ──────────────────────────────────────────────────────────────────────────────────────────────────────

def evaluate(case: Dict[str, Any], ev: Dict[str, Any]) -> Dict[str, Any]:
    """One case's result: status pass | fail | skipped | error, its checks, the turn record."""
    out: Dict[str, Any] = {"id": case["id"], "text": case["text"]}
    ev = ev or {}
    if case.get("fixture") and not ev.get("fixture_seeded"):
        return dict(out, status="skipped", reason=ev.get("fixture_error") or "fixture not seeded", checks=[])
    if case.get("minds"):
        out["minds"] = dict(case["minds"])
    if case.get("fixture"):
        out["fixture"] = "seeded"
    s = parse_sse(ev.get("stream") or "")
    reply = s["reply"]
    loop_reqs = list(ev.get("loop") or [])
    route = "loop" if (loop_reqs or LOOP_MARKER in reply) else "chat"
    record = TP.turn_record(s["frames"], agent=str(ev.get("agent") or "assistant"),
                            elapsed_ms=int(ev.get("elapsed_ms") or 0), chars=len(reply),
                            ok=not (ev.get("error") or s["error"]))
    acq = acquired(s["frames"])
    domains = canvas_domains(ev.get("canvas"))
    calls = list(ev.get("calls") or [])
    out.update(route=route, acquired=acq, canvas_domains=domains, reply=reply[:4000], record=record,
               client_ttft_ms=ev.get("client_ttft_ms"), elapsed_ms=int(ev.get("elapsed_ms") or 0),
               would_call=[c.get("name") for c in calls if c.get("would_call")],
               called=[c.get("name") for c in calls if not c.get("would_call")],
               loop_requests=len(loop_reqs))
    err = str(ev.get("error") or s["error"] or "")
    if err or (not reply and route == "chat"):
        return dict(out, status="error", reason=err or "no reply", checks=[])

    checks: List[Dict[str, Any]] = [{"check": "route", "want": case["route"], "got": route,
                                     "ok": route == case["route"]}]
    want_acq = case.get("acquire", "none")
    if want_acq != "any":
        acq_ok = (not acq) if want_acq == "none" else (want_acq in acq)
        checks.append({"check": "acquire", "want": want_acq, "got": ",".join(acq) or "none", "ok": acq_ok})
    used = [c.get("name") for c in calls if c.get("name")]
    if case.get("tools_any"):
        hit = [n for n in used if n in case["tools_any"]]
        checks.append({"check": "tools_any", "want": case["tools_any"], "got": ",".join(used) or "none",
                       "ok": bool(hit)})
    if case.get("tools_none"):
        bad = [n for n in used if "*" in case["tools_none"] or n in case["tools_none"]]
        checks.append({"check": "tools_none", "want": case["tools_none"], "got": ",".join(bad) or "none",
                       "ok": not bad})
    if case.get("cites"):
        import re as _re
        checks.append({"check": "cites", "want": "[n] citations", "got": len(_re.findall(CITE_RE, reply)),
                       "ok": bool(_re.search(CITE_RE, reply))})
    low = reply.lower()
    if case.get("mentions"):
        missing = [t for t in case["mentions"] if not _has(low, t)]
        checks.append({"check": "mentions", "want": case["mentions"], "got": missing and f"missing {missing}" or "all",
                       "ok": not missing})
    if case.get("not_mentions"):
        present = [t for t in case["not_mentions"] if _has(low, t)]
        checks.append({"check": "not_mentions", "want": case["not_mentions"],
                       "got": present and f"present {present}" or "none", "ok": not present})
    if case.get("canvas"):
        checks.append({"check": "canvas", "want": case["canvas"], "got": domains, "ok": case["canvas"] in domains})
    if route == "chat":
        budget = TTFT_BUDGET_MS.get(record["kind"], TTFT_BUDGET_MS["plain"])
        t = record.get("ttft_ms")
        checks.append({"check": "ttft_ms", "want": budget, "got": t, "ok": t is not None and t <= budget,
                       "advisory": True})
    hard = [c for c in checks if not c.get("advisory")]
    return dict(out, status="pass" if all(c["ok"] for c in hard) else "fail", checks=checks)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = max(0, min(len(v) - 1, int(math.ceil(q * len(v))) - 1))
    return v[k]


def summarise(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    status: Dict[str, int] = {}
    per_check: Dict[str, Dict[str, int]] = {}
    ttft: Dict[str, List[int]] = {"plain": [], "triggered": []}
    for r in results:
        status[r["status"]] = status.get(r["status"], 0) + 1
        for c in r.get("checks") or []:
            pc = per_check.setdefault(c["check"], {"ok": 0, "total": 0})
            pc["total"] += 1
            pc["ok"] += 1 if c["ok"] else 0
        rec = r.get("record") or {}
        if r["status"] in ("pass", "fail") and r.get("route") == "chat" and rec.get("ttft_ms") is not None:
            ttft.setdefault(rec.get("kind") or "plain", []).append(int(rec["ttft_ms"]))
    judged = status.get("pass", 0) + status.get("fail", 0)
    return {"cases": len(results), "status": status,
            "pass_rate": round(status.get("pass", 0) / judged, 3) if judged else None,
            "checks": {k: per_check[k] for k in CHECKS if k in per_check},
            "ttft_ms": {k: {"n": len(v), "p50": percentile(v, 0.5), "p90": percentile(v, 0.9)}
                        for k, v in ttft.items()},
            "failed": [{"id": r["id"], "checks": [c["check"] for c in r.get("checks") or []
                                                  if not c["ok"] and not c.get("advisory")]}
                       for r in results if r["status"] == "fail"],
            "errors": [{"id": r["id"], "reason": r.get("reason", "")} for r in results if r["status"] == "error"]}


def render_md(run: Dict[str, Any], summary: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> str:
    st = summary["status"]
    lines = [f"# Chat corpus {run.get('corpus', 'v0')} · run {run.get('run_id', '')}", "",
             f"target {run.get('target', '')} · {summary['cases']} cases · pass {st.get('pass', 0)} · "
             f"fail {st.get('fail', 0)} · error {st.get('error', 0)} · skipped {st.get('skipped', 0)}"
             + (f" · pass rate {summary['pass_rate']:.0%}" if summary.get("pass_rate") is not None else ""), "",
             "| check | ok / total |", "|---|---|"]
    for k, v in summary["checks"].items():
        lines.append(f"| {k}{' (advisory)' if k == 'ttft_ms' else ''} | {v['ok']} / {v['total']} |")
    lines += ["", "TTFT (ms): " + " · ".join(f"{k} n={v['n']} p50={v['p50']} p90={v['p90']}"
                                           for k, v in summary["ttft_ms"].items()), "",
              "| # | status | route | acquired | canvas | ttft | failed checks / reason |", "|---|---|---|---|---|---|---|"]
    for r in results:
        bad = ", ".join(f"{c['check']} (want {c['want']}, got {c['got']})" for c in r.get("checks") or []
                        if not c["ok"] and not c.get("advisory"))
        rec = r.get("record") or {}
        lines.append(f"| {r['id']} | {r['status']} | {r.get('route', '')} | {','.join(r.get('acquired') or []) or '-'} | "
                     f"{','.join(r.get('canvas_domains') or []) or '-'} | {rec.get('ttft_ms', '')} | "
                     f"{(bad or r.get('reason', '')).replace('|', '/')} |")
    return "\n".join(lines) + "\n"
