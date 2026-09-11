// The one context-menu registry (UI redesign m1 foundations; vera/ui/menus.js): every kind returns rows of the two
// row shapes with the same tail; aliases resolve; an unknown kind gets the generic rows; noPin drops the pin.
//   node tests/test_menus_lib.mjs
import fs from 'node:fs'; import path from 'node:path'; import vm from 'node:vm'; import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'menus.js'), 'utf8');
const ctx = { window: {}, console };
vm.runInNewContext(src, ctx);
const M = ctx.window.MENUS;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('api', typeof M.rows === 'function' && typeof M.kindOf === 'function' && typeof M.label === 'function' && Array.isArray(M.kinds));
t('the kinds cover entities, memory, UI elements, canvas forms and surfaces', ['host', 'commit', 'memory', 'hop', 'message', 'tool', 'note', 'widget', 'canvas', 'view'].every(k => M.kinds.includes(k)) && M.kinds.length >= 45);
const okRow = (r) => (r.t === 'act' && typeof r.id === 'string' && typeof r.n === 'string' && 'k' in r && 'cls' in r) || (r.t === 'cap' && typeof r.cap === 'string' && r.arg && typeof r.arg === 'object');
let allOk = true;
for (const k of M.kinds) { const rows = M.rows(k, 'x'); if (!rows.length || !rows.every(okRow)) { allOk = false; console.log('  bad rows for ' + k); } }
t('every kind returns well-formed rows', allOk);
const host = M.rows('host', 'ct126');
t('host rows: the tailored actions first', host[0].id === 'terminal' && host[0].k === '⌘T');
t('the tail is the same on every kind: ask · pin · chat.ask · provenance · print · copy', ['ask', 'pin'].every(id => host.some(r => r.id === id)) && host.some(r => r.t === 'cap' && r.cap === 'print.card') && host[host.length - 1].id === 'copy');
t('the staged capability carries the name', host.some(r => r.t === 'cap' && r.cap === 'sysmon.status' && r.arg.node === 'ct126'));
t('noPin drops the pin from the tail', !M.rows('host', 'x', { noPin:true }).some(r => r.id === 'pin') && M.rows('host', 'x').some(r => r.id === 'pin'));
t('aliases resolve', M.kindOf('kpi') === 'stat' && M.kindOf('canvas item') === 'note' && M.kindOf('tree') === 'table' && M.kindOf('nope') === null);
const gen = M.rows('nope', 'thing');
t('an unknown kind gets the generic rows + the tail', gen[0].id === 'open' && gen.some(r => r.id === 'trace') && gen.some(r => r.id === 'ask'));
t('labels', M.label('record') === 'fabric record' && M.label('kpi') === 'stat' && M.label('hop') === 'graph hop');
t('danger rows are marked', M.rows('container', 'c').some(r => r.id === 'restart' && r.cls === 'danger'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
