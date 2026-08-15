# ============================================================================
# board_capabilities.py — the board.* work-plane capabilities (Stage 1, files)
# ============================================================================
#
# Agents write through THESE caps, never the raw forge/file — so ownership,
# quota discipline and secret redaction are properties of the system, not of
# every agent remembering the rule (agentic swarm.md §3.1, §6.2). This first
# slice is backed by the FileBoardProvider (the always-available file tier);
# GitHub/Gitea providers slot in behind the same seam in later stages.
# ============================================================================

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from typing import List, Optional

from Vera.vera.capability_orchestration import APP, capability, CAPABILITY_REGISTRY, emit_event
from Vera.vera.board import board_core as bc
from Vera.vera.sandbox_guard import write_blocked as _sbx_write_blocked


async def _call(name: str, **kw):
    reg = CAPABILITY_REGISTRY.get(name) or {}
    fn = reg.get("func")
    if not fn:
        return None
    try:
        return await fn(**kw)
    except Exception as e:
        log.debug("board dispatch: %s failed: %s", name, e)
        return None


async def _dispatch_run(item_id: str, executor: str, agent: str, target: str,
                        seat: Optional[dict], check: str, session_id: str) -> None:
    """The heavy half of board.dispatch, run as a BACKGROUND task so a caller's
    timeout can never cancel it mid-flight (begin + a secret-scanned commit is
    slow). Drives begin → record → execute → gate, reflecting progress on the
    board the whole way. Never raises — failures land the item in 'blocked'."""
    try:
        prov = provider()
        spawn = executor in ("vera", "capable")
        beg = await _call("evolve.pipeline.begin", title=(await prov.get(item_id)).title,
                          spawn=spawn, repo=((await prov.get(item_id)).repo or "vera"),
                          session_id=session_id)
        if not (beg and beg.get("ok")):
            await prov.move(item_id, "blocked")
            await prov.comment(item_id, bc.AgentMessage(kind="blocked", frm=agent, item=item_id,
                               body="evolve.pipeline.begin failed — cannot dispatch."))
            return
        branch, pid = beg.get("branch", ""), beg.get("id", "")

        it = await prov.get(item_id)
        it.branch, it.pipeline, it.executor = branch, pid, executor
        it.session = session_id or it.session
        if seat:
            it.model = seat.get("seat_id", "")
        it.lane = "in_progress"
        await prov.upsert(it)
        await prov.comment(item_id, bc.AgentMessage(
            kind="progress", frm=agent, item=item_id,
            body=f"dispatched · executor={executor} · pipeline {pid} · branch {branch}"
                 + (f" · seat {seat['seat_id']}@{target}" if seat else "")))
        await emit_event({"type": "board.dispatch", "id": item_id, "executor": executor,
                          "pipeline": pid, "branch": branch, "target": target})

        ok = False
        if executor == "deterministic":
            work = check or ("printf '%s\\n' " + shlex.quote("dispatched: " + it.title)
                             + " >> DISPATCH_NOTE.md && "
                             "git -c user.name=BoeJaker -c user.email=boejaker80@gmail.com "
                             "add DISPATCH_NOTE.md && "
                             "git -c user.name=BoeJaker -c user.email=boejaker80@gmail.com "
                             "commit -q -m " + shlex.quote(f"board.dispatch: {it.title[:60]}")
                             + " && echo VERA_DISPATCH_OK")
            ex = await _call("evolve.sandbox.exec", where="worktree", branch=branch, cmd=work)
            ok = bool(ex and (ex.get("code") == 0))
        else:
            engine = "vera-agent" if executor == "vera" else "claude"
            # Hand the agent the FULL context — the umbrella plan + this item + its
            # siblings (§0.3) — so it sees the code (repo/branch), the plan, and the
            # part it plays, not just a bare title.
            ctx = await _call("board.context", id=item_id)
            brief = (ctx.get("context") if (ctx and ctx.get("ok")) else
                     (it.title + "\n\n" + (it.body or "")))
            run = await _call("ide.remote.run", instance_id=target, task=brief[:8000],
                              engine=engine, session_id=session_id, source="dispatch")
            ok = bool(run and run.get("ok"))

        if ok:
            await prov.move(item_id, "needs_review")
            await prov.comment(item_id, bc.AgentMessage(
                kind="progress", frm=agent, item=item_id,
                body=f"work done → needs_review. A human promotes pipeline {pid} to land (§10.1)."))
        else:
            await prov.move(item_id, "blocked")
            await prov.comment(item_id, bc.AgentMessage(
                kind="blocked", frm=agent, item=item_id, body=f"dispatch failed — see pipeline {pid}."))
        await emit_event({"type": "board.dispatch.done", "id": item_id, "ok": ok, "pipeline": pid})
    except Exception as e:
        log.error("board.dispatch background run for %s failed: %s", item_id, e)
        try:
            await provider().move(item_id, "blocked")
            await provider().comment(item_id, bc.AgentMessage(
                kind="blocked", frm=agent, item=item_id, body=f"dispatch crashed: {e}"))
        except Exception:
            pass

