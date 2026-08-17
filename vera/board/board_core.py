# ============================================================================
# board_core.py — pure item model + FileBoardProvider (Agent Boards & Comms)
# ============================================================================
#
# The work plane behind one provider seam (see ~/vera_sandbox/agentic swarm.md
# §6.1). This module is the FILE tier — always present, always writable,
# human-editable, git-versionable — the bootstrap and the ultimate fallback.
# GitHub/Gitea providers land in later stages behind the same shape.
#
# Design rules this pins from the plan:
#   • board-meta lives IN the item body on every provider (§2.1), so items stay
#     portable and nothing leaks into the protocol.
#   • A claim is a LEASE by an append-only, totally-ordered primitive — the
#     lowest comment id wins (§3.3). withdraw cancels a claim.
#   • Live board state is written OUTSIDE the tracked tree (state_paths.board_dir),
#     never into the repo — a dirty prod checkout blocks every promote
#     (dev-lifecycle §8.2 #7 / this plan's §9.0 Stage 0).
#
# The pure text<->model helpers carry the tests; FileBoardProvider is a thin,
# atomic IO layer over them.
# ============================================================================

from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# ── vocabulary ──────────────────────────────────────────────────────────────
# Ordered lanes (§2.1 + the Vera-executor lanes §5.6). `blocked`/`dropped` are
# lanes, not deletions.
LANES = [
    "inbox", "ready", "in_progress", "blocked", "needs_review", "review",
    "done", "dropped", "queued_vera", "in_progress_vera",
]
LANE_SET = set(LANES)
DEFAULT_LANE = "inbox"

# Envelope kinds (§3.4) + resume/note used by auto-resume and free notes.
ENVELOPE_KINDS = {
    "claim", "withdraw", "handoff", "help-request", "reply",
    "progress", "blocked", "steer", "resume", "note",
}

