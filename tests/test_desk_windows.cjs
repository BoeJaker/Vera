// WSU-55: window mode - a pill the desk had no room for opens on a click as a window over the others; the last pressed
// card is on top; another press sends it back to its stacking place; a placing pass keeps the windows; its pill control
// closes it.   node tests/test_desk_windows.cjs
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('one open-as-a-card step for the hover peek and the window', C.includes('    _pillCard(it) {') && C.includes('this._pillT = setTimeout(() => {') && C.includes('this._pillCard(it); }, 260);'));
t('a window stays open when the pointer leaves', C.includes("if (s.open && this._wins && this._wins.has(it.dataset.key)) return;   /* a window stays (WSU-55) */"));
t('a click on a border pill (not one you minimised) opens a window', C.includes("&& (wc.classList.contains('desk-chip') || wc.classList.contains('pill-open')) && !t.closest('[data-act]:not(.it-hd):not(.t)')) {"));
t('an unfocused window sits at its stacking place (closeness to the chat)', C.includes("it.style.zIndex = win && this._focusKey !== it.dataset.key ? String(s.z || 10) : '210';"));
t('the pressed card rises; the one before goes back', C.includes("it.classList.add('desk-focus'); it.style.zIndex = '230'; }") && C.includes("prev.style.zIndex = prev.dataset.zRest; delete prev.dataset.zRest;"));
t('a placing pass keeps the windows and the focus', C.includes("this.addEventListener('vera:canvas:placed', () => { try { this._winRestore(); }"));
t('Esc or the pill control closes a window', C.includes("if (ev.key === 'Escape' && this._focusKey && this._wins && this._wins.has(this._focusKey)) this._winClose(this._focusKey);") && C.includes("if (act === 'pill' && this._wins && this._wins.has(key)) { this._winClose(key); return; }"));
t('windows from one column do not open over each other', C.includes('left = Math.max(4, Math.min(W - 4 - w, left - 28)); top = Math.max(4, Math.min(H - 4 - h, top + 28));'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
