"""The step verifier and file paths (4 Oct 2026 census):
- a criterion naming /tmp/x.md (a path the loop cannot write) is met by ./x.md;
- a 'met' verdict that leans on a file which does not exist is overturned.

Pure tests on verify_paths_core, then _v6_verify_step with the filesystem probe and the
judge mocked (same pattern as test_v6_verify_list_ext_gate.py)."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import verify_paths_core as V  # noqa: E402
from vera.dag import dag_workshop_capabilities as W  # noqa: E402


# ── pure ──────────────────────────────────────────────────────────────────────

def test_foreign_alias():
    assert V.foreign_alias("/tmp/redis_valkey_report.md") == "redis_valkey_report.md"
    assert V.foreign_alias("/w/scratch/qdrant_docs.txt") == "qdrant_docs.txt"
    assert V.foreign_alias("/mnt/data/out.csv") == "out.csv"
    for p in ("/workspace/report.md", "/workspace", "report.md", "docs/a.md", "/tmp/", "/tmp/dir", ""):
        assert V.foreign_alias(p) == "", p


def test_cited_paths_are_explicit_paths_not_urls_or_bare_names():
    r = ("The file /w/scratch/qdrant_docs.txt (referenced in Run Journal Step [4] and supported "
         "by https://qdrant.tech/documentation/overview.html) contains docs; report.md too.")
    assert V.cited_paths(r) == ["/w/scratch/qdrant_docs.txt"]
    assert V.cited_paths("wrote /workspace/out/report.md, and /workspace/out/report.md.") == ["/workspace/out/report.md"]
    assert V.cited_paths("no paths here, only report.md") == []
    assert V.cited_paths("see /usr/bin and /etc/hosts") == []      # no file extension


def test_missing_after_alias():
    exist = {"/tmp/r.md": False, "r.md": True, "/w/a.txt": False, "/workspace/x.md": False}
    assert V.missing_after_alias(["/tmp/r.md", "/w/a.txt", "/workspace/x.md", "/q/unknown.md"], exist) \
        == ["/w/a.txt", "/workspace/x.md"]


# ── the verifier ──────────────────────────────────────────────────────────────

def _verify(monkeypatch, crit, on_disk, judge_met=True, judge_reason="judge: ok"):
    probes = []

    async def _fake_workdir(session_id, limit=40):
        return [p for p in on_disk if "/" not in p]

    async def _fake_exist(session_id, paths):
        probes.append(list(paths))
        return {p: (p in on_disk) for p in (paths or [])}

    async def _fake_generate(*a, **kw):
        return json.dumps({"met": bool(judge_met), "reason": judge_reason})

    monkeypatch.setattr(W, "_v5_workdir_files", _fake_workdir)
    monkeypatch.setattr(W, "_v6_check_paths_exist", _fake_exist)
    monkeypatch.setattr(W, "_safe_ollama_generate_dw", _fake_generate)
    step = {"id": 4, "title": "Draft the report", "goal": "write the report", "success": crit}
    res = {"ok": True, "summary": "wrote the report", "outputs": {},
           "history": [{"tool": "prose.author", "ok": True, "args": {"path": "redis_valkey_report.md"},
                        "preview": "wrote redis_valkey_report.md"}]}
    out = asyncio.run(W._v6_verify_step(step, res, session_id="s1", model="m",
                                        instance_id="i", prefer_gpu=False))
    return out, probes


def test_a_tmp_criterion_is_met_by_the_file_in_the_working_dir(monkeypatch):
    out, probes = _verify(monkeypatch, "The final report is saved to /tmp/redis_valkey_report.md",
                          on_disk={"redis_valkey_report.md"})
    assert out["met"] is True, out
    assert any("redis_valkey_report.md" in ps for ps in probes)


def test_a_tmp_criterion_still_fails_when_the_file_is_nowhere(monkeypatch):
    out, _ = _verify(monkeypatch, "The final report is saved to /tmp/redis_valkey_report.md",
                     on_disk=set())
    assert out["met"] is False
    assert "/tmp/redis_valkey_report.md" in out["reason"]


def test_a_workspace_criterion_is_not_aliased(monkeypatch):
    # /workspace/out/report.md missing, report.md at the root: no alias - still missing
    out, _ = _verify(monkeypatch, "The report is written to /workspace/out/report.md",
                     on_disk={"report.md"})
    assert out["met"] is False


def test_a_pass_resting_on_a_missing_file_is_overturned(monkeypatch):
    out, _ = _verify(monkeypatch, "At least one documentation file is fetched for each database",
                     on_disk={"Milvus.txt"}, judge_met=True,
                     judge_reason="The file /w/scratch/qdrant_docs.txt (referenced in Run Journal "
                                  "Step [4]) contains Qdrant documentation exceeding 500 characters")
    assert out["met"] is False
    assert "/w/scratch/qdrant_docs.txt" in out["reason"]


def test_a_pass_citing_a_real_file_stands(monkeypatch):
    out, _ = _verify(monkeypatch, "At least one documentation file is fetched for each database",
                     on_disk={"/workspace/Milvus.txt", "Milvus.txt"}, judge_met=True,
                     judge_reason="/workspace/Milvus.txt holds the Milvus docs")
    assert out["met"] is True


def test_a_pass_citing_a_foreign_path_whose_file_exists_stands(monkeypatch):
    out, _ = _verify(monkeypatch, "At least one documentation file is fetched for each database",
                     on_disk={"qdrant_docs.txt"}, judge_met=True,
                     judge_reason="/w/scratch/qdrant_docs.txt holds the Qdrant docs")
    assert out["met"] is True


def test_a_fail_verdict_is_left_alone(monkeypatch):
    out, _ = _verify(monkeypatch, "At least one documentation file is fetched for each database",
                     on_disk=set(), judge_met=False, judge_reason="/w/x.txt was never written")
    assert out["met"] is False and out["reason"] == "/w/x.txt was never written"
