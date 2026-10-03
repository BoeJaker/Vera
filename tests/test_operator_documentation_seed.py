import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from vera.operator import browser_engine as _be
from vera.operator import targets as _targets
from vera.operator.docs import domain_map as _dm
from vera.operator.missions import documentation as _doc
from vera.operator.missions import seeds as _seeds


pytestmark = pytest.mark.critical


def _seeded_slug() -> str:
    return next(d["slug"] for d in _dm.DOMAINS if d.get("seed"))


def _run(monkeypatch, tmp_path: Path, **params):
    seeded = []

    async def _ensure(target, call_cap, default_base):
        return {"ready": True, "kind": "live", "base_url": default_base}

    async def _fetch(client, url):
        return [{"id": "p1", "label": "P1"}] if url.endswith("/ui/panels") else []

    class _Ctx:
        async def add_init_script(self, script):
            return None

    async def _start(base_url, viewport=None):
        return SimpleNamespace(session_id="s1", page=None, context=_Ctx())

    async def _close(session_id):
        return None

    async def _seed(name, call_target):
        seeded.append(name)
        return {"ok": True, "notes": []}

    monkeypatch.setattr(_targets, "ensure_target", _ensure)
    monkeypatch.setattr(_doc, "_fetch_json", _fetch)
    monkeypatch.setattr(_be, "playwright_available", lambda: True)
    monkeypatch.setattr(_be, "start_session", _start)
    monkeypatch.setattr(_be, "close_session", _close)
    monkeypatch.setattr(_seeds, "run_seed", _seed)

    result = asyncio.run(_doc.run_documentation_mission(
        {"target": "live", "base_url": "https://vera.test", "write_docs": False,
         "domains": [_seeded_slug()], **params},
        {"repo_root": str(tmp_path)}))
    return result, seeded


def test_seed_false_never_writes_fixtures_to_the_target(monkeypatch, tmp_path: Path):
    result, seeded = _run(monkeypatch, tmp_path, seed=False)

    assert "error" not in result
    assert seeded == []


def test_seed_defaults_on(monkeypatch, tmp_path: Path):
    result, seeded = _run(monkeypatch, tmp_path)

    assert "error" not in result
    assert seeded == [next(d["seed"] for d in _dm.DOMAINS if d.get("seed"))]
