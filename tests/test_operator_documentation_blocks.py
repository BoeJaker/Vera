"""The documentation mission's doc blocks: what a run writes, keeps and leaves alone."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from vera.operator import browser_engine as _be
from vera.operator import targets as _targets
from vera.operator.docs import domain_map as _dm
from vera.operator.missions import documentation as _doc

pytestmark = pytest.mark.critical

DOMAIN = {"slug": "demo", "doc": "90-demo.md", "title": "Demo", "cap_prefixes": ["demo"],
          "panel_ids": ["alpha", "beta"], "match": [], "seed": None}
PAGES = {"slug": "overview", "doc": "", "title": "Overview", "cap_prefixes": [],
         "panel_ids": [], "match": [], "seed": None,
         "pages": [{"name": "dashboard", "path": "/", "label": "Main dashboard"}]}
DOC = """# 90 · Demo

Authored prose.

## Screenshots

<!-- VERA:AUTO:screenshots START -->
old
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
| curated table |
<!-- VERA:AUTO:capabilities END -->
"""


class _Ctx:
    def __init__(self):
        self.scripts = []
        self.routes = []

    async def add_init_script(self, script):
        self.scripts.append(script)

    async def route(self, matcher, handler):
        self.routes.append((matcher, handler))


VOID = {"id": "void", "type": "dark", "vars": {"--bg": "#000"}}


def _run(monkeypatch, tmp_path: Path, fail=(), started=None, caps_down=False, **params):
    shot_urls = []

    async def _ensure(target, call_cap, default_base):
        return {"ready": True, "kind": "live", "base_url": default_base}

    async def _fetch(client, url, timeout=15):
        if url.endswith("/ui/panels"):
            return [{"id": "alpha", "label": "Alpha"}, {"id": "beta", "label": "Beta"},
                    {"id": "stray", "label": "Unmapped"}]
        if url.endswith("/ui/themes"):
            return {"active": "ash", "themes": [VOID]}
        if caps_down:                                   # /mcp/tools timed out: _fetch_json returns None
            return None
        return [{"name": "demo.ping", "description": "Ping."}]

    async def _start(base_url, viewport=None):
        sess = SimpleNamespace(session_id="s1", page=None, context=_Ctx(), viewport=viewport)
        if started is not None:
            started.append(sess)
        return sess

    async def _close(session_id):
        return None

    async def _shoot(session, url, abspath, **kw):
        shot_urls.append(url)
        if any(f in url for f in fail):
            return {"ok": False, "failed_condition": "meaningful_content", "error": "empty"}
        Path(abspath).parent.mkdir(parents=True, exist_ok=True)
        Path(abspath).write_bytes(b"png")
        return {"ok": True, "readiness": []}

    monkeypatch.setattr(_dm, "DOMAINS", [DOMAIN, PAGES])
    monkeypatch.setattr(_dm, "PANEL_DOCS", {"alpha": "demo", "beta": "demo"})
    monkeypatch.setattr(_targets, "ensure_target", _ensure)
    monkeypatch.setattr(_doc, "_fetch_json", _fetch)
    monkeypatch.setattr(_doc, "_shoot_panel", _shoot)
    monkeypatch.setattr(_be, "playwright_available", lambda: True)
    monkeypatch.setattr(_be, "start_session", _start)
    monkeypatch.setattr(_be, "close_session", _close)

    docs = tmp_path / "documentation"
    (docs / "assets").mkdir(parents=True, exist_ok=True)
    if not (docs / "90-demo.md").exists():
        (docs / "90-demo.md").write_text(DOC, encoding="utf-8")
    result = asyncio.run(_doc.run_documentation_mission(
        {"target": "live", "base_url": "https://vera.test", "seed": False, **params},
        {"repo_root": str(tmp_path)}))
    return result, shot_urls, (docs / "90-demo.md").read_text(encoding="utf-8")


def test_write_caps_false_refreshes_screenshots_and_keeps_the_curated_table(monkeypatch, tmp_path):
    _, _, md = _run(monkeypatch, tmp_path, write_caps=False)

    assert "| curated table |" in md
    assert "demo.ping" not in md
    assert "assets/demo/alpha.png" in md and "assets/demo/beta.png" in md
    assert "Authored prose." in md


def test_a_failed_catalogue_fetch_leaves_every_capability_table(monkeypatch, tmp_path):
    """S31 / D32-3: a /mcp/tools timeout wrote "No capabilities resolved" over 33 of 42 pages' tables."""
    result, _, md = _run(monkeypatch, tmp_path, caps_down=True)

    assert "| curated table |" in md and "No capabilities resolved" not in md
    assert result["caps_written"] is False and "unavailable" in result["caps_note"]
    assert "assets/demo/alpha.png" in md                  # the screenshots still refresh


