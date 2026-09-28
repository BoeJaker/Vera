// The widget review, round 3: every clickable part carries its item and a click opens the DATA DRAWER on it (every
// dashboard), the calendar panel's parts as forms (month · schedule · calnav), the Vera graph as a form (vgraph).
//   node tests/test_widget_drawer_forms.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const VD = fs.readFileSync(path.join(R, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const items = (h) => [...h.matchAll(/data-item="([^"]*)"/g)].map((m) => JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')));
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

// ── 1. the item on every part ──
const rows = [{ name: 'LLM', vmid: 104, status: 'running', node: 'corp', cpu_pct: 46.7, extra: { deep: 1 } }, { name: 'Kali', vmid: 101, status: 'stopped', node: 'corp', cpu_pct: 0 }];
const T = W.draw('table', rows, 'l', { sample: false, height: 200, record: { draw: { columns: ['name', 'status'] } }, draw: { columns: ['name', 'status'] } });
t('a table row carries the WHOLE row (fields the tile does not show too, nested ones kept)', items(T).length === 2 && items(T)[0].vmid === 104 && items(T)[0].extra.deep === 1 && /data-path="row"/.test(T));
t('ranked bars, pills, donut slices, columns, log lines, numbers, heat rows each carry theirs',
  items(W.draw('ranked', { a: 3, b: 1 }, 'm', { sample: false })).some((x) => x.name === 'a' && x.value === 3)
  && items(W.draw('pills', rows, 'm', { sample: false })).some((x) => x.vmid === 101)
  && items(W.draw('donut', { running: 3, stopped: 1 }, 'm', { sample: false })).some((x) => x.name === 'running' && x.share === 75 && x.of === 4)
  && items(W.draw('column', { mon: 3, tue: 5 }, 'm', { sample: false })).some((x) => x.name === 'tue' && x.value === 5)
  && items(W.draw('log', [{ ts: '2026-09-27T10:00:00Z', text: 'boom', level: 'ERROR', trace: 'abc' }], 'm', { sample: false })).some((x) => x.trace === 'abc')
  && items(W.draw('numbers', { a: 1, b: 2 }, 'm', { sample: false })).length === 2
  && items(W.draw('heat', [{ name: 'w1', cpu: 30, ram: 50 }], 'm', { sample: false, height: 90 })).some((x) => x.name === 'w1' && x.cpu === 30));
