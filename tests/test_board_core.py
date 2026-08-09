"""Tests for the Agent Boards file tier (vera/board/board_core.py).

The pure text<->model round-trips and the §3.3 claim-lease resolution carry the
weight; FileBoardProvider is exercised against a tmp root so nothing touches the
real out-of-tree board dir.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.board import board_core as bc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ── pure: board-meta + envelope round-trips ──────────────────────────────────
def test_board_meta_round_trip():
    it = bc.BoardItem(id="a1", title="T", lane="ready",
                      labels=["code", "needs:help"], agent="claude-a",
                      branch="feat/x", hops=2)
    meta = bc.parse_board_meta(bc.render_board_meta(it))
    assert meta["lane"] == "ready"
    assert meta["labels"] == "code, needs:help"
    assert meta["agent"] == "claude-a"
    assert meta["branch"] == "feat/x"
    assert meta["hops"] == "2"


def test_envelope_round_trip_with_donewhen_and_body():
    m = bc.AgentMessage(kind="help-request", frm="claude-a", to="claude-b",
                        item="42", hops=1, done_when="tests pass",
                        id=3, created_at="2026-01-01T00:00:00+00:00",
                        body="see file.py:10 — need a hand")
    parsed = bc.parse_agent_msgs(bc.render_agent_msg(m))
    assert len(parsed) == 1
    p = parsed[0]
    assert p.kind == "help-request" and p.frm == "claude-a" and p.to == "claude-b"
    assert p.item == "42" and p.hops == 1 and p.done_when == "tests pass"
    assert p.id == 3 and "file.py:10" in p.body


def test_item_markdown_round_trip():
    it = bc.BoardItem(
        id="b2", title="Add docstring", lane="in_progress",
        labels=["code"], agent="claude-a", body="Body line one.\nLine two.",
        comments=[
            bc.AgentMessage(kind="claim", frm="claude-a", item="b2", id=1,
                            created_at="t1"),
            bc.AgentMessage(kind="progress", frm="claude-a", item="b2", id=2,
                            created_at="t2", body="halfway"),
        ],
    )
    text = bc.item_to_markdown(it)
    back = bc.item_from_markdown(text, "b2")
    assert back.title == "Add docstring"
    assert back.lane == "in_progress"
    assert back.labels == ["code"]
    assert back.agent == "claude-a"
    assert "Body line one." in back.body and "Line two." in back.body
    assert [c.kind for c in back.comments] == ["claim", "progress"]
    assert back.comments[1].body == "halfway"


def test_body_not_polluted_by_comments():
    it = bc.BoardItem(id="c3", title="T", body="just the body",
                      comments=[bc.AgentMessage(kind="note", frm="x", id=1,
                                                body="a comment")])
    back = bc.item_from_markdown(bc.item_to_markdown(it), "c3")
    assert back.body.strip() == "just the body"
    assert "a comment" not in back.body


# ── pure: claim resolution (§3.3) ────────────────────────────────────────────
def test_resolve_claim_empty():
    assert bc.resolve_claim([]) == ""
    assert bc.resolve_claim([bc.AgentMessage(kind="note", frm="a", id=1)]) == ""


def test_resolve_claim_lowest_id_wins():
    cs = [
        bc.AgentMessage(kind="claim", frm="b", id=2),
        bc.AgentMessage(kind="claim", frm="a", id=1),
    ]
    assert bc.resolve_claim(cs) == "a"


def test_resolve_claim_withdraw_cancels():
    cs = [
        bc.AgentMessage(kind="claim", frm="a", id=1),
        bc.AgentMessage(kind="claim", frm="b", id=2),
        bc.AgentMessage(kind="withdraw", frm="a", id=3),
    ]
    assert bc.resolve_claim(cs) == "b"


# ── FileBoardProvider ────────────────────────────────────────────────────────
@pytest.fixture
def prov(tmp_path):
    return bc.FileBoardProvider(root=tmp_path / "board")


def test_upsert_assigns_id_and_persists(prov):
    it = run(prov.upsert(bc.BoardItem(title="hello", lane="ready")))
    assert it.id and it.created_at
    again = run(prov.get(it.id))
    assert again.title == "hello" and again.lane == "ready"


def test_upsert_rejects_unknown_lane(prov):
    with pytest.raises(ValueError):
        run(prov.upsert(bc.BoardItem(title="x", lane="nope")))


def test_upsert_preserves_comments_when_none_passed(prov):
    it = run(prov.upsert(bc.BoardItem(title="x", lane="ready")))
    run(prov.comment(it.id, bc.AgentMessage(kind="progress", frm="a", body="p1")))
    # re-upsert a fresh copy with no comments -> thread preserved
    run(prov.upsert(bc.BoardItem(id=it.id, title="x2", lane="ready")))
    got = run(prov.get(it.id))
    assert got.title == "x2"
    assert [c.body for c in got.comments] == ["p1"]


def test_items_query_filters(prov):
    run(prov.upsert(bc.BoardItem(title="one", lane="ready", labels=["code"])))
    run(prov.upsert(bc.BoardItem(title="two", lane="blocked", labels=["docs"])))
    ready = run(prov.items(bc.BoardQuery(lane="ready")))
    assert [i.title for i in ready] == ["one"]
    docs = run(prov.items(bc.BoardQuery(label="docs")))
    assert [i.title for i in docs] == ["two"]


def test_move(prov):
    it = run(prov.upsert(bc.BoardItem(title="m", lane="ready")))
    moved = run(prov.move(it.id, "needs_review"))
    assert moved.lane == "needs_review"
    assert run(prov.get(it.id)).lane == "needs_review"


def test_claim_takes_lease(prov):
    it = run(prov.upsert(bc.BoardItem(title="c", lane="ready")))
    res = run(prov.claim(it.id, "claude-a"))
    assert res.ok and res.held_by == "claude-a"
    got = run(prov.get(it.id))
    assert got.agent == "claude-a" and "agent:claude-a" in got.labels
    assert got.lane == "in_progress" and got.heartbeat


def test_claim_race_earlier_wins(prov):
    it = run(prov.upsert(bc.BoardItem(title="c", lane="ready")))
    a = run(prov.claim(it.id, "claude-a"))   # lower comment id
    b = run(prov.claim(it.id, "claude-b"))   # higher comment id -> loses
    assert a.ok is True
    assert b.ok is False and b.lost is True and b.held_by == "claude-a"
    got = run(prov.get(it.id))
    assert got.agent == "claude-a"
    assert "agent:claude-b" not in got.labels
    # b left an auditable withdraw
    assert any(c.kind == "withdraw" and c.frm == "claude-b" for c in got.comments)


def test_comment_appends_and_ids_monotonic(prov):
    it = run(prov.upsert(bc.BoardItem(title="c", lane="ready")))
    id1 = run(prov.comment(it.id, bc.AgentMessage(kind="progress", frm="a", body="p1")))
    id2 = run(prov.comment(it.id, bc.AgentMessage(kind="progress", frm="a", body="p2")))
    assert int(id2) == int(id1) + 1
    cs = run(prov.comments(it.id))
    assert [c.body for c in cs] == ["p1", "p2"]


def test_inbox(prov):
    mine = run(prov.upsert(bc.BoardItem(title="mine", lane="ready",
                                        labels=["agent:claude-a"])))
    run(prov.upsert(bc.BoardItem(id=mine.id, title="mine", lane="ready",
                                 labels=["agent:claude-a"], agent="claude-a")))
    run(prov.upsert(bc.BoardItem(title="help", lane="ready", labels=["needs:help"])))
    run(prov.upsert(bc.BoardItem(title="mention", lane="ready",
                                 body="hey claude-a look here")))
    run(prov.upsert(bc.BoardItem(title="other", lane="ready", labels=["agent:claude-b"],
                                 agent="claude-b")))
    titles = {i.title for i in run(prov.inbox("claude-a"))}
    assert "mine" in titles and "help" in titles and "mention" in titles
    assert "other" not in titles


def test_default_root_is_out_of_tree():
    # When no root is passed the provider must use the out-of-tree state board dir,
    # never a path inside the repo (dev-lifecycle §8.2 #7).
    from vera import state_paths
    p = bc.FileBoardProvider(root=state_paths.board_dir())
    assert not state_paths.is_under_repo(p.root)


# ── fabric index projection (§6.3) ───────────────────────────────────────────
def test_index_row_is_flat_and_scalar():
    it = bc.BoardItem(id="i1", title="T", lane="blocked", body="x" * 5000,
                      labels=["code", "needs:help"], agent="claude-a",
                      comments=[bc.AgentMessage(kind="note", frm="a", id=1)])
    row = bc.index_row(it)
    assert row["id"] == "i1" and row["lane"] == "blocked"
    assert row["labels"] == "code needs:help"        # flattened, not a list
    assert row["blocked"] is True and row["needs_help"] is True
    assert row["comment_count"] == 1
    assert len(row["text"]) <= 2000                  # truncated for retrieval
    assert all(not isinstance(v, (list, dict)) for v in row.values())


def test_repo_project_round_trip_and_index(prov):
    it = run(prov.upsert(bc.BoardItem(title="cross-repo item", lane="ready",
                                      repo="acme-web", project="q3-launch")))
    back = run(prov.get(it.id))
    assert back.repo == "acme-web" and back.project == "q3-launch"
    row = bc.index_row(back)
    assert row["repo"] == "acme-web" and row["project"] == "q3-launch"


def test_query_by_repo_and_project(prov):
    run(prov.upsert(bc.BoardItem(title="a", lane="ready", repo="r1", project="p1")))
    run(prov.upsert(bc.BoardItem(title="b", lane="ready", repo="r2", project="p1")))
    by_repo = run(prov.items(bc.BoardQuery(repo="r1")))
    assert [i.title for i in by_repo] == ["a"]
    by_proj = run(prov.items(bc.BoardQuery(project="p1")))
    assert {i.title for i in by_proj} == {"a", "b"}


def test_summarize_rollup():
    items = [
        bc.BoardItem(id="1", lane="blocked", title="b1", agent="claude-a"),
        bc.BoardItem(id="2", lane="ready", title="r1", labels=["needs:help"]),
        bc.BoardItem(id="3", lane="in_progress", title="p1", agent="claude-a"),
        bc.BoardItem(id="4", lane="ready", title="r2", labels=["needs:help"],
                     agent="claude-b"),   # claimed → not "unclaimed"
    ]
    s = bc.summarize(items)
    assert s["total"] == 4
    assert s["by_lane"] == {"blocked": 1, "ready": 2, "in_progress": 1}
    assert s["by_agent"] == {"claude-a": 2, "claude-b": 1}
    assert [b["id"] for b in s["blocked"]] == ["1"]
    assert [h["id"] for h in s["needs_help_unclaimed"]] == ["2"]  # 4 is claimed
