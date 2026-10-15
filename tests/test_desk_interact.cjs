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
t('U4: a hold starts when the person moves or sizes a card', C.includes('    _holdStart(key) {') && C.includes("this._holdStart(it.dataset.key); this._rzPlacedAt = 0;") && C.includes('this._holdStart(d.key); }'));
t('U4: the hold ends when the desk is dealt afresh (its signature changes)', C.includes('if (this._hold && this._hold.sig !== sig) this._hold = null;'));
t('U4: held cards start where they are, at their size', C.includes('if (hr) Object.assign(o, { x: hr.x, y: hr.y, w: hr.w, h: hr.h, bw: hr.bw || hr.w, bh: hr.bh || hr.h, held: true, keepSize: true, face0: hr.face });'));
t('U4: nothing rises or grows under the hold', C.includes("if (!hold && typeof D.settle === 'function'") && C.includes("if (!hold && typeof D.grow === 'function'"));
t('U4: a card given back to the arrangement ends the hold', C.includes('this._deskPosSet(it.dataset.key, null); this._holdEnd(); this._placeNow();') && C.includes("{ w: u.w, h: u.h, face: u.face, free: true } : null); this._holdEnd(); return this._placeNow(); }"));
/* U1 - neighbours make room during a resize */
t('U1: the desk is placed about every 90 ms while the grip is held', C.includes('if (now - (this._rzPlacedAt || 0) >= 90) { this._rzPlacedAt = now;')  && !C.includes('if (this._rz === r) { try { this._placeNow(); } catch (e) { /* optional */ } } }, 220)'));
t('U1: during a resize the others start from the desk as it was when it began', C.includes('seed = hold ? ((this._rz && hold.base) || hold.rects) : null'));
t('U1: a neighbour pushed aside may give back the room it grew into', C.includes('{ w: Math.min(it.w, Math.max(it.bw || it.w, 160)), h: Math.min(it.h, Math.max(it.bh || it.h, 80)) }'));
t('U1: the release places the desk at once (not after the server refresh)', C.includes("if (this.deskAlign() && this.hasAttribute('stage')) { clearTimeout(this._rzPlaceT); try { this._placeNow(); }"));
/* U6 / U2 - window mode */
t('U6: a card the person sized that still overlaps becomes a window', C.includes('const mine = (it) => !!(it.sizing || it.own || (it.fixed && !it.isLock));') && C.includes("pick.forEach((it) => { wins.add(it); it.win = true; });"));
t('U6: of two that meet, the last touched is the window', C.includes('const tOf = (it) => it.sizing ? Infinity : (actAt[it.id] || 0);') && C.includes('(this._actAt = this._actAt || {})[d.key] = Date.now();'));
t('U6: the others are placed again without the window', C.includes('D.separate(plan.filter((it) => !wins.has(it)), R, sepSize, { W: W, H: H });'));
t('U6: a window keeps its size and is over the tiled cards', C.includes("s = it.win ? { w: it.w, h: it.h, face:") && C.includes(': it.win ? 160 : D.stackOrder(s.nd)'));
t('U6: a window looks like one (ground, frame, title bar, focus ring)', C.includes('.stage .it.desk-win{background:var(--bg1,#15181d)!important') && C.includes('.stage .it.desk-win.desk-focus{') && C.includes(':not(.chip-peek):not(.desk-win){background:transparent!important'));
t('U10: windows can be switched off (desk-windows)', C.includes("this.getAttribute('desk-windows') !== 'off'") && C.includes("'desk-fit', 'desk-windows']") && H.includes("cfgCvWindows:['desk-windows','on']") && H.includes("windows:'cfgCvWindows'"));
t('U2: a drop in a gap keeps a rectangle only when the gap made it smaller', C.includes('!d.own && (gw < d.el.offsetWidth - 2 || gh < d.el.offsetHeight - 2)'));
/* U3 - by the hand */
t('U3: a carried card is read where the pointer is', C.includes('const bare = D.sizeAt(g.R, px, py, {}, g.desk);') && C.includes('const ox = (d.cx0 - d.sx) / d.w0, oy = (d.cy0 - d.sy) / d.h0;'));
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

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
