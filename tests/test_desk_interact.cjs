// Review 2026-10-15 fixes (REVIEW-2026-10-15-desk-handover-and-spine.md U1-U14, U18): the desk's interaction against the
// version on main - only what is in the way moves, neighbours make room during a resize, a card the person sized that has
// no room becomes a window, a carried card is read by the pointer; the declared reader keeps forms and colours; the last
// declarations are kept.   node tests/test_desk_interact.cjs
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const root = path.join(__dirname, '..');
const C = fs.readFileSync(path.join(root, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const H = fs.readFileSync(path.join(root, 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

/* U4 - the hold */
t('U4: a hold starts when the person moves or sizes a card', C.includes('    _holdStart(key) {') && C.includes("this._holdStart(it.dataset.key); this._rzPlacedAt = 0;") && C.includes('this._holdStart(d.key); this._ptrHold(ev); }'));
t('U4: the hold ends when the desk is dealt afresh (its size, mode or fill change) - not when the topic changes or an item goes', C.includes("if (this._hold.sb ? this._hold.sb !== sigBase : this._hold.sig !== sig) { if (this._hold.sb && this._hold.sb !== sigBase) this._hold = null;")
  && C.includes("(this._hold.keys || []).forEach((k) => { if (!now.has(k) && this._hold.rects) delete this._hold.rects[k]; });") && !/T \? \(T\.order \|\| \[\]\)\.join\(','\) : ''\]\.join\('#'\)/.test(C));
/* owner 2026-10-16: "the layout still tends to jump about a lot when there is no need to" - the desk rests held, and a topic change
   moves only the cards that joined or left it */
t('the desk rests held once it has settled; a topic change frees only the cards whose membership changed', C.includes('this._autoHoldT = setTimeout(() => { const lp = this._lastPlan; if (!this._hold && lp && !this._rz && !this._dg) {')
  && C.includes("Object.keys(this._hold.rects).forEach((k) => { if (was.has(k) !== now.has(k)) delete this._hold.rects[k]; }); }"));
t('a window let go after a resize is decided again (shrunk to fit, a card); one carried onto the strip is a pill', C.includes('if (this._winKeepGet()[key]) this._winKeepSet(key, null);')
  && C.includes("if (keptWin[it.id] && !it.strip && !it.sizing && it.id !== this._rzLetGo)") && C.includes("if (asChip) { if (this._winKeepGet()[d.key]) { this._winKeepSet(d.key, null);"));
t('U4: held cards start where they are, at their size', C.includes('if (hr) Object.assign(o, { x: hr.x, y: hr.y, w: hr.w, h: hr.h, bw: hr.bw || hr.w, bh: hr.bh || hr.h, held: true, keepSize: true, face0: hr.face });'));
t('U4: nothing rises or grows under the hold', C.includes("if (!hold && !modeLaid && typeof D.settle === 'function'") && C.includes("if (!hold && !modeLaid && typeof D.grow === 'function'"));
t('U4: a card given back to the arrangement ends the hold (its menu; a double click now expands it, LV-8)', C.includes("{ w: u.w, h: u.h, face: u.face, free: true } : null); this._holdEnd(); return this._placeNow(); }"));
/* U1 - neighbours make room during a resize */
t('U1: the desk is placed about every 90 ms while the grip is held', C.includes('if (now - (this._rzPlacedAt || 0) >= 90) { this._rzPlacedAt = now;')  && !C.includes('if (this._rz === r) { try { this._placeNow(); } catch (e) { /* optional */ } } }, 220)'));
t('U1: during a resize the others start from the desk as it was when it began', C.includes('seed = hold ? ((this._rz && hold.base) || hold.rects) : null'));
t('U1: a neighbour pushed aside may give back the room it grew into', C.includes('{ w: Math.min(it.w, Math.max(it.bw || it.w, 160)), h: Math.min(it.h, Math.max(it.bh || it.h, 80)) }'));
t('U1: the release places the desk at once (not after the server refresh)', C.includes("if (this.deskAlign() && this.hasAttribute('stage')) { clearTimeout(this._rzPlaceT); this._rzLetGo = key; try { this._placeNow(); }"));
/* U6 / U2 - window mode */
t('U6: a card the person sized that still overlaps becomes a window', C.includes("pick.forEach((it) => { wins.add(it); it.win = true; if (it.id === this._rzLetGo) this._winKeepSet(it.id,"));
/* owner 2026-10-16: only the person makes a window, and only by a resize the desk has no room for; nothing else makes or
   unmakes one, and a window goes back to the desk by its dock control or Esc */
t('only the card under the resize, or the one just let go, can become a window; no other pass decides', C.includes('const mine = (it) => !it.strip && !wins.has(it) && !!(it.sizing || it.id === this._rzLetGo);') && !C.includes('it.sizing || it.own || (it.fixed && !it.isLock));'));
t('a kept window stays one, at its size and place, through every pass; the others are placed without it', C.includes('const keptWin = this._winKeepGet();') && C.includes("start.forEach(([it, x, y, w, h, k]) => { if (keptWin[it.id]) return;") && C.includes('if (kw && at && isFinite(at.fx) && isFinite(at.fy)) this._winKeepSet(key,'));
t('back to the desk: a dock control in the window\'s head, and Esc; the size that made it a window goes too', C.includes("dock: () => hb('dock', 'restore',") && C.includes("window: desk && !!this._winKeepGet()[key]")   /* WSU-58 S6: the row is VeraItem.controls */ && C.includes("if (act === 'dock') { this._winDock(key); return; }") && C.includes('this._winKeepSet(key, null); delete this._pw[key]; delete this._px[key]; this._deskPosSet(key, null);'));
t('U6: of two that meet, the last touched is the window', C.includes('const tOf = (it) => it.sizing ? Infinity : (actAt[it.id] || 0);') && C.includes('(this._actAt = this._actAt || {})[d.key] = Date.now();'));
t('U6: the others are placed again without the window', C.includes('D.separate(withStrip(bySize(plan.filter((it) => !wins.has(it)))), R, sepSize, { W: W, H: H });'));
t('U6: a window keeps its size and is over the tiled cards', C.includes(": it.win ? { w: it.w, h: it.h, face:") && C.includes(": it.win ? zOf('window', Math.max(0, (this._winOrder || []).indexOf(key))) : zOf('tile', D.stackOrder(s.nd) - ZB.tile[0])"));
/* owner 2026-10-17: "there is still some re-positioning of elements happening when no activity is happening"; "if you swap a tab
   on a composite desk item it changes the layout" - a held desk at rest is put back exactly, none of the passes run */
t('a held desk at rest is put back exactly: same decisions, nothing carried or sized, every card (or its group) already placed', C.includes('let replay = !!(hold && hold.rects && hold.want === wantSig && !this._rz && !this._dg && !this._rzLetGo && plan.length);')
  && C.includes("passes: { if (replay) break passes;") && C.includes("}   /* passes */") && C.includes(": it.replay ? { w: it.w, h: it.h, face: it.face0 || D.faceOf(it.w, it.h), nd: it.nd0 != null ? it.nd0 : 0.5 }")
  && C.includes("(grp.membersOf.get(it.c) || []).map((m) => hold.rects[m.dataset.key || '']).find(Boolean)"));
t('what the person decides is in the replay key (places, locks, pills, windows, groups taken out, sizes, layers)', C.includes('[pp[k] || 0, lk[k] || 0, pl[k] || 0, wk[k] || 0, sp[k] || 0, this._pw[k] || 0, this._px[k] || 0, (this._layer || {})[k] || 0]')
  && C.includes('this._hold.want = wantSig;') && C.includes('rects: base, base, want: lp.want }') && C.includes('face: o.face, nd: o.nd, win: !!o.win };'));/* owner 2026-10-17: "like the ui panels on the desk id like to be able to send full screen to the front and back" */
t('any item fills the desk in front or behind (its menu; its head while it fills the desk); a maximised one sent behind stops being maximised',
  C.includes("layer: desk && !inHead('[data-act=\"layer-behind\"]'), dive: true };") && C.includes("layer: () => '<hr>' + row('layer-behind', 'Full size, behind',")   /* WSU-58 S6: VeraItem.menu's entries */
  && C.includes("if (!dLay && desk && ((this._layer || {})[key] || this._max === key)) { const L1 = this._max === key ? 'front' : this._layer[key];")   /* WSU-58 S3: unless it declared its own (a panel) */
  && C.includes("if (this._max === key) { this._max = ''; try { localStorage.removeItem('vera:canvas:max:' + this.canvasId); }")
  && C.includes("const bk = this._blockOf(key); if (bk && bk.type !== 'panel' && this._doc) { this._html = null; this.render(this._doc); return; }"));/* owner 2026-10-17: "after re-sizing an element the other elements jump around to different positions allot" */
t('under the hold each card goes back to where it was whenever that is clear; one pressed only by the card under the hand is trimmed on that side, not moved',
  C.includes("if (clearOf(it, cand)) { Object.assign(it, cand, { keepSize: true, face0: r.face, held: true, restored: true }); back++; return; }")
  && C.includes("if (!hits.length || !hits.every(handOn) || (chatBox && ovl(cand, chatBox, -1))) return;")
  && C.includes(".filter((q) => q.r - q.l >= 220 && q.b - q.t >= 120).sort(") && !C.includes('_rdbg'));/* owner 2026-10-17 (WSU-91): the proposal ("Keep your arrangement?") sat on a card's title */
t('the desk proposal goes to the first place along the top (then the bottom) clear of every open card, placed again each pass',
  C.includes('_proposePlace(st) {') && C.includes("for (const y of [6, Math.max(6, H - h - 6)])") && C.includes('st.appendChild(el); this._proposePlace(st);') && C.includes('this._propose(st, mode, info, T, W); this._proposePlace(st);'));/* owner 2026-10-17 (LV-6): "it should be easier to split a part of a composite element out to its own element" */
t('a tab has a take-out control, and a tab carried 28 px off its strip is taken out and put where it is let go (no use of the setup doc before it is declared)',
  C.includes('<span class="cmp-out" data-act="cmpsep" data-layer-key="${esc(c.dataset.key)}"') && C.includes('if (!d.out && far > 28) { d.out = true;') && C.includes("const tabUp = (ev) => { const d = this._tabDrag; this._tabDrag = null;") && C.includes("tdoc.addEventListener(t, (ev) => { if (ptrOf(this._tabDrag, ev)) tabUp(ev); })")
  && C.includes("if ((act === 'cmptab' || act === 'cmpsep') && this._tabDragAt && Date.now() - this._tabDragAt < 400) return;") && !/\n      doc\.addEventListener\('mousemove', \(ev\) => \{ const d = this\._tabDrag;/.test(C));/* owner 2026-10-17: "sending something to the pill section affects/changes the layout of open element - this should not be possible" */
t('a card sent to the pills goes there whatever size it was given or held at, and the desk is held first so nothing else moves',
  C.includes('const pillish = (it) => { if (it.board) return false; if (it.pilled && !it.isLock && !it.sizing) return true;') && C.includes('const hr = seed && !userAt(key) && !pilledKey ? (seed[key]') && C.includes(', pilled: pilledKey };')
  && C.includes("if (act === 'pill') { if (!this._hold) this._holdStart(key); this._histPush('pill', key); this._pillSet(key, true);"));
/* LV-5: in the slots arrangement a pill clicked joins the layout; the topic moving on sends it back */
t('slots: a pill clicked is wanted (a slot, or its aspect composite front); a topic change forgets it', C.includes('(this._slotWant = this._slotWant || {})[k] = Date.now();') && C.includes("rank: want0[c.dataset.key] ? -1 :") && C.includes("if (this._wantMid !== undefined && this._wantMid !== (mid || '')) this._slotWant = null;"));/* LV-7 (owner 2026-10-17: "layouts should be able to walk backwards if actions are undone") */
t('a tab taken out or a card sent to the pills keeps the desk as it was; putting it back / opening the pill holds the desk at exactly that',
  C.includes("_histPush(kind, key) {") && C.includes("const inv = { cmpjoin: 'cmpsep', unpill: 'pill' }[kind];") && C.includes("if (act === 'cmpsep') { this._histPush('cmpsep', key); this._joinSet(key, null); } else this._histBack('cmpjoin', key);")
  && C.includes("this._histPush('cmpsep', d.key); this._joinSet(d.key, null); this._sepSet(d.key, true);") && C.includes("this._histBack('unpill', pc.dataset.key); this._pillSet(pc.dataset.key, false);"));/* owner 2026-10-17: "Elements going dark when they fill the desk - this happens if i press the expand button ... once i press front or back it
   becomes visible but until then the element is blank"; "the market chart still is not interactive, nor are the node graphs" */
t('a live element (chart, graph, widget) stacks with its card as the card IS (computed z): a card filling the desk is z 150 by its rule, not its style',
  C.includes("m.cs = getComputedStyle(card); m.z = m.cs.zIndex === 'auto' ? '' : m.cs.zIndex;") && C.includes("const z = m.z != null ? m.z : (card.style.zIndex || '');")
  && C.includes("const zc = getComputedStyle(c).zIndex; const z = zc && zc !== 'auto' ? zc : (c.style.zIndex || '');"));/* owner 2026-10-17: "composites of elements that have different aspects are being made i.e. a node graph in a tall slot"; "i dont see any automatic composites happening in slot mode" */
t('slots: similar items of ONE kind and ONE aspect share a card (turn, subject or topic), besides the overflow', C.includes("const fa = f + '/' + (this._aspectOf(c, c.dataset.key || '') || '');") && C.includes("[c.dataset.mid ? fa + '|m|' + c.dataset.mid : '', s.length >= 4 ? fa + '|s|' + s : '', inTopic(c) ? fa + '|t' : '']"));/* LV-8 (owner 2026-10-17): "if i double click a top-bar of an element can it auto expand to take up the available space and push the other elements around it -
   then if i double click the top of another can the last enlarged one give way to it and let the new one expand into that same space" */
t('a double click on a card head expands it into the free desk; again gives the room back; on another card the expanded one gives way and the new one takes that room',
  C.includes("body.addEventListener('dblclick', (ev) => { const it = deskHandle(ev); if (!it) return; ev.preventDefault(); this._expandToggle(it.dataset.key); });")
  && C.includes('_expandToggle(key) {') && C.includes("if (ex) { this._deskPosSet(ex.key, ex.pos); room = ex.room; before = ex.rects; this._expanded = null;"));/* owner 2026-10-17: "shrinking shouldnt be the first method to get out of the way of a dropped element"; cards never left overlapping */
t('room first, shrinking last: a pressed card moves at its size to the nearest room before it is trimmed; at the end no two open cards overlap (the less firm makes way: room at its size, then smaller, then a pill); a pill in the strip is drawn as one',
  C.includes('const roomFor = (v, rest, ks, tx, ty) => {') && C.includes("const mv = roomFor(cand, plan.filter((o) => o !== it && !o.win && (o.strip || o.h > 40)), [1], r.x, r.y);")
  && C.includes('const firmV = (x) => (x.sizing || x.isLock || x.id === exK || x.id === handKey) ? 3 : (x.fixed || x.stay) ? 2 : x.own ? 1 : 0;') && C.includes("s = it.strip && it.h <= 40 ? { w: it.w, h: it.h, face: it.face0 === 'icon' ? 'icon' : 'chip', nd: 0 }")
  && C.includes('it.isLock || it.fixed || it.h <= 40) return;'));/* LV-10 (owner 2026-10-17): "the much smaller items on the canvas should all composite onto a single page like a composite widget ... they flicker about and dont layout well" */
t('three or more small items the arrangement places share one board, placed as one block, its items in a grid of cells; never a pill', C.includes('const onBoard = new Set(boardX.length >= 3 ? boardX : []);')
  && C.includes("plan.push({ c: boardEl, id: '__board',") && C.includes('if (it.board) return false;') && C.includes("m.classList.add('on-board');") && C.includes('.stage > .desk-board{position:absolute;'));
t('what is drawn never overlaps: the drawn rectangles are checked, the less firm (or the other) moves to room, else the pill strip', C.includes("const solid = () => out.filter((o) => !o.behind && !o.board && !o.win && !/^(chip|icon|layer-)/.test(o.face || '') && o.h > 40);")
  && C.includes('for (const c0 of [v0, v0 === A ? B2 : A]) {') && !C.includes('/*DBG*/'));/* owner 2026-10-17: "the layout flickers about a bit as stuff loads and should not"; "as the user scrolls through the chat ... smoother" */
t('a new card is unseen until it is still (340 ms) and measured (the fit has run since), at most 2 s; a desk not placed yet starts all unseen; another canvas is another desk',
  C.includes('const fitDone = this.getAttribute(\'desk-fit\') === \'off\' || (this._fitRanAt || 0) > (e ? e.first : 0);') && C.includes("if (!e || (t - e.at >= 340 && fitDone) || t - e.first >= 2000) {")
  && C.includes("if (stage && this.deskAlign() && !(this._placed && this._placed.mode === 'desk')) layers.items.querySelectorAll('.it[data-key]').forEach((it) => it.classList.add('desk-new'));")
  && C.includes('this._placed = null; this._lastPlan = null; this._hold = null; this._topicMem = null;'));
t('a topic comes back as it was: each topic\'s desk is kept and held again when the chat scrolls back to it', C.includes("const memK = (ks || []).slice().sort().join('|'), lpT = this._lastPlan;") && C.includes('if (back && lpT && back.sb === lpT.sb) { this._hold = {'));t('a card turning pill <-> card, or coming into view (a new front tab), cross-fades while it glides', C.includes("const cameIn = this._shownLast && !this._shownLast.has(key) && !!was;") && C.includes(".stage .it.face-swap{opacity:.08;") && C.includes("this._shownLast = new Set(out.filter((o) => !o.behind).map((o) => o.key));"));/* round 9 (owner 2026-10-17) */
t('WSU-98: nothing becomes a pill because the person moved something - carried off the strip a card keeps a card face; a placed card is never pilled by distance; while the hand acts (2.5 s) no pass pills a card (least size where it covers least); the card let go never makes way',
  C.includes("if (!onStrip && (bare.face === 'chip' || bare.face === 'icon')) own = own || { w: Math.max(260, d.w0), h: Math.max(160, d.h0), face: 'card' };") && C.includes('if (it.fixed || it.sizing || it.own || it.stay) return false;')
  && C.includes('const handNow = !!(this._dg || this._rz || Date.now() - (this._handAt || 0) < 2500);') && C.includes('for (let round = 0; round < 4 && stripBox && !handNow; round++) {') && C.includes('if (!best && handNow) continue;'));
t('WSU-99: an opened pill rests with the windows', C.includes("const zWin = String(zOf('window', (this._winOrder || []).length));"));
t('WSU-100: a panel added to the desk is placed among the cards (not behind them)', C.includes("const args = { kind: 'panel', key, content: { panel: id, title: label || id }, at: 'now', size: 'l' };"));
t('WSU-101: a tab picked on a card opened from its pill opens the new front the same way', C.includes("const opened = fr && (fr.classList.contains('pill-open') || fr.classList.contains('chip-peek') || (this._wins && this._wins.has(fr.dataset.key)));") && C.includes('if (n && n.classList.contains(\'desk-chip\')) { if (wasWin) this._winOpen(n); else this._pillCard(n); }'));/* WSU-97 / LV-11 (owner 2026-10-17): "id like to be able to re-combine composite elements by dragging and dropping them on one another" */
t('a card carried well inside another marks it (join-target); let go there it joins it as a tab (kept per canvas); taking the tab out leaves the join',
  C.includes('_joinSet(key, to) {') && C.includes("Object.keys(joins).forEach((k) => { const a = byJ.get(k), b2 = byJ.get(joins[k]); if (a && b2) par.set(find(a), find(b2)); });")
  && C.includes("if (d.joinTo && d.joinTo !== d.key) { const to = d.joinTo;") && C.includes(".stage .it.join-target{outline:2px dashed"));/* WSU-95: the pills moved once (x 1061 -> 1081) at the first touch after a load */
t('a card drawn as a pill is drawn in the strip in the same pass (it ended a pill beside the strip and the next pass slotted it)', C.includes("if ((s.face === 'chip' || s.face === 'icon') && !it.strip && !it.win && !it.board && !it.fixed && !it.stay && stripFn) { stripFn(it);"));/* WSU-96: a chart, graph or widget (the live layer) fades with its card - unseen while it settles, dimmed while it changes face */
t('the live layer fades with its card', C.includes("const fade = card.classList.contains('desk-new') ? '0' : card.classList.contains('face-swap') ? '0.08' : '';") && C.includes('if (shown) { try { this._liveLayout(); }'));t('U6: a window looks like one (ground, frame, title bar, focus ring)', C.includes('.stage .it.desk-win{background:var(--bg1,#15181d)!important') && C.includes('.stage .it.desk-win.desk-focus{') && C.includes(':not(.chip-peek):not(.desk-win){background:transparent!important'));
t('U10: windows can be switched off (desk-windows)', C.includes("this.getAttribute('desk-windows') !== 'off'") && C.includes("'desk-fit', 'desk-windows']") && H.includes("cfgCvWindows:['desk-windows','on']") && H.includes("windows:'cfgCvWindows'"));
t('U2: a drop in a gap keeps a rectangle only when the gap made it smaller', C.includes('!d.own && (gw < d.el.offsetWidth - 2 || gh < d.el.offsetHeight - 2)'));
/* U3 - by the hand */
t('U3: a carried card is read where the pointer is', C.includes('let bare = D.sizeAt(g.R, px, py, {}, g.desk);') && C.includes('const ox = (d.cx0 - d.sx) / d.w0, oy = (d.cy0 - d.sy) / d.h0;'));
t('U3: a carried pill window goes on at its size', C.includes("if (this._wins && this._wins.delete(d.key)) { d.winOwn = { w: d.w0, h: d.h0, face: 'card' };"));
/* U7 - a pill window opened after the hover */
t('U7: a click after the hover still makes the window', C.includes("if (it._cpw && it._cpw.open) { const win = !!(this._wins && this._wins.has(it.dataset.key)); it.classList.toggle('desk-win', win);"));
/* U13 - dives */
t('U13: a deep dive is no wider than 460 nor taller than 360', C.includes('const dive = /^dive:/.test(key);') && C.includes('(dive ? 460 : 0) ||'));
/* U14 - tidy */
t('U14: tidy deals the desk afresh, from the Canvas menu', C.includes('    tidy() { this._holdEnd();') && H.includes("if(a.a==='tidy'){ if(typeof c.tidy==='function') c.tidy();") && H.includes('data-cvm="tidy"'));
/* U11 / U18 - the declared reader */
t('U11: a table or record that names its own form gets that form', H.includes("if((pr.element==='table'||pr.element==='kv')&&P.form&&P.form!=='table'&&P.form!=='kv') return widget("));
const fn = H.slice(H.indexOf('function _cvPresented('), H.indexOf('function _cvAdapt('));
t('U18: the declared reader takes its colours from the theme', fn.length > 100 && !/col:'#/.test(fn) && fn.includes("col:'var(--dv5, #a78bfa)'"));

/* round 7 (owner 2026-10-15) */
t('WSU-78: windows off - a sized card is cut back to the room, never below 160 x 90', C.includes('const gap = 10, MINW = 160, MINH = 90;') && C.includes('const sizedByPerson = (it) => !it.sizing && !it.strip && (it.own || (it.fixed && !it.isLock));'));
t('WSU-78: a size the person gave is kept wherever the card is pushed', C.includes('if (it.own && !it.held) return { w: it.w, h: it.h };'));
t('WSU-79: a peeked pill shows one menu, which holds the rest', C.includes('.stage .it.pill-open:not(.desk-win) > .hda > .hb:not([data-act="hdmore"]){display:none}') && C.includes("const peek = c.classList.contains('pill-open') && !c.classList.contains('desk-win');") && C.includes("drop: () => row('remove', 'Take it off the desk'") && C.includes("refresh: !!src.cap && !inHead('[data-act=\"rerun\"]'), drop: peek,"));
t('WSU-79: the peek stays open while its menu is', C.includes("[...stg.querySelectorAll(':scope > .hd-pop')].some((p) => p.dataset.key === it.dataset.key)) return; }"));
t('WSU-80: pills go to a strip on the far edge; no card enters it', C.includes("Object.assign(it, { x: cx, y: SGAP + CHH / 2 + row * (CHH + SGAP), w: CHW, h: CHH, fixed: true, strip: true, keepSize: true, face0: iconStrip ? 'icon' : 'chip' });") && C.includes("const stripBox = stripW ? { id: '__strip', fixed: true, strip: true,") && C.includes('if (farRight && it.x + it.w / 2 > sl - G)'));
t('WSU-80: a pill never shows its body (fused code + explode)', C.includes('.stage .it[data-desk-face]:not(.desk-chip) > .it-bd.fu-beside{flex-direction:row!important;display:flex!important;'));
t('WSU-80: a narrow desk has a strip of icons; the strip keeps its order under the hold', C.includes('const iconStrip = W < 760;') && C.includes('if (hold) { const slot = (it) => it.held ?'));
t('WSU-80: with no room at all, the less relevant of two placed cards goes to the strip (never a locked or hand-placed one)', C.includes('const kept = (it) => it.strip || it.win || it.isLock || it.stay || it.sizing || it.own') && C.includes('toStrip(victim, pills.length); pills.push(victim);'));
t('U4: the hold counts every item - a combined card switching its front tab does not end it', C.includes("const sigKeys = cards.map((c) => c.dataset.key || '').filter(Boolean).sort().join('|');") && C.includes('/* a group\'s new front tab takes the group\'s place */'));
t('WSU-83: when only streamed cards changed, those cards alone are swapped and nothing is placed again', C.includes('else if (this._html !== mark && this._html && layers.items.childElementCount && this._livePatch(layers.items, html, mark)) patched = true;') && C.includes('if (stage && !patched) this._placeNow();') && C.includes('    _livePatch(items, html, mark) {'));
t('WSU-83: a swapped card keeps its place, state, scroll and the hand on it', C.includes("if (this._dg && this._dg.el === old) this._dg.el = neu; if (this._rz && this._rz.el === old) this._rz.el = neu;") && C.includes("const st = old.getAttribute('style'); if (st != null) neu.setAttribute('style', st);"));
t('WSU-83: redraws of a stream are paced by their cost, slower while a card is carried or sized', C.includes('const gap = Math.max(busy ? 250 : 0, 2 * (this._liveCost || 0))'));
t('WSU-83: the desk is not placed again for the transcript growing (setTurns)', C.includes("const ts = Object.keys(this._turns).sort().join('|'); if (ts === this._turnsSig) return;"));
t('WSU-83: the live layer measures every element before it writes any', C.includes('const SL = body.scrollLeft, ST = body.scrollTop, M = {};') && C.includes("if (!h || !m.vis) { el.style.display = 'none'; el._at = false; return; }") && !/_liveLayout\(\) \{[\s\S]*?const grip = this\.deskAlign\(\) && card\.querySelector/.test(C));
t('WSU-84: a widget\'s family is what it draws - charts, graphs, news with reading', C.includes("return CHART.test(fm) ? 'charts' : GRAPH.test(fm) ? 'graphs' : /^(reading|news|feed|headlines|articles|sources)$/.test(fm) ? 'reading' : 'widgets'; };"));
t('WSU-84: items of one family in the context join across turns', C.includes("inTopic(c) ? f + '|t' : ''"));
t('WSU-84: only the in-context members stay in a group; the rest leave together', C.includes('sets.set(r, inT); sets.set({ out: r }, list.filter((c) => !inTopic(c))); });'));
t('WSU-84: the chat sets a first topic at once, then the relevance answer', H.includes('if(keys.length) cv.setTopic(keys, {}, mid); }catch(_){} }') && H.includes('const own=b=>(Array.isArray(b.anchors)?b.anchors:[]).some(a=>a&&(a.mid===mid||a.turn===mid));'));
t('WSU-85: within the same firmness the larger card is separated first - the smaller gives way', C.includes('const bySize = (list) => list.slice().sort((p, q) => firmOf(q) - firmOf(p) || (q.w * q.h) - (p.w * p.h));') && C.includes('D.separate(withStrip(bySize(plan)), R, sepSize, { W: W, H: H });'));
t('WSU-81: cards near the chat-side edge are flush with it', C.includes('const nearLeft = side !== \'right\', EDGE = 8, REACH = 80;') && C.includes('if (off > 1 && off <= REACH)'));

/* U12 - the last declarations are kept (functional) */
const store = {}; const ls = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
const defined = {};
const ctx = { setTimeout, clearTimeout, window: {}, console, localStorage: ls, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ls;
vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'widget_element.js'), 'utf8'), ctx);
vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'present.js'), 'utf8'), ctx);
const W = ctx.window.VeraWidget;
const decl = { kind: 'table', form: 'containers', status: 'declared', map: { rows: 'containers' } };
W.presentations([{ name: 'docker.ps', presentation: decl }, { name: 'x.y' }]);
t('U12: a list with declarations is used and kept', !!W.presentationOf('docker.ps') && !!store['vera:presentations']);
W.presentations([{ name: 'docker.ps' }, { name: 'x.y' }]);
t('U12: a list without any keeps the last ones', !!W.presentationOf('docker.ps') && W.presentationOf('docker.ps').form === 'containers');
W.presentations([]);
t('U12: an empty list is empty (nothing loaded yet)', !W.presentationOf('docker.ps'));

/* owner 2026-10-17: "the elements that are not in windowed mode can cover each other? this shouldnt be possible" */
t('no two open cards cover each other: of two fixed cards that meet, the one touched longer ago makes way (its own size, else the largest room it fits, else a pill)', C.includes("const can = [p, q].filter((x) => x.fixed && !x.isLock && !x.sizing && x.id !== exK && x.id !== handKey && !victims.has(x)); if (can.length === 2) victims.add(touched(p) <= touched(q) ? p : q);")
  && C.includes("for (const k of [1, 0.85, 0.7, 0.55, 0.42]) { const w = Math.round(v.w * k), h = Math.round(v.h * k); if (w < 220 || h < 120) break;") && C.includes("else { toStrip(v, pills.length); pills.push(v); v.held = false; } });")
  /* the move is the desk's, for the pass: never written as the person's place or a pill */
  && !C.includes("this._deskPosSet(v.id,"));
t('a held card at a pill\'s height is a pill (not a sliver with a card\'s face)', C.includes("face: it.h <= 40 ? 'chip' : (it.face0 || 'chip') })"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
