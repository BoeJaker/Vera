// 2026-10-03 (owner), the LHM audit, point 9: "animations of the LHMs need to be unified, smooth and complete"
//   node tests/test_lhm_one_motion.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const LHM = R('vera', 'chat', 'vera-lhm.js'), HAR = R('vera', 'capability_orchestration.html'), PCSS = R('vera', 'vera-panel.css');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── one set of tokens, the same in the library and the panel shell; reduced motion zeroes them ───────────────────────
t('the library sets the three tokens', /':root\{--lhm-dur:\.34s;--lhm-ease:cubic-bezier\(\.2,\.8,\.2,1\);--lhm-fade:\.22s\}'/.test(LHM));
t('the panel shell sets the same three', /:root \{ --lhm-dur: \.34s; --lhm-ease: cubic-bezier\(\.2,\.8,\.2,1\); --lhm-fade: \.22s; \}/.test(PCSS));
t('reduced motion zeroes them in both', /'@media \(prefers-reduced-motion:reduce\)\{:root\{--lhm-dur:0s;--lhm-fade:0s\}\}'/.test(LHM) && /@media \(prefers-reduced-motion: reduce\) \{ :root \{ --lhm-dur: 0s; --lhm-fade: 0s; \} \}/.test(PCSS));

// ── every LHM motion uses them: the harness slot, the panel sidebar's collapse, the side menu's fold, the glide ──────
t('the harness slot opens and folds on the tokens', /\.lhm-nav\{transition:width var\(--lhm-dur,\.34s\) var\(--lhm-ease,/.test(HAR) && /\.body-wrap\.lhm #lhmNav\.lhm-nav\{transition:width var\(--lhm-dur,\.34s\)[^}]*min-width var\(--lhm-dur,\.34s\)/.test(HAR) && /\.body-wrap\.lhm #lhmNav\.lhm-nav\.autohide\{transition:width var\(--lhm-dur,\.34s\)/.test(HAR));
t('a panel\'s own sidebar collapses on them (it was .16s ease)', /transition: width var\(--lhm-dur, \.34s\) var\(--lhm-ease, cubic-bezier\(\.2,\.8,\.2,1\)\);/.test(PCSS) && /#lhm-toggle svg \{[^}]*transition: transform var\(--lhm-dur, \.34s\)/.test(PCSS) && !/transition: width \.16s ease/.test(PCSS));
t('the side menu\'s labels and its unfold fade on them', /transition:opacity var\(--lhm-fade\) var\(--lhm-ease\)/.test(LHM) && /animation:lhmUnfold var\(--lhm-fade\) var\(--lhm-ease\) calc\(var\(--lhm-fade\) \/ 2\) both/.test(LHM));
t('the glide reads them (and stops at 0)', /duration: _motion\.ms\(\), easing: _motion\.ease\(\)/.test(LHM) && /if\(!_motion\.ms\(\)\) return;/.test(LHM));
t('no LHM motion keeps a duration of its own', !/lhm[^']*\{[^}']*transition:[^}']*\b\.(16|22|3|36)s ease/.test(LHM) && !/\.lhm-nav\{transition:width \.36s/.test(HAR));

// ── a menu that changes rises in: run absorb() against a stand-in DOM ─────────────────────────────────────────────
{
  class El {
    constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.cls = new Set(); this.style = {}; this.ownerDocument = doc; this.swaps = 0;
      const s = this; this.classList = { add: (c) => { if (c === 'lhm-swap') s.swaps++; s.cls.add(c); }, remove: (c) => s.cls.delete(c), contains: (c) => s.cls.has(c), toggle: (c, on) => { (on === undefined ? !s.cls.has(c) : on) ? s.cls.add(c) : s.cls.delete(c); } }; }
    set className(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => this.cls.add(c)); } get className() { return [...this.cls].join(' '); }
    set innerHTML(v) { this.children = []; } set textContent(v) { this._t = v; } get textContent() { return this._t || ''; }
    get offsetWidth() { return 1; } get firstChild() { return this.children[0] || null; }
    appendChild(c) { this.children.push(c); return c; } insertBefore(c) { this.children.unshift(c); return c; } setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k] == null ? null : this.attrs[k]; }
    addEventListener() {} querySelector() { return null; } querySelectorAll() { return []; } hasAttribute(k) { return k in this.attrs; }
  }
  var doc = { createElement: (t) => new El(t), getElementById: () => ({}), head: new El('head'), documentElement: new El('html'), querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
  const win = { addEventListener() {}, parent: null, localStorage: { getItem: () => null, setItem() {} }, customElements: null };
  win.parent = win;
  const ctx = { window: win, document: doc, localStorage: win.localStorage, getComputedStyle: () => ({ getPropertyValue: () => '' }), setTimeout: () => 0, clearTimeout() {}, console, JSON, Math, Array, Object, String, Date, Promise, Symbol, Intl, parseFloat, isNaN };
  vm.createContext(ctx); vm.runInContext(LHM, ctx);
  const L = win.VeraLHM;
  const spec = (menu) => ({ title: 'Fabric', active: { menu, tab: '' }, menus: [{ id: 'a', label: 'Overview', tabs: [{ id: 'x', label: 'X' }] }, { id: 'b', label: 'Sources', tabs: [{ id: 'y', label: 'Y' }] }] });
  const draw = (key, menu) => { const host = new El('div'); const wrap = L.absorb(host, spec(menu), () => {}, { editKey: key }); return { wrap, tabs: wrap.children.find((c) => c.cls.has('lhm-tabs')) }; };
  const first = draw('fabric', 'a');
  t('the first draw does not move', first.wrap.swaps === 0 && first.tabs.swaps === 0);
  const same = draw('fabric', 'a');
  t('the same menu redrawn (a state published again) stays still', same.wrap.swaps === 0 && same.tabs.swaps === 0);
  const other = draw('fabric', 'b');
  t('another of its menus: its list rises in', other.tabs.swaps === 1 && other.wrap.swaps === 0, other.tabs.swaps + '/' + other.wrap.swaps);
  const panel = draw('dream', 'a');
  t('another panel\'s menu: the whole menu rises in', panel.wrap.swaps === 1, panel.wrap.swaps);
}
t('the chat\'s own menu rises in when you switch menus', /var moved = !!_active && \(_active !== menuId \|\| _topMode\);/.test(LHM) && /if\(moved\)\{ if\(_quick && _quick\.style\.display !== 'none'\)\{ _swap\(_hd\); _swap\(_quick\); \} else _swap\(_det\); \}/.test(LHM));
t('the rise is a fade + 4 px on the tokens, and off under reduced motion', /'@keyframes lhmSwap\{from\{opacity:0;transform:translateY\(4px\)\}to\{opacity:1;transform:none\}\}'/.test(LHM) && /'\.lhm-swap\{animation:lhmSwap var\(--lhm-fade\) var\(--lhm-ease\) both\}'/.test(LHM) && /\.lhm-swap\{animation:none!important\}/.test(LHM));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
