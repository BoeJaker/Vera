// The GLOBE form (the Globes board — godseye on the canvas): one globe form, any config. A record's points are pins on an
// orthographic globe that faces the set it shows, a few at a time with a list beside them and a page to the next few;
// links are arcs, the night side a terminator, a sweep turns; the iso variant stands on a plinth with flags on posts.
//   node tests/test_widget_globe.cjs
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname;
const src = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(src, ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const count = (h, re) => (h.match(re) || []).length;
const HOSTS = { title: 'The estate', points: [['llm.int · Manchester', -2.24, 53.48, '#6ea8d8', 'the rack', '32 cores'], ['edge · Frankfurt', 8.68, 50.11, '#7f9cf5', 'edge proxy', '4 cores'], ['worker · Virginia', -77.5, 39.0, '#4fd1c5', 'cloud worker', '8 cores'], ['worker · Singapore', 103.8, 1.35, '#4fd1c5', 'cloud worker', '8 cores'], ['workstation · home', -1.9, 52.5, '#e9b85a', 'workstation', '12 cores']], links: [[0, 1], [0, 2], [0, 3, '#e9b85a'], [0, 4]], pins: 5 };
const EV = W.sample('globe');
t('the form is drawn in its own right — no longer an alias of the scatter — flat and iso, and the catalogue of the element says so', W.forms().some((f) => f.id === 'globe' && f.drawn && !f.as && f.iso && f.shape === 'points') && !W.forms().some((f) => f.id === 'globe' && f.as));
const g = W.draw('globe', HOSTS, 'm', { width: 300, height: 200 }), gsvg = g.split('<div class="side">')[0];
t('a globe: the disc, the rim, a graticule by projection (no map data), every visible pin with its label', /class="disc"/.test(g) && /class="rim"/.test(g) && count(g, /class="gl"/g) >= 10 && count(gsvg, /class="pin/g) >= 3 && /class="pl"[^>]*>llm\.int</.test(gsvg) && /vb-globe/.test(g));
t('the globe faces the middle of the set: llm.int and the edge are on the visible face, Singapore is behind it (in the list, not on the globe)', /llm\.int/.test(gsvg) && /edge/.test(gsvg) && !/Singapore/.test(gsvg) && /Singapore/.test(g));
t('the links are arcs from host to host, the given one in its own colour', count(g, /class="arc"/g) >= 3 && /class="arc" points="[^"]+" stroke="#e9b85a"/.test(g));
t('the list beside the globe names every shown host with its detail and meta', count(g, /<span class="r">/g) === 5 && /<b>llm\.int · Manchester<\/b>32 cores<br><small>the rack<\/small>/.test(g));
t('a pin is sized by the point\'s size (cores) when it has one', (() => { const h = W.draw('globe', { points: [{ name: 'a', lon: -2, lat: 53, cores: 8 }, { name: 'b', lon: 8, lat: 50, cores: 32 }], view: [3, 50] }, 'm', { width: 300, height: 200 }); return /r="7\.0"/.test(h) && /r="4\.0"/.test(h); })());
const e1 = W.draw('globe', EV, 'm', { width: 300, height: 200 });
t('the board\'s events: four at a time, numbered on the globe and in the list, a way to page to the next four', /data-shown="4"/.test(e1) && /data-pages="2"/.test(e1) && count(e1, /<span class="r">/g) === 4 && /data-vb-set="gpage:1"/.test(e1) && /1 \/ 2 · 8/.test(e1) && /class="pn"/.test(e1));
const e2 = W.draw('globe', EV, 'm', { width: 300, height: 200, ui: { gpage: 1 } });
t('the second page shows the other four, the globe turned to face them', /data-page="1"/.test(e2) && /Houston/.test(e2) && !/Houston/.test(e1) && /data-vb-set="gpage:0"/.test(e2));
t('the night side and the sweep are drawn when the record asks for them', /class="term" d="M/.test(e1) && /class="scan"/.test(e1) && !/class="scan"/.test(g));
const s = W.draw('globe', HOSTS, 'm', { width: 90, height: 80 });
t('in a low frame (the rail) the globe stands alone — no list, a coarser graticule', /class="disc"/.test(s) && !/class="side"/.test(s) && count(s, /class="gl"/g) < count(g, /class="gl"/g));
const iso = W.draw('globe', { points: [['SEV1', 32.5, 30.5, '#e8706b', 'Suez', 'shipping halt'], ['SEV2', 120.5, 24.2, '#e9b85a', 'Taiwan Strait', 'watch']], view: [70, 20] }, 'l', { width: 320, height: 260, projection: 'iso' });
t('the iso globe stands on a plinth, its pins flags on posts', /class="plinth"/.test(iso) && /class="vb-globe posts"/.test(iso) && count(iso, /<rect /g) >= 2 && /-118 220 235/.test(iso));
t('a record without a longitude and a latitude says what it needs', /needs points with a longitude and a latitude/.test(W.draw('globe', [{ x: 1, y: 2 }], 'm', { width: 300, height: 200, sample: false })));
t('an object record: points, pins, night as a sun longitude, an explicit view', (() => { const h = W.draw('globe', { pins: [{ name: 'A', lon: 0, lat: 0 }, { name: 'B', lon: 10, lat: 5, open: true }], night: -40, view: [0, 0] }, 'm', { width: 300, height: 200 }); return /<b>A<\/b>/.test(h) && /class="pin p"/.test(h) && /class="term"/.test(h); })());
t('the sample face is the Globes board\'s own set (eight events of the hour)', Array.isArray(EV.points) && EV.points.length === 8 && EV.pins === 4 && EV.night === true);
t('the API version says the form landed', W.version >= 5);
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
