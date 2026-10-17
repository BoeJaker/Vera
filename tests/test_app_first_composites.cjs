// DA-07c: the first composite apps (DESK-SPINE-DESIGN §6.2) - Ticker (markets), Estate at a glance, Sandbox (looplab),
// Briefing (calendar) - as manifests, and what drawing them needed: a part that asks for a table gets one (its own
// columns), and their reads open without a press.
//   node tests/test_app_first_composites.cjs
'use strict';
const fs = require('fs'), path = require('path');
const R = path.join(__dirname, '..');
const V = require(path.join(R, 'vera', 'canvas', 'canvas_element.js'));
const S = fs.readFileSync(path.join(R, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const app = (file, id) => JSON.parse(fs.readFileSync(path.join(R, 'vera', 'apps', file), 'utf8')).find((a) => a.id === id);

const FIRST = [['markets.json', 'markets.ticker', 3], ['estate.json', 'estate.glance', 4], ['looplab.json', 'looplab.sandbox', 3], ['calendar-panel.json', 'calendar.briefing', 3]];
for (const [f, id, n] of FIRST) { const m = app(f, id);
  t(id + ': a composite of ' + n + ' parts, each part a read', !!m && m.view.kind === 'composite' && m.read.length === n && m.view.parts.every((p) => m.read.some((r) => r.as === p.from))); }
t('Sandbox joins its sandboxes and their logs by branch', JSON.stringify(app('looplab.json', 'looplab.sandbox').view.join) === '{"sandboxes.branch":"logs.br"}');
t('Ticker reads by the watchlist key (exchange:symbol)', app('markets.json', 'markets.ticker').read.filter((r) => r.args.symbol_key === '{arg.exchange}:{arg.symbol}').length === 2);
t('Briefing is today and the inbox (and a Telegram chat only when given its chat_id), as a dashboard', app('calendar-panel.json', 'calendar.briefing').view.layout === 'dashboard' && app('calendar-panel.json', 'calendar.briefing').read[0].args.start === 'today'
  && app('calendar-panel.json', 'calendar.briefing').read[2].args.chat_id === '{arg.chat_id?}');

// a part that asks for a table: a table, in its own columns; with no join there is nothing to pick
const rows = [{ id: 'u1', label: 'pve01', addr: '10.0.0.1', user: 'root', docker_kind: 'ssh', hw: { cpu: 8 } }, { id: 'u2', label: 'gpu-1', addr: '10.0.0.2', user: 'vera', docker_kind: '', hw: {} }];
const man = { title: 'G', read: [{ as: 'nodes', cap: 'nodes.list' }], view: { kind: 'composite', parts: [{ from: 'nodes', kind: 'table', columns: ['label', 'addr', 'user'] }] } };
const h = V.BLOCK.app({ app: 'x.g' }, 'l', 'app:g', { _apps: { 'app:g': { manifest: man, parts: [{ as: 'nodes', cap: 'nodes.list', args: {}, result: { nodes: rows } }], result: null, ui: {} } } });
t('a table part draws a table in its columns', /<th>label<\/th><th>addr<\/th><th>user<\/th><\/tr>/.test(h) && /<td>pve01<\/td><td>10\.0\.0\.1<\/td><td>root<\/td>/.test(h) && !/u1/.test(h));
t('...with nothing to pick when it is joined to nothing', !/data-app-prow/.test(h));
// the reads of the first composites open by themselves (read-shaped by name), and nothing that acts does
const rs = (S.match(/const readShaped = \(cap\) => (\/.*\/)\.test\(String\(cap \|\| ''\)\);/) || [])[1]; const RS = rs ? eval(rs) : /x^/;
t('their reads open without a press', ['ollama.instances', 'syslog.errors', 'evolve.sandbox.logs', 'evolve.sandbox.diff', 'markets.quotes', 'nodes.list', 'obs.node_temps', 'evolve.sandbox.list', 'markets.sentiment.history', 'markets.news.feed', 'cal.events.list', 'mail.inbox.list', 'markets.chart'].every((c) => RS.test(c)));
t('...and nothing that acts is read-shaped by these', !['evolve.sandbox.spawn', 'syslog.clear', 'ollama.pull', 'markets.quote.set'].some((c) => RS.test(c)));
// a composite scrolls inside its card; its parts keep their heights (no overlap when the card is small)
t('the composite scrolls in its card, the parts keep their heights', S.includes('.vc-app-comp{display:flex;flex-direction:column;gap:6px;min-height:0;height:100%;overflow:auto}') && S.includes('.vc-app-parts{display:grid;gap:8px;flex:0 0 auto;grid-auto-rows:max-content;'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
