// 2026-10-03: a parked chat frame comes back with its transcript; the LHM's small graphs have no ground with blocks off
// and keep their controls behind ☰; the LHM terminal survives the menu's redraws; the dashboard's tiles lose the
// "fine" dot, gain a larger title, and scroll with quiet scrollbars only when there is something to scroll
//   node tests/test_chat_park_resume_lhm_graphs_terminal_dash.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const H = R('vera/chat/chat_panel.html'), T = R('vera/remote/vera-terminal.js'), LG = R('vera/loop_graph_element.js'), W = R('vera/widgets/widget_element.js'), HAR = R('vera/capability_orchestration.html');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const i = H.indexOf('async function _resumeSession(){'), rs = H.slice(i, i + 2200);
t("a reloaded (parked) chat asks for its own session's messages, not the unawaited history list", rs.includes("if(empty && !known && SID){ try{ const r=await api('/memory/search','POST',{query:'',limit:3,record_type:'message',category:'chat',session_id:SID});")
  && rs.indexOf('known=(r&&Array.isArray(r.results)') < rs.indexOf('await loadSession(SID);'));

t('the terminal is torn down only if it is not back within a moment (a move keeps its shell)', T.includes("this._dropT = setTimeout(() => { this._dropT = 0; if (!this.isConnected) this.destroy(); }, 1500);")
  && T.includes("if (this._dropT) { clearTimeout(this._dropT); this._dropT = 0; this._doFit(true); if (this._hadFocus) this.focus(); return; }"));

t('blocks off: the docked graphs have no ground', H.includes('html[data-blocks="off"] #rightRail.lhm-host .lhm-quick .lhm-dk.dk-graph .lhm-dk-b{background:transparent!important;box-shadow:none!important}')
  && H.includes('html[data-blocks="off"] #rightRail.lhm-host .lhm-quick .lhm-dk.dk-graph vera-loop-graph{--lg-bg:transparent}'));
t('a docked graph keeps its controls behind ☰', H.includes("bg.className='dk-burger'; bg.textContent='\\u2630';") && H.includes(':not(.dk-tools) :is(#memGraphCtrl,#dagGraphCtrl,#memLegend,.vg-header,.vg-bottom-area,.vg-legend){display:none!important}'));
t('...the loop graph hides its own buttons on tools="hidden"', LG.includes(':host([tools="hidden"]) .bar .btns,:host([tools="hidden"]) .legend{display:none}') && H.includes("if(on) lg.removeAttribute('tools'); else lg.setAttribute('tools','hidden');"));

t('the dashboard drops the "fine" dot, keeps warn/err', HAR.includes('.dash-grid .w-head .w-dot:not(.warn):not(.err){display:none}'));
t('...titles a step larger', HAR.includes('.dash-grid .w-head .w-title{font-size:calc(var(--label-size,9px) + 1.5px)}'));
t('...thin, quiet scrollbars on the board and inside every widget', HAR.includes('.dash-grid,.dash-grid *{scrollbar-width:thin;') && W.includes('*{scrollbar-width:thin;scrollbar-color:var(--bd2,var(--border2,rgba(255,255,255,.16))) transparent}'));
t('...and a face a few pixels taller than its box does not scroll', W.includes("e.classList.toggle('vw-fit', over > 0 && over <= 6);") && W.includes('.vw-fit{overflow:hidden!important}'));
// owner, 2026-10-04: "the memory graph in the chat LHM has stopped displaying memories" - a tab pick overwrote the pane's
// class and with it 'vg' (its Vera graph drawn); only 'on' is the tab's to set
t('a tab pick keeps the memory graph drawn (toggles on, never overwrites the class)', H.includes("mp.classList.add('ctx-pane'); mp.classList.toggle('on', name==='memgraph');")
  && !H.includes("document.getElementById('paneMemGraph').className='ctx-pane'"));
// owner, 2026-10-04: "pressing edit on code in the canvas only lets you edit the first few lines" - the editor opened at
// its 72 px minimum; it now takes its text's height (to 70% of the window) and grows as it is typed into
{ const CV = R('vera/canvas/canvas_element.js');
  t('the canvas editor is as tall as its text and grows with it; Tab indents code', CV.includes('if (ta) this._editWire(ta); }')
    && CV.includes("ta.style.height = Math.min(ta.scrollHeight + 4, Math.round((root.innerHeight || 900) * 0.7)) + 'px';") && CV.includes("ta.addEventListener('input', fit);")
    && CV.includes("if (ev.key !== 'Tab' || ev.ctrlKey || ev.altKey || ev.metaKey) return;")); }
