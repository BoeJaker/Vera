// The chat round of 2026-09-27 (owner):
//   * "when the lhm is opened id like it to display over the top of [the glow] so you can see the glow behind it. and when
//      the lhm expands out it should expand over the glow background"
//   * "the lhm expanding and collapsing animations need to be smoother"
//   * "id like the chat canvas and graph buttons to trigger nice animations so the sections dont simply flash in"
//   * "id like for the harness to be able to absorb controls for the current open panel into the top bar ... one top bar"
//   * "on the dashboard ... 2 top header bars - this needs to be combined into one"
//   node tests/test_chat_glow_motion_one_topbar.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the glow shows under the menu ───────────────────────────────────────────────────────────────────────────────
t('the menu is a translucent surface over the wash', /#rightRail\.lhm-host \.lhm-det\{background:color-mix\(in srgb,var\(--surf\) 68%,transparent\);[^}]*backdrop-filter:blur/.test(CHAT));
t('in the harness the frames step aside for the harness\'s one wash', /html\[data-harness\] body\{background:transparent\}/.test(CHAT) && /html\[data-harness\] #veraWash\{display:none\}/.test(CHAT));
t('the harness draws that wash while the chat is open', /#veraWashH\{position:fixed;/.test(HAR) && /body\.chat-open #veraWashH\{opacity:1\}/.test(HAR) && /function _washMount\(\)/.test(HAR));
t('and its menu slot is translucent over it', /body\.chat-open #lhmNav\.chatmenu\{background:color-mix\(/.test(HAR));

// ── smoother ────────────────────────────────────────────────────────────────────────────────────────────────────
t('the menu folds by width, not a display switch', /#rightRail\.lhm-host\.slim > \.lhm-det\{display:flex!important;width:0!important;/.test(CHAT) && /#rightRail\.lhm-host > \.lhm-det\{transition:width \.36s/.test(CHAT));
t('never while its edge is being dragged', /body\.dragging-split #rightRail\.lhm-host > \.lhm-det\{transition:none\}/.test(CHAT));
t('"is it open" is its width now, so the active icon still folds and opens it', /_det\.getBoundingClientRect\(\)\.width > 4/.test(LHM) && !/_det\.getClientRects\(\)\.length/.test(LHM));
t('the harness slot grows and shrinks on the same easing', /\.lhm-nav\{transition:width \.36s cubic-bezier/.test(HAR));
t('reduced motion is respected', /@media \(prefers-reduced-motion:reduce\)\{ #rightRail\.lhm-host > \.lhm-det\{transition:none\}/.test(CHAT));

// ── the columns arrive ──────────────────────────────────────────────────────────────────────────────────────────
t('the canvas column opens from the edge, to the width it will have', /col\.animate\(\[\{ maxWidth:'0px', opacity:0, transform:'translateX\(28px\)' \}, \{ maxWidth:w\+'px', opacity:1, transform:'none' \}\]/.test(CHAT));
t('and leaves before it is taken out', /a\.onfinish=\(\)=>\{ col\._leaving=false; if\(_pages\.has\(name\)\)\{ _pages\.delete\(name\); _pagesApply\(\); \}/.test(CHAT));

// ── one top bar: run the chat's offer against a stand-in bar ────────────────────────────────────────────────────
{
  const a = CHAT.indexOf('  const _HDR_PRIO='), b = CHAT.indexOf('  function _hdrOfferMount(){');
  t('the offer is in the page', a > 0 && b > a);
  const mkEl = (tag, o) => Object.assign({ tagName: tag, id: '', className: '', title: '', textContent: '', hidden: false, dataset: {}, children: [], parentElement: null,
    classList: { contains(c) { return String(this._el.className).split(/\s+/).indexOf(c) >= 0; } },
    querySelector(sel) { return (this._all || []).find((x) => x.tagName === sel.toUpperCase()) || null; },
    querySelectorAll(sel) { return (this._all || []).filter((x) => x.tagName === sel.toUpperCase()); } }, o || {});
  const bar = mkEl('DIV', { id: 'topBar' });
  const kid = (tag, o, kids) => { const e = mkEl(tag, o); e.classList._el = e; e.parentElement = bar; e._all = kids || []; (kids || []).forEach((k) => { k.parentElement = e; k.classList._el = k; }); bar.children.push(e); return e; };
  kid('SPAN', { className: 'tb-logo', textContent: 'VERA' });
  kid('DIV', { id: 'connDot', className: 'conn-dot' });
  const sel = mkEl('SELECT', { value: 'aide', options: [{ value: 'aide', textContent: 'Aide' }, { value: 'coder', textContent: 'Coder' }] });
  kid('DIV', { className: 'agent', title: 'The agent' }, [sel]);
  kid('DIV', { className: 'sess' });
  const ask = mkEl('BUTTON', { id: 'autoActBtn', className: 'tb-btn on', textContent: 'Ask first', title: 'act or ask' });
  kid('DIV', { className: 'tb-group', title: 'Execution behaviour' }, [ask]);
  const cv = mkEl('BUTTON', { id: 'colCanvasBtn', className: 'tb-btn', textContent: '+ Canvas' });
  kid('SPAN', { className: 'xmseg pseg' }, [cv]);
  kid('BUTTON', { id: 'hdrMore', className: 'ico', title: 'All chat tools' });
  kid('BUTTON', { id: 'hdrAa', className: 'ico', textContent: 'Aa' });
  const docs = { topBar: bar, sessionName: { textContent: 'My session', dataset: {} }, sessMeta: { textContent: '3 msgs' }, agentMeta: { textContent: 'qwen · ct126' } };
  const ctx = { document: { getElementById: (id) => docs[id] || null }, getComputedStyle: () => ({ display: 'flex' }), JSON, window: {} };
  vm.createContext(ctx);
  vm.runInContext(CHAT.slice(a, b) + '\nthis.I=_hdrItems;', ctx);
  const G = ctx.I();
  const names = G.map((g) => g.grp);
  t('what the harness already has is not offered (logo, connection dot, Aa)', !names.includes('logo') && !JSON.stringify(G).includes('"Aa"') && !JSON.stringify(G).includes('VERA'), JSON.stringify(names));
  t('the agent is offered as a picker with its options', (G.find((g) => g.grp === 'agent') || { items: [{}] }).items[0].kind === 'select' && G.find((g) => g.grp === 'agent').items[0].options.length === 2);
  t('the session is offered by its name', (G.find((g) => g.grp === 'session') || { items: [{}] }).items[0].label === 'My session');
  const ex = G.find((g) => g.items && g.items[0] && g.items[0].label === 'Ask first');
  t('a switch carries its state', !!ex && ex.items[0].on === true && ex.prio === 3, JSON.stringify(ex));
  t('the pages come before the rarer tools', (G.find((g) => g.items && g.items[0] && g.items[0].label === '+ Canvas') || {}).prio === 2);
  t('the chat\'s own ⋯ is offered and never dropped', (G.find((g) => g.grp === 'more') || {}).prio === 0);
  t('a press comes back as the same click on the same element', /if\(d\.type!=='vera:hdr:act'\) return;/.test(CHAT) && /else el\.click\(\);/.test(CHAT));
  t('only the harness that holds it can fold the bar', /ev\.source!==window\.parent\) return;/.test(CHAT) && /html\.hdr-absorbed #topBar\{height:0!important;/.test(CHAT));
  t('the ⋯ sheet still opens while the bar is folded', /html\.hdr-absorbed #topBar #toolsSheet\{visibility:visible\}/.test(CHAT));
  t('the chat offers only inside the harness, as the chat instance', /if\(_EMBED\.harness&&_EMBED\.only==='chat'\) _hdrOfferMount\(\);/.test(CHAT));
}
{
  t('the harness takes the offer from the open tab and tells the panel it holds it', /d\.type !== 'vera:hdr:offer'/.test(HAR) && /o\.src\.postMessage\(\{ type: 'vera:hdr:absorbed', on \}/.test(HAR));
  t('its stats, activity, style and theme step into a drawer', /const _HDR_DRAWER_IDS = \['hdrPills', 'hdrMetrics', 'hdrMetricCfgWrap', 'vao-pill', 'hdrStyleSeg', 'hdrSwatches'\];/.test(HAR));
  t('and come back when no panel holds the bar', /else if\(el\.parentNode === dr && el\._hdrMark && el\._hdrMark\.parentNode\)/.test(HAR));
  t('what does not fit goes, lowest priority first', /function _hdrFit\(host\)/.test(HAR) && /\+g\.dataset\.prio > 1/.test(HAR));
  t('the bar follows every change of tab', /_tabRender = function\(\)\{ const r = __tabRender\.apply\(this, arguments\); try\{ _hdrSync\(\); \}catch\(_\)\{\} return r; \};/.test(HAR));
}

// ── one dashboard header ───────────────────────────────────────────────────────────────────────────────────────
t('the dashboard toolbar is one row', /#panel-dashboard \.dash-toolbar\{flex-wrap:nowrap;/.test(HAR));
t('the health readings join it rather than taking a row of their own', /tb\.insertBefore\(hs,/.test(HAR) && /#panel-dashboard \.dash-toolbar #dashHealthStrip:empty\{display:none!important\}/.test(HAR));
t('what does not fit goes behind ⋯, the add button last', /function _dashToolbarFit\(\)/.test(HAR) && /add widget/i.test(HAR));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
