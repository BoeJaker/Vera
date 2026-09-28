"""
The canvas item vocabulary (A16 of Notes/42) on the chat page and in the canvas's own vocabulary: the session canvas
knows the notebook API (the cell runs through the notebook's exec); "Send to the notebook" lands the cell it made as a
keyed item; a panel item's page is resolved the way the chat resolves any panel; the context menu's terminal ·
notebook · panel actions reach the item's own controls; BLOCK_TYPES carries notebook and panel (unknown kinds fall
back to note). Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
PY = _read("vera", "canvas", "canvas_capabilities.py")


def test_the_vocabulary_carries_the_live_items():
    assert '"notebook": {"desc": "A notebook cell as an item' in PY
    assert '"panel":    {"desc": "A WHOLE panel as an item' in PY
    assert "host_id?:str, container?:str, shell?:str, ws?:str, attached?:bool" in PY, "the terminal's fields"
    assert '"notebook": "content",' in PY and '"panel": "panel"}.get(btype, "text")' in PY, "a bare string lands in the natural field"


def test_the_canvas_knows_the_notebook_api():
    assert "try{ el.setAttribute('nb-api', _NB_API_BASE()); }catch(_){}" in HTML


def test_send_to_the_notebook_lands_the_cell():
    assert "async function _cvLandCell(nbId, cell, cellType, lang, content){" in HTML
    assert "const key='notebook:'+nbId+':'+cell.id;" in HTML, "keyed, never twice"
    assert "_capCall('canvas.add',{session_id:SID, kind:'notebook', key, at:'now', size:'m'" in HTML
    assert "try{ const cell=await cellResp.json(); _cvLandCell(nbId, cell, cellType, lang, content); }catch(_){}" in HTML


def test_a_panel_items_page_is_resolved_like_any_panel():
    assert "document.addEventListener('vera:canvas:panel-src', async (ev)=>{" in HTML
    assert "const src=await _panelSrcAsync(p); if(src) await _capCall('canvas.update',{session_id:SID, key:d.key," in HTML


def test_the_context_menu_reaches_the_items_own_controls():
    assert "function _cvItemAct(key, act){" in HTML
    assert "loop:'loop',session:'terminal'})[kind]||kind" in HTML, "a session item is a terminal to the menu"
    assert "if(x.key&&(kind==='terminal'||kind==='panel')) return _cvItemAct(x.key,'open');" in HTML
    assert "if(x.key&&kind==='notebook') return _cvItemAct(x.key,'nbopen');" in HTML
    assert "if(x.key&&kind==='terminal') return _cvItemAct(x.key,'tattach');" in HTML
    assert "case 'standalone': if(x.key) return _cvItemAct(x.key,'pstand'); break;" in HTML
    assert "case 'refresh': if(x.key) return _cvItemAct(x.key,'prefresh'); break;" in HTML
    assert "if(x.key&&kind==='notebook') return _cvItemAct(x.key,'nbrun');" in HTML
    assert "_cmOpen,_cmClose,_crunOpen,_cmTarget,_cvLandCell,_cvItemAct" in HTML
