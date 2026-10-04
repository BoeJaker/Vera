"""4 Oct 2026 census probe (strategic-vector-db-report, 24 min, report never written):

1. Every https://host.tld/x.html URL was refused as the "invented absolute path"
   /host.tld/x.html: the path regex starts at the second slash of "://", and the URL
   check looked one character short of it.
2. The step verifier saw 500-char previews of what a fetch step got and failed it for
   "only a truncated 'Table of Contents' snippet" - our clip. Fetch steps retried 3+3 times.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402

URLS = [
    "https://wgall.com/blog/2026-03-19-vector-databases-weaviate-qdrant-milvus.html",
    "https://milvus.io/docs/introduction.md",
    "https://valkey.io/blog/2024-10-03-valkey-launch.html",
    "http://example.com/a.json",
]


# ── 1. URLs are not invented paths ────────────────────────────────────────────

def test_https_urls_are_not_foreign_paths():
    for u in URLS:
        assert W._v5_foreign_abs_paths({"url": u}, "/w/run") == [], u


def test_real_foreign_paths_still_are():
    assert W._v5_foreign_abs_paths({"path": "/tmp/report.md"}, "/w/run") == ["/tmp/report.md"]
    assert W._v5_foreign_abs_paths({"path": "/workspace/report.md"}, "/w/run") == []
    # a real foreign path beside a URL is still caught, the URL is not
    got = W._v5_foreign_abs_paths({"path": "/mnt/data/in.csv", "url": "https://x.io/a.html"}, "/w/run")
    assert got == ["/mnt/data/in.csv"]


def test_pos_within_url_at_the_second_slash():
    s = "https://milvus.io/docs/introduction.md"
    assert W._v5_pos_within_url(s, 7) is True          # "/milvus.io/..." starts here
    assert W._v5_pos_within_url("see /tmp/a.md", 4) is False


def test_other_path_extractors_skip_urls_too():
    assert not any("milvus.io" in p for p in W._v6_extract_paths("fetched " + URLS[1]))
    assert not any("wgall.com" in p for p in W._v5_prompt_file_refs("use " + URLS[0]))


# ── 2. the verifier sees what the step fetched ────────────────────────────────

PAGE = ("Table of Contents\n" + "intro text " * 60 +
        "\n## Licensing\nQdrant is Apache-2.0; Milvus is Apache-2.0; Weaviate is BSD-3.\n"
        "## Performance\nQdrant p99 4 ms at 1M vectors.\n")


def _verify(monkeypatch, judge_met=True):
    seen = {}

    async def _fake_exist(session_id, paths):
        return {}

    async def _fake_workdir(session_id, limit=40):
        return []

    async def _fake_generate(prompt, **kw):
        seen["prompt"], seen["system"] = prompt, kw.get("system", "")
        return json.dumps({"met": judge_met, "reason": "judge"})

    monkeypatch.setattr(W, "_v6_check_paths_exist", _fake_exist)
    monkeypatch.setattr(W, "_v5_workdir_files", _fake_workdir)
    monkeypatch.setattr(W, "_safe_ollama_generate_dw", _fake_generate)
    step = {"id": 2, "title": "Fetch the comparison guide",
            "goal": "fetch the guide", "success": "The guide's sections on licensing and performance are fetched"}
    res = {"ok": True, "summary": "fetched the guide", "outputs": {},
           "history": [{"tool": "web.fetch", "ok": True, "args": {"url": URLS[0]},
                        "preview": json.dumps({"url": URLS[0], "title": "Guide", "text": PAGE})}]}
    out = asyncio.run(W._v6_verify_step(step, res, session_id="s1", model="m",
                                        instance_id="i", prefer_gpu=False))
    return out, seen


def test_the_judge_gets_the_fetched_text_beyond_the_preview(monkeypatch):
    out, seen = _verify(monkeypatch)
    p = seen["prompt"]
    assert "WHAT THIS STEP FETCHED" in p
    fetched = p[p.index("WHAT THIS STEP FETCHED"):]
    assert "Apache-2.0" in fetched and "p99 4 ms" in fetched       # past the 500-char preview
    assert "...[preview clipped]" in p[:p.index("WHAT THIS STEP FETCHED")]
    assert out["met"] is True


def test_the_judge_is_told_previews_are_clipped(monkeypatch):
    _, seen = _verify(monkeypatch)
    assert "SHORT PREVIEWS cut by us" in seen["system"]


def test_a_step_without_web_calls_gets_no_fetched_block(monkeypatch):
    seen = {}

    async def _fake_exist(session_id, paths):
        return {}

    async def _fake_gen(prompt, **kw):
        seen["prompt"] = prompt
        return json.dumps({"met": True, "reason": "ok"})

    monkeypatch.setattr(W, "_v6_check_paths_exist", _fake_exist)
    monkeypatch.setattr(W, "_safe_ollama_generate_dw", _fake_gen)
    step = {"id": 1, "title": "count", "goal": "count", "success": "the count is reported"}
    res = {"ok": True, "summary": "42", "outputs": {},
           "history": [{"tool": "exec.bash.run", "ok": True, "args": {"command": "ls | wc -l"}, "preview": "42"}]}
    asyncio.run(W._v6_verify_step(step, res, session_id="s", model="m", instance_id="i", prefer_gpu=False))
    assert "WHAT THIS STEP FETCHED" not in seen["prompt"]