const city = W.draw('city', rows.map((r) => Object.assign({ load: r.cpu_pct }, r)), 'l', { width: 480, height: 220, bare: true });
t('an iso block carries its row: every face of it, with its entity', items(city).some((x) => x.vmid === 104 && x.extra && x.extra.deep === 1) && /data-ref="guest:104"/.test(city));
const big = { name: 'x', blob: 'y'.repeat(9000), n: 1, nested: { a: 1 } }; const bi = items(W.draw('table', [big], 'l', { sample: false }))[0];
t('a row too large to carry keeps its plain fields and says it was trimmed', bi && bi._trimmed === true && bi.n === 1 && bi.blob.length === 400 && !('nested' in bi));
// the element: the click
t('a click finds the item (a part\'s data-item, a composite\'s section, a block, else the tile\'s whole answer) and dispatches widget:item first', WE.includes('function itemAt(host, target)') && WE.includes("new CustomEvent('widget:item', { bubbles: true, composed: true, cancelable: true, detail })") && WE.includes("return { item: host._data, path: '', ref: '', part: null };"));
t('on a host with item-drawer the click opens the drawer (not while arranging); without it a block keeps opening its entity', WE.includes("if (host.hasAttribute('item-drawer') && !(host.closest && host.closest('.dash-grid.editing'))) { e.stopPropagation(); drawer(detail); return; }") && WE.includes("if (el && el.hasAttribute('data-b')) { if (openBlock(host, rec,"));
t('every dashboard tile asks for the drawer (VeraDash: the page\'s tiles and the file\'s)', (VD.match(/setAttribute\('item-drawer', ''\)/g) || []).length === 2);
t('the drawer: the item whole, nested folded, related items to follow (‹ back), where it comes from, and Open entity · Open place · Deep dive · Copy JSON', WE.includes('function drawer(detail)') && WE.includes('data-dr="entity"') && WE.includes('data-dr="place"') && WE.includes('data-dr="dive"') && WE.includes('data-dr="copy"') && WE.includes('data-dr="back"') && WE.includes("'<h4>Where it comes from</h4>") && WE.includes('drJson(Array.isArray(obj) ? obj'));
t('it draws in the page that holds the widget (an embedded panel\'s own page) and closes on Esc', WE.includes('const doc = (detail.host && detail.host.ownerDocument) || document;') && WE.includes("if (e.key === 'Escape' && _drawer && _drawer.el.isConnected"));
// related: shared ids, not shared hosts
const ans = { guests: [{ name: 'LLM', vmid: 104, node: 'corp' }, { name: 'Kali', vmid: 101, node: 'corp' }, { name: 'n8n', vmid: 110, node: 'corp' }, { name: 'Gitea', vmid: 111, node: 'corp' }, { name: 'TC', vmid: 112, node: 'corp' }], backups: [{ vmid: 104, name: 'LLM', state: 'ok' }, { vmid: 111, name: 'Gitea', state: 'ok' }] };
const rel = W.relatedTo(ans, ans.guests[0]);
t('related: the rows that share an id or a name with the item (its backup) - not the ones that share a value everyone has (node corp)', rel.length === 1 && rel[0].path === 'backups[0]' && rel[0].via.some((v) => /vmid 104/.test(v)) && !rel.some((r) => r.via.some((v) => /node/.test(v))), JSON.stringify(rel.map((r) => [r.path, r.via])));

