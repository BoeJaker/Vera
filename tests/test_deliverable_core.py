"""The answer is composed from the deliverable (plan item 23).

run70-73 (24 Sep 2026): research-web's file cited its sources every run and the
delivered answer never did; five deliverables opened with the delivery prompt's
own section instruction copied verbatim; one pasted the whole form.html.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag import deliverable_core as D  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
ECHO = ("## Result \u2014 Lead with the direct answer/outcome of the goal: the substance, not the "
        "process. If the goal was not fully achieved, say so plainly here.\n\n"
        "As of September 2026, WebGPU is supported in Chrome, Firefox and Safari.\n\n"
        "## What was done \u2014 a faithful, concrete account of the actions taken\n"
        "Searched three sources and wrote the summary file.\n")
HTML = "<!DOCTYPE html>\n<html>\n<head>\n<style>\n.error { color: red }\n</style>\n</head>\n<body>\n" \
       "<input id=\"email\" type=\"email\">\n<script>\nfunction validate() {\n  return true;\n}\n" \
       "document.getElementById('email').addEventListener('blur', validate);\n</script>\n</body>\n</html>"


def test_the_echoed_template_is_removed_and_the_answer_kept():
    out = D.strip_template_echo(ECHO)
    assert out.startswith("## Result\n")
    assert "Lead with the direct answer" not in out and "faithful, concrete account" not in out
    assert "WebGPU is supported in Chrome" in out and "## What was done\n" in out


def test_a_real_title_after_the_heading_stays():
    md = "# Result \u2014 Redis Licensing Change and Valkey Fork Report\n\nOn 20 March 2024 ..."
    assert D.strip_template_echo(md) == md


def test_a_pasted_source_file_becomes_a_pointer():
    md = "## Result\nform.html was created:\n\n```html\n" + HTML + "\n```\n\n## Artifacts\n- form.html"
    out, n = D.collapse_source_dumps(md, files=["form.html"])
    assert n == 1 and "<!DOCTYPE" not in out
    assert "the source of form.html is in the file itself" in out
    assert "## Artifacts" in out


def test_a_short_snippet_and_a_command_listing_stay():
    md = "```bash\ndu -sh /workspace\n```\n\n```\n" + "\n".join(f"{i}K  file{i}" for i in range(20)) + "\n```"
    out, n = D.collapse_source_dumps(md, files=["x.html"])
    assert n == 0 and out == md


def test_the_files_urls_are_appended_only_when_the_answer_has_none():
    urls = ["https://caniuse.com/webgpu", "https://developer.mozilla.org/docs/Web/API/WebGPU_API"]
    out, added = D.ensure_sources("WebGPU is supported everywhere.", urls)
    assert added == 2 and out.endswith("- " + urls[1]) and "## Sources" in out
    same, added2 = D.ensure_sources("See https://caniuse.com/webgpu.", urls)
    assert added2 == 0 and same == "See https://caniuse.com/webgpu."
    assert D.urls_in("cite https://a.example/x, and (https://b.example/y).") == ["https://a.example/x", "https://b.example/y"]


def test_document_files_picks_the_documents_not_the_code():
    keys = ["file:form.html", "file:webgpu-browser-support-summary.md", "cmd", "file:notes.txt", "file:form.html"]
    assert D.document_files(keys) == ["webgpu-browser-support-summary.md", "notes.txt"]


def test_finish_reports_what_it_changed():
    md = ECHO + "\n```html\n" + HTML + "\n```\n"
    out, notes = D.finish(md, goal="write a short summary citing your sources",
                          files=["form.html"], file_urls=["https://caniuse.com/webgpu"])
    assert len(notes) == 3 and "## Sources" in out and "<!DOCTYPE" not in out
    assert "Lead with the direct answer" not in out


def _src():
    return open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()


def test_the_delivery_stage_reads_the_deliverable_and_finishes_the_answer():
    src = _src()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v6_deliver")
    body = ast.get_source_segment(src, fn)
    assert "_deliverable.document_files(" in body
    assert "_v6_read_artifact_text(session_id, _rel" in body
    assert "DELIVERABLE FILE(S) - compose the Result from these" in body
    assert "_deliverable.finish(" in body
    assert "NEVER paste a code file's source" in body
    assert "session_id=sid," in src and "agent_loop_v6.deliverable_shaped" in src
