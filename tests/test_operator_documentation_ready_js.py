"""The documentation capture's readiness predicates, run in a real Chromium.

These are JavaScript, so a fake page cannot exercise them; each case below is a
page shape a real panel had when it was misjudged.
"""
import asyncio

import pytest

from vera.operator.missions.documentation import (
    _MEANINGFUL_CONTENT_JS, _READY_COUNT_JS, _READY_PAINTED_JS, _READY_TEXT_JS)

playwright = pytest.importorskip("playwright.async_api")

FILLER = "<div style='height:400px'></div>"
WORDS = "Agent loop configuration with planner, controllers and tool budgets"


async def _eval(html, expression, arg=None):
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # browser binary not installed here
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content(html)
            await page.wait_for_timeout(50)
            return await page.evaluate(f"({expression})", arg) if arg is not None \
                else await page.evaluate(f"({expression})()")
        finally:
            await browser.close()


def _run(html, expression, arg=None):
    return asyncio.run(_eval(html, expression, arg))


def test_content_inside_an_open_shadow_root_counts():
    html = (FILLER + "<div id='host'></div><script>"
            "document.getElementById('host').attachShadow({mode:'open'})"
            f".innerHTML = '<p>{WORDS}</p>';</script>")
    assert _run(html, _MEANINGFUL_CONTENT_JS) is True


def test_content_inside_a_same_origin_iframe_counts():
    html = FILLER + f"<iframe srcdoc='<p>{WORDS}</p>'></iframe>"
    async def _with_frame():
        async with playwright.async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as exc:
                pytest.skip(f"chromium unavailable: {exc}")
            try:
                page = await browser.new_page()
                await page.set_content(html)
                await page.wait_for_function("() => { const f=document.querySelector('iframe');"
                                             " return f.contentDocument && f.contentDocument.body"
                                             " && f.contentDocument.body.innerText.length > 0; }")
                return await page.evaluate(f"({_MEANINGFUL_CONTENT_JS})()")
            finally:
                await browser.close()
    assert asyncio.run(_with_frame()) is True


def test_a_shadow_stylesheet_and_a_palette_button_are_not_content():
    css = "x{color:red} " * 40
    html = (FILLER + "<button>&#127912;</button><div id='host'></div><script>"
            "document.getElementById('host').attachShadow({mode:'open'})"
            f".innerHTML = '<style>{css}</style><div></div>';</script>")
    assert _run(html, _MEANINGFUL_CONTENT_JS) is False


@pytest.mark.parametrize("placeholder", [
    "Loading…", "Checking the estate…", "Waiting for an agentic run…",
    "Loading palette…", "reading sysmon.status…"])
def test_a_visible_placeholder_line_means_not_ready_whatever_surrounds_it(placeholder):
    html = (FILLER + f"<nav>{WORDS} {WORDS}</nav><div id='host'></div><script>"
            "document.getElementById('host').attachShadow({mode:'open'})"
            f".innerHTML = '<p>{placeholder}</p>';</script>")
    assert _run(html, _MEANINGFUL_CONTENT_JS) is False


def test_words_that_merely_mention_loading_are_not_placeholders():
    html = FILLER + f"<p>{WORDS}</p><p>Demo data loaded from the fabric</p>"
    assert _run(html, _MEANINGFUL_CONTENT_JS) is True


def test_hidden_text_is_not_content():
    html = FILLER + f"<div style='display:none'>{WORDS}</div>"
    assert _run(html, _MEANINGFUL_CONTENT_JS) is False


def _canvas(draw):
    return ("<canvas id='cv' width='600' height='400'></canvas><script>"
            "const g = document.getElementById('cv').getContext('2d');"
            "g.fillStyle = '#050505'; g.fillRect(0, 0, 600, 400);" + draw + "</script>")


def test_a_canvas_filled_only_with_its_background_is_not_painted():
    assert _run(_canvas(""), _READY_PAINTED_JS, "#cv") is False


def test_a_canvas_with_a_drawn_graph_is_painted():
    draw = ("g.fillStyle = '#4fa3ff';"
            "for (let i = 0; i < 300; i++) { g.beginPath();"
            " g.arc(50 + (i * 37) % 500, 40 + (i * 53) % 320, 6, 0, 7); g.fill(); }")
    assert _run(_canvas(draw), _READY_PAINTED_JS, "#cv") is True


@pytest.mark.parametrize("text,ready", [
    ("938 nodes · 1587 edges", True),
    ("1 nodes · 0 edges loading", False),
    ("2D VIEW -- 0 nodes / 0 edges", False),
    ("2D VIEW -- 412 nodes / 700 edges", True),
    ("fabric graph — 0 nodes, 0 edges", False),
    ("400", True),
    ("0", False),
])
def test_count_reads_the_first_quantity_and_rejects_loading(text, ready):
    assert _run(f"<span id='s'>{text}</span>", _READY_COUNT_JS, "#s") is ready


@pytest.mark.parametrize("text,ready", [
    ("Loading…", False),
    ("1 nodes · 0 edges loading", False),
    ("DATASETS 0 626,262 records", True),
    ("Demo data loaded", True),
])
def test_text_rejects_loading_anywhere(text, ready):
    assert _run(f"<span id='s'>{text}</span>", _READY_TEXT_JS, "#s") is ready