log = logging.getLogger("vera.board")

# The fabric dataset the board projects into (§6.3) — derived, rebuilt each
# index, never authoritative.
_INDEX_DATASET = "board_index"

# ── active provider (file tier for now) ─────────────────────────────────────
_PROVIDER: Optional[bc.FileBoardProvider] = None


def provider() -> bc.FileBoardProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = bc.FileBoardProvider()   # → state_paths.board_dir() (out of tree)
    return _PROVIDER


# ── serialization at the cap boundary ───────────────────────────────────────
def _msg_dict(m: bc.AgentMessage) -> dict:
    return {"kind": m.kind, "from": m.frm, "to": m.to, "item": m.item,
            "hops": m.hops, "done_when": m.done_when, "id": m.id,
            "created_at": m.created_at, "body": m.body}


def _item_dict(it: bc.BoardItem, full: bool = False) -> dict:
    d = {"id": it.id, "title": it.title, "lane": it.lane, "labels": it.labels,
         "agent": it.agent, "repo": it.repo, "project": it.project, "plan": it.plan,
         "branch": it.branch, "pipeline": it.pipeline, "sync_sig": it.sync_sig,
         "session": it.session, "executor": it.executor, "model": it.model,
         "reviewed_by": it.reviewed_by, "heartbeat": it.heartbeat,
         "hops": it.hops, "created_at": it.created_at, "updated_at": it.updated_at,
         "comment_count": len(it.comments)}
    if full:
        d["body"] = it.body
        d["comments"] = [_msg_dict(c) for c in it.comments]
    return d


# ── secret scan on the write path (§6.2 / §7) ───────────────────────────────
# On the private file board this is defence in depth; it becomes the ONLY thing
# between an agent's pasted traceback and the internet on the public GitHub
# provider (Stage 2), where it must be wired to the full secret_scan. Kept
# conservative here so it never false-positives on ordinary prose — only
# unambiguous secrets are blocked.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
]


def _scan_secret(text: str) -> str:
    """Return a short reason string if `text` contains an unambiguous secret,
    else "". A private board makes this defence in depth; Stage 2 replaces it
    with the repo's full secret_scan on the public write path."""
    for pat in _SECRET_PATTERNS:
        if pat.search(text or ""):
            return f"blocked: comment appears to contain a secret ({pat.pattern[:24]}…)"
    return ""


