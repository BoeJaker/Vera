"""A document wrapped in an unclosed fence is saved without the fence line (live
2026-10-03, session 6ef06017: why_tests.md began with ```markdown file=why_tests.md)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dfc", ROOT / "vera" / "dag" / "doc_fence_core.py")
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_the_live_case():
    t = "```markdown file=why_tests.md\n# Why Unit Tests Matter\n\n1. a\n2. b\n"
    assert D.unwrap(t) == "# Why Unit Tests Matter\n\n1. a\n2. b\n"


def test_a_closed_wrapper_loses_both_fences():
    assert D.unwrap("```md\n# T\nbody\n```\n") == "# T\nbody\n"


def test_a_document_ending_in_its_own_code_block_keeps_it():
    doc = "```markdown file=README.md\n# Usage\n\n```bash\nrun.sh\n```\n"
    assert D.unwrap(doc) == "# Usage\n\n```bash\nrun.sh\n```\n"


def test_untouched_when_not_wrapped():
    for t in ("# Title\n\ntext\n", "```python\nprint(1)\n```\n", ""):
        assert D.unwrap(t) == t


def test_prose_author_fallback_uses_it():
    w = SRC[SRC.index("async def cap_prose_author("):]
    w = w[:w.index("# ── One repair pass")]
    assert "_doc_fence_core.unwrap(" in w
