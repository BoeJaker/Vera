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
    "lane", "labels", "agent", "repo", "project", "branch", "pipeline",
    "session", "executor", "model", "reviewed_by", "heartbeat", "hops",
    "created_at", "updated_at",
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
    branch: str = ""
    pipeline: str = ""
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