# board-meta scalar fields (everything but title/body/comments).
META_FIELDS = [
    "lane", "labels", "agent", "repo", "project", "plan", "branch", "pipeline",
    "session", "executor", "model", "reviewed_by", "heartbeat", "hops",
    "created_at", "updated_at", "sync_sig",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_item_id() -> str:
    return uuid.uuid4().hex[:8]


# ── data model ──────────────────────────────────────────────────────────────
@dataclass
class AgentMessage:
    """The §3.4 envelope. `frm` serialises as `from:` (a Python keyword)."""
    kind: str = "note"
    frm: str = ""
    to: str = "any"
    item: str = ""
    hops: int = 0
    done_when: str = ""
    body: str = ""
    id: int = 0
    created_at: str = ""


@dataclass
class BoardItem:
    id: str = ""
    title: str = ""
    lane: str = DEFAULT_LANE
    body: str = ""
    labels: List[str] = field(default_factory=list)
    agent: str = ""          # owner — the agent:<name> whose lease is current
    repo: str = ""           # WHERE it lands (evolve.repo.add id) — §0.2 #2
    project: str = ""        # the project/effort it belongs to (may be non-code)
    plan: str = ""           # id of the umbrella PLAN item this work belongs to
    branch: str = ""
    pipeline: str = ""
    # Opaque fingerprint of the linked pipeline's state as of the last
    # board.sync â€” decision|gate_passed|review_requested|lane. Lets sync be
    # idempotent (a repeated call with nothing changed is a no-op, not a
    # duplicate comment) without needing a separate side-store.
    sync_sig: str = ""
    session: str = ""
    executor: str = ""       # deterministic|vera|capable
    model: str = ""
    reviewed_by: str = ""
    heartbeat: str = ""
    hops: int = 0
    created_at: str = ""
    updated_at: str = ""
    comments: List[AgentMessage] = field(default_factory=list)


@dataclass
class BoardQuery:
    lane: str = ""
    label: str = ""
    agent: str = ""
    repo: str = ""
    project: str = ""
    mentions: str = ""
    text: str = ""


@dataclass
class ClaimResult:
    ok: bool
    held_by: str = ""
    lost: bool = False
    comment_id: int = 0
    reason: str = ""


# ── board-meta (parse / render) ─────────────────────────────────────────────
_META_RE = re.compile(r"```board-meta[ \t]*\n(.*?)\n```", re.DOTALL)


def render_board_meta(item: BoardItem) -> str:
    lines = ["```board-meta"]
    for f in META_FIELDS:
        v = getattr(item, f)
        if f == "labels":
            v = ", ".join(v or [])
        lines.append(f"{f}: {v}")
    lines.append("```")
    return "\n".join(lines)


def parse_board_meta(text: str) -> dict:
    m = _META_RE.search(text or "")
    out: dict = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


# ── agent-msg envelope (parse / render) ─────────────────────────────────────
# Each block is a fenced ```agent-msg ... ``` header immediately followed by its
# prose body, up to the next block (or end).
_MSG_RE = re.compile(
    r"```agent-msg[ \t]*\n(.*?)\n```[ \t]*\n?(.*?)(?=\n```agent-msg|\Z)",
    re.DOTALL,
)


def render_agent_msg(msg: AgentMessage) -> str:
    lines = [
        "```agent-msg",
        f"kind: {msg.kind}",
        f"from: {msg.frm}",
        f"to: {msg.to or 'any'}",
        f"item: {msg.item}",
        f"hops: {msg.hops}",
    ]
    if msg.done_when:
        lines.append(f"done-when: {msg.done_when}")
    lines += [f"id: {msg.id}", f"created_at: {msg.created_at}", "```"]
    out = "\n".join(lines)
    if (msg.body or "").strip():
        out += "\n" + msg.body.strip()
    return out


def parse_agent_msgs(text: str) -> List[AgentMessage]:
    out: List[AgentMessage] = []
    for m in _MSG_RE.finditer(text or ""):
        hdr, body = m.group(1), (m.group(2) or "").strip()
        d: dict = {}
        for line in hdr.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                d[k.strip()] = v.strip()
        out.append(AgentMessage(
            kind=d.get("kind", "note"),
            frm=d.get("from", ""),
            to=d.get("to", "any"),
            item=d.get("item", ""),
            hops=int(d.get("hops", 0) or 0),
            done_when=d.get("done-when", ""),
            id=int(d.get("id", 0) or 0),
            created_at=d.get("created_at", ""),
            body=body,
        ))
    return out


# ── item <-> markdown ───────────────────────────────────────────────────────
def item_to_markdown(item: BoardItem) -> str:
    parts = [f"# {item.title}".rstrip(), "", render_board_meta(item), ""]
    if (item.body or "").strip():
        parts += [item.body.strip(), ""]
    parts.append("## comments")
    for c in item.comments:
        parts += ["", render_agent_msg(c)]
    return "\n".join(parts).rstrip() + "\n"


def item_from_markdown(text: str, item_id: str) -> BoardItem:
    title = ""
    mt = re.search(r"^#[ \t]+(.+)$", text or "", re.M)
    if mt:
        title = mt.group(1).strip()
    meta = parse_board_meta(text)

    mm = _META_RE.search(text or "")
    start = mm.end() if mm else 0
    ci = text.find("## comments", start)
    body = text[start: ci if ci >= 0 else len(text)].strip()
    comments = parse_agent_msgs(text[ci:] if ci >= 0 else "")

    labels = [x.strip() for x in meta.get("labels", "").split(",") if x.strip()]
    return BoardItem(
        id=item_id,
        title=title,
        lane=meta.get("lane", DEFAULT_LANE) or DEFAULT_LANE,
        body=body,
        labels=labels,
        agent=meta.get("agent", ""),
        repo=meta.get("repo", ""),
        project=meta.get("project", ""),
        plan=meta.get("plan", ""),
        branch=meta.get("branch", ""),
        pipeline=meta.get("pipeline", ""),
        session=meta.get("session", ""),
        executor=meta.get("executor", ""),
        model=meta.get("model", ""),
        reviewed_by=meta.get("reviewed_by", ""),
        heartbeat=meta.get("heartbeat", ""),
        hops=int(meta.get("hops", 0) or 0),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        sync_sig=meta.get("sync_sig", ""),
        comments=comments,
    )


# ── claim resolution (§3.3) ─────────────────────────────────────────────────
def resolve_claim(comments: List[AgentMessage]) -> str:
    """The current lease holder: the lowest-id `claim` whose author has not since
    `withdraw`n. Empty string when unclaimed. Totally ordered by comment id, so
    two racing claims deterministically resolve to the earlier one."""
    withdrawn = {c.frm for c in comments if c.kind == "withdraw"}
    active = [c for c in comments if c.kind == "claim" and c.frm not in withdrawn]
    if not active:
        return ""
    return min(active, key=lambda c: c.id or 10 ** 9).frm


def handoff_plan(comments: List[AgentMessage], frm: str, to: str) -> dict:
    """Validate an atomic claim handoff from `frm` to `to` (Phase E cross-machine
    handoff). A handoff is legal ONLY when `frm` is the CURRENT lease holder — an
    agent cannot give away a claim it does not hold, and cannot hand to itself or to
    an empty identity. Returns {ok, error?}. The provider, on ok, appends a `handoff`
    audit envelope + `frm`'s `withdraw` + `to`'s `claim`, after which
    resolve_claim(...) == to. Pure + tested so the safety check has one home."""
    frm = (frm or "").strip()
    to = (to or "").strip()
    if not frm or not to:
        return {"ok": False, "error": "both frm and to are required"}
    if frm == to:
        return {"ok": False, "error": "cannot hand off to the same agent"}
    holder = resolve_claim(comments)
    if holder != frm:
        return {"ok": False,
                "error": f"{frm} does not hold the claim (holder={holder or 'unclaimed'}) "
                         "— only the current holder can hand off"}
    return {"ok": True}


def item_matches(it: BoardItem, q: Optional[BoardQuery]) -> bool:
    if not q:
        return True
    if q.lane and it.lane != q.lane:
        return False
    if q.label and q.label not in it.labels:
        return False
    if q.agent and it.agent != q.agent:
        return False
    if q.repo and it.repo != q.repo:
        return False
    if q.project and it.project != q.project:
        return False
    hay_body = it.body + " " + " ".join(c.body for c in it.comments)
    if q.mentions and q.mentions not in hay_body:
        return False
    if q.text and q.text.lower() not in (it.title + " " + it.body).lower():
        return False
    return True


# ── plan import (structured doc → board items, §1 capture→work) ──────────────
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MD_NOISE = re.compile(r"[`*_]+")


def slugify(s: str, n: int = 40) -> str:
    return _SLUG_RE.sub("-", (s or "").lower()).strip("-")[:n] or "x"


def parse_plan_items(text: str, project: str = "", repo: str = "",
                     level: int = 2, lane: str = DEFAULT_LANE,
                     labels: Optional[List[str]] = None) -> List[dict]:
    """Turn a markdown PLAN (more structured than a braindump) into board items —
    one per heading at `level`, body = the section text up to the next heading of
    the same or higher level. Ids are deterministic per (project, heading) so a
    re-import UPDATES in place rather than duplicating. Returns plain dicts (the
    cap layer converts to BoardItem + upserts). Pure — unit-tested."""
    base_labels = list(labels or ["plan"])
    lines = (text or "").splitlines()
    hpat = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
    heads = []
    for i, l in enumerate(lines):
        m = hpat.match(l)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    out: List[dict] = []
    for idx, (li, lv, title) in enumerate(heads):
        if lv != level:
            continue
        end = len(lines)
        for (li2, lv2, _t) in heads[idx + 1:]:
            if lv2 <= lv:
                end = li2
                break
        body = "\n".join(lines[li + 1:end]).strip()
        t = _MD_NOISE.sub("", title).strip()
        iid = ("plan-" + (slugify(project) + "-" if project else "") + slugify(t))
        out.append({
            "id": iid, "title": t[:200], "body": body[:8000], "lane": lane,
            "labels": base_labels, "project": project, "repo": repo,
        })
    return out


# Headings that are OVERALL CONTEXT (apply to every step), not a discrete task —
# these should NOT clutter the board as work items; they belong to the plan.
_CONTEXT_HEAD = re.compile(
    r"\b(why|overview|introduction|intro|principles?|rationale|background|context|"
    r"decisions?|glossary|appendix|summary|scope|non-?goals?|open questions?|"
    r"known (failure|issue)|references?|changelog|revision|status|legend|"
    r"terminology|assumptions?|design decision)\b", re.I)
_WORK_HEAD = re.compile(
    r"\b(phase|stage|step|task|milestone|deliverable|build|implement|fix|add|create|"
    r"deploy|migrate|refactor|wire|write|remove|enable|ship|slice)\b", re.I)
_CHECKLIST = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.M)


