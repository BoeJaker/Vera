// The desk's item kinds, reviewed one by one (2026-10-11): a lookup's evidence, fields in words, no title drawn twice,
// names without the reply's markdown, cards fitted to what they hold, diagrams that say when they cannot be drawn,
// one item per thing harvested.
const fs = require('node:fs');
const path = require('node:path');

let fails = 0;
const t = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) fails++; };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const B = mod.BLOCK;
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

/* evidence (info.seek) */
const ev = { title: 'Looked up · ai model releases', kind: 'evidence', query: 'ai model releases',
  items: [{ id: 'u1', n: 1, url: 'https://a.example/1', domain: 'a.example', title: 'Release one', snippet: 'one', when: '2026-10-10', kind: 'web' },
          { id: 'u2', n: 2, url: 'https://b.example/2', domain: 'b.example', title: 'Tracked two', snippet: 'two', when: '2026-10-05', kind: 'intel' }],
  evidence: { need: 'latest', subject: 'ai model releases', question: 'what are the latest ai model releases', since: '2026-09-27', answerable: true,
    checked: ['news: 7 headlines', 'web: 3 searches'], gaps: ['no benchmark numbers'], tracked: 'AI research', ms: 32000 } };
const e = B.records(ev, 'l', 'k', {});
t('evidence: the question and how it was looked up', /what are the latest ai model releases/.test(e) && /need<\/i>latest/.test(e) && /since<\/i>2026-09-27/.test(e) && /answerable/.test(e) && /tracked<\/i>AI research/.test(e));
t('evidence: each page under the number the reply cites it by; a tracked topic\'s in its own colour', /vc-rbx-num" title="cited as \[1\]/.test(e) && /vc-rbx-num intel/.test(e));
t('evidence: what was checked and the gaps, the gaps open', /<details class="vc-ev-foot" open>/.test(e) && /no benchmark numbers/.test(e) && /web: 3 searches/.test(e));
t('the chat lands info.seek as evidence, before the generic page list', chat.indexOf("cvAdapter('evidence'") > 0 && chat.indexOf("cvAdapter('evidence'") < chat.indexOf("cvAdapter('sources'"));
t('evidence: a scraper\'s header is taken off the excerpt', chat.includes('Markdown Content:') && chat.includes('/^\\s*Title:/i.test(txt)'));

/* fields */
const kv = B.kv({ title: 'gpu-250 · status', fields: { vram_used: '21.4 GB', modelsLoaded: 3 } }, 'm', 'k', {});
t('fields: named in words, the raw name as the tip', />Vram used</.test(kv) && />Models loaded</.test(kv) && /title="vram_used"/.test(kv));
t('fields: the header names it, so the body does not', !/gpu-250 · status/.test(kv));
t('fields: tiles when wide (a container query on an inner grid, not on the container)', src.includes('.vc-kv-g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))'));

/* documents */
const md = B.markdown({ title: 'Choosing a 7B model', md: '# Choosing a 7B model\n\nBody text.' }, 'm', '', {});
t('a document whose first heading is its title does not draw it twice', !/Choosing a 7B model/.test(md) && /Body text/.test(md));
const md2 = B.markdown({ title: 'Notes', md: '# Something else\n\nBody.' }, 'm', '', {});
t('...a different first heading stays', /Something else/.test(md2));

