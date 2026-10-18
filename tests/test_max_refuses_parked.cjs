// WSU-58 S2 rule (item_core.transition 'max'): only an item in the now or pinned band can go full - a parked item is a
// pill on the shelf. The element's maximise path (_toggleMax) refuses a parked (or hidden) item instead of setting
// this._max and clearing it at the next render.   node tests/test_max_refuses_parked.cjs
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const at = C.indexOf('    _toggleMax(key) {');
const end = C.indexOf('\n    }\n', at);
t('the element has a _toggleMax method', at > 0 && end > at);
const body = C.slice(at + '    _toggleMax(key) {'.length, end);
const toggleMax = new Function('key', body);

function el(blocks, max) {
  const store = {};
  global.localStorage = { setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; }, getItem: (k) => store[k] || null };
  const o = { canvasId: 'cv1', _max: max || '', dataset: {}, renders: 0, events: [], _doc: { blocks },
    shadowRoot: { getElementById: () => ({ scrollTop: 0 }) },
    dispatchEvent(e) { this.events.push(e); }, render() { this.renders++; },
    _blockOf(key) { return blocks.find((b) => String(b.key) === String(key)) || null; } };
  o.store = store;
  return o;
}
global.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };

for (const st of ['parked', 'hidden']) {
  const o = el([{ key: 'note:a', state: st }]);
  toggleMax.call(o, 'note:a');
  t(`a ${st} item is not maximised: _max stays empty, nothing is stored, no event, no render`,
    o._max === '' && !o.store['vera:canvas:max:cv1'] && !o.dataset.maxed && o.events.length === 0 && o.renders === 0);
}
for (const st of ['now', 'pinned', undefined]) {
  const o = el([{ key: 'note:a', state: st }]);
  toggleMax.call(o, 'note:a');
  t(`a ${st || 'state-less (now)'} item is maximised`, o._max === 'note:a' && o.dataset.maxed === '1' && o.store['vera:canvas:max:cv1'] === 'note:a');
}
{ // an item already full that has since been parked can still be restored (the guard only stops going full)
  const o = el([{ key: 'note:a', state: 'parked' }], 'note:a');
  toggleMax.call(o, 'note:a');
  t('a maximised item that is now parked is restored, not kept full', o._max === '' && !o.dataset.maxed);
}
console.log(fails ? `\n${fails} FAILED` : '\nall passed');
process.exit(fails ? 1 : 0);