def classify_plan_section(title: str, body: str) -> str:
    """work | context — is a plan section a discrete WORK item, or OVERALL CONTEXT
    that applies to every step? Heuristic (a design doc is mostly context): a
    section with a checklist, or a phase/stage/task heading, is work; a why/
    overview/principles/decisions heading is context; else default to context so
    descriptive sections don't clutter the board as fake work items."""
    if _CHECKLIST.search(body or ""):
        return "work"
    t = title or ""
    if _WORK_HEAD.search(t):
        return "work"
    if _CONTEXT_HEAD.search(t):
        return "context"
    # Default WORK: a plan section is a task unless it's CLEARLY overall guidance
    # (why/principles/decisions/…). Folding only the clear context is what removes
    # the "overall instructions on the board" noise without emptying the board.
    return "work"


def parse_plan(text: str, project: str = "", repo: str = "",
               level: int = 2, lane: str = "inbox") -> dict:
    """Split a plan into ONE umbrella PLAN item (the title + all overall CONTEXT
    sections — the guidance that applies to every step) and the discrete WORK
    items, each tagged `plan=<umbrella id>` so they group under it and the shared
    context travels with a task. Context sections do NOT become separate board
    items — that was the noise. Deterministic ids (re-import updates in place).
    Output: {plan_item, work_items, work_count, context_count}. Pure."""
    rows = parse_plan_items(text, project=project, repo=repo, level=level, lane=lane)
    m = re.search(r"^#\s+(.+)$", text or "", re.M)
    plan_title = _MD_NOISE.sub("", (m.group(1).strip() if m else (project or "Plan"))).strip()
    plan_id = "plan-" + (slugify(project) if project else slugify(plan_title))
    context_parts: List[str] = []
    work_items: List[dict] = []
    for r in rows:
        if classify_plan_section(r["title"], r["body"]) == "context":
            context_parts.append("## " + r["title"] + "\n\n" + r["body"])
        else:
            r["plan"] = plan_id
            r["labels"] = [x for x in (r.get("labels") or []) if x != "plan"] + ["work"]
            work_items.append(r)
    plan_body = ("_Overall plan context — applies to every work item below._\n\n"
                 + "\n\n---\n\n".join(context_parts))
    plan_item = {
        "id": plan_id, "title": plan_title[:200], "lane": lane, "labels": ["plan"],
        "project": project, "repo": repo, "plan": "", "body": plan_body[:16000],
    }
    return {"plan_item": plan_item, "work_items": work_items,
            "work_count": len(work_items), "context_count": len(context_parts)}


