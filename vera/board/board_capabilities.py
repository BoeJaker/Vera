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

import logging
import os
import re
from typing import List, Optional

from Vera.vera.capability_orchestration import APP, capability, CAPABILITY_REGISTRY
from Vera.vera.board import board_core as bc
from Vera.vera.sandbox_guard import write_blocked as _sbx_write_blocked

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
         "agent": it.agent, "repo": it.repo, "project": it.project,
         "branch": it.branch, "pipeline": it.pipeline,
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
                                    branch: str = "", pipeline: str = "",
                                    session: str = "", executor: str = "", model: str = "",
                                    reviewed_by: str = "", hops: int = 0, trace_id=None) -> dict:
        if lane not in bc.LANE_SET:
            return {"ok": False, "error": f"unknown lane: {lane}", "lanes": bc.LANES}
        it = bc.BoardItem(
            id=id or "", title=title, lane=lane, body=body,
            labels=list(labels or []), agent=agent, repo=repo, project=project,
            branch=branch, pipeline=pipeline,
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
        description="Pull an EXISTING structured plan (a markdown doc — more structured than a "
                    "braindump) into the board as items: one item per heading at `level` (default "
                    "2 = '## '), body = that section's text, deterministic id per (project, "
                    "heading) so re-import UPDATES rather than duplicates. Inputs: path (str — a "
                    "file to read; absolute, or repo-relative) OR text (str — inline markdown), "
                    "project (str! — the effort these items belong to; §0.2 #2), repo (str=vera), "
                    "level (int=2), lane (str=inbox), labels (list, default ['plan']), title "
                    "(str — a title label appended). Output: {ok, project, imported, ids, items}.",
    )
    async def cap_board_import_plan(path: str = "", text: str = "", project: str = "",
                                    repo: str = "vera", level: int = 2, lane: str = "inbox",
                                    labels: Optional[List[str]] = None, trace_id=None) -> dict:
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
        rows = bc.parse_plan_items(body_text, project=project, repo=repo,
                                   level=int(level or 2), lane=lane,
                                   labels=list(labels) if labels else None)
        if not rows:
            return {"ok": True, "imported": 0, "project": project,
                    "note": f"no level-{level} headings found — try a different level"}
        ids = []
        for r in rows:
            it = bc.BoardItem(id=r["id"], title=r["title"], lane=r["lane"], body=r["body"],
                              labels=r["labels"], project=r["project"], repo=r["repo"])
            saved = await provider().upsert(it)
            ids.append(saved.id)
        return {"ok": True, "project": project, "imported": len(ids), "ids": ids,
                "items": [{"id": i} for i in ids]}

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
