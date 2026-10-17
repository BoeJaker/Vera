// Canvas engagement events (four-minds FM-6, Q7): the element's mapping from an action to the curator's outcome,
// and the hooks that send it without getting in the way.
//   node tests/test_canvas_engagement.cjs
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const E = V.engagementOf;
t('park and remove close', E('park', 'now') === 'closed' && E('remove', 'pinned') === 'closed');
t('acting on a parked or hidden item restores it', E('restore', 'parked') === 'restored' && E('pin', 'hidden') === 'restored');
t('open and maximise open', E('open', 'now') === 'opened' && E('max', 'pinned') === 'opened');
t('pin, rerun, edit, answer are use', ['pin', 'rerun', 'edit', 'answer', 'explode'].every((a) => E(a, 'now') === 'used'));
t('layout-only actions say nothing', ['up', 'down', 'fold', 'split', 'desk-auto', 'layer-front', 'clear', 'add'].every((a) => E(a, 'now') === ''));
t('the surface is the cap or panel, else the type',
  V.surfaceOf({ type: 'panel', content: { panel: 'markets' } }) === 'markets' &&
  V.surfaceOf({ type: 'result', content: { cap: 'web.search' } }) === 'web.search' &&
  V.surfaceOf({ type: 'note', content: {} }) === 'note' && V.surfaceOf(null) === '');
t('the dispatcher reports every action once, before acting', /const key = btn\.dataset\.layerKey[^\n]*\n\s+this\._engage\(act, key, it\);/.test(SRC));
t('a shelf chip reports restore, a header click reports open', /this\.call\('canvas\.add', \{ key: ch\.dataset\.key \}\); this\._engage\('restore', ch\.dataset\.key\)/.test(SRC) && /this\._engage\('open', it\.dataset\.key, it\); this\._toggleOpen/.test(SRC));
t('batched, keepalive, failures swallowed', /setTimeout\(\(\) => this\._engFlush\(\), 5000\)/.test(SRC) && /keepalive: true/.test(SRC) && /name: 'canvas\.engagement\.record'/.test(SRC) && /\.catch\(\(\) => \{\}\)/.test(SRC));
t('closing an open item or un-maximising is not interest', /act === 'open' && this\._open && this\._open\.has\(String\(key\)\)\) return/.test(SRC) && /act === 'max' && it && it\.classList\.contains\('maxed'\)\) return/.test(SRC));

if (fails) { console.log(fails + ' failing'); process.exit(1); } else console.log('all ok');