# ── fabric index projection (§6.3) ───────────────────────────────────────────
def index_row(it: BoardItem) -> dict:
    """A flat, fabric-ingestable row for one item (keyed by id). Labels/comments
    are flattened to strings so the row is a plain scalar dict; `text` carries
    title+body for text/semantic retrieval. Derived — never written back."""
    return {
        "id": it.id,
        "title": it.title,
        "lane": it.lane,
        "labels": " ".join(it.labels),
        "agent": it.agent,
        "repo": it.repo,
        "project": it.project,
        "plan": it.plan,
        "branch": it.branch,
        "pipeline": it.pipeline,
        "session": it.session,
        "executor": it.executor,
        "model": it.model,
        "blocked": it.lane == "blocked",
        "needs_help": "needs:help" in it.labels,
        "comment_count": len(it.comments),
        "heartbeat": it.heartbeat,
        "updated_at": it.updated_at,
        "text": (it.title + "\n" + it.body).strip()[:2000],
    }


def summarize(items: List[BoardItem]) -> dict:
    """A queryable rollup answering the plan's headline questions directly
    (what's blocked, who owns what) without needing the fabric round-trip."""
    by_lane: dict = {}
    by_agent: dict = {}
    blocked: List[dict] = []
    needs_help: List[dict] = []
    for it in items:
        by_lane[it.lane] = by_lane.get(it.lane, 0) + 1
        if it.agent:
            by_agent[it.agent] = by_agent.get(it.agent, 0) + 1
        if it.lane == "blocked":
            blocked.append({"id": it.id, "title": it.title, "agent": it.agent})
        if "needs:help" in it.labels and not it.agent:
            needs_help.append({"id": it.id, "title": it.title})
    return {
        "total": len(items),
        "by_lane": by_lane,
        "by_agent": by_agent,
        "blocked": blocked,
        "needs_help_unclaimed": needs_help,
    }