def test_a_working_catalogue_still_writes_the_table(monkeypatch, tmp_path):
    result, _, md = _run(monkeypatch, tmp_path)

    assert "demo.ping" in md and "| curated table |" not in md and result["caps_written"] is True


def test_the_catalogue_fetch_gets_a_longer_timeout():
    seen = {}

    class _Client:
        async def get(self, url, timeout=None):
            seen[url.rsplit("/", 1)[-1]] = timeout
            raise RuntimeError("down")
    for path in ("/ui/panels", "/mcp/tools"):
        assert asyncio.run(_doc._fetch_json(_Client(), "https://vera.test" + path)) is None
    assert seen["tools"] == _doc.CAPS_FETCH_TIMEOUT > seen["panels"]


def test_a_failed_capture_keeps_the_image_already_on_disk(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, write_caps=False)
    result, _, md = _run(monkeypatch, tmp_path, fail=("beta",), write_caps=False)

    assert [f["panel_id"] for f in result["capture_failures"]] == ["beta"]
    assert "assets/demo/beta.png" in md


def test_a_failed_capture_with_no_earlier_image_is_left_out(monkeypatch, tmp_path):
    _, _, md = _run(monkeypatch, tmp_path, fail=("beta",), write_caps=False)

    assert "assets/demo/alpha.png" in md
    assert "assets/demo/beta.png" not in md


def test_a_selective_recapture_keeps_the_panels_it_left_out(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, write_caps=False)
    _, urls, md = _run(monkeypatch, tmp_path, panels=["alpha"], write_caps=False)

    assert all("beta" not in u for u in urls)
    assert "assets/demo/alpha.png" in md and "assets/demo/beta.png" in md


def test_the_gallery_never_links_a_shot_removed_from_disk(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, write_caps=False)
    (tmp_path / "documentation/assets/demo/alpha.png").unlink()
    _run(monkeypatch, tmp_path, capture=False, write_caps=False)

    gallery = next((tmp_path / "documentation").glob("GALLERY*.md")).read_text(encoding="utf-8")
    assert "assets/demo/alpha.png" not in gallery
    assert "assets/demo/beta.png" in gallery


def test_the_capture_browser_gets_doc_prefs_not_the_servers(monkeypatch, tmp_path):
    started = []
    _run(monkeypatch, tmp_path, started=started, write_caps=False)

    sess = started[0]
    assert sess.viewport == {"width": 1440, "height": 900}
    script = sess.context.scripts[0]
    assert '"vera:ui:scale": "1"' in script
    assert "lhm" in script and "true" in script


def test_capture_prefs_and_viewport_can_be_overridden(monkeypatch, tmp_path):
    started = []
    _run(monkeypatch, tmp_path, started=started, write_caps=False,
         viewport={"width": 1920, "height": 1080}, ui_storage={"vera:ui:scale": "1.2"})

    assert started[0].viewport == {"width": 1920, "height": 1080}
    assert '"vera:ui:scale": "1.2"' in started[0].context.scripts[0]


def test_the_docs_theme_is_pinned_by_answering_ui_theme(monkeypatch, tmp_path):
    started = []
    _run(monkeypatch, tmp_path, started=started, write_caps=False)

    ctx = started[0].context
    assert '"vera:ui:theme": "void"' in ctx.scripts[0]
    matcher, _ = ctx.routes[0]
    assert matcher("https://vera.test/ui/theme") is True
    assert matcher("https://vera.test/ui/themes") is False


def test_unmapped_live_panels_are_reported_not_guessed(monkeypatch, tmp_path):
    result, urls, _ = _run(monkeypatch, tmp_path, write_caps=False)

    assert result["unmapped_panels"] == ["stray"]
    assert all("stray" not in u for u in urls)


def test_a_page_that_calls_the_model_runs_only_when_named(monkeypatch, tmp_path):
    PAGES["pages"].append({"name": "hero", "path": "/chat_panel", "on_request": True})
    try:
        _, urls, _ = _run(monkeypatch, tmp_path, domains=["overview"])
        assert "https://vera.test/chat_panel" not in urls
        _, urls, _ = _run(monkeypatch, tmp_path, domains=["overview"], panels=["hero"])
        assert urls == ["https://vera.test/chat_panel"]
    finally:
        PAGES["pages"].pop()


def test_pages_are_shot_by_path_to_a_stable_name_without_a_doc(monkeypatch, tmp_path):
    result, urls, _ = _run(monkeypatch, tmp_path, domains=["overview"])

    assert urls == ["https://vera.test/"]
    assert (tmp_path / "documentation/assets/overview/dashboard.png").exists()
    assert result["capture_failures"] == []
    assert not list((tmp_path / "documentation").glob("*-.md"))
