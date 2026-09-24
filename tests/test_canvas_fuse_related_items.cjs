// FUSING (owner, 2026-09-24, twice: "the canvas should be able to combine text elements, widget, ui panels, charts,
// notes, terminal into a homogenous functional contextual ui - not one that is just a bunch of tiles", and then "i
// wanted to fuse elements and use them together not just display a grid???? was i unclear?").
//
// Items that are about the same thing are drawn as ONE element with panes in it. Three bindings, every one read off
// the document rather than guessed: a diagram bound to its source (beside), one figure and one table out of the same
// message (over), three or more glanceable things from one message (strip). The lead keeps the card; a member keeps
// its KEY, so its live slot, the runs drawn to it and every capability that addresses it go on working.
//   node tests/test_canvas_fuse_related_items.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const at = (m) => ({ anchor: { turn: 't1', mid: 't1', from: m } });

/* ── 1. a diagram OF something: the one binding the document states outright ─────────────────────────────────── */
{
  const code = Object.assign({ key: 'code:1', type: 'code', size: 'm', content: { code: 'x=1' } }, at('a1'));
  const gr = Object.assign({ key: 'explode:code:1', type: 'explode', size: 'm', content: { binds: 'code:1' } }, at('a1'));
  const F = V.fuseOf([code, gr]);
  t('the source leads and its structure diagram is a pane of it',
    F.of['explode:code:1'] === 'code:1' && !!F.groups['code:1'] && F.groups['code:1'].members.join() === 'explode:code:1',
    JSON.stringify(F));
  t('...laid out beside it, not under it', F.groups['code:1'].layout === 'beside');
  t('the lead is not a member of anything — it keeps its card', F.of['code:1'] === 'code:1' && !F.groups['explode:code:1']);
  // a diagram bound to something that is not here is nobody's pane
  const G = V.fuseOf([gr, Object.assign({ key: 'note:9', type: 'note', size: 'm', content: {} }, at('a1'))]);
  t('a diagram bound to an item that is NOT on the canvas fuses into nothing', !G.of['explode:code:1'], JSON.stringify(G));
}

/* ── 2. one figure and one table out of the same message ────────────────────────────────────────────────────── */
{
  const w = Object.assign({ key: 'w:1', type: 'widget', size: 's', content: {} }, at('a2'));
  const tb = Object.assign({ key: 'tb:1', type: 'table', size: 'm', content: { columns: ['a'], rows: [['1']] } }, at('a2'));
  const F = V.fuseOf([w, tb]);
  t('the chart leads and the rows it is drawn from sit under it',
    F.of['tb:1'] === 'w:1' && F.groups['w:1'].layout === 'over', JSON.stringify(F));
  // two tables in one message: which one is the chart of which? the canvas does not guess
  const two = V.fuseOf([w, tb, Object.assign({ key: 'tb:2', type: 'table', size: 'm', content: {} }, at('a2'))]);
  t('two tables and one chart fuse nothing — the canvas does not guess which is which', !Object.keys(two.groups).length, JSON.stringify(two));
  // and a table from ANOTHER message is another message's business
  const far = V.fuseOf([w, Object.assign({ key: 'tb:3', type: 'table', size: 'm', content: {} }, at('a3'))]);
  t('a figure and a table from different replies are left alone', !Object.keys(far.groups).length, JSON.stringify(far));
}

/* ── 3. a strip of indicators, instead of three cards saying where they came from ────────────────────────────── */
{
  const chips = ['c1', 'c2', 'c3'].map((k) => Object.assign({ key: k, type: 'widget', size: 'xs', content: {} }, at('a4')));
  const F = V.fuseOf(chips);
  t('three glanceable things from one reply are one strip',
    F.groups.c1 && F.groups.c1.layout === 'strip' && F.groups.c1.members.join() === 'c2,c3', JSON.stringify(F));
  t('...and two are just two items', !Object.keys(V.fuseOf(chips.slice(0, 2)).groups).length);
  // a strip is a column wide; a pane beside or under something is a reading width; the lead's own size is not the group's
  t('a strip asks for a column, and anything with a pane asks for a reading width',
    V.unitsOf({ size: 'xs', fuse: 'strip' }) === 1 && V.unitsOf({ size: 'xs', fuse: 'beside' }) === 2
    && V.unitsOf({ type: 'widget', size: 's', fuse: 'over' }) === 2);
  t('but a FOLDED group is still a chip, and one you opened still takes the stage',
    V.unitsOf({ fuse: 'beside', folded: true }) === 1 / 3 && V.unitsOf({ fuse: 'strip', open: true }) === 4);
}