// ...and the kit's editor (the Edit on every code card - the chat's, the canvas's): its scroll box is a flex row of fixed
// height, which stretched the code layer (and the textarea laid over it) to that height - every line drawn, only the
// first screenful clickable. Laid out at the top, the layer is as tall as the code and the box scrolls it.
{ const VO = R('vera/vera_output_element.js');
  t('the kit editor\'s textarea is as tall as the code it covers', VO.includes('.vo-ed-sc{display:flex;align-items:flex-start;overflow:auto;')); }
{ const CV = R('vera/canvas/canvas_element.js');
  t('one Edit per canvas item: the header\'s only where the kit draws none (code: the card\'s; markdown: the row\'s)',
    CV.includes("const kitEdits = !!(root.VeraOut && key && (b.type === 'code' || b.type === 'markdown'));") && CV.includes('const editable = EDITABLE.includes(b.type) && !kitEdits;')); }
// owner, 2026-10-04: "if you mouseover some elements on the canvas the top and bottom bar appear over the top of the
// element instead of above and below" - in Hover/Zen the bars are overlays (so an item never resizes under the pointer);
// the item now keeps their bands, so they fade in over nothing
{ const CV = R('vera/canvas/canvas_element.js');
  t('hover/zen: an item keeps bands for its header and rail, so they never cover its content', CV.includes(':host([data-tier="hover"]) .it:not(.compact):not(.overfold):has(> .it-hd),:host([data-tier="zen"]) .it:not(.compact):not(.overfold):has(> .it-hd){padding-top:var(--vc-hd-band,29px)}')
    && !CV.includes('class="it-ft"')); }
// owner, 2026-10-04: "the memory graph in the lhm of chat needs me to press reload to load in" - it was read on a session
// load and a tab pick only; a finished reply now re-reads it (twice, as its memories land) in the chat and the menu frame
t('a finished reply re-reads the memory graph, here and in the menu instance', H.includes('try{ _memAfterTurn(); }catch(_){}   // a reply finished')
  && H.includes("if(_EMBED.only!=='menu') _lhmPost('memory',{});") && H.includes('_memTurnT=[4000, 15000].map(ms=>setTimeout(()=>{ try{ if(SID) memGraphLoad(); }catch(_){} }, ms));')
  && H.includes("else if(m.act==='memory'){ try{ _memAfterTurn(); }catch(_){} }"));
// owner, 2026-10-04: one bar per canvas item, on hover; the canvas follows Full / Hover / Zen
{ const CV = R('vera/canvas/canvas_element.js');
  t('hover/zen: every kind\'s one head (the container\'s, DEEP-29 11A.4) is its hover bar - no kind keeps a head of its own', !CV.includes('.ownhead') && CV.includes(", bar: false })") && CV.includes(':host(:not([data-tier="full"])) .it:not(.desk-chip) > .hda{opacity:0}'));
  t('...a records item\'s own heading goes (the header carries its title and count)', CV.includes(':host(:is([data-tier="hover"],[data-tier="zen"])) .it:not(.compact) .vc-rbx > .vc-rbx-top{display:none}')
    && CV.includes('(Array.isArray(c.items) ? `<span class="fdn" title="what it holds">${c.items.length}</span>` : \'\')'));
  t('full draws every control; the toolbar follows hover (the canvas) and zen (the toolbar itself)', CV.includes(':host([data-tier="hover"]:hover) .addbar,:host([data-tier="zen"]) .addbar:is(:hover,:focus-within){opacity:1}'));
  t('a head leaves room for the item\'s controls at its right end', CV.includes('.it:not(.desk-chip) > .it-hd{padding-right:calc(var(--hda-n, 0) * 27px + 14px)}')); }
