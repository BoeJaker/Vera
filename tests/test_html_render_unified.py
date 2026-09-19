"""One way to show HTML, and the pop-out is the user's.

render.html and a ```html fenced block were two different things on screen. The cap got a bare titled iframe
whose only control was a pop-out; the fence got the code card (source toggle, Pane, Copy, Save, lint, auto-save
to artifacts) on a different sandbox policy. Same content, two looks, two sets of things you could do with it.

And render.html popped a floating window over the whole UI BY DEFAULT — `popout: bool = True`, with the
description advertising "default true", so a model that merely wanted to show a chart took over the screen every
time. The other three render caps already defaulted false; this one was the outlier. The card has always carried
its own pop-out button, so nothing is lost by letting the reader press it.

Verified end-to-end against the live cap before landing: render.html produced isCodeCard=true / isArtCard=false,
toolbar ["Copy", "⇩ Save", "Source", "Pane", "⤢ Pop out"], label "html · My Preview", the whole document drew
itself at 320px, and popWindows stayed 0 until the button was pressed, which made it 1.

The third surface, the canvas, is covered here too: see test_canvas_preview_has_room.
"""
from pathlib import Path
import inspect
import re

ROOT = Path(__file__).resolve().parents[1]
CHAT = (ROOT / "vera" / "chat" / "chat_panel.html").read_text(encoding="utf-8")
CANVAS = (ROOT / "vera" / "canvas" / "canvas_element.js").read_text(encoding="utf-8")
CAP = (ROOT / "vera" / "render" / "chat_render_capabilities.py").read_text(encoding="utf-8")


def test_render_html_does_not_pop_out_unless_asked():
    # the signature itself, not the prose about it
    sig = re.search(r"async def cap_render_html\((.*?)\) ->", CAP, re.S)
    assert sig, "cap_render_html not found"
    assert "popout: bool = False" in sig.group(1), (
        "render.html must not float a window over the user's UI by default"
    )
    # and the description must not tell the model otherwise — that is what it actually reads
    desc = CAP[CAP.index('"render.html"'):CAP.index("async def cap_render_html")]
    assert "default true" not in desc.lower(), "the description advertised popout=true"
    assert "DO NOT pass popout" in desc, "the model needs telling, not just a changed default"

    # the sibling caps were already right; keep them that way
    for name in ("cap_render_mermaid", "cap_render_chart"):
        s = re.search(r"async def " + name + r"\((.*?)\) ->", CAP, re.S)
        assert s and "popout: bool = False" in s.group(1), name


def test_render_screen_still_floats_because_that_is_its_whole_purpose():
    # render.screen exists to float a panel; it has no inline form to fall back to
    body = CAP[CAP.index("async def cap_render_screen"):]
    assert '"popout": True' in body


def test_html_from_the_cap_is_the_same_card_as_a_fenced_block():
    # the dispatch entry routes html to the code card rather than the artifact card
    assert "if(p.kind==='html') _htmlCardToChat(p); else _artToChat(p);" in CHAT
    assert "function _htmlCardToChat(p){" in CHAT
    assert "_mdFenceCard('html','',String(p.content||''),{},{ title:p.title||'' })" in CHAT, (
        "the cap's html must go through the SAME card builder a fence uses"
    )
    # and it gets the same post-render pass a reply's own blocks get
    assert "try{ _lintRenderedBlocks(m.body); }catch(_){}" in CHAT
    assert "try{ _maybeAutoSaveArtifacts(m.body); }catch(_){}" in CHAT
    # popping out is still POSSIBLE — it is just no longer done to the reader
    assert "if(p.popout){ const aid='a'+(++_artSeq); _ART_STORE[aid]=p; _artPop(aid); }" in CHAT


def test_the_card_offers_the_pop_out_the_cap_used_to_force():
    assert "function _codePop(id){" in CHAT
    assert "CH._codePop('${id}')" in CHAT
    assert "_codeCopy,_codePreview,_codeInline,_codeRun,_codePop," in CHAT, "exported on CH"
    # it reuses the artifact pop-out window rather than inventing a second kind
    pop = CHAT[CHAT.index("function _codePop(id){"):]
    assert "_artPop(aid);" in pop[:700]
    # and it renders through the card's own document builder, not a second one
    assert "content:_cbDoc(blk.lang, blk.code)" in CHAT


def test_canvas_preview_has_room():
    """A rendered page on the canvas was drawn into a 620x40 strip.

    The preview slot is a .vc-live, and a code item lands at size "s" (_cvLandSize gives 's' to everything that
    is not a diagram, table or image), so `.it[data-size="s"] .vc-live{height:40px}` applied to it. Measured on
    the canvas before: slotHeight 40, frameHeight 40, for a whole HTML document that previews itself on sight.
    Diagrams were given their own height long ago; previews never were. Measured after: 200.
    """
    assert '.it[data-size="s"] .vc-live{height:40px}' in CANVAS, "the size rule this has to beat"
    assert ".vc-live.vc-preview{height:260px}" in CANVAS
    for size, px in (("s", 200), ("l", 380), ("xl", 560)):
        assert '.it[data-size="%s"] .vc-live.vc-preview{height:%dpx}' % (size, px) in CANVAS, size
    # a dragged size still wins, exactly as it does for every other live slot
    assert ".it.sized .vc-live.vc-preview{height:auto}" in CANVAS
    # the preview slot must actually carry both classes, or none of the above selects it
    assert 'class="vc-live vc-preview"' in CANVAS


def test_the_chat_and_the_canvas_agree_on_what_draws_itself():
    """Both surfaces auto-render a whole document and make a fragment wait. Keep them in step."""
    chat = re.search(r"const _cbWholePage=\(lang,code\)=>(.*?);", CHAT, re.S)
    canvas = re.search(r"const WHOLE_PAGE = \(lang, code\) =>(.*?);", CANVAS, re.S)
    assert chat and canvas
    norm = lambda s: re.sub(r"\s+", "", s.group(1))
    assert norm(chat) == norm(canvas), (
        "the chat and the canvas disagree on what counts as a whole page:\n"
        f"  chat  : {norm(chat)}\n  canvas: {norm(canvas)}"
    )
