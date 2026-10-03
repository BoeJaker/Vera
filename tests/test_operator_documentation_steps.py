"""Capture steps: put a panel into the state the docs show, or fail by name."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from vera.operator.missions import documentation as _doc

pytestmark = pytest.mark.critical


class _Loc:
    def __init__(self, page, sel, nth=0):
        self.page, self.sel, self.n = page, sel, nth

    def nth(self, n):
        return _Loc(self.page, self.sel, n)

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        if self.sel in self.page.missing:
            raise TimeoutError(self.sel)
        self.page.log.append(("click", self.sel, self.n))

    async def fill(self, text, timeout=None):
        self.page.log.append(("fill", self.sel, text))

    async def hover(self, timeout=None):
        self.page.log.append(("hover", self.sel))

    async def press(self, key, timeout=None):
        self.page.log.append(("press_on", self.sel, key))

    async def select_option(self, value, timeout=None):
        self.page.log.append(("select", self.sel, value))

    async def wait_for(self, state=None, timeout=None):
        raise TimeoutError("no login form")


class _Page:
    def __init__(self, missing=()):
        self.log, self.missing = [], set(missing)
        self.keyboard = SimpleNamespace(press=self._press)
        self.url = "https://vera.test/vscode/central/"

    async def _press(self, key):
        self.log.append(("press", key))

    def locator(self, sel):
        return _Loc(self, sel)

    async def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))

    async def wait_for_selector(self, sel, state=None, timeout=None):
        if sel in self.missing:
            raise TimeoutError(sel)
        self.log.append(("wait_for", sel))


def test_steps_run_in_order_with_their_arguments():
    page = _Page()
    asyncio.run(_doc._run_steps(page, [
        {"click": "#menu", "then_ms": 100},
        {"click": ".item", "nth": 2},
        {"fill": "#q", "text": "gpu"},
        {"press": "Enter"},
        {"wait_for": ".result"},
        {"select": "#mode", "value": "graph"},
    ]))
    assert page.log == [("click", "#menu", 0), ("wait", 100), ("click", ".item", 2),
                        ("fill", "#q", "gpu"), ("press", "Enter"), ("wait_for", ".result"),
                        ("select", "#mode", "graph")]


def test_a_step_that_cannot_be_done_fails_by_name():
    page = _Page(missing={"#absent"})
    with pytest.raises(_doc.PanelReadinessError) as err:
        asyncio.run(_doc._run_steps(page, [{"click": "#menu"}, {"click": "#absent"}]))
    assert err.value.condition == "step2:click:#absent"


def test_code_server_login_is_skipped_when_no_login_form_is_shown():
    page = _Page()
    asyncio.run(_doc._run_steps(page, [{"code_server_login": "central"}]))
    assert page.log == []


def test_a_panel_is_retried_once_when_the_target_comes_back(monkeypatch, tmp_path: Path):
    from vera.operator import browser_engine as _be
    from vera.operator import targets as _targets
    from vera.operator.docs import domain_map as _dm

    calls = []

    async def _shoot(session, url, abspath, **kw):
        calls.append(url)
        if len(calls) == 1:
            return {"ok": False, "failed_condition": "navigation:domcontentloaded", "error": "down"}
        Path(abspath).parent.mkdir(parents=True, exist_ok=True)
        Path(abspath).write_bytes(b"png")
        return {"ok": True, "readiness": []}

    async def _up(base_url, *a, **k):
        return True

    async def _ensure(target, call_cap, default_base):
        return {"ready": True, "kind": "live", "base_url": default_base}

    async def _fetch(client, url):
        return [{"id": "alpha", "label": "Alpha"}] if url.endswith("/ui/panels") else []

    class _Ctx:
        async def add_init_script(self, s):
            return None

        async def route(self, m, h):
            return None

    async def _start(base_url, viewport=None):
        return SimpleNamespace(session_id="s", page=None, context=_Ctx())

    async def _close(sid):
        return None

    monkeypatch.setattr(_dm, "DOMAINS", [{"slug": "demo", "doc": "", "title": "Demo",
                                          "cap_prefixes": [], "panel_ids": ["alpha"],
                                          "match": [], "seed": None}])
    monkeypatch.setattr(_dm, "PANEL_DOCS", {"alpha": "demo"})
    monkeypatch.setattr(_doc, "_shoot_panel", _shoot)
    monkeypatch.setattr(_doc, "_wait_target_up", _up)
    monkeypatch.setattr(_doc, "_fetch_json", _fetch)
    monkeypatch.setattr(_targets, "ensure_target", _ensure)
    monkeypatch.setattr(_be, "playwright_available", lambda: True)
    monkeypatch.setattr(_be, "start_session", _start)
    monkeypatch.setattr(_be, "close_session", _close)

    result = asyncio.run(_doc.run_documentation_mission(
        {"target": "live", "base_url": "https://vera.test", "seed": False, "write_docs": False},
        {"repo_root": str(tmp_path)}))
    assert len(calls) == 2
    assert result["capture_failures"] == []
    assert result["screenshots"] == 1