/* ── 4. what fusing must not do ─────────────────────────────────────────────────────────────────────────────── */
{
  const code = Object.assign({ key: 'code:1', type: 'code', size: 'm', content: { code: 'x' } }, at('a1'));
  const gr = Object.assign({ key: 'explode:code:1', type: 'explode', size: 'm', content: { binds: 'code:1' } }, at('a1'));
  t('off, nothing fuses — the reader can always have one card each', !Object.keys(V.fuseOf([code, gr], { off: true }).groups).length);
  t('an item you SPLIT out is never fused again', !V.fuseOf([code, gr], { except: { 'explode:code:1': 1 } }).of['explode:code:1']);
  t('one item alone fuses into nothing', !Object.keys(V.fuseOf([code]).groups).length);
  t('an item with no anchor belongs to no message, so it joins no group by message',
    !Object.keys(V.fuseOf([{ key: 'a', type: 'widget', size: 'xs' }, { key: 'b', type: 'widget', size: 'xs' },
      { key: 'c', type: 'widget', size: 'xs' }]).groups).length);
}

/* ── 5. the element draws it as one thing ───────────────────────────────────────────────────────────────────── */
t('a member is drawn as a pane of its lead and never again as a card of its own',
  /const nowOrder = now\.filter\(b => !isMember\(b\)\)/.test(SRC)
  && /pinned\.filter\(b => !isMember\(b\)\)\.map\(card\)/.test(SRC));
t('the panes are inside the lead\'s BODY, so the card, its header, its rail and its ceiling are the group\'s',
  /<div class="it-bd\$\{panes\.length \? ' fu fu-' \+ esc\(g\.layout\) : ''\}">\$\{inner\}\$\{panes\.map\(paneHtml\)\.join\(''\)\}<\/div>/.test(SRC));
t('a pane keeps its own key, so its live slot, its runs and every capability still address it',
  /return `<div class="fu-p" data-key="\$\{esc\(m\.key\)\}"/.test(SRC)
  && /ih = fn\(m\.content \|\| \{\}, m\.size, String\(m\.key\), this\);/.test(SRC));
t('the live layer needs nothing new: it mounts by the slot\'s key, wherever the slot is',
  /body\.querySelectorAll\('#items \.vc-live\[data-live\]'\)/.test(SRC));
t('a control INSIDE a pane acts on the pane\'s item, not on the lead it is drawn in',
  /const pane = btn\.closest\('\.fu-p\[data-key\]'\);/.test(SRC)
  && /const key = pane \? pane\.dataset\.key : it \? it\.dataset\.key : '';/.test(SRC));
t('split takes one item back out, and is kept here rather than written to the canvas',
  /\(this\._split \|\| \(this\._split = new Set\(\)\)\)\.add\(String\(pane\.dataset\.key\)\);/.test(SRC)
  && /const except = \{\}; \(this\._split \|\| new Set\(\)\)\.forEach/.test(SRC));
t('the placer is told the group is fused, so the count and the width follow from it',
  /fuse: c\.dataset\.fuse \|\| ''/.test(SRC) && /data-fuse="' \+ esc\(g\.layout\)/.test(SRC));
t('and which keys went into it, so the host can still find them', /data-fused="' \+ esc\(panes\.map\(p => p\.key\)\.join\(' '\)\)/.test(SRC));

/* ── 6. a pane is a pane, not a card in a card ───────────────────────────────────────────────────────────────── */
t('a pane has no border and no background of its own — the hairline is the join',
  /\.fu-p\{display:flex;flex-direction:column;min-height:0;min-width:0\}/.test(SRC)
  && !/\.fu-p\{[^}]*background/.test(SRC));
t('beside falls back to a stack when the card is too narrow to halve',
  /@container \(max-width: 420px\)\{\.it-bd\.fu-beside\{flex-direction:column\}/.test(SRC)
  && /container-type:inline-size/.test(SRC));
t('a strip flows its panes at the width each one needs', /\.it-bd\.fu-strip\{flex-direction:row;flex-wrap:wrap/.test(SRC)
  && /\.it-bd\.fu-strip>\*\{flex:0 1 auto\}/.test(SRC));

/* ── 7. the reader's own switch ──────────────────────────────────────────────────────────────────────────────── */
t('fuse is an attribute of the element, defaulting to on',
  /String\(this\.getAttribute\('fuse'\) \|\| 'on'\)\.toLowerCase\(\) === 'off'/.test(SRC)
  && /'explode-offer', 'fuse'\]/.test(SRC)
  && /name === 'fuse'\) && this\._doc\) this\.render\(this\._doc\)/.test(SRC));
t('and a setting in the canvas\'s own settings, which is how it gets there',
  /id="cfgCvFuse"[\s\S]*?value="on"[\s\S]*?value="off"/.test(CHAT) && /cfgCvFuse:\['fuse','on'\]/.test(CHAT));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
