// Panels and widgets as canvas citizens (vera/chat/chat_panel.html, vera/canvas/canvas_element.js).
//
// "The user can already open full panels by saying something like 'open the calendar' - the canvas needs to be
// unified with the panel bridge system. The panel bridge also lets the LLM drive the panels - this could be
// extended to widgets."
//
// Half of it was already built and had no way in. The canvas has drawn a `panel` block all along - live in its
// frame, with query and dispatch over the bridge and a console to type an action into - and nothing ever put one
// there. panel.open opened a page beside the chat and the canvas never heard about it, so the working area had no
// idea what the user was working in, and the panel could not be kept, parked, recalled, or set beside the things
// it relates to.
//
// The widget was the opposite: plenty of ways in, and no way to drive it. It was a picture of a capability's
// answer at the moment it landed, and a working area made of stale pictures is a scrapbook. Its record has always
// carried the cap it reads and the arguments it read with - the same pair the chat used to place it - so it can
// read it again.
//
//   node tests/test_canvas_panels_widgets.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const cv = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));

// ── a panel the model opens lands on the canvas too ────────────────────────────────────────────────────────────
{
  t('opening a panel lands it', /_cvLandPanel\(String\(a\.id\|\|''\), a\.title\|\|a\.label\|\|'', _cvRelMid\|\|''\)/.test(chat));
  t('as the panel block the canvas already drew', /kind:'panel', key:'panel:'\+id/.test(chat));
  /* keyed by the panel, so "open the calendar" twice keeps one item - canvas.add on an existing key is a show
     through the resolver, not a second copy */
  t('keyed by the panel, so it is shown rather than doubled', /key:'panel:'\+id/.test(chat));
  t('and it is large, because a panel in a small item is a letterbox', /kind:'panel'[^}]*size:'l'/.test(chat));
  /* the page beside the chat still opens - this ADDS the canvas, it does not move the panel off the screen the
     user asked for it on */
  t('the page beside the chat still opens', /const ok=await panelOpen\(String\(a\.id\|\|''\), who\);/.test(chat));
  t('and the canvas only hears about it when the open succeeded', /if\(ok\) try\{ _cvLandPanel/.test(chat));
}

// ── the panel item is driven over the bridge, which it already was ─────────────────────────────────────────────
{
  t('the canvas draws a panel', typeof mod.BLOCK.panel === 'function');
  const h = mod.BLOCK.panel({ panel: 'calendar-panel', title: 'Calendar' }, 'l', 'panel:calendar-panel', null);
  t('live in its frame', /data-live="frame"/.test(h) && /calendar-panel/.test(h));
  t('with query and dispatch over the bridge', /data-act="pquery"/.test(h) && /data-act="pdispatch"/.test(h));
  t('a console to drive it by hand', /data-f="action"/.test(h) && /data-f="payload"/.test(h));
  t('and a way out to the standalone page', /data-act="pstand"/.test(h));
  t('the bridge is the capability, not a private channel', /panel\.dispatch/.test(cv) && /panel\.query/.test(cv));
}

// ── a widget can be read again ─────────────────────────────────────────────────────────────────────────────────
{
  /* the live-element branch is the one that runs in a browser; without a customElements the module falls back to
     its record card, which is the drawing for a page that has not loaded the widget element. Shim it so the
     branch under test is the branch under test. */
  global.customElements = { get: () => function () {} };
  const rec = { form: 'table', draw: { form: 'table', size: 'm' }, reads: { cap: 'docker.hosts', args: { all: true } } };
  const withSrc = mod.BLOCK.widget(rec, 'm', 'widget:hosts');
  t('a widget that knows its source offers to read it again', /data-wid-act="refresh"/.test(withSrc), withSrc.slice(0, 120));
  t('and says which capability that is', /docker\.hosts/.test(withSrc));
  /* a widget drawn from inline data has nothing to go back to, and a button that cannot work is worse than no
     button - it is a promise the item cannot keep */
  const inline = mod.BLOCK.widget({ form: 'table', draw: { form: 'table' }, data: [{ a: 1 }] }, 'm', 'widget:inline');
  t('one drawn from inline data does not offer', !/data-wid-act="refresh"/.test(inline));

  t('the refresh calls the cap the record names', /const cap = \(rec\.reads && rec\.reads\.cap\) \? String\(rec\.reads\.cap\) : ''/.test(cv)
    && /await this\.callResult\(cap, args\)/.test(cv));
  t('with the arguments it read with', /rec\.reads\.args && typeof rec\.reads\.args === 'object'/.test(cv));
  t('and writes the answer back into the item, so it survives a reload', /this\.call\('canvas\.update', \{ key, content: next \}\)/.test(cv));
  t('a failure says so rather than silently doing nothing', /return this\._readout\(key, \(r && r\.error\) \|\| 'nothing came back'\)/.test(cv));
  t('the button reaches the handler', /\[data-wid-act\]/.test(cv));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
