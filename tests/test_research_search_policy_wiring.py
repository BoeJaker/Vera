from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


def test_research_and_web_dispatch_through_shared_policy():
    root = Path(__file__).parents[1] / "vera"
    research = (root / "research" / "researcher_api.py").read_text(encoding="utf-8")
    web = (root / "web" / "web_capabilities.py").read_text(encoding="utf-8")
    assert "return _engines.decode_redirect(url)" in research
    assert "await _engines.dispatch_search(" in research
    assert web.count("await _engines.dispatch_search(") == 2
