"""A writer reply that is its planning notes is never saved as the document, and a
prose.author write refreshes the run's file registry (probe 3 Oct, 7c5bf52c: report.md v1
was the model's plan; v2 was real but the registry kept serving v1, so the verifier failed
the step twice more and the run reworked the report until its budget ran out)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("woc", ROOT / "vera" / "dag" / "writer_output_core.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

PLAN = """    *   **Content:** A structured multi-section Markdown report comparing at least four open-source vector databases.
    *   **Key Aspects to Cover:** Features, Licensing, Performance Claims.
    *   **Grounding:** Specifically on the fetched text from 'Vector Database Benchmarks'.
    *   Note: The prompt says "SOURCE MATERIAL - what this run already fetched". I must treat this as the actual data.

3.  **Drafting Plan
5.  **Formatting:**
    *   Start with:"""
REPORT = """# Vector Database Performance Benchmarks Report

## Executive Summary

This document presents benchmark results. The prompt says nothing here; I will not plan.
Qdrant leads at 4 ms p50 [Lushbinary](https://lushbinary.com/blog/x)."""


def test_the_live_plan_is_recognised():
    assert W.looks_like_planning(PLAN)


def test_a_real_document_is_not():
    assert not W.looks_like_planning(REPORT)          # a title up top wins over stray phrases
    assert not W.looks_like_planning("Plain text note without markers.")
    assert not W.looks_like_planning("")


def test_prose_author_refuses_to_save_planning():
    w = SRC[SRC.index("async def cap_prose_author("):]
    w = w[:w.index("# ── One repair pass")]
    assert "_writer_output_core.looks_like_planning(" in w
    assert "_writer_output_core.RETRY_NOTE" in w


def test_a_prose_author_write_refreshes_the_registry():
    assert 'tool in ("code.author", "code.edit", "prose.author") and isinstance(invoke.get("result"), dict)' in SRC
