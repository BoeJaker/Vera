"""The answer is composed from the deliverable, not from the run's story (plan item 23).

Census run70-73 (24 Sep 2026), graded on the delivered markdown:

  * research-web wrote webgpu-browser-support-summary.md citing its sources
    in every run, and the delivered answer never carried one URL (q=0.667,
    three runs): the delivery agent only ever saw the step summaries, never
    the file it was supposed to deliver.
  * Five deliverables opened with the delivery prompt's OWN section
    instruction copied verbatim: "## Result - Lead with the direct
    answer/outcome of the goal: the substance, not the process. If the goal
    was not fully achieved, say so plainly here." A small model treats a
    template as text to reproduce.
  * One build answer (run71 build-browser-verified) pasted the whole
    form.html; four across the set pasted a file's source where a sentence
    and the path were the answer.

Pure functions, no app imports. `document_files` picks the document(s) the
run wrote so the runner can read them into the delivery prompt; `finish`
runs the deterministic clean-up on what the model produced: the echoed
template goes, a pasted source file becomes a pointer to the file, and the
file's own source URLs are appended when the answer dropped every one.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, List, Sequence, Tuple

DOC_EXT = (".md", ".markdown", ".txt", ".rst")
CODE_EXT = (".html", ".htm", ".js", ".ts", ".css", ".py", ".sh", ".json", ".yaml", ".yml", ".go", ".rs")
HEADINGS = ("Result", "What was done", "Artifacts", "Usage")
#: Phrases from the delivery system prompt that a model copies into its output.
TEMPLATE_PHRASES = (
    "lead with the direct answer/outcome of the goal",
    "the substance, not the process",
    "if the goal was not fully achieved, say so plainly",
    "a faithful, concrete account of the actions taken",
    "do not gloss over failed steps",
    "files/outputs produced, with their paths",
    "only if the run produced code, configuration, or something deployed",
    "a brief practical guide for a developer",
    "omit a section when it has no content",
)
HEADING_RE = re.compile(r"^(#{1,4}\s*)(Result|What was done|Artifacts|Usage)\b(.*)$", re.I)
FENCE_RE = re.compile(r"```[A-Za-z0-9_+#.-]*[ \t]*\n(.*?)\n[ \t]*```", re.S)
SOURCE_HINT_RE = re.compile(
    r"<!DOCTYPE|<html\b|<head\b|<body\b|<script\b|<style\b|\bfunction\s+\w+\s*\(|"
    r"^\s*(?:def|class|import|from)\s+\w+|=>\s*\{|document\.(?:getElementById|querySelector)|"
    r"addEventListener\(|^\s*[.#][\w-]+\s*\{", re.M)
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
DUMP_MIN_LINES = 12
DUMP_MIN_CHARS = 700
#: Tools whose results ARE the run's sources.
RESEARCH_TOOLS = ("web.research", "web.fetch", "web.search", "http.get", "web.crawl")
GOAL_WANTS_SOURCES_RE = re.compile(r"\b(cit\w+|sources?|references?)\b", re.I)
#: Not a source anyone can follow: the run's own sandbox, a search engine's results page.
NOT_A_SOURCE_RE = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|[^/]*\.local)(?::\d+)?/"
    r"|^https?://(?:www\.)?(?:google|bing|duckduckgo|startpage)\.[a-z.]+/(?:search|html)?", re.I)


def document_files(output_keys: Iterable[str]) -> List[str]:
    """Relative paths of the DOCUMENT files a run wrote (`file:<path>` output keys), in order."""
    out: List[str] = []
    for k in output_keys:
        k = str(k or "")
        if not k.startswith("file:"):
            continue
        rel = k[5:].strip()
        if rel and rel.lower().endswith(DOC_EXT) and rel not in out:
            out.append(rel)
    return out


def urls_in(text: str) -> List[str]:
    out: List[str] = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?'\"")
        if u and u not in out:
            out.append(u)
    return out


def goal_wants_sources(goal: str) -> bool:
    return bool(GOAL_WANTS_SOURCES_RE.search(goal or ""))


def evidence_urls(results, *, limit: int = 10) -> List[str]:
    """The URLs the run's research tools returned, in order of first appearance.

    run74 research-report (25 Sep 2026): the research steps carried 26 URLs in
    one web.research result alone, the written report cited none, and the
    delivered answer cited none - q=0.75 on "cites at least one source". The
    sources were in the run's own evidence the whole time.
    """
    out: List[str] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        for h in (r.get("history") or []):
            if not isinstance(h, dict) or not h.get("ok"):
                continue
            tool = str(h.get("tool") or "")
            if not any(tool == t or tool.startswith(t + ".") for t in RESEARCH_TOOLS):
                continue
            for u in urls_in(str(h.get("preview") or "")):
                if NOT_A_SOURCE_RE.search(u) or u in out:
                    continue
                out.append(u)
                if len(out) >= limit:
                    return out
    return out


def _is_template(sentence: str) -> bool:
    s = sentence.lower()
    return any(p in s for p in TEMPLATE_PHRASES)


def strip_template_echo(md: str) -> str:
    """The delivery prompt's own section instructions, removed where the model echoed them."""
    out: List[str] = []
    for line in (md or "").splitlines():
        m = HEADING_RE.match(line)
        if m:
            rest = m.group(3)
            if rest and _is_template(rest):
                line = (m.group(1) + m.group(2)).rstrip()
            out.append(line)
            continue
        if _is_template(line):
            parts = re.split(r"(?<=[.!?])\s+", line)
            kept = [p for p in parts if p and not _is_template(p)]
            line = " ".join(kept).strip()
            if not line:
                continue
        out.append(line)
    return "\n".join(out).strip()


