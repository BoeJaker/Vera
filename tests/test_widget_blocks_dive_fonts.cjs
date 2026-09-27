// The widget review, round 2: blocks with detail (hover, light, click through), motion, the deep dive, the text-size
// setting reaching every font size, and the donut sized to its height.
//   node tests/test_widget_blocks_dive_fonts.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const VD = fs.readFileSync(path.join(R, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const attr = (h, a) => [...h.matchAll(new RegExp(a + '="([^"]*)"', 'g'))].map((m) => m[1]);

// ── 1. blocks: every iso form that draws rows carries each row's detail, entity and group on its faces ──
const guests = [{ name: 'LLM', vmid: 104, status: 'running', node: 'corp', cpu_pct: 46.7, mem_pct: 89.3 }, { name: 'Kali', vmid: 101, status: 'stopped', node: 'corp', cpu_pct: 0, mem_pct: 0 }];
const city = W.draw('city', guests.map((g) => Object.assign({ load: g.cpu_pct, temp: g.mem_pct }, g)), 'l', { width: 480, height: 220, bare: true });
t('city: every building\'s faces carry its block id, its detail (the row\'s own fields, state first) and its entity (guest:<vmid>)', new Set(attr(city, 'data-b')).size === 2 && attr(city, 'data-ref').includes('guest:104') && attr(city, 'data-tip').some((x) => /^LLM\nstatus: running\n/.test(x.replace(/&#10;|\n/g, '\n'))), attr(city, 'data-tip').slice(0, 1).join());
const racks = W.draw('racks', guests, 'l', { width: 480, height: 220, bare: true });
t('racks: each guest a block with its detail and entity, grouped by rack', attr(racks, 'data-ref').includes('guest:101') && attr(racks, 'data-g').every((g) => /^rack\d+$/.test(g)));
const cont = W.draw('containers', [{ Names: ['/vera'], name: 'vera', host: 'h1', State: 'running', Image: 'vera:latest', Id: 'da56964c4bff3aa38bade2dc5ecaa06ae19e6a45e0d13c2118d9d533974af7ca' }, { name: 'redis', host: 'h2', State: 'exited' }], 'l', { width: 480, height: 220, bare: true });
const ctip = attr(cont, 'data-tip').join(' ');
t('containers: a block\'s detail says what it is and where it runs, not its 64-hex id; the host is its group', /Image: vera:latest/.test(ctip) && !/da56964c4bff/.test(ctip) && attr(cont, 'data-g').includes('h1'));
['sandboxes', 'hosts', 'models', 'datasets', 'devices', 'notebook', 'pages', 'library'].forEach((f) => { const h = W.draw(f, [{ name: 'a', status: 'running', size: 3 }, { name: 'b', status: 'idle', size: 1 }], 'l', { width: 480, height: 220, bare: true });
  t(f + ': its row blocks carry their detail', attr(h, 'data-b').length > 0 && attr(h, 'data-tip').some((x) => /^a/.test(x)), text(h).slice(0, 60)); });
const lv = W.draw('level', { value: 1, max: 4, unit: 'held' }, 'm', { draw: { cells: 4 } });
t('a level\'s cells say what they are (in use · free, n of max)', (lv.match(/data-tip="in use/g) || []).length === 1 && (lv.match(/data-tip="free/g) || []).length === 3);
const dn = W.draw('donut', { running: 22, stopped: 34 }, 'm', { height: 100 });
t('a donut\'s parts are blocks: arc and legend entry share an id, the detail gives the share', attr(dn, 'data-b').filter((b) => b === 'p0').length === 2 && /61% of 56/.test(dn));
t('rowTip and rowRef: state first, ids raw (a port is not 8,998), hashes left out, epoch times as dates', (() => { const s = W.rowTip({ name: 'x', port: 8998, Id: 'da56964c4bff3aa38bade2dc5ecaa06ae19e6a45e0d13c2118d9d533974af7ca', Created: 1790520199, status: 'up' }); return /^x\nstatus: up/.test(s) && /port: 8998/.test(s) && !/da56964c/.test(s) && /Created: 2026-09-/.test(s); })() && W.rowRef({ vmid: 7 }) === 'guest:7' && W.rowRef({ ssh_host_id: 'ab' }) === 'host:ab' && W.rowRef({ ref: 'cert:x' }) === 'cert:x');
// the element wires it: hover lights the block and its group, shows the card; a click opens the entity or the place
t('the element: hover lights a block (every face of it) and its group, dims the rest, and shows the detail card', WE.includes('function wireParts(host)') && WE.includes("(b ? root.querySelectorAll('[data-b=\"' + CSS_ESC(b) + '\"]') : [el]).forEach((x) => x.classList.add('hot'));") && /\.vw-root\.lit \.vb-isow i\.f\[data-b\]:not\(\.hot\):not\(\.warm\)/.test(WE) && WE.includes('function showTip(root, text, x, y, act, html)'));
t('a click on a block opens its entity (the drawer, else vera:entity:open to the harness) or the record\'s place (vera:place:open with the block as the entity)', WE.includes("postTop({ type: 'vera:entity:open', ref: String(ref) })") && WE.includes("postTop({ type: 'vera:place:open', place: String(place), entity: String(name || '') })") && WE.includes('window.veraEntityDrawer.open(ref)'));
t('a title becomes the card (the browser\'s own tooltip would stand over it)', WE.includes("if (el.hasAttribute('title')) { if (!el.hasAttribute('data-tip')) el.setAttribute('data-tip', el.getAttribute('title')); el.removeAttribute('title'); }"));
// ── motion ──
t('motion: faces rise in once (on the first real reading), bars grow, columns rise; figures count and bars slide between readings; off under reduced motion and motion false', /:host\(:not\(\[data-entered\]\)\) \.vb-isow i\.f\{animation:vw-rise/.test(WE) && WE.includes("this.setAttribute('data-entered', '')") && WE.includes('function motionAfter(root, was)') && WE.includes('@media (prefers-reduced-motion: reduce)') && WE.includes('.vw-root[data-motion="0"] *'));
// ── 2. the deep dive ──
t('the deep dive: VeraWidget.dive opens the record at XL, every row as a searchable table, the raw answer, read again, the place', typeof W.dive === 'function' && WE.includes("big.setAttribute('size', 'xl')") && WE.includes("form: 'table', title: 'Every row', data: rw, draw: { search: true }") && WE.includes("'<h4>What ' + esc(rec.source || 'the record') + ' answered</h4><pre>'") && WE.includes('data-dv="refresh"'));
t('the dashboard: a tile\'s face opens its dive on a click (not while arranging) and its head has ⤢', VD.includes("el.setAttribute('dive-on-click', '')") && VD.includes('function ensureDive(w)') && VD.includes('window.VeraWidget.dive(x)') && WE.includes("host.closest('.dash-grid.editing')"));
// ── 3. the text-size setting ──
const css = W.css();
const small = [...css.matchAll(/font-size:\s*([0-9.]+)px/g)].map((m) => +m[1]).filter((n) => n < 13);
t('every font size under 13 px follows the setting (none left as a bare px value)', small.length === 0 && /font-size:calc\(max\(var\(--vw-fmin, 10px\), 8\.5px\) \* var\(--vw-fx, 1\)\)/.test(css), small.slice(0, 5).join(','));
t('the sizes inside a clamp() follow it too', !/clamp\(\s*[0-9.]+px,/.test(css.replace(/font-size:clamp\(calc/g, '')) || /font-size:clamp\(calc\(max\(var\(--vw-fmin/.test(css));
t('the setting sets the floor and the factor on the document (compact 0 · default 10 · large 11 ×1.1 · larger 12 ×1.22)', WE.includes('html{--vw-fmin:10px;--vw-fx:1}html[data-text="compact"]{--vw-fmin:0px;--vw-fx:1}html[data-text="large"]{--vw-fmin:11px;--vw-fx:1.1}html[data-text="larger"]{--vw-fmin:12px;--vw-fx:1.22}'));
t('no SVG font-size attribute the setting cannot reach (the bar labels are a class now)', !/font-size="8"/.test(WE) && /class="vw-svgt"/.test(WE));
// ── 4. the donut ──
const d1 = W.draw('donut', { a: 3, b: 2, c: 1 }, 'm', { height: 90 });
const svgW = +((d1.match(/style="height:(\d+)px;width:(\d+)px;flex:none"/) || [])[2] || 0);
t('the donut is as wide as it is tall (the body\'s height), its total in the middle, a compact legend with shares beside it', svgW > 0 && svgW <= 90 && /class="vw-dtot"[^>]*>6</.test(d1) && /vw-legend-col/.test(d1) && /50%/.test(d1), 'svg ' + svgW);
const mains = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', 'main.json'), 'utf8')).widgets.filter((w) => w.record && w.record.form === 'donut');
const eo = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', 'estate-overview.json'), 'utf8')).widgets.filter((w) => w.record.form === 'donut');
t('the donut tiles are three columns wide now (they were four)', mains.concat(eo).length >= 3 && mains.concat(eo).every((w) => w.span[0] === 3), mains.concat(eo).map((w) => w.record.id + ' ' + w.span).join('; '));
// ── the containers tiles read docker.ps slim (the full Engine records were ~650 KB a read) ──
{ const dps = []; ['main', 'main-estate', 'main-compute', 'main-inference', 'estate-overview', 'dream', 'wol-workers', 'wol-ollama', 'wol-jobs', 'wol-wkjobs', 'wol-observe'].forEach((k) => { const L = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', k + '.json'), 'utf8'));
    const walk = (r) => { if (!r || typeof r !== 'object') return; if (r.source === 'docker.ps') dps.push(k + ':' + r.id + ':' + JSON.stringify((r.read || {}).args || {})); (r.children || []).forEach((c) => walk(c.record)); }; L.widgets.forEach((w) => walk(w.record)); });
  t('every record that reads docker.ps asks for it slim', dps.length >= 2 && dps.every((x) => /"slim":true/.test(x)), dps.join(' ')); }
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
