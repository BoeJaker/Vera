from pathlib import Path

import pytest

from vera.research.alias_compatibility import RESEARCH_ALIASES, research_alias


pytestmark = pytest.mark.critical


EXPECTED = {
    "research.report": ("single", "report", "research.run"),
    "research.parallel": ("parallel", "report", "research.run"),
    "research.deep": ("deep", "report", "research.run"),
    "research.code": ("deep", "code", "research.run"),
    "research.guide": ("single", "guide", "research.run"),
    "research.filestore": ("deep", "filestore", "research.run"),
    "research.quick_search": ("single", "report", "research.report"),
}


def test_legacy_alias_matrix_has_explicit_stable_semantics():
    assert set(RESEARCH_ALIASES) == set(EXPECTED)
    for name, (mode, output_mode, replacement) in EXPECTED.items():
        alias = research_alias(name)
        assert (alias.mode, alias.output_mode, alias.replacement) == (
            mode, output_mode, replacement)


def test_alias_request_preserves_context_and_normalizes_empty_context_mode():
    request = research_alias("research.deep").request(
        query="topic", project_id="project", context="prior", context_mode="")
    assert request == {
        "query": "topic",
        "mode": "deep",
        "output_mode": "report",
        "project_id": "project",
        "context": "prior",
        "context_mode": "fresh",
    }


def test_unknown_alias_fails_closed():
    with pytest.raises(ValueError, match="unknown research compatibility alias"):
        research_alias("research.missing")


def test_quick_search_is_not_misrepresented_as_direct_search():
    alias = research_alias("research.quick_search")
    assert alias.direct_search is False
    assert alias.request(query="fact")["output_mode"] == "report"

    source = (Path(__file__).parents[1] / "vera" / "research" /
              "researcher_api.py").read_text(encoding="utf-8")
    assert "It has the same long-running job behavior as research.report" in source
    assert "use web.search" in source


def test_every_compatibility_wrapper_dispatches_through_the_shared_matrix():
    source = (Path(__file__).parents[1] / "vera" / "research" /
              "researcher_api.py").read_text(encoding="utf-8")
    for name in EXPECTED:
        assert f'"{name}", query=query' in source
    assert source.count("return await _run_research_alias(") == len(EXPECTED)