def collapse_source_dumps(md: str, *, files: Sequence[str] = (),
                          min_lines: int = DUMP_MIN_LINES) -> Tuple[str, int]:
    """A fenced block that is a whole source file becomes a pointer to the file."""
    names = [os.path.basename(str(f)) for f in files if f]
    count = 0

    def _rep(m: "re.Match[str]") -> str:
        nonlocal count
        body = m.group(1)
        lines = body.count("\n") + 1
        if lines < min_lines and len(body) < DUMP_MIN_CHARS:
            return m.group(0)
        if not SOURCE_HINT_RE.search(body):
            return m.group(0)
        before = (md[max(0, m.start() - 400):m.start()]).lower()
        name = next((n for n in names if n.lower() in before), "")
        if not name:
            name = next((n for n in names if n.lower().endswith(CODE_EXT)), "the file")
        count += 1
        return f"_(the source of {name} is in the file itself - {lines} lines, not repeated here)_"

    return FENCE_RE.sub(_rep, md or ""), count


def ensure_sources(md: str, file_urls: Sequence[str], *, goal: str = "") -> Tuple[str, int]:
    """Append the deliverable file's source URLs when the answer carries none."""
    urls = [u for u in dict.fromkeys(file_urls or []) if u]
    if not urls or urls_in(md):
        return md, 0
    urls = urls[:10]
    return (md.rstrip() + "\n\n## Sources\n" + "\n".join(f"- {u}" for u in urls)), len(urls)


def finish(md: str, *, goal: str = "", files: Sequence[str] = (),
           file_urls: Sequence[str] = ()) -> Tuple[str, List[str]]:
    """The deterministic clean-up of a delivered answer, with notes on what changed."""
    notes: List[str] = []
    m2 = strip_template_echo(md)
    if m2 != (md or "").strip():
        notes.append("removed the delivery template's own instructions echoed into the answer")
    m3, n = collapse_source_dumps(m2, files=files)
    if n:
        notes.append(f"replaced {n} pasted source file(s) with a pointer to the file")
    m4, added = ensure_sources(m3, file_urls, goal=goal)
    if added:
        notes.append(f"appended the deliverable file's {added} source URL(s) the answer had dropped")
    return m4, notes
