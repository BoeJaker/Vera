"""
The composer's modes, the formats a reply may carry, and the attachments a question carries (UI redesign
chat-modes; the Canvas, Paste and Formats boards; Notes/40 §5):

  • the composer's foot holds one chip per mode the top bar already has (Context · Web · Attach · Voice · Council ·
    Diffuse) — each chip calls the SAME handler the top-bar button calls; nothing is a second implementation;
  • the voice · council · diffuse strips sit in one box above the composer: council and diffuse are the chat's own
    panels MOVED there at init (never a copy), voice is painted from the mic's own state (MIC_ACTIVE, the level the
    capture already measures, the pause the config already holds);
  • a pasted document still goes to the model as the fenced prefix it always did, and ALSO shows as a card on the
    sent question (collapsed, excerpt on open, Open · Copy) — and a question read back from history gets its cards
    from the very prefix _buildDocsPrefix wrote;
  • ONE markdown renderer: the chat renders replies through the shared vera_markdown element (headings, lists, task
    lists, tables, blockquotes, callouts, footnotes, math) and only supplies its fenced-block cards through the
    element's fence hook; the older inline renderer remains as the fallback;
  • a mermaid fence renders as the tokens arrive: the element's stream() places complete lines, ghosts the pending
    one, never shows an error banner mid-stream; the chat keeps ONE element alive across streaming repaints and
    settles it on the closing fence;
  • widgets in the reply: a ```widget fence (the model's choice) or a capability result's shape (by rule) becomes a
    block carrying the same record widget.template.* normalises; Pin to canvas copies the record through
    canvas.append, deduplicated on its key.
The files are text, so this runs anywhere.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
MD = _read("vera", "vera_markdown_element.js")
MM = _read("vera", "render", "vera_mermaid.js")


def _fn(name, src=HTML):
    """the body of a top-level function declaration (up to the next top-level function)"""
    m = re.search(r"\n  (?:async )?function %s\(" % re.escape(name), src)
    assert m, "no function " + name
    nxt = re.search(r"\n  (?:async )?function \w+\(|\n  /\* ═", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


# ── the composer's foot: one chip per mode, calling the handler the top bar already calls ────────────────────────

def test_composer_chips_are_one_per_mode_and_mirror_the_top_bar():
    chips = re.findall(r'<span class="cchip" id="cmpChip(\w+)" onclick="CH\._cmpChip\(\'(\w+)\'\)"', HTML)
    assert [c[1] for c in chips] == ["context", "web", "attach", "voice", "council", "diffuse"]
    body = _fn("_cmpChip")
    # the same handlers as the top-bar buttons — no second implementation of any mode
    for key, handler in (("web", "toggleWebSearch()"), ("voice", "toggleMic()"), ("council", "toggleCouncil()"), ("diffuse", "toggleSD()")):
        assert re.search(r"k==='%s'\)\{ %s" % (key, re.escape(handler)), body), key
    assert "VeraLHM.pick('context/ctx')" in body            # Context opens the unified menu's Context tab
    for handler in ("toggleWebSearch", "toggleMic", "toggleCouncil", "toggleSD", "toggleTts"):
        assert re.search(r'onclick="CH\.%s\(\)"' % handler, HTML), handler + " is still on the top bar"


def test_chips_read_the_modes_own_state():
    body = _fn("_cmpSync")
    assert "on('cmpChipVoice', MIC_ACTIVE)" in body
    assert "on('cmpChipCouncil', COUNCIL_MODE); on('cmpChipDiffuse', SD_MODE)" in body
    assert "_DOCS.length" in body and "CTX_NODES.length" in body
    assert "webSearchBtn" in body                              # web follows the button's own class
    assert "_cmpTimer=setInterval(_cmpSync, 500)" in _fn("_cmpMount")


# ── the strips: the chat's own panels moved above the composer, voice painted from the mic's state ───────────────

def test_council_and_diffuse_are_moved_not_copied():
    assert HTML.count('id="councilPanel"') == 1 and HTML.count('id="sdPanel"') == 1
    mount = _fn("_cmpMount")
    assert "['councilPanel','sdPanel'].forEach" in mount and "box.insertBefore(p, voice)" in mount
    # the council's mode segment drives the select the chat already reads; the diffuse toggle drives the config box
    assert "sel.value=m[0]; sel.dispatchEvent(new Event('change'))" in mount
    assert "[['vote','Vote'" in mount and "['combine','Synthesise'" in mount and "['show','Show all'" in mount
    assert "cfgIllustrate" in mount
    assert "#councilPanel.on.cmp-strip,#sdPanel.on.cmp-strip{display:flex}" in HTML
    assert re.search(r'<div id="cmpStrips">\s*<div id="cmpVoice" class="cmp-strip cmp-voice"', HTML)
    assert re.search(r'</div>\s*<div id="inputBar">', HTML[HTML.index('id="cmpStrips"'):])


def test_voice_strip_is_painted_from_the_mic():
    paint = _fn("_cmpPaintVoice")
    assert "st.style.display=MIC_ACTIVE?'flex':'none'" in paint
    assert "busy?'speaking':(_micTx>0?'transcribing':'listening')" in paint
    assert "_micT0" in paint and "cfgVadPause" in paint and "TTS_ON" in paint
    # the capture publishes its level; the transcription counter wraps the segment call; the UI hook repaints
    assert "_micLevel=v;" in HTML
    assert "_micTx++; let txt=''; try{ txt=await _sttSegment(samples,rate); } finally { _micTx=Math.max(0,_micTx-1); }" in HTML
    assert re.search(r"function _micUi\(on\)\{\n    MIC_ACTIVE=!!on;\n    try\{ _micHist=\[\]; _cmpPaintVoice\(\); \}", HTML)
    for el in ("cmpVMeter", "cmpVWord", "cmpVTime", "cmpVHint", "cmpVPause", "cmpVTts"):
        assert 'id="%s"' % el in HTML, el


def test_strips_and_chips_are_mounted_never_fatally():
    assert "try{ _cmpMount(); }catch(e){ console.warn('composer modes: mount failed', e); }" in HTML
    for name in ("_cmpChip", "_cmpSync", "_paOpen", "_paCopy", "_wRead", "_wXl", "_wForm", "_wPin"):
        assert re.search(r"\n    [^\n]*\b%s," % name, HTML), name + " is exported on CH"


# ── attachments on the question ─────────────────────────────────────────────────────────────────────────────────

def test_pasted_docs_still_reach_the_model_and_become_cards():
    send = _fn("send")
    i = send.index("_LAST_SENT_DOCS=hasDocs?_DOCS.map")
    assert i < send.index("msg = docPrefix + (msg||")          # the stash is taken before the prefix folds in
    assert "_docsClearAfterSend();" in send                     # the lifecycle is unchanged
    assert "_paDecorate(uMsg.body, uCtx);" in HTML              # the plain send path
    assert "_paDecorate(uCouncil.body," in HTML                 # and the council path
    assert "_paFromMarkdown(displayTxt)" in HTML                 # history read-back
    render = _fn("_paRenderAttachments")
    for piece in ('class="pa-att"', 'CH._paOpen(this)', 'CH._paCopy(this)', "lines.slice(0,12)", "attached inline"):
        assert piece in render, piece


def test_history_cards_parse_exactly_the_prefix_the_chat_writes():
    # _buildDocsPrefix writes "### title\n```lang\nbody\n```" blocks joined by blank lines; the parser reads that shape
    assert "return `### ${d.title}\\n\\`\\`\\`${lang}\\n${d.body}\\n\\`\\`\\``;" in HTML
    body = _fn("_paFromMarkdown")
    assert r"/^### ([^\n]+)\n```([^\n]*)\n([\s\S]*?)\n```\n*/" in body


# ── one markdown renderer ───────────────────────────────────────────────────────────────────────────────────────

def test_chat_renders_through_the_shared_element_with_its_cards_as_the_hook():
    assert '<script src="/ui/elements/vera_markdown.js"></script>' in HTML
    r = _fn("renderMd")
    assert "VeraMD.render(t,{fence:(lang,info,code,st)=>_mdFenceCard(lang,info,code,st,opts)})" in r
    assert "VeraMD.version>=2" in r
    assert "let s=t.replace(/&/g,'&amp;')" in r                  # the fallback stays
    card = _fn("_mdFenceCard")
    for piece in ("CH._codeCopy(", "CH._codeSaveArtifact(", "CH._codePreview(", "CH._codeRun(", "CH._codeExec(", "highlightCode(raw, lang)", 'data-incomplete="1"'):
        assert piece in card, piece
    assert HTML.count("CH._codeSaveArtifact('${id}',this)") == 1   # the card is built in one place


def test_shared_element_has_the_hook_and_the_blocks_a_reply_may_contain():
    assert "function render(md, opts)" in MD and "opts.fence" in MD
    assert "fence(f.lang, f.info, f.code, { incomplete: f.incomplete })" in MD
    assert "render(bq.map(unesc).join('\\n'), opts)" in MD        # the hook survives recursion
    assert "return out.join('');" in MD                          # no newlines for a pre-wrap host
    for kind in ("note", "warning", "caution", "stop", "tip"):
        assert "%s:" % kind in MD, kind
    assert 'class="vmd-call ' in MD and 'class="vmd-fn"' in MD and 'class="vmd-math"' in MD and 'class="vmd-sup"' in MD
    assert "window.VeraMD = { render, escape: esc, fenceCard, version: 2 };" in MD
    # the chat styles those blocks in a message body
    for sel in (".msg-body table", ".msg-body .vmd-call", ".msg-body .vmd-fn", ".msg-body .vmd-math", ".msg-body ul,.msg-body ol", ".msg-body blockquote"):
        assert sel in HTML, sel


# ── a diagram that renders as the tokens arrive ─────────────────────────────────────────────────────────────────

def test_mermaid_element_streams_complete_lines_and_ghosts_the_tail():
    assert "stream(code) {" in MM
    s = MM[MM.index("stream(code) {"):MM.index("_paint(result, type) {")]
    assert "const partial = /\\n$/.test(code) ? '' : (lines.pop() || '');" in s
    assert "if (!gA.nodes.has(id)) n.ghost = true;" in s and "gB.edges[i].ghost = true;" in s
    assert "never an error banner" in s and "waiting for a complete line" in s
    assert "vm:stream" in s
    assert 'stroke-dasharray="3 3"' in MM                        # a ghost node is dashed
    assert "e.ghost ? t.dim : t.line" in MM                      # a ghost edge is dim
    assert "settled" in MM                                       # render() after stream() says so once


def test_mermaid_labels_keep_their_own_operators():
    assert "masked = line.replace(" in MM and "unmask(" in MM   # "digest == stored?" is a label, not a thick edge


def test_chat_keeps_one_live_diagram_across_streaming_repaints():
    assert "_paintStream(bubEl, fullText);" in HTML
    p = _fn("_paintStream")
    assert "_MM_LIVE_KEY=key;" in p and "_MM_LIVE_EL[key]" in p and "el.stream(code)" in p
    card = _fn("_mdFenceCard")
    assert "st.incomplete && _MM_LIVE_KEY" in card and 'data-mm-live="1" data-live="1"' in card
    # the hydrator settles that same element on the final render instead of remounting
    h = _fn("_hydrateMermaidSlots")
    assert "const live=key&&_MM_LIVE_EL[key]; if(live){ el=live; delete _MM_LIVE_EL[key]; }" in h


# ── widgets in the reply ────────────────────────────────────────────────────────────────────────────────────────

def test_widget_fence_and_rule_share_one_record_and_one_block():
    card = _fn("_mdFenceCard")
    assert "L==='widget' && !st.incomplete" in card and "_widgetFence(raw)" in card
    rec = _fn("_widgetRecord")
    # the short form's window / size / refresh are sugar for reads.* and draw.* — the registry's shape
    for key in ("reads:{cap, args, every:", "draw:{form:", "placed:['reply']", "source:{origin:by==='rule'?'rule':'aide'"):
        assert key in rec, key
    shape = _fn("_widgetFormByShape")
    for form in ("'radial'", "'trace'", "'thermo'", "'heat'", "'log'", "'lane'", "'pipes'", "'table'", "'files'"):
        assert form in shape, form
    assert "ruleWidget=_widgetBlock(_widgetRecord({form:f,source:capName,args:capArgs,name:capName},'rule'),'rule',content)" in HTML
    block = _fn("_widgetBlock")
    for act in ("CH._wPin(", "CH._wXl(", "CH._wForm("):
        assert act in block, act
    assert "by <b>rule</b>" in block and "chosen by <b>aide</b>" in block


def test_pin_to_canvas_copies_the_record_and_dedupes_on_its_key():
    pin = _fn("_wPin")
    assert "_capCall('canvas.append',{id:cid,type:'widget',content:content,meta:{key,from:'reply',by:w.by}})" in pin
    assert "b.type==='widget'&&b.meta&&b.meta.key===key" in pin
    assert "_capCall('canvas.show'" in pin
    assert "delete content.data" in pin                          # a copy of the record, not of the data


def test_widget_reads_only_quiet_capabilities_on_its_own():
    r = _fn("_widgetReadable")
    assert "write|delete|remove|create|run|exec|kill|restart|stop|start|set|save|send|post|push|upsert" in r
    read = _fn("_wRead")
    assert "if(!forced&&!_widgetReadable(w.rec.reads.cap)) return;" in read
    assert "every>=10" in read                                   # refresh never tighter than 10 s


def test_the_model_is_told_about_the_widget_fence():
    assert "Widgets IN the reply" in HTML and "```widget fenced block" in HTML
