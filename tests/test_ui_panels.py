"""Every registered UI panel renders standalone (requires the full app).

Uses the in-process ASGI transport (no live server) via asyncio.run, so no
pytest-asyncio dependency is needed.
"""

import asyncio


def test_operator_panel_registered(orch):
    assert "operator-studio" in orch.UI_PANELS


def test_all_panels_render(orch):
    import httpx

    async def _run():
        transport = httpx.ASGITransport(app=orch.APP)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            failures = []
            for pid in list(orch.UI_PANELS.keys()):
                r = await c.get("/ui/panel/window", params={"id": pid})
                if r.status_code != 200 or len(r.text) < 40:
                    failures.append((pid, r.status_code, len(r.text)))
            assert not failures, f"panels failed to render: {failures}"

    asyncio.run(_run())


def test_operator_panel_route_serves_html(orch):
    import httpx

    async def _run():
        transport = httpx.ASGITransport(app=orch.APP)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/operator/panel")
            assert r.status_code == 200
            assert "Operator Studio" in r.text

    asyncio.run(_run())