/* names */
const en = B.entities({ groups: [{ group: 'products', entities: [{ text: '`Phi', type: 'PRODUCT', count: 1 }, { text: 'Qwen-2.5-Coder`', type: 'PRODUCT', count: 1 }] }] }, 'm', 'k', {});
t('names without the reply\'s markdown round them', />Phi</.test(en) && />Qwen-2.5-Coder</.test(en) && !/`/.test(en.replace(/<[^>]+>/g, '')));

/* calendar */
const cal = B.calendar({ month: '2026-10', events: [] }, 'm', 'k', {});
t('calendar: an inner grid the wide layout lays out (month beside its list)', /<div class="vc-cal"><div class="vc-cal-in">/.test(cal) && src.includes('grid-template-areas:"hd l" "w l" "g l"'));

/* fitting and drawing */
t('cards are fitted to what they hold after a placement; a fit that overflows a little grows, it is not dropped',
  src.includes('try { this._deskFit(); }') && src.includes('if (more > 160) delete nat[key]; else nat[key] = c.offsetHeight + more + 6;'));
t('the fit caps a card\'s face, its separation size and its growth', src.includes('maxH: nat || undefined') && src.includes("Math.min(H - 8, s.h, chip ? Infinity : (it.maxH || Infinity))) };") && src.includes('it.maxH || Infinity'));
t('WSU-69/70: separation also caps width by kind/form and keeps a measured least height (minH)', src.includes('chip ? Infinity : (it.maxW || Infinity)') && src.includes('Math.min(H - 8, it.minH || 0)') && src.includes('const formMaxW = (f) =>') && !src.includes('DESK_FORM_MAXW'));
t('an image fills its card, whole', src.includes('.vc-fig > img{flex:1 1 0;min-height:0;width:100%;height:100%;max-width:none;object-fit:contain}'));
t('a diagram no longer being written is drawn whole (render), so a broken one shows its error', src.includes("if (!streaming && typeof inner.render === 'function') { try { inner.render(code); } catch (e) {} return; }"));
t('a mermaid fence lands once, not as a code item and a diagram both', chat.includes("const sig=m.kind==='diagram'?'d:'"));

/* round 2: titles from what an item holds, a command that ran, the timeline's ticks, the header's score, widget tables */
const btSrc = src.slice(src.indexOf('function blockTitle(b)'), src.indexOf('const EDITABLE'));
const textFieldOf = () => 'text'; const blockTitle = new Function('textFieldOf', btSrc + '; return blockTitle;')(textFieldOf);
t('code is named by its language and first definition, not its key', blockTitle({ key: 'turn:m7:code:0', type: 'code', content: { lang: 'python', code: 'import os\n\ndef scan(net):\n  pass\n' } }) === 'python · scan');
t('a table by its first columns and rows', blockTitle({ key: 'turn:m9:table:0', type: 'table', content: { columns: ['Model', 'Strengths', 'Best For', 'x'], rows: [[1], [2]] } }) === 'Model · Strengths · Best For — 2 rows');
t('a terminal by its command, html by its first heading', blockTitle({ key: 'k:t', type: 'session', content: { command: 'nvidia-smi  -q' } }) === '$ nvidia-smi -q'
  && blockTitle({ key: 'k:h', type: 'html', content: { html: '<div><h2>Benchmark <b>summary</b></h2></div>' } }) === 'Benchmark summary');
const ran = B.session({ command: 'nvidia-smi', output: 'Tesla V100', host: 'gpu-250' }, 'm', 'k', {});
t('a command that ran shows what ran, where, and its output - not a connect form', /vc-ran-cmd/.test(ran) && /\$<\/span><code>nvidia-smi/.test(ran) && /Tesla V100/.test(ran) && !/vc-tconnect/.test(ran));
const tl = B.timeline({ events: [{ when: '2024-04', label: 'a' }, { when: '2025-01', label: 'b' }, { when: '2026-10', label: 'c' }] });
t('a timeline\'s first year tick stays on the axis', !/left:-/.test(tl));
t('the desk header does not show a bare relevance number', src.includes('.stage .it > .it-hd > .sc{display:none}'));
const wsrc = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
t('widget table rows share their column tracks (no auto: each row is its own grid)', wsrc.includes("return i ? (nm ? 'minmax(44px,.6fr)' : 'minmax(56px,1fr)') : 'minmax(0,1.4fr)'") && !wsrc.includes("'minmax(40px,auto)'"));

/* minimal cards, a page opened takes the room, cards settle up the desk (2026-10-11) */
t('minimal: counts and stats at the top are not drawn unless the canvas shows its full chrome', src.includes(':host(:not([data-tier="full"])) .it .vc-rbx > :is(.vc-rbx-top,.vc-rbx-glance){display:none}') && src.includes(':is(.vc-rr-n,.vc-rr-ft,.vc-act-n){display:none}'));
t('minimal: controls over the item, shown when pointed at, used or opened; facets when the filter is used', src.includes('.it:is(:hover,.hov,:focus-within,.maxed) .vc-rbx > .vc-rbx-tools{opacity:1;pointer-events:auto}') && src.includes('.it:not(:focus-within):not(.maxed) .vc-rbx > .vc-rbx-facets{display:none}'));
t('minimal: the card\'s icon row, a combined card\'s tabs and a drawing\'s caption and menu likewise', src.includes(':host(:not([data-tier="full"])) .it:not(.desk-chip) > .hda{opacity:0}') && src.includes('> .cmp-tabs{opacity:0') && src.includes('#live .lv :is(.vge-note,.vg-layer){opacity:0'));
t('a page opened in a list fills the room, alone, and closing the last puts the card back', src.includes('_readerFollow(key, grow) {') && src.includes("if (any && grow !== false && this._max !== key) { this._autoMax = key; this._toggleMax(key); return true; }")
  && src.includes("if (!any && this._autoMax === key) { this._autoMax = null; if (this._max === key) this._toggleMax(key); return true; }") && src.includes("(maxed && this._autoMax === key ? ' reading' : '')"));
t('an opened page has a way back, shown while it fills the room', src.includes('class="vc-rbx-b rb-back" data-rec-act="open"') && src.includes('.rb-back{display:none!important}.it.reading .rb-back{display:inline-flex!important}'));
t('the desk settles its cards upward after pulling them apart; a risen card keeps its size', src.includes('D.settle(withStrip(tiled), R, { W: W, H: H }, { gap: 12 });') && src.includes('if (it.settled && !it.fixed) { it.keepSize = true;'));

/* round 4 (owner, 2026-10-11): reader on the title, plain click in place, carry back from full, pills, names menu, sizes */
t('a plain click opens a result in place; only reader mode fills the room', src.includes('_readerFollow(key, grow)') && src.includes('if (!st.open[it.id] && this._readerFollow(key, false)) return;'));
t('a result\'s title opens reader mode (ctrl / middle click still opens the page)', src.includes("const tl = t.closest('a.vc-rbx-t');") && src.includes("this._recAct(row.dataset.recKey, 'read', row.dataset.recArg)"));
t('reader text has no box of its own and the card filling the room is never see-through', src.includes('.it.reading .vc-rbx-open :is(.vc-rbx-body,.vc-reader,.vc-rd-body){background:transparent!important') && src.includes('.stage .it:is(.maxed,.reading){background:var(--bg1,#15181d)!important'));
t('a filling card carried by its header goes back to the desk where it is let go', src.includes("this._dgMax = { key: it.dataset.key, x0: ev.clientX, y0: ev.clientY, pid: ev.pointerId }") && src.includes('if (this._max === m.key) this._toggleMax(m.key);'));
t('a card minimises to a pill from its controls and opens again on a click', src.includes("hb('pill', 'pill'") && src.includes("if (act === 'pill') { if (!this._hold) this._holdStart(key); this._histPush('pill', key); this._pillSet(key, true);") && src.includes('this._pillSet(pc.dataset.key, false)'));
t('names and sites are a menu of ranked widgets, not rows of pills', src.includes('data-rec-act="names"') && src.includes("form: 'ranked'") && src.includes(':host(:not([data-tier="full"])) .it .vc-rbx > .vc-rbx-facets{display:none!important}'));
t('the card\'s controls are bigger; narrow kinds are no wider than they use', src.includes('.hda > .hb{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px') && src.includes('entities: 460, note: 420, kv: 560'));
const ge = fs.readFileSync(path.join(__dirname, '..', 'vera', 'graph_embed_element.js'), 'utf8');
t('a graph of the host\'s own data takes no estate events (it flickered with each one)', ge.includes("subscribeLiveEvents: !(this.getAttribute('renderer') === 'data' || this.hasAttribute('calm'))"));
t('the memory graph says why it is empty rather than drawing nothing', chat.includes('session started on a sandbox') && chat.includes("textContent='nothing stored in memory for this session'"));

t('a card filling the desk is above every card and pill (the held canvas\'s z 8 put it under them: "see-through")', src.includes(':host([align="desk"]:not([desk-narrow])) .stage .it.maxed{z-index:var(--z-full,150)!important}'));

/* round 5 (WSU-50, 51, 53, 56) */
t('a pill opens as a card under the pointer, whatever its kind, and goes back when the pointer leaves', src.includes("it.classList.add('chip-peek', 'pill-open'); it.dataset.deskFace = 'card';") && src.includes("if (s.open) { it.classList.add('desk-chip'); it.dataset.deskFace = s.face;"));
t('carried to where a card is a pill, it is a pill whatever size it was given; let go there it joins the border', src.includes("let bare = D.sizeAt(g.R, px, py, {}, g.desk);") && src.includes("this._deskPosSet(d.key, { chip: true });")
  /* owner 2026-10-16: over the pill strip it is a pill at once (no margin), and the hold lets a card let go as a pill go */
  && src.includes("const Z0 = this._stripZone; const onStrip = !!(Z0 && px >= Z0.l - 10 && px <= Z0.r + 10);") && src.includes("if (onStrip) face = 'chip';")
  && src.includes("if (this._hold && this._hold.rects) { delete this._hold.rects[d.key];"));
t('let go on the desk it stays there (and is no longer a minimised pill); the upward settle leaves it', src.includes("if (this._pillGet()[d.key]) this._pillSet(d.key, false);") && src.includes('stay: !!userAt(key)'));
t('code scrolls in its card on the desk', src.includes('.vc-codewrap > .code-card > pre{flex:1 1 auto;min-height:0;max-height:none!important;overflow:auto!important'));

/* round 6 (owner, 2026-10-11): research in one place, formatted streaming, pane below or beside, no dump items */
t('a research run on the canvas leaves a chip in the chat (one place)', chat.includes("_cvOnCanvas({ _el:host, n:'research'+(r.q?' · '+r.q.slice(0,40):'') }, r.key)"));
t('the run streams in formatted, as the chat draws it', src.includes(': live ? `<div class="vc-rr-doc vc-rr-live">${draw(live)}</div>`'));
t('its second pane goes beside the report or under it', src.includes("act === 'rrlay'") && src.includes('.vc-rr.lay-below .vc-rr-body{display:grid!important'));
t('a tool result nothing can draw makes no item (no fenced dump of the card); the old ones are not drawn', !chat.includes("content:{md:'**'+n+'**\\n\\n'+(res?'```\\n'+res+'\\n```':'')}") && src.includes("/^turn:[^:]+:cap:\\d+$/.test(String(b.key || ''))"));
t('a tool card\'s insides are not lifted again by the code, diagram, table and image hooks', chat.includes("cvHook('code', 'pre code', (el, out, i) => { if(_inTool(el)) return;") && chat.includes("cvHook('table', 'table', (el, out, i) => { if(_inTool(el)) return;"));
t('the chat\'s "on the canvas" chip focuses its item (WSU-60: every chip, one delegated click; the canvas opens; pill, tab and card to the front)',
  chat.includes("closest('#msgs .cv-moved')") && chat.includes("if(!_pages.has('canvas')){ try{ openPage('canvas'); }catch(_){} }")
  && chat.includes("cv.focusItem(key)") && !chat.includes("cv.flash&&cv.flash(key)")
  && src.includes('focusItem(key, again) {') && src.includes("if (it && it.classList.contains('desk-chip')) { if (again) this._hovKey = null; this._hovSet(String(key)); }"));

const D = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));
const g = [{ id: 'n', x: 300, y: 200, w: 300, h: 100, maxH: 120 }];
D.grow(g, { l: -440, t: 0, r: 0, b: 900 }, { W: 1200, H: 900 }, {});
t('a card grows no taller than what it holds (and no wider than a page of it)', g[0].h <= 120 && g[0].w >= 300);

/* owner 2026-10-17: each kind grows the way it reads - a list down, a chart across, a table to its rows and columns */
{ const gr = (it) => { D.grow([it], null, { W: 1600, H: 1000 }, { maxW: 1584, maxH: 992 }); return it; };
  const chart = gr({ x: 800, y: 300, w: 600, h: 340, maxH: 600, ratio: 4.5 });
  const list = gr({ x: 300, y: 500, w: 400, h: 400, maxW: 560, ratio: 3.2 });
  const page = gr({ x: 800, y: 500, w: 400, h: 300 });
  t('a chart grows across the desk, no taller than 600', chart.w > 1500 && chart.h <= 600 && chart.w / chart.h > 2.2);
  t('a list grows down, no wider than 560', list.w <= 560 && list.h > 900 && list.h / list.w > 1.6);
  t('any other card keeps within a page aspect (2.2 : 1)', page.w / page.h <= 2.21 && page.h / page.w <= 2.21);
  t('the kinds ask for it: reading widgets are lists; lists, charts and tables get their caps and aspect',
    /\|reading\|headlines\|articles\|sources\)\$\/\.test\(f\)/.test(src) && src.includes("if (!user && kind === 'list') { o.maxW = Math.min(o.maxW || Infinity, Math.max(o.w, 560)); o.ratio = 3.2; }")
    && src.includes("else if (!user && kind === 'chart') { o.maxH = Math.min(o.maxH || Infinity, 600); o.ratio = 4.5; }")); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
