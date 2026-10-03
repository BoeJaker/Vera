// 2026-10-03 (owner), the LHM audit, point 2: "the icons in the lhm rail line up to the text menu items"; decision 1: "one
// icon per group for menu items that have sub items". Measured on the mirror before the fix: a flat menu drifted 1px per
// row (rail gap 2px, list gap 1px - 13px off by the 13th), a nested section pushed every row under it below its icon, and a
// grouped menu listed only the open group's items, so its group icons stood beside nothing.
//   node tests/test_lhm_rail_aligns.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('the list keeps the rail\'s 2px gap', /'\.lhm-absorbed \.lhm-tabs\{flex:1;display:flex;flex-direction:column;gap:2px;/.test(LHM) && !/\.lhm-absorbed \.lhm-tabs\{[^']*gap:1px/.test(LHM));
t('a group heading is as tall as its icon', /'\.lhm-absorbed \.lhm-grp-h\{height:34px;box-sizing:border-box;/.test(LHM) && /'\.lhm-absorbed \.lhm-tab\{height:34px;box-sizing:border-box;/.test(LHM));

// ── a stand-in DOM with a column layout: padding 6, gap 2, heights by class, margin-top honoured ────────────────────
const H = { 'lhm-ttl': 28, 'lhm-rsp': 28, 'lhm-ico': 34, 'lhm-tab': 34, 'lhm-grp-h': 34, 'lhm-ebar': 40, 'lhm-sp': 0, 'lhm-a-ws': 60 };
class El {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.cls = new Set(); this.style = {}; this.ownerDocument = doc; this._p = null; this.scrollTop = 0; this.isConnected = true; this.listeners = {};
    const s = this; this.classList = { add: (c) => s.cls.add(c), remove: (c) => s.cls.delete(c), contains: (c) => s.cls.has(c), toggle: (c, on) => { (on === undefined ? !s.cls.has(c) : on) ? s.cls.add(c) : s.cls.delete(c); } }; }
  set className(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => this.cls.add(c)); } get className() { return [...this.cls].join(' '); }
  set innerHTML(v) { this.children = []; } set textContent(v) { this._t = v; } get textContent() { return this._t || ''; }
  get offsetWidth() { return 1; } get firstChild() { return this.children[0] || null; }
  appendChild(c) { c._p = this; this.children.push(c); return c; } insertBefore(c, ref) { c._p = this; const i = this.children.indexOf(ref); if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k] == null ? null : this.attrs[k]; } hasAttribute(k) { return k in this.attrs; }
  addEventListener(k, fn) { (this.listeners[k] = this.listeners[k] || []).push(fn); } querySelector(sel) { const c = sel.replace(/^\./, ''); const f = (e) => { for (const k of e.children) { if (k.cls && k.cls.has(c)) return k; const r = f(k); if (r) return r; } return null; }; return f(this); } querySelectorAll() { return []; }
  height() { for (const c of this.cls) if (c in H) return H[c]; return 34; }
  getBoundingClientRect() { const p = this._p; if (!p) return { top: 0, height: 0 }; let y = 6 - (p.scrollTop || 0); for (const k of p.children) { y += parseFloat(k.style.marginTop || 0) || 0; if (k === this) return { top: y, height: this.height() }; y += k.height() + 2; } return { top: 0, height: 0 }; }
}
var doc = { createElement: (tg) => new El(tg), getElementById: () => ({}), head: new El('head'), documentElement: new El('html'), querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
const win = { addEventListener() {}, localStorage: { getItem: () => null, setItem() {} }, customElements: null }; win.parent = win;
const ctx = { window: win, document: doc, localStorage: win.localStorage, getComputedStyle: () => ({ getPropertyValue: () => '' }), requestAnimationFrame: (fn) => { fn(); return 1; }, setTimeout: () => 0, clearTimeout() {}, console, JSON, Math, Array, Object, String, Date, Promise, Symbol, Intl, parseFloat, isNaN };
vm.createContext(ctx); vm.runInContext(LHM, ctx);
const L = win.VeraLHM;
const draw = (spec, key) => { const host = new El('div'); const wrap = L.absorb(host, spec, () => {}, { editKey: key }); const rail = wrap.children.find((c) => c.cls.has('lhm-rail')), tabs = wrap.children.find((c) => c.cls.has('lhm-tabs')); return { rail, tabs, icons: rail.children.filter((c) => c.cls.has('lhm-ico')) }; };
const off = (a, b) => Math.abs(a.getBoundingClientRect().top - b.getBoundingClientRect().top);

// FLAT: thirteen items, an icon each (Dream's shape) - each icon level with its item
{
  const items = Array.from({ length: 13 }, (_, i) => ({ id: 'i' + i, label: 'Item ' + i }));
  const spec = { title: 'Dream', active: { menu: '§i0', tab: 'i0' }, menus: items.map((it, i) => ({ id: '§i' + i, label: it.label, tabs: items })) };
  const d = draw(spec, 'dream'); const rows = d.tabs.children.filter((c) => c.cls.has('lhm-tab'));
  t('flat: an item per icon', rows.length === 13 && d.icons.length === 13);
  const worst = Math.max(...d.icons.map((ic, i) => off(ic, rows[i])));
  t('flat: every icon level with its item (the 13th too)', worst < 0.5, 'worst ' + worst + 'px');
}
// FLAT with a section nested under the open item (Comms > Calendar > its sections): the icons below it move down with their rows
{
  const items = ['system', 'calendar', 'email', 'telegram'].map((id) => ({ id, label: id }));
  const open = [items[0], items[1], { id: 'c:month', label: 'Month', depth: 1 }, { id: 'c:agenda', label: 'Agenda', depth: 1 }, items[2], items[3]];
  const spec = { title: 'Comms', active: { menu: '§i1', tab: 'c:agenda' }, menus: items.map((it, i) => ({ id: '§i' + i, label: it.label, tabs: i === 1 ? open : items })) };
  const d = draw(spec, 'comms'); const tops = d.tabs.children.filter((c) => c.cls.has('lhm-tab') && !c.cls.has('sub'));
  t('flat + nested: still read as flat (only the open item carries the sections)', d.tabs.children.filter((c) => c.cls.has('lhm-tab')).length === 6 && !d.tabs.children.some((c) => c.cls.has('lhm-grp-h')));
  const worst = Math.max(...d.icons.map((ic, i) => off(ic, tops[i])));
  t('flat + nested: Email and Telegram\'s icons move down past the sections, level with their rows', worst < 0.5 && parseFloat(d.icons[2].style.marginTop) > 70, 'worst ' + worst + ' margin ' + d.icons[2].style.marginTop);
}
// GROUPED: four groups (the Estate's shape) - every group is a heading level with its icon, the open one's items under it
{
  const g = (n, k) => Array.from({ length: k }, (_, i) => ({ id: n + i, label: n + ' ' + i }));
  const groups = [['Overview', g('o', 5)], ['Machines', g('m', 4)], ['Network', g('n', 3)], ['Trust', g('t', 6)]];
  const spec = { title: 'Estate', active: { menu: '§g1', tab: 'm2' }, menus: groups.map(([nm, tabs], i) => ({ id: '§g' + i, label: nm, tabs })) };
  const d = draw(spec, 'estate'); const heads = d.tabs.children.filter((c) => c.cls.has('lhm-grp-h'));
  t('grouped: a heading per group, titled by the panel', heads.length === 4 && d.tabs.children[0].cls.has('lhm-ttl') && d.tabs.children[0].textContent === 'Estate');
  t('grouped: only the open group\'s items, under its heading', d.tabs.children.filter((c) => c.cls.has('lhm-tab')).length === 4 && d.tabs.children[d.tabs.children.indexOf(heads[1]) + 1].cls.has('in-grp') && heads[1].cls.has('on'));
  const worst = Math.max(...d.icons.map((ic, i) => off(ic, heads[i])));
  t('grouped: every group icon level with its heading (past the open group\'s items)', worst < 0.5, 'worst ' + worst);
  t('grouped: a heading names its count', heads[3].children.some((c) => c.tagName === 'B' && c.textContent === '6'));
}
// the rail scrolls with the list
{
  const items = Array.from({ length: 30 }, (_, i) => ({ id: 'i' + i, label: 'I' + i }));
  const d = draw({ title: 'Long', active: { menu: '§i0', tab: 'i0' }, menus: items.map((it, i) => ({ id: '§i' + i, label: it.label, tabs: items })) }, 'long');
  d.tabs.scrollTop = 140; (d.tabs.listeners.scroll || []).forEach((fn) => fn());
  t('the rail scrolls with the list', d.rail.scrollTop === 140);
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