# ── FileBoardProvider ───────────────────────────────────────────────────────
class FileBoardProvider:
    """Markdown-file board: one `<id>.md` per item under an out-of-tree root.
    Writes are atomic (temp + os.replace) so a concurrent reader never sees a
    torn file. Cross-process claim races are resolved by the lowest-comment-id
    rule (§3.3), with container/branch isolation as the real safety net."""

    name = "file"

    def __init__(self, root=None):
        if root is None:
            from Vera.vera import state_paths  # lazy: avoids import at test time
            root = state_paths.board_dir()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- sync internals ------------------------------------------------------
    def _path(self, item_id: str) -> Path:
        return self.root / f"{item_id}.md"

    def _read(self, item_id: str) -> Optional[BoardItem]:
        p = self._path(item_id)
        if not p.exists():
            return None
        return item_from_markdown(p.read_text(encoding="utf-8"), item_id)

    def _write(self, item: BoardItem) -> None:
        item.updated_at = _now()
        p = self._path(item.id)
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(item_to_markdown(item), encoding="utf-8")
        os.replace(tmp, p)

    @staticmethod
    def _next_comment_id(item: BoardItem) -> int:
        return max((c.id for c in item.comments), default=0) + 1

    def _items_sync(self, query) -> List[BoardItem]:
        out = []
        for p in sorted(self.root.glob("*.md")):
            it = item_from_markdown(p.read_text(encoding="utf-8"), p.stem)
            if item_matches(it, query):
                out.append(it)
        return out

    def _upsert_sync(self, item: BoardItem) -> BoardItem:
        if not item.id:
            item.id = new_item_id()
        if item.lane not in LANE_SET:
            raise ValueError(f"unknown lane: {item.lane}")
        existing = self._read(item.id)
        if existing is None:
            item.created_at = item.created_at or _now()
        else:
            item.created_at = item.created_at or existing.created_at
            if not item.comments:          # never silently drop the thread
                item.comments = existing.comments
        self._write(item)
        return item

    def _move_sync(self, item_id: str, lane: str) -> BoardItem:
        if lane not in LANE_SET:
            raise ValueError(f"unknown lane: {lane}")
        it = self._read(item_id)
        if it is None:
            raise KeyError(item_id)
        it.lane = lane
        self._write(it)
        return it

    def _claim_sync(self, item_id: str, agent: str) -> ClaimResult:
        it = self._read(item_id)
        if it is None:
            raise KeyError(item_id)
        cid = self._next_comment_id(it)
        it.comments.append(AgentMessage(
            kind="claim", frm=agent, to="any", item=item_id, id=cid,
            created_at=_now()))
        # tentatively take it (batched write — §3.3 step 3)
        lbl = f"agent:{agent}"
        if lbl not in it.labels:
            it.labels.append(lbl)
        it.agent = agent
        it.lane = "in_progress"
        it.heartbeat = _now()
        self._write(it)
        # re-resolve (§3.3 step 4): an earlier claim from another agent wins
        winner = resolve_claim(it.comments)
        if winner and winner != agent:
            it.comments.append(AgentMessage(
                kind="withdraw", frm=agent, item=item_id,
                id=self._next_comment_id(it), created_at=_now()))
            it.labels = [x for x in it.labels if x != lbl]
            it.agent = winner
            self._write(it)
            return ClaimResult(ok=False, held_by=winner, lost=True,
                               comment_id=cid, reason="earlier claim wins")
        return ClaimResult(ok=True, held_by=agent, comment_id=cid)

    def _handoff_sync(self, item_id: str, frm: str, to: str, body: str = "") -> dict:
        """Atomic claim handoff frm→to in a single write (§3.3): validate `frm` holds
        the lease, then append handoff+withdraw+claim envelopes and swap the assignee.
        Returns {ok, held_by, error?}. Never partially applies — validation is up front."""
        it = self._read(item_id)
        if it is None:
            raise KeyError(item_id)
        plan = handoff_plan(it.comments, frm, to)
        if not plan.get("ok"):
            return {"ok": False, "held_by": resolve_claim(it.comments), "error": plan.get("error")}
        for msg in (
            AgentMessage(kind="handoff", frm=frm, to=to, item=item_id, body=body,
                         id=self._next_comment_id(it), created_at=_now()),
            AgentMessage(kind="withdraw", frm=frm, to=to, item=item_id,
                         id=self._next_comment_id(it), created_at=_now()),
            AgentMessage(kind="claim", frm=to, to=frm, item=item_id,
                         id=self._next_comment_id(it), created_at=_now()),
        ):
            it.comments.append(msg)
        it.labels = [x for x in it.labels if x != f"agent:{frm}"]
        if f"agent:{to}" not in it.labels:
            it.labels.append(f"agent:{to}")
        it.agent = to
        it.lane = "in_progress"
        it.heartbeat = _now()
        self._write(it)
        winner = resolve_claim(it.comments)          # invariant: to now holds the lease
        return {"ok": winner == to, "held_by": winner}

    def _comment_sync(self, item_id: str, msg: AgentMessage) -> str:
        it = self._read(item_id)
        if it is None:
            raise KeyError(item_id)
        msg.item = item_id
        msg.id = self._next_comment_id(it)
        msg.created_at = msg.created_at or _now()
        it.comments.append(msg)
        it.heartbeat = _now()
        self._write(it)
        return str(msg.id)

    # -- async surface (Protocol) -------------------------------------------
    async def items(self, query: Optional[BoardQuery] = None) -> List[BoardItem]:
        return await asyncio.to_thread(self._items_sync, query)

    async def get(self, item_id: str) -> Optional[BoardItem]:
        return await asyncio.to_thread(self._read, item_id)

    async def upsert(self, item: BoardItem) -> BoardItem:
        return await asyncio.to_thread(self._upsert_sync, item)

    async def move(self, item_id: str, lane: str) -> BoardItem:
        return await asyncio.to_thread(self._move_sync, item_id, lane)

    async def claim(self, item_id: str, agent: str) -> ClaimResult:
        return await asyncio.to_thread(self._claim_sync, item_id, agent)

    async def handoff(self, item_id: str, frm: str, to: str, body: str = "") -> dict:
        return await asyncio.to_thread(self._handoff_sync, item_id, frm, to, body)

    async def comment(self, item_id: str, msg: AgentMessage) -> str:
        return await asyncio.to_thread(self._comment_sync, item_id, msg)

    async def comments(self, item_id: str) -> List[AgentMessage]:
        it = await asyncio.to_thread(self._read, item_id)
        return it.comments if it else []

    async def inbox(self, agent: str) -> List[BoardItem]:
        items = await self.items()

        def relevant(it: BoardItem) -> bool:
            if f"agent:{agent}" in it.labels:
                return True
            if "needs:help" in it.labels and not it.agent:
                return True
            hay = it.body + " " + " ".join(c.body for c in it.comments)
            return bool(agent) and (agent in hay)

        return [it for it in items if relevant(it)]