// owner, 2026-10-04: folded items flicker when hovered and sometimes do not preview; elements greedy with width; chips
// hard to tell apart; Apply at the foot of a long editor
{ const CV = R('vera/canvas/canvas_element.js'), VO = R('vera/vera_output_element.js');
  t('any folded item previews after a pause, over the column, never re-placing it', CV.includes("if (!key || this._peekKey === key || this._rz || it.classList.contains('ghost') || !(it.classList.contains('compact') || it.classList.contains('overfold'))) return;")
    && CV.includes("this._peekT = setTimeout(() => { if (this._hovKey === key) this._peekOpen(key); }, 220);") && !/_hoverOpen\(it\) \{[\s\S]{0,600}this\._placeNow\(\)/.test(CV));
  t('...to the column it is still its folded line (placer and height watcher)', CV.includes("const measured = c.classList.contains('peek') && c._peekH ? c._peekH : c.offsetHeight;")
    && CV.includes("if (e.target.classList.contains('peek')) return;   // a preview opening is not a card growing") && CV.includes('const hovered = false;'));
  t('...at least a reading width, inside the stage; redrawn open', CV.includes('want = Math.min(W, Math.max(cur, 440))') && CV.includes('if (this._peekKey) { const pk = this._peekKey; this._peekKey = null; this._peekOpen(pk); }'));
  t('a folded chip shows its kind and its title (two lines), not its key, turn or buttons', CV.includes('.it.compact > .it-hd :is(.k,.src,.sc,.mx,.xp,.fu-w){display:none}') && CV.includes('-webkit-line-clamp:2'));
  t('the editor\'s Apply / Cancel sit above the code (kit and canvas editors)', VO.includes('pre.after(bar); bar.after(ed.box);') && CV.includes('<div class="edit" data-w="canvas.update"><div class="edit-a">')); }
// owner, 2026-10-04: "if collapsed some canvas elements are only appearing on hover" / resized elements "dont fill the space"
{ const CV = R('vera/canvas/canvas_element.js');
  t('a column-folded item is a chip: its one head is the header line; hover/zen never hide a folded head',
    CV.includes('`<div class="it-hd" style="--hda-n:') && !CV.includes('own-fold')
    && CV.includes(':host([data-tier="hover"]) .it:not(.compact):not(.overfold) > .it-hd,') && CV.includes("hFold: hd && hd.offsetHeight ? hd.offsetHeight + 2 : 28,"));
  t('...and previews on hover like any fold', CV.includes("!(it.classList.contains('compact') || it.classList.contains('overfold'))) return;") && CV.includes("if (it._peekWas !== 'overfold') it.classList.add('compact');"));
  t('a resized item\'s drawing takes the room: widget slot, records list, table, deck/sheet/page card',
    CV.includes('.it:is(.sized,.openin,.maxed) > .it-bd > :is(.vc-wid,.vc-rbx,.vc-filewrap){flex:1 1 auto;min-height:0;display:flex;flex-direction:column}')
    && CV.includes('.it:is(.sized,.openin,.maxed) > .it-bd > .vc-wid > .vc-live{flex:1 1 auto;height:auto;min-height:60px}')
    && CV.includes('.it:is(.sized,.openin,.maxed) > .it-bd > .vc-tablewrap{flex:1 1 auto;min-height:0;overflow:auto}'));
  t('...and a drag starts from the height the item had, read before it is marked sized', CV.includes("const h0 = it.offsetHeight, w0 = it.offsetWidth; it.classList.add('sized', 'resizing');")); }
// owner, 2026-10-04: graphs "dont resize on the y axis" / "cannot resize an element ... to span more columns"
{ const CV = R('vera/canvas/canvas_element.js'), VG = R('vera/vera_graph.js');
  t('a records graph takes its slot\'s height and fits its nodes to it', CV.includes("if (g && typeof g._setHeight === 'function' && hh && Math.abs((g._h || 0) - (hh - 16)) > 3) g._setHeight(Math.max(this.deskAlign() ? 60 : 160, hh - 16));")   /* a desk card may be smaller */
    && CV.includes("const gg = g && g._graph; if (gg && typeof gg.fit === 'function') gg.fit();") && VG.includes('fit:            function(){ try { fitView(); } catch(e){} },'));
  t('a drop writes the dragged width onto the card the placer reads', CV.includes('r.el.dataset.pw = String(Math.round(r.w));')); }
// owner, 2026-10-04: the canvas explode "opens very far away from the other elements and you can barely see it"
{ const CV = R('vera/canvas/canvas_element.js');
  t('an explode joins its source\'s band and turn (so it fuses beside it, wherever the source is)',
    CV.includes("at: b.state === 'pinned' ? 'pinned' : 'now', size: 'l' },\n          b.anchor && typeof b.anchor === 'object' ? { anchor: b.anchor } : {}));")
    && CV.includes("at: sb.state === 'pinned' ? 'pinned' : 'now', size: 'l',"));
  t('...and its diagram takes the pane\'s height beside the code', CV.includes('.it-bd.fu-beside > .fu-p > .fu-b,.it-bd.fu-over > .fu-p > .fu-b{display:flex;flex-direction:column}')); }
// owner, 2026-10-04: "the explode opens but there isnt enough space to scroll down and see it"
{ const CV = R('vera/canvas/canvas_element.js');
  t('an exploded item is one you opened (never folded by the column) and is brought into view', CV.includes('this._open.add(key); this._revealSoon(key);') && CV.includes('_revealSoon(key, tries) {'));
  t('...its panes stay side by side, in halves, when it is opened or resized', CV.includes('.it:is(.openin,.sized,.maxed) > .it-bd.fu-beside{flex-direction:row}')
    && CV.includes('.it:is(.openin,.sized,.maxed) > .it-bd.fu-beside > *{flex:1 1 0;min-width:0}'));
  t('...with a ceiling, so the diagram and its box cannot grow each other', CV.includes('.it.openin:not(.sized):not(.maxed)[data-fuse] > .it-bd{max-height:clamp(260px,calc(.8 * var(--vc-vh,460px)),900px)!important}')); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
