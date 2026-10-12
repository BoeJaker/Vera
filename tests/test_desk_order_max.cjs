// WSU-59's open item: a maximised card keeps its place in the desk's kept order. The pass while it was maximised left it
// out and saved the order without it; it came back at the end, so a long code card restored after an edit stood somewhere
// else (1080 px instead of 488).   node tests/test_desk_order_max.cjs
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const a = C.indexOf('let nowOrder = ranked.map('), b = C.indexOf('this._deskOrderSet(nowOrder);', a);
t('the kept order is merged before it is saved', a > 0 && b > a);
const body = C.slice(a, b);
t('a card left out of the pass but still on the canvas is held', body.includes("const held = kept.filter((k) => !here.has(k) && !!this._blockOf(k));"));
t('it keeps its index; the cards of the pass keep their rank (a combined card by its best member)', body.includes("held.forEach((k) => val.set(k, pos[k]));") && body.includes("nowOrder = [...val.keys()].sort((a, b) => val.get(a) - val.get(b));"));
// the merge itself, on the shapes the harness saw
const merge = (kept, now, alive) => { const here = new Set(now), ks = new Set(kept); const held = kept.filter((k) => !here.has(k) && alive.has(k));
  return held.length ? kept.filter((k) => here.has(k) || held.indexOf(k) >= 0).concat(now.filter((k) => !ks.has(k))) : now; };
t('maximised: [e200, e3] stays [e200, e3]', JSON.stringify(merge(['e200', 'e3'], ['e3'], new Set(['e200', 'e3']))) === '["e200","e3"]');
t('removed from the canvas: it leaves the order', JSON.stringify(merge(['e200', 'e3'], ['e3'], new Set(['e3']))) === '["e3"]');
t('a new card joins at the end', JSON.stringify(merge(['a', 'b'], ['b', 'c'], new Set(['a', 'b', 'c']))) === '["a","b","c"]');
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
