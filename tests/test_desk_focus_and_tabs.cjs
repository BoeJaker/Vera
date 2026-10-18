// Owner 12 Oct: "if i pickup and drop one it goes under but it was the last interacted so should be at the top";
// "sometimes i swap a combined element to another tab and it closes to a pill".
//   node tests/test_desk_focus_and_tabs.cjs
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const w = C.slice(C.indexOf('    _winRestore() {'), C.indexOf('    _chipPeek(it, on) {'));
t('the focus is put back after every placing pass, window or not', w.startsWith('    _winRestore() {\n      if (!this.deskAlign()) return;') && w.includes('if (this._wins && this._wins.size) [...this._wins].forEach(') && w.includes('if (f) { delete f.dataset.zRest; this._focusKey = null; this._focusSet(f); }'));
t('a drop makes the carried card the focused one', C.includes("this._dgEnd = Date.now(); d.el.classList.remove('desk-dragging');\n        /* what was just carried is the last touched") && C.includes("if (d.el.dataset.key) { delete d.el.dataset.zRest; this._focusKey = null; this._focusSet(d.el); }"));
t('a combined card ranks by its best member', C.includes("const atOf = (x) => { let best = at[x.c.dataset.key || '']; (grp.membersOf.get(x.c) || []).forEach((m) => {") && C.includes('ranked.sort((a, b) => rk.get(a) - rk.get(b));'));
// the kept-order merge, on the harness's shapes: a combined card [front f2 | member f1] keeps the best place
const merge = (kept, nowOrder, members, alive) => { const here = new Set(nowOrder); const held = kept.filter((k) => !here.has(k) && alive.has(k)); if (!held.length) return nowOrder;
  const pos = {}; kept.forEach((k, i) => { pos[k] = i; }); const val = new Map();
  nowOrder.forEach((k) => { let b = pos[k]; (members[k] || []).forEach((m) => { const q = pos[m]; if (q != null && (b == null || q < b)) b = q; }); val.set(k, b != null ? b - 0.5 : 1e6 + val.size); });
  held.forEach((k) => val.set(k, pos[k])); return [...val.keys()].sort((a, b) => val.get(a) - val.get(b)); };
const kept = ['a', 'f1', 'b', 'c', 'f2', 'd'];
t('switching the front to the later tab keeps the card at its best place', JSON.stringify(merge(kept, ['a', 'f2', 'b', 'c', 'd'], { f2: ['f2', 'f1'] }, new Set(kept))) === JSON.stringify(['a', 'f2', 'f1', 'b', 'c', 'd']));
t('a maximised card still keeps its own place', JSON.stringify(merge(['x', 'y', 'z'], ['x', 'z'], {}, new Set(['x', 'y', 'z']))) === JSON.stringify(['x', 'y', 'z']));
t('a removed card leaves', JSON.stringify(merge(['x', 'y', 'z'], ['x', 'z'], {}, new Set(['x', 'z']))) === JSON.stringify(['x', 'z']));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
