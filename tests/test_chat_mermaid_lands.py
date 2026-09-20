"""
Mermaid from the chat (agent V's hooks): a mermaid fence in a reply — a .mm-slot the mermaid element fills, its source in
_MM_BLOCKS — lands on the session canvas as a diagram item; the exploded scene's diagram cards carry their source; a
reply's landed items carry mermaid / record for the element. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_mermaid_fence_lands_as_a_diagram_item():
    src = _read("vera", "chat", "chat_panel.html")
    # the harvest is a table of hooks now; the rule is the same one, asserted by what it looks for and what it
    # makes rather than by the loop that used to hold it
    assert "cvHook('diagram', '.mm-slot[data-mm]'" in src, "a mermaid slot is a hook"
    assert "_MM_BLOCKS[el.dataset.mm]" in src, "and it takes the source renderMd kept"
    assert "kind:'diagram'" in src and "k:'diagram'" in src, "which lands as a diagram item"
    assert src.index("const _MM_BLOCKS={};") < src.index("function _cvHarvest")


def test_exploded_cards_and_landed_items_carry_their_source():
    src = _read("vera", "chat", "chat_panel.html")
    assert "made.push({n:'Diagram', d:'mermaid', col:'#a78bfa', kind:'diagram', mermaid:src.slice(0,24000)});" in src   # a whole diagram (Notes/42 defect 52)
    assert "mermaid:m.kind==='diagram'?m.content.mermaid:undefined, record:m.kind==='widget'?(m.content.record||undefined):undefined});" in src