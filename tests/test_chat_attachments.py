"""
Attachments of every kind in the chat (UI redesign, Notes/40 §5.2; the Paste
board): images, files, links and rich text through paste, drop, the picker and
/attach; POST /chat/attachment into the session's artifact store; chips per
kind; the size guard; the editor's language picker, preview and send-as; the
message builder's fences and [attachment …] reference lines; the history
parser reading both back as cards. Text-level, so it runs anywhere.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
PY = _read("vera", "chat", "chat_panels_capabilities.py")


def _fn(name):
    i = HTML.index("function %s(" % name)
    j = HTML.find("\n  function ", i + 10)
    return HTML[i:j if j > 0 else i + 20000]


def test_the_upload_route_puts_the_file_in_the_sessions_artifact_store():
    assert '@APP.post("/chat/attachment", include_in_schema=False)' in PY
    assert "async def _chat_attachment_upload(file: _AttUpload = _AttFile(...), session_id: str = _AttForm(\"\")):" in PY
    assert "write_artifact_file(relpath=rel, content=text, session_id=session_id)" in PY, "text goes through the sandbox-aware writer"
    assert "artifact_dir(session_id=session_id, create=True)" in PY and 'with open(_att_os.path.join(full, att_id + "_" + name), "wb") as fh:' in PY, "binaries on the host artifact dir"
    assert 'preview = "/exec/artifacts/download?session_id="' in PY, "served back by the existing download route"
    assert '"text_extracted": text[:200000], "pages": pages' in PY
    assert "from pypdf import PdfReader" in PY, "pdf text when pypdf is there"
    for k in ('".diff": "diff"', '".csv": "csv"', '".pdf": "pdf"', 'return "image"'):
        assert k in PY, k


def test_paste_reads_every_kind():
    p = _fn("_onPaste")
    assert "cd.files" in p and "it.kind==='file'" in p and "_attUpload(f)" in p, "files and images upload"
    assert "cd.getData('text/html')" in p and "_paIngestText(text, html)" in p
    ing = _fn("_paIngestText")
    assert "if(_paIsUrl(text)){" in ing and "kind:'link'" in ing, "a URL becomes a link chip"
    assert "_paHtmlToMd(html)" in ing and "kind:'rich'" in ing and "lang:'markdown'" in ing, "html becomes markdown"
    assert "if((text||'').length<PASTE_THRESHOLD) return false;" in ing, "a small paste still goes into the textarea"
    md = _fn("_paHtmlToMd")
    for piece in ("if(/^h[1-6]$/.test(tag))", "if(tag==='a'){", "if(tag==='ul'||tag==='ol'){", "if(tag==='table'){", "if(tag==='pre')"):
        assert piece in md, piece


def test_drop_picker_and_slash():
    d = _fn("_paDropInit")
    assert "document.addEventListener('dragenter'" in d and "document.body.classList.add('pa-dragging')" in d and "document.addEventListener('drop'" in d
    assert "[...dt.files].forEach(f=>_attUpload(f))" in d
    assert '<div id="paDrop" class="pa-drop"><span>Drop to attach · images · files · text · links</span></div>' in HTML
    assert 'body.pa-dragging .pa-drop{display:flex}' in HTML
    assert '<input type="file" id="paFile" multiple style="display:none" onchange="CH._paFilesChosen(this)">' in HTML
    assert "{name:'attach', alias:['file','upload']" in HTML and "_paPickFiles(); return true;" in HTML
    assert "else if(k==='attach'){ const ed=document.getElementById('docEditor'); if(_DOCS.length&&ed&&ed.style.display==='none') _docEditorOpen(_DOCS[0].id); else _paPickFiles(); }" in HTML
    assert "try{ _paDropInit(); }catch(_){}" in HTML


def test_upload_makes_a_chip_and_a_record():
    u = _fn("_attUpload")
    assert "fetch(BASE+'/chat/attachment',{method:'POST',body:fd})" in u and "fd.append('session_id', SID||'')" in u
    assert "thumb:isImg?URL.createObjectURL(file):''" in u, "an image chip carries its thumbnail"
    assert "d.att=j; d.rel=j.rel; d.preview=j.preview; d.pages=j.pages||0;" in u
    assert "if(j.text_extracted&&!isImg){ d.body=j.text_extracted;" in u, "extracted text can go inline"
    chips = _fn("_renderDocChips")
    assert "doc-chip-icon img" in chips and '">attached · \'+_DOCS.length' in chips and "_paGuardHtml()" in chips


def test_the_size_guard():
    assert "function _paTokens(){" in HTML and "function _paBudget(){" in HTML
    g = _fn("_paGuardHtml")
    assert "<b>Large.</b> Together these are" in g and "inline the diff" in g and "files for the rest" in g and "everything inline" in g
    gg = _fn("_paGuard")
    assert "if(mode==='all') d.send='inline'; else if(mode==='files') d.send='file'; else d.send=(k==='diff'||d.lang==='diff')?'inline':'file';" in gg
    assert "_paUploadText(d)" in gg, "a text sent as a file is stored first"


def test_the_editor_picker_preview_and_send_as():
    assert '<select id="docEditorLang" onchange="CH._docEditorLang(this.value)"' in HTML
    assert '<span id="docEditorSend" class="doc-send"' in HTML and 'data-send="file" onclick="CH._docEditorSend(\'file\')">file the model can open</button>' in HTML
    assert '<div id="docEditorPrev" class="doc-prev"></div>' in HTML
    pv = _fn("_paPreview")
    for piece in ("if(k==='image')", "if(k==='link')", "d.lang==='diff'", "d.lang==='csv'", "d.lang==='markdown'||k==='rich'", "renderMd(d.body.slice(0,4000))"):
        assert piece in pv, piece
    assert "d.langSet=true" in _fn("_docEditorLang")


def test_the_builder_writes_fences_and_reference_lines_the_parser_reads_back():
    b = _fn("_buildDocsPrefix")
    assert "refs.push('[attachment '+n+' · link] '+d.url)" in b
    assert "refs.push('[attachment '+n+' · image · '+d.title" in b
    assert "const inline=(d.send!=='file')||!d.att;" in b, "a file the model can open needs the stored copy, else it stays inline"
    assert "in this session\\'s artifacts — open it to read]" in b
    p = _fn("_paFromMarkdown")
    assert r"/^### ([^\n]+)\n```([^\n]*)\n([\s\S]*?)\n```\n*/" in p, "the fence shape is unchanged"
    assert r"/^\[attachment \d+ · ([a-z]+)(?: · ([^\]]*?))?\](?: (\S+))?\n*/" in p
    cards = _fn("_paRenderAttachments")
    assert "k==='link'?'a link · the URLs source'" in cards and "'as a file the model can open'" in cards
    assert "if(d.url){ try{ window.open(d.url,'_blank','noopener'); }catch(_){} return; }" in _fn("_paOpen")
    # the stash the sent question keeps carries every kind
    assert "kind:_paKindOf(d),url:d.url||'',preview:d.preview||'',bytes:d.bytes||0,pages:d.pages||0,send:d.send||'inline'" in HTML
    for name in ("_paPickFiles", "_paFilesChosen", "_paGuard", "_docEditorLang", "_docEditorSend", "_attUpload", "_paIngestText", "_buildDocsPrefix"):
        assert re.search(r"\n    [^\n]*\b%s," % name, HTML), name + " is exported on CH"