if True:  # capability registration (mirrors the guard style of the other modules)

    @capability(
        "board.items", http_method="GET", http_path="/board/items",
        http_tags=["board"], memory="off", silent=True,
        description="List work-plane items (compact), newest storage order. Filter with "
                    "lane, label, agent, mentions, text. Output: {ok, items:[{id,title,lane,"
                    "labels,agent,branch,pipeline,session,comment_count,...}], count}.",
    )
    async def cap_board_items(lane: str = "", label: str = "", agent: str = "",
                              repo: str = "", project: str = "",
                              mentions: str = "", text: str = "", trace_id=None) -> dict:
        q = bc.BoardQuery(lane=lane, label=label, agent=agent, repo=repo,
                          project=project, mentions=mentions, text=text)
        items = await provider().items(q)
        return {"ok": True, "items": [_item_dict(i) for i in items], "count": len(items),
                "provider": provider().name}

    @capability(
        "board.item.get", http_method="GET", http_path="/board/item",
        http_tags=["board"], memory="off", silent=True,
        description="Full record of one item incl. body + the comment thread. Input: id (str!). "
                    "Output: {ok, item:{...,body,comments:[{kind,from,to,done_when,body,...}]}}.",
    )
    async def cap_board_item_get(id: str = "", trace_id=None) -> dict:
        it = await provider().get(id)
        if it is None:
            return {"ok": False, "error": f"no such item: {id}"}
        return {"ok": True, "item": _item_dict(it, full=True)}

    @capability(
        "board.item.upsert", http_method="POST", http_path="/board/item/upsert",
        http_tags=["board"], memory="on",
        description="Create or update an item (structure begins at promotion — see the notepad "
                    "capture plane for messy braindumps). Inputs: id (str — blank to create), "
                    "title, lane (inbox|ready|in_progress|blocked|needs_review|review|done|dropped|"
                    "queued_vera|in_progress_vera), body, labels (list — route+needs:*), agent, "
                    "repo (WHERE it lands — an evolve.repo.add id; blank = Vera), project (the "
                    "effort it belongs to, may be non-code), branch, pipeline, session, executor "
                    "(deterministic|vera|capable), model, reviewed_by, hops (int). Comments are "
                    "preserved. Output: {ok, item}.",
    )
    async def cap_board_item_upsert(id: str = "", title: str = "", lane: str = "inbox",
                                    body: str = "", labels: Optional[List[str]] = None,
                                    agent: str = "", repo: str = "", project: str = "",
                                    plan: str = "", branch: str = "", pipeline: str = "",
                                    session: str = "", executor: str = "", model: str = "",
                                    reviewed_by: str = "", hops: int = 0, trace_id=None) -> dict:
        if lane not in bc.LANE_SET:
            return {"ok": False, "error": f"unknown lane: {lane}", "lanes": bc.LANES}
        it = bc.BoardItem(
            id=id or "", title=title, lane=lane, body=body,
            labels=list(labels or []), agent=agent, repo=repo, project=project,
            plan=plan, branch=branch, pipeline=pipeline,
            session=session, executor=executor, model=model, reviewed_by=reviewed_by,
            hops=int(hops or 0))
        saved = await provider().upsert(it)
        return {"ok": True, "item": _item_dict(saved, full=True)}

    @capability(
        "board.item.move", http_method="POST", http_path="/board/item/move",
        http_tags=["board"], memory="on",
        description="Move an item to a lane (blocked/dropped are lanes, not deletions). "
                    "Inputs: id (str!), lane (str!). Output: {ok, item}.",
    )
    async def cap_board_item_move(id: str = "", lane: str = "", trace_id=None) -> dict:
        if lane not in bc.LANE_SET:
            return {"ok": False, "error": f"unknown lane: {lane}", "lanes": bc.LANES}
        try:
            it = await provider().move(id, lane)
        except KeyError:
            return {"ok": False, "error": f"no such item: {id}"}
        return {"ok": True, "item": _item_dict(it)}

    @capability(
        "board.claim", http_method="POST", http_path="/board/claim",
        http_tags=["board"], memory="on",
        description="Claim an item as a LEASE (§3.3): posts a claim comment, takes the "
                    "agent:<name> label, moves to in_progress, stamps a heartbeat. If an EARLIER "
                    "claim from another agent exists you lose — a withdraw is posted and the item "
                    "stays theirs (lowest comment id wins). Inputs: id (str!), agent (str!). "
                    "Output: {ok, held_by, lost, comment_id, reason}.",
    )
    async def cap_board_claim(id: str = "", agent: str = "", trace_id=None) -> dict:
        if not agent:
            return {"ok": False, "error": "agent is required (the claiming identity)"}
        try:
            r = await provider().claim(id, agent)
        except KeyError:
            return {"ok": False, "error": f"no such item: {id}"}
        return {"ok": r.ok, "held_by": r.held_by, "lost": r.lost,
                "comment_id": r.comment_id, "reason": r.reason}

    @capability(
        "board.comment", http_method="POST", http_path="/board/comment",
        http_tags=["board"], memory="on",
        description="Post an agent-msg comment on an item (the comms plane). A secret scan runs "
                    "on the write path. Inputs: id (str!), frm (str! — the from: identity), "
                    "kind (claim|withdraw|handoff|help-request|reply|progress|blocked|steer|"
                    "resume|note), to (str='any'), body (str — point at file:line, never a dump), "
                    "done_when (str — mandatory when asking another agent to act), hops (int). "
                    "Output: {ok, comment_id}.",
    )
    async def cap_board_comment(id: str = "", frm: str = "", kind: str = "note",
                                to: str = "any", body: str = "", done_when: str = "",
                                hops: int = 0, trace_id=None) -> dict:
        if not frm:
            return {"ok": False, "error": "frm is required — the from: identity (§3.1)"}
        if kind not in bc.ENVELOPE_KINDS:
            return {"ok": False, "error": f"unknown kind: {kind}", "kinds": sorted(bc.ENVELOPE_KINDS)}
        reason = _scan_secret(body) or _scan_secret(done_when)
        if reason:
            return {"ok": False, "error": reason}
        msg = bc.AgentMessage(kind=kind, frm=frm, to=to or "any", item=id,
                              hops=int(hops or 0), done_when=done_when, body=body)
        try:
            cid = await provider().comment(id, msg)
        except KeyError:
            return {"ok": False, "error": f"no such item: {id}"}
        return {"ok": True, "comment_id": cid}

    @capability(
        "board.inbox", http_method="GET", http_path="/board/inbox",
        http_tags=["board"], memory="off", silent=True,
        description="An agent's inbox as a QUERY (not a file): items labelled agent:<me>, items "
                    "mentioning me, and unassigned needs:help. Input: agent (str!). "
                    "Output: {ok, items, count}.",
    )
    async def cap_board_inbox(agent: str = "", trace_id=None) -> dict:
        if not agent:
            return {"ok": False, "error": "agent is required"}
        items = await provider().inbox(agent)
        return {"ok": True, "items": [_item_dict(i) for i in items], "count": len(items)}

    @capability(
        "board.help", http_method="POST", http_path="/board/help",
        http_tags=["board"], memory="on",
        description="Raise a help request on an item: adds the needs:help label and posts a "
                    "help-request comment. Inputs: id (str!), frm (str!), body (str), "
                    "done_when (str — what 'helped' means), to (str='any'). Output: {ok, comment_id}.",
    )
    async def cap_board_help(id: str = "", frm: str = "", body: str = "",
                             done_when: str = "", to: str = "any", trace_id=None) -> dict:
        if not frm:
            return {"ok": False, "error": "frm is required"}
        it = await provider().get(id)
        if it is None:
            return {"ok": False, "error": f"no such item: {id}"}
        if "needs:help" not in it.labels:
            it.labels.append("needs:help")
            await provider().upsert(it)
        reason = _scan_secret(body)
        if reason:
            return {"ok": False, "error": reason}
        cid = await provider().comment(id, bc.AgentMessage(
            kind="help-request", frm=frm, to=to or "any", item=id,
            done_when=done_when, body=body))
        return {"ok": True, "comment_id": cid}

    @capability(
        "board.provider", http_method="GET", http_path="/board/provider",
        http_tags=["board"], memory="off", silent=True,
        description="The active board provider and where it stores state. Output: {ok, active, "
                    "root, degraded}. (Only the file tier exists in Stage 1; GitHub/Gitea land "
                    "behind the same seam later.)",
    )
    async def cap_board_provider(trace_id=None) -> dict:
        p = provider()
        return {"ok": True, "active": p.name, "root": str(p.root), "degraded": False}

    @capability(
        "board.import_plan", http_method="POST", http_path="/board/import_plan",
        http_tags=["board"], memory="on",
        description="Pull an EXISTING structured plan (a markdown doc) into the board. By default "
                    "(group=true) it SEPARATES overall CONTEXT from discrete WORK: it creates one "
                    "umbrella PLAN item (id 'plan-<project>', holding the title + all the why/"
                    "overview/principles/decisions sections — the guidance that applies to every "
                    "step) and WORK items for the actionable sections (phase/stage/task/…, or any "
                    "with a checklist), each tagged plan=<umbrella id> so they group under it. "
                    "Context sections do NOT clutter the board as fake work items. group=false "
                    "restores the old flat one-item-per-heading behaviour. Deterministic ids "
                    "(re-import UPDATES in place). Inputs: path (file, absolute or repo-relative) OR "
                    "text (inline md), project (str!), repo (str=vera), level (int=2), lane "
                    "(str=inbox), group (bool=true). Output: {ok, project, plan, work_count, "
                    "context_count, ids}.",
    )
    async def cap_board_import_plan(path: str = "", text: str = "", project: str = "",
                                    repo: str = "vera", level: int = 2, lane: str = "inbox",
                                    group: bool = True, labels: Optional[List[str]] = None,
                                    trace_id=None) -> dict:
        if not project:
            return {"ok": False, "error": "project is required — the effort these items belong to"}
        if lane not in bc.LANE_SET:
            return {"ok": False, "error": f"unknown lane: {lane}", "lanes": bc.LANES}
        body_text = text
        if not body_text and path:
            p = os.path.expanduser(path)
            cands = [p]
            if not os.path.isabs(p):
                try:
                    from Vera.vera import state_paths
                    cands.append(str(state_paths.repo_root() / p))
                except Exception:
                    pass
            for cand in cands:
                try:
                    with open(cand, "r", encoding="utf-8", errors="replace") as fh:
                        body_text = fh.read()
                    break
                except Exception:
                    continue
            if not body_text:
                return {"ok": False, "error": f"could not read plan file (tried: {cands})"}
        if not (body_text or "").strip():
            return {"ok": False, "error": "provide a non-empty `text` or a readable `path`"}

        async def _upsert_row(r: dict):
            it = bc.BoardItem(id=r["id"], title=r["title"], lane=r["lane"], body=r["body"],
                              labels=list(r.get("labels") or []), project=r.get("project", ""),
                              repo=r.get("repo", ""), plan=r.get("plan", ""))
            return (await provider().upsert(it)).id

        if not group:
            rows = bc.parse_plan_items(body_text, project=project, repo=repo,
                                       level=int(level or 2), lane=lane,
                                       labels=list(labels) if labels else None)
            if not rows:
                return {"ok": True, "imported": 0, "project": project,
                        "note": f"no level-{level} headings found — try a different level"}
            ids = [await _upsert_row(r) for r in rows]
            return {"ok": True, "project": project, "imported": len(ids), "grouped": False, "ids": ids}

        parsed = bc.parse_plan(body_text, project=project, repo=repo,
                               level=int(level or 2), lane=lane)
        if not parsed["work_items"] and parsed["context_count"] == 0:
            return {"ok": True, "imported": 0, "project": project,
                    "note": f"no level-{level} headings found — try a different level"}
        plan_id = await _upsert_row(parsed["plan_item"])
        work_ids = [await _upsert_row(r) for r in parsed["work_items"]]
        return {"ok": True, "project": project, "grouped": True, "plan": plan_id,
                "work_count": len(work_ids), "context_count": parsed["context_count"],
                "ids": [plan_id] + work_ids}

    @capability(
        "board.dispatch", http_method="POST", http_path="/board/dispatch",
        http_tags=["board"], memory="on",
        description="ORCHESTRATE one board item through the SAME pipeline used to build Vera "
                    "(§0.2 #1): claim it → evolve.pipeline.begin (typed branch + worktree + "
                    "[for model executors] a dev sandbox + a CI/CD pipeline record) → run the "
                    "executor → move to needs_review (HITL: a human always promotes, §10.1) or "
                    "blocked on failure. Records branch/pipeline/executor on the item + announces "
                    "each step. Executors (§5): deterministic (no model — runs `check` or a marker "
                    "change in the worktree, host-side), vera (local Ollama via ide.remote.run, "
                    "needs a target), capable (Claude — picks an AVAILABLE pinned (target,seat) "
                    "from capacity.status, REFUSES if none rather than downgrading, §6.5). Inputs: "
                    "id (str!), executor (deterministic|vera|capable, default deterministic), "
                    "agent (str='orchestrator'), instance_id (str — target for vera; overrides the "
                    "seat's target for capable), check (str — deterministic command), session_id "
                    "(str). Output: {ok, item, lane, executor, pipeline, branch, worktree, url}.",
    )
    async def cap_board_dispatch(id: str = "", executor: str = "deterministic",
                                 agent: str = "orchestrator", instance_id: str = "",
                                 check: str = "", session_id: str = "", trace_id=None) -> dict:
        it = await provider().get(id)
        if it is None:
            return {"ok": False, "error": f"no such item: {id}"}
        if it.lane in ("done", "dropped", "needs_review", "review"):
            return {"ok": False, "error": f"item is '{it.lane}' — nothing to dispatch"}
        executor = (executor or "deterministic").lower()
        if executor not in ("deterministic", "vera", "capable"):
            return {"ok": False, "error": f"unknown executor: {executor}"}

        # Resolve target/seat for model executors (refuse, don't downgrade — §6.5).
        seat = None
        target = instance_id
        if executor == "capable":
            cst = await _call("capacity.status") or {}
            free = [s for s in (cst.get("seats") or [])
                    if s.get("state") == "available" and s.get("free", 0) > 0]
            if not free:
                return {"ok": False, "error": "no available Claude seat — leaving the item "
                        "queued (refuse, don't downgrade §6.5). Enrol a seat: capacity.seat.register.",
                        "need": "seat"}
            seat = free[0]
            target = instance_id or seat.get("target")
        if executor in ("vera", "capable") and not target:
            return {"ok": False, "error": f"executor={executor} needs a target instance_id "
                    "(or a pinned seat's target)"}

        # Claim (the §3.3 lease) — this moves the item to in_progress + posts a
        # claim comment, so the board reflects the dispatch immediately.
        claim = await provider().claim(id, agent)
        if not claim.ok:
            return {"ok": False, "error": "claim lost to another agent", "held_by": claim.held_by}

        # The heavy work (begin + worktree + a secret-scanned commit / agent run)
        # is SLOW; run it in the BACKGROUND so a caller's timeout can't cancel it
        # mid-flight and strand the item. The board reflects progress; poll it.
        asyncio.ensure_future(_dispatch_run(id, executor, agent, target, seat, check, session_id))
        return {"ok": True, "dispatched": True, "item": id, "executor": executor,
                "target": target, "status": "running",
                "note": "dispatched in the background — poll board.item / board.items; the "
                        "item moves in_progress → needs_review (a human then promotes) or blocked."}

    @capability(
        "board.context", http_method="GET", http_path="/board/context",
        http_tags=["board"], memory="off", silent=True,
        description="Assemble the FULL context to hand an agent working an item: the umbrella "
                    "PLAN's overall guidance (if the item has plan=), this item's own body + "
                    "done-when, its repo/branch pointers, and the sibling work items in the same "
                    "plan (so it knows the part it plays). This is what makes a dispatched task "
                    "see 'the code, the plan, and its part'. Input: id (str!). Output: {ok, plan, "
                    "context}.",
    )
    async def cap_board_context(id: str = "", trace_id=None) -> dict:
        it = await provider().get(id)
        if it is None:
            return {"ok": False, "error": f"no such item: {id}"}
        parts = [f"# Task: {it.title}"]
        ptrs = []
        if it.repo:
            ptrs.append(f"repo: {it.repo}")
        if it.branch:
            ptrs.append(f"branch: {it.branch}")
        if it.project:
            ptrs.append(f"project: {it.project}")
        if ptrs:
            parts.append(" · ".join(ptrs))
        plan = None
        if it.plan:
            plan = await provider().get(it.plan)
            if plan:
                parts.append(f"\n## Overall plan — {plan.title}\n\n{plan.body}")
        if (it.body or "").strip():
            parts.append(f"\n## This work item\n\n{it.body}")
        if it.plan:
            sibs = [s for s in await provider().items()
                    if s.plan == it.plan and s.id != it.id]
            if sibs:
                parts.append("\n## Sibling work items in this plan (the parts around yours)\n"
                             + "\n".join(f"- [{s.lane}] {s.title}" for s in sibs))
        return {"ok": True, "item": id, "plan": it.plan,
                "context": "\n".join(parts)}

    @capability(
        "board.index", http_method="POST", http_path="/board/index",
        http_tags=["board"], memory="on",
        description="(Re)build the fabric index of the board from files (+ board) so items are "
                    "queryable via fabric.query / memory.seek — 'what is blocked', 'what touched "
                    "the planner', 'what did agent B conclude'. DERIVED, never authoritative: "
                    "rebuilt wholesale each call (dataset '" + _INDEX_DATASET + "', keyed by id, "
                    "mode=replace), never written back. PROD-SIDE ONLY: fabric writes are "
                    "suppressed inside a dev sandbox (sandbox_guard), so an index built in a "
                    "sandbox is empty BY DESIGN — treat that as expected, not a finding (swarm "
                    "§6.3). Output: {ok, indexed, dataset, prod_side_only?, summary:{total,"
                    "by_lane,by_agent,blocked,needs_help_unclaimed}}.",
    )
    async def cap_board_index(trace_id=None) -> dict:
        items = await provider().items()
        summary = bc.summarize(items)
        # A dev sandbox must not write to the prod-shared fabric — and it must
        # say so, not silently look like an empty index (swarm §6.3).
        if _sbx_write_blocked():
            return {"ok": True, "indexed": 0, "prod_side_only": True,
                    "dataset": _INDEX_DATASET, "candidate_rows": len(items),
                    "note": "fabric writes suppressed in this dev sandbox; the board index is "
                            "built prod-side. An empty index here is expected, not a finding.",
                    "summary": summary}
        reg = CAPABILITY_REGISTRY.get("fabric.upsert") or {}
        func = reg.get("func")
        if not func:
            return {"ok": False, "error": "fabric.upsert unavailable — cannot index",
                    "summary": summary}
        if not items:
            return {"ok": True, "indexed": 0, "dataset": _INDEX_DATASET,
                    "summary": summary, "note": "no board items to index yet"}
        rows = [bc.index_row(it) for it in items]
        res = await func(dataset_id=_INDEX_DATASET, rows=rows, key="id",
                         mode="replace", source="board.index", tags="board,index")
        return {"ok": True, "indexed": len(rows), "dataset": _INDEX_DATASET,
                "summary": summary, "fabric": res}


    # â”€â”€ board.sync (M4, route-forward.md â€” Stage 2 start, local/no GitHub) â”€â”€
    # Reflects a linked pipeline's CURRENT state onto its board item(s), so the
    # board is the single work view instead of something cross-referenced by
    # hand against the CI/CD tab. Every board.dispatch already stamps
    # it.pipeline/it.branch (see _dispatch_run above) â€” this is the other half:
    # keeping the item's lane/comments in sync with what that pipeline is
    # ACTUALLY doing as it moves through adopt â†’ review â†’ promote.
    def _lane_for_pipeline(rec: dict) -> str:
        """Map a pipeline record's decision/gate/review state onto a board
        lane. decision is 'pending' throughout adopt/review, 'held' if the
        gate blocked promote or a merge conflicted, 'promoted' once merged
        (evolve_capabilities.py evolve_pipeline_promote) â€” variant pipelines
        use 'promoted' too (evolve_variant_promote path)."""
        decision = (rec.get("decision") or "pending").lower()
        if decision == "promoted":
            return "done"
        if decision == "held":
            return "blocked"
        if rec.get("review_requested"):
            return "needs_review"
        return "in_progress"

    async def _sync_one(it: "bc.BoardItem") -> dict:
        """Pull `it`'s linked pipeline onto the item. Idempotent via
        it.sync_sig (a fingerprint of decision/gate/review/lane) â€” a repeated
        sync with nothing changed is a cheap no-op, not a duplicate comment,
        so this is safe to call on a timer or after every board view load."""
        if not it.pipeline:
            return {"id": it.id, "synced": False, "reason": "no linked pipeline"}
        got = await _call("evolve.pipeline.get", id=it.pipeline)
        rec = (got or {}).get("pipeline")
        if not rec:
            return {"id": it.id, "synced": False,
                    "reason": (got or {}).get("error") or "pipeline not found"}
        new_lane = _lane_for_pipeline(rec)
        gate = rec.get("gate_passed")
        sig = f"{rec.get('decision')}|{gate}|{bool(rec.get('review_requested'))}|{new_lane}"
        if it.sync_sig == sig:
            return {"id": it.id, "synced": False, "reason": "unchanged"}
        # Never yank an item OUT of a lane a human parked it in on purpose â€”
        # only advance/reflect while it's still in a pipeline-driven lane.
        changed_lane = new_lane != it.lane and it.lane not in ("dropped", "done")
        if changed_lane:
            it.lane = new_lane
        it.sync_sig = sig
        await provider().upsert(it)
        gate_txt = "n/a" if gate is None else ("PASS" if gate else "FAIL")
        body = (f"pipeline {it.pipeline}: decision={rec.get('decision')}, gate={gate_txt}"
                + (", review requested" if rec.get("review_requested") else "")
                + (f" \u2192 lane: {new_lane}" if changed_lane else ""))
        await provider().comment(it.id, bc.AgentMessage(
            kind="progress", frm="board.sync", item=it.id, body=body))
        await emit_event({"type": "board.synced", "id": it.id, "pipeline": it.pipeline,
                          "lane": new_lane, "changed_lane": changed_lane})
        return {"id": it.id, "synced": True, "lane": new_lane, "changed_lane": changed_lane}

    @capability(
        "board.sync", http_method="POST", http_path="/board/sync",
        http_tags=["board"], memory="on",
        description="Reflect a linked pipeline's CURRENT state (decision/gate/review) "
                    "onto its board item(s) \u2014 lane + a progress comment \u2014 so the board "
                    "is the single work view (route-forward.md M4, Stage 2 start; local "
                    "only, no GitHub needed \u2014 GitHub provider + board.budget are the next "
                    "part of Stage 2, not this). Idempotent: a repeated sync with nothing "
                    "changed is a cheap no-op, not a duplicate comment \u2014 safe to call "
                    "after every board load or on a timer. Never yanks an item out of "
                    "'dropped'/'done' \u2014 those are human decisions, not pipeline-driven. "
                    "Input: id (str \u2014 sync one item; empty = every item with a linked "
                    "pipeline). Output: {ok, synced:[{id,lane,changed_lane}], "
                    "skipped:[{id,reason}], total}.",
    )
    async def cap_board_sync(id: str = "", trace_id=None) -> dict:
        if id:
            it = await provider().get(id)
            if not it:
                return {"ok": False, "error": f"no such item: {id}"}
            targets = [it]
        else:
            targets = [it for it in await provider().items() if it.pipeline]
        synced: list = []
        skipped: list = []
        for it in targets:
            res = await _sync_one(it)
            (synced if res.get("synced") else skipped).append(res)
        return {"ok": True, "synced": synced, "skipped": skipped, "total": len(targets)}