// ── 2. the calendar ──
const now = new Date(), ymd = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const today = ymd(now), tomorrow = ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
const EV = { events: [{ id: 'a', title: 'Work', start: today + 'T08:45:00+01:00', end: today + 'T17:30:00+01:00', color: '#fa573c', location: 'Bidwell House, Cambridge' }, { id: 'b', title: 'Rent', start: tomorrow, end: ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 2)), all_day: true, color: '#9fe1e7' }] };
const M = W.draw('month', EV, 'l', { sample: false, height: 300 });
t('month: a grid of days Monday first, today ringed, the month in its head with ‹ › (and Today once moved)', (M.match(/class="vb-mday/g) || []).length >= 35 && (M.match(/vb-mday[^"]*today/g) || []).length === 1 && /<b>[A-Z][a-z]+ \d{4}<\/b>/.test(M) && /data-calnav="-1"/.test(M) && /data-calnav="1"/.test(M));
t('month: each day and each event is an item (a day carries all its events), chips in their calendar\'s colour', items(M).some((x) => x.date === today && x.count === 1 && x.events[0].id === 'a') && items(M).some((x) => x.id === 'b') && /--c:#fa573c/.test(M) && /data-vb-sel="sel:/.test(M));
t('schedule: what is coming by day (Today · Tomorrow), time, title, where, each event an item', /Today/.test(text(W.draw('schedule', EV, 'm', { sample: false, height: 200 }))) && /Tomorrow/.test(text(W.draw('schedule', EV, 'm', { sample: false, height: 200 }))) && /08:45<small>17:30<\/small>/.test(W.draw('schedule', EV, 'm', { sample: false, height: 200 })) && /Bidwell House/.test(W.draw('schedule', EV, 'm', { sample: false, height: 200 })) && items(W.draw('schedule', EV, 'm', { sample: false, height: 200 })).some((x) => x.id === 'a'));
const N = W.draw('calnav', undefined, 's', {});
t('calnav: the controls draw with nothing to read (‹ month › Today · the views) and name their group', /vb-calh big/.test(N) && /data-calview="schedule"/.test(N) && /group cal/.test(N) && !/data-sample/.test(N));
t('the calendar\'s arguments follow the month: @month_start / @month_end are the grid\'s bounds, @today±Nd is a date', (() => { const a = W.resolveArgs({ start: '@month_start', end: '@month_end', x: 1 }, { off: 0 }), b = W.resolveArgs({ start: '@today', end: '@today+14d' }, {}); const g = new Date(a.start + 'T00:00:00'); return g.getDay() === 1 && Math.round((new Date(a.end + 'T00:00:00') - g) / 864e5) === 42 && a.x === 1 && b.start === today && /^\d{4}-\d{2}-\d{2}$/.test(b.end); })());
t('a month moved (its ‹ ›, a calnav, a chosen day) re-reads when its arguments follow the month and moves every calendar of its group', WE.includes("document.dispatchEvent(new CustomEvent('vera:calnav', { detail: { group: String((r.draw && r.draw.group) || 'cal')") && WE.includes("document.addEventListener('vera:calnav', this._calL)") && WE.includes('if (hasArgTokens(r) && r.source) this.read(true); else this.render();') && WE.includes('resolveArgs(this._rec.read.args, this._ui)'));

// ── 3. the Vera graph form ──
const G = W.toVeraGraph({ nodes: [{ id: 'mem_1', name: 'a question', label: 'Query', labels: ['Query', 'Entity'] }, { id: 'hub', label: 'Vera', kind: 'hub' }], edges: [{ from: 'hub', to: 'mem_1', rel: 'REL' }, { from_id: 'mem_1', to_id: 'hub', relation: 'CONTAINS' }] });
t('vgraph reads any nodes + edges: a Neo4j-shaped node keeps its type (labels) and its words (name); topology\'s label is its words; from/to, from_id/to_id', G.nodes[0].label === 'a question' && G.nodes[0].type === 'Query' && G.nodes[1].label === 'Vera' && G.nodes[1].type === 'hub' && G.edges.length === 2 && G.edges[1].from === 'mem_1' && G.edges[1].rel === 'CONTAINS');
const VG = W.draw('vgraph', { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ from: 'a', to: 'b' }] }, 'm', { sample: false, height: 160, draw: { mode: 'estate-2d' } });
t('vgraph draws a slot the element fills with veraUI.Graph (the flat graph as its fallback), sized to the tile, and says its size (not the record\'s mode - the graph\'s own picker says which mode is showing)', /<slot name="vgraph">/.test(VG) && /2 nodes · 1 edges/.test(text(VG)) && !/estate-2d/.test(text(VG)) && /height:146px/.test(VG));
t('the element mounts veraUI.Graph lazily (/ui/vera-graph.js) in its light DOM, keeps it across refreshes, sets the mode (once - then the pick is kept), and a node click opens the drawer', WE.includes("s.src = (base || '') + '/ui/vera-graph.js'") && WE.includes("host.setAttribute('slot', 'vgraph')") && WE.includes('if (sig !== el._vgSig) { el._vgSig = sig; try { g.load(G); } catch (_) {} }') && WE.includes('g.setMode(want)') && WE.includes('onNodeClick: (node) =>') && WE.includes("if (host._vgHost && host._vgHost.contains(e.target)) return;"));
t('the new forms are drawn forms and the catalogue knows them', ['month', 'schedule', 'calnav', 'vgraph'].every((f) => W.forms().some((x) => x.id === f)) && ['month', 'schedule', 'calnav', 'vgraph'].every((f) => /_F\("/.test(fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_record.py'), 'utf8')) && fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_record.py'), 'utf8').includes('_F("' + f + '"')));
t('fromCapResult draws a graph answer as vgraph and the calendar\'s events as a schedule', W.fromCapResult('topology.snapshot', { nodes: [{ id: 'a' }], edges: [] })[0].form === 'vgraph' && W.fromCapResult('cal.events.list', EV)[0].form === 'schedule');
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