# ── board.sync decision logic (M4) — pure, unit-tested in test_board_sync.py ──
# The cap (board_capabilities.cap_board_sync) wraps these with provider IO; the
# lane mapping / idempotency fingerprint / human-parked-lane protection live here
# so a regression in "which lane does a pipeline state imply" is caught by tests,
# not in production board state.

# Lanes a human parks an item in on purpose: board.sync reflects INTO them
# (promoted -> done, rolled_back -> dropped) but never yanks an item back OUT.
TERMINAL_LANES = {"dropped", "done"}


def pipeline_lane(rec: dict) -> str:
    """Map a linked pipeline record's decision/gate/review state onto a board lane.

    `decision` is 'pending' through adopt/review, 'held' if the gate blocked the
    promote or a merge conflicted, 'promoted' once merged, 'rolled_back' if the
    change was discarded (evolve_pipeline_rollback). `review_requested` flags a
    pending adversarial review. Everything still moving stays 'in_progress'."""
    decision = (rec.get("decision") or "pending").lower()
    if decision == "promoted":
        return "done"
    if decision == "rolled_back":
        return "dropped"
    if decision == "held":
        return "blocked"
    if rec.get("review_requested"):
        return "needs_review"
    return "in_progress"


def pipeline_sync_sig(rec: dict, lane: str) -> str:
    """Opaque fingerprint of the pipeline state board.sync last reflected onto an
    item — decision|gate_passed|review_requested|lane. Equal signatures mean
    nothing changed, so a repeat sync is a cheap no-op (no duplicate comment),
    safe to call after every board load or on a timer."""
    return f"{rec.get('decision')}|{rec.get('gate_passed')}|{bool(rec.get('review_requested'))}|{lane}"


def should_apply_lane(current_lane: str, new_lane: str) -> bool:
    """True if board.sync should move an item from current_lane to new_lane: the
    lane actually differs AND the item isn't parked in a terminal lane (done /
    dropped) that a human owns — those are reflected into, never yanked out of."""
    return new_lane != current_lane and current_lane not in TERMINAL_LANES
