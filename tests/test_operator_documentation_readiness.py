import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from vera.operator.missions.documentation import _shoot_panel, _wait_ready


pytestmark = pytest.mark.critical


class _FirstLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def click(self, timeout):
        if self.page.fail_click:
            raise TimeoutError("click timed out")


class _Page:
    def __init__(self, failures=()):
        self.failures = set(failures)
        self.fail_click = "click" in self.failures
        self.screenshots = []

    async def goto(self, *args, **kwargs):
        if "navigation" in self.failures:
            raise TimeoutError("navigation timed out")

    async def wait_for_load_state(self, *args, **kwargs):
        if "network_idle" in self.failures:
            raise TimeoutError("network idle timed out")

    async def wait_for_function(self, expression, *, arg=None, timeout=None):
        condition = ("count" if arg is not None and "match" in expression
                     else "text" if arg is not None else "meaningful_content")
        if condition in self.failures:
            raise TimeoutError(f"{condition} timed out")

    async def wait_for_selector(self, *args, **kwargs):
        if "selector" in self.failures:
            raise TimeoutError("selector timed out")

    async def evaluate(self, *args, **kwargs):
        if "assets_settled" in self.failures:
            raise TimeoutError("assets timed out")

    async def wait_for_timeout(self, *args, **kwargs):
        return None

    def locator(self, selector):
        return _FirstLocator(self)

    async def screenshot(self, **kwargs):
        self.screenshots.append(kwargs)


def test_default_capture_requires_meaningful_content(tmp_path: Path):
    page = _Page({"meaningful_content"})
    result = asyncio.run(_shoot_panel(
        SimpleNamespace(page=page), "https://vera.test/panel",
        str(tmp_path / "panel.png")))

    assert result["ok"] is False
    assert result["failed_condition"] == "meaningful_content"
    assert "meaningful_content" in result["error"]
    assert page.screenshots == []


def test_explicit_selector_failure_is_named_and_blocks_capture(tmp_path: Path):
    page = _Page({"meaningful_content", "selector"})
    result = asyncio.run(_shoot_panel(
        SimpleNamespace(page=page), "https://vera.test/panel",
        str(tmp_path / "panel.png"), state={"ready_selector": "#graph canvas"}))

    assert result["ok"] is False
    assert result["failed_condition"] == "selector:#graph canvas"
    assert result["readiness"][-1] == {
        "condition": "selector:#graph canvas", "required": True, "status": "timeout"}
    assert page.screenshots == []


def test_explicit_readiness_allows_optional_timeouts(tmp_path: Path):
    page = _Page({"network_idle", "meaningful_content", "assets_settled"})
    result = asyncio.run(_shoot_panel(
        SimpleNamespace(page=page), "https://vera.test/panel",
        str(tmp_path / "panel.png"),
        state={"ready_selector": "#graph", "ready_text": "#graph-meta"}))

    assert result["ok"] is True
    by_condition = {item["condition"]: item for item in result["readiness"]}
    assert by_condition["network_idle"]["status"] == "timeout"
    assert by_condition["meaningful_content"]["required"] is False
    assert by_condition["selector:#graph"]["status"] == "ready"
    assert by_condition["text:#graph-meta"]["status"] == "ready"
    assert by_condition["assets_settled"]["status"] == "timeout"
    assert len(page.screenshots) == 1


def test_click_failure_names_action_and_never_takes_screenshot(tmp_path: Path):
    page = _Page({"click"})
    result = asyncio.run(_shoot_panel(
        SimpleNamespace(page=page), "https://vera.test/panel",
        str(tmp_path / "panel.png"), state={"click": "#show-graph"}))

    assert result["ok"] is False
    assert result["failed_condition"] == "click:#show-graph"
    assert page.screenshots == []


def test_navigation_failure_names_load_condition(tmp_path: Path):
    page = _Page({"navigation"})
    result = asyncio.run(_shoot_panel(
        SimpleNamespace(page=page), "https://vera.test/panel",
        str(tmp_path / "panel.png")))

    assert result["ok"] is False
    assert result["failed_condition"] == "navigation:domcontentloaded"
    assert page.screenshots == []


def test_wait_ready_returns_bounded_named_evidence():
    evidence = asyncio.run(_wait_ready(_Page(), 300, {}))

    assert evidence == [
        {"condition": "network_idle", "required": False, "status": "ready"},
        {"condition": "meaningful_content", "required": True, "status": "ready"},
        {"condition": "assets_settled", "required": False, "status": "ready"},
    ]


def test_positive_count_is_required_for_populated_capture(tmp_path: Path):
    page = _Page({"count"})
    result = asyncio.run(_shoot_panel(
        SimpleNamespace(page=page), "https://vera.test/panel",
        str(tmp_path / "panel.png"),
        state={"ready_selector": "#graph", "ready_count": "#graph-stats"}))

    assert result["ok"] is False
    assert result["failed_condition"] == "count:#graph-stats"
    assert page.screenshots == []
