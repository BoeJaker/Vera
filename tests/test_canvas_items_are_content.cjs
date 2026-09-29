// AN ITEM IS ITS CONTENT, NOT A TILE (owner, 2026-09-24: "every item in the canvas is still displayed inside a box -
// id like the elements to feel more homogeneous instead of a rigid tile layout", "its still a rigid grid (worse than
// the existing dashboard layout too)", "canvas items that are smaller than a column have large blank areas i.e.
// widgets", "the expanding of the canvas elements doesnt work - it shrinks the content").
//   node tests/test_canvas_items_are_content.cjs
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const at = (P) => Object.fromEntries(P.placements.map((p) => [p.key, p]));
const turns = { m1: { top: 0, height: 400 }, m2: { top: 500, height: 300 } };
const COL = { columns: 1, gap: 10, colWidth: 300, pad: 0, viewport: 900 };

/* ── no box, by default, not by setting ─────────────────────────────────────────────────────────────────────── */
t('an item draws no border, no ground, no radius', /\.it\{border:0;border-radius:0;background:none;/.test(SRC));
t('what separates one from the next is space and its own caption, not a frame', /margin:2px 0 10px;/.test(SRC));
t('the states that MEAN something still draw: waiting, opened, pinned, a suggestion',
  /\.it\.waiting\{animation:waitring/.test(SRC) && /\.it\.openin\{\}/.test(SRC) &&
  /\.it\.pinned\{box-shadow:inset 2px 0 0 0/.test(SRC) && /\.it\.ghost\{background:transparent;border:1px dashed/.test(SRC));
/* and the one that does NOT: "now" is the band every live item is in, so a ring on it was a box around everything */
t('being in the NOW band draws no ring — that was the border on every item',
  !/\.it\.now\{box-shadow/.test(SRC) && !/:host\(\[blocks="off"\]\) \.it\.now\{box-shadow/.test(SRC));
t('the item you are pointing at lifts, so the surface is still readable', /\.it:is\(:hover,\.hov\)\{background:color-mix/.test(SRC));
t('the live slot is a hole, not a plate — no ground behind a widget or a graph',
  /\.vc-live\{flex:1 1 auto;min-height:40px;height:110px;border-radius:0;background:none;/.test(SRC) &&
  /#live \.lv\{position:absolute;box-sizing:border-box;border-radius:0;overflow:hidden;background:none\}/.test(SRC));
t('...but a terminal, a rendered page and a panel keep theirs: they ARE surfaces',
  /#live \.lv\[data-kind="term"\],#live \.lv\[data-kind="preview"\],#live \.lv\[data-kind="panel"\]\{background:#000/.test(SRC));
t('a widget is mounted BARE — its own frame inside an item was a box in a box, in a shadow root no rule of ours could reach',
  /createElement\('vera-widget'\); inner\.setAttribute\('size', h\.dataset\.size \|\| 'm'\); inner\.setAttribute\('bare', ''\)/.test(SRC));
t('a code block keeps a ground because a monospace block IS a surface — but a rule, not a card', /\.vc-pre\{background:color-mix\(in srgb,var\(--bg2,#1c2026\) 55%,transparent\);border:0;/.test(SRC));

/* ── a small item does not take a whole row ──────────────────────────────────────────────────────────────────── */
{
  const P = V.place([{ key: 'a', h: 60, mid: 'm1', want: 1 / 3 }, { key: 'b', h: 70, mid: 'm1', want: 1 / 3 },
                     { key: 'c', h: 50, mid: 'm1', want: 1 / 3 }, { key: 'd', h: 200, mid: 'm1', want: 1 }], turns, COL);
  const p = at(P);
  t('three stickers flow across one row, each a third of the width',
    p.a.y === 0 && p.b.y === 0 && p.c.y === 0 && p.a.x === 0 && p.b.x === 100 && p.c.x === 200 && p.a.w === 90,
    JSON.stringify(P.placements.map((x) => [x.key, x.x, x.y, x.w])));
  t('the row reports which items joined it', !p.a.beside && p.b.beside && p.c.beside);
  t('a full-width item starts a new row, below the tallest of that row', p.d.y === 80 && p.d.w === 300 && !p.d.beside, JSON.stringify(p.d));
}
{
  const P = V.place([{ key: 'a', h: 60, mid: 'm1', want: .5 }, { key: 'b', h: 80, mid: 'm1', want: .5 },
                     { key: 'c', h: 40, mid: 'm1', want: .5 }, { key: 'd', h: 40, mid: 'm1', want: .5 }], turns, COL);
  const p = at(P);
  t('halves pair up and WRAP: two on a row, the next two below the taller of the first',
    p.a.y === 0 && p.b.y === 0 && p.c.y === 90 && p.d.y === 90 && p.d.beside, JSON.stringify(P.placements.map((x) => [x.key, x.x, x.y])));
}
t('an item of a LATER turn never joins an earlier row — a row is not worth dragging an item off its level',
  (() => { const p = at(V.place([{ key: 'a', h: 60, mid: 'm1', want: .5 }, { key: 'z', h: 60, mid: 'm2', want: .5 }], turns, COL));
    return p.z.y === 500 && !p.z.beside; })());
t('an item that asks for nothing takes the column, exactly as before',
  (() => { const p = at(V.place([{ key: 'a', h: 50, mid: 'm1' }, { key: 'b', h: 90, mid: 'm1' }], turns, { columns: 1, gap: 10, colWidth: 300, pad: 0 }));
    return p.a.w === 300 && p.b.y === 60 && !p.a.beside; })());
// what an item wants is read off the card in COLUMN UNITS (unitsOf) and then clamped to the columns there are, so the
// same widths the placer lays out at are the widths the column COUNT is chosen from (autoCols)
t('and one you opened or dragged takes the stage, whatever its size record says',
  /if \(d\.open\) return 4;/.test(SRC) &&
  /open: c\.classList\.contains\('openin'\) \|\| c\.classList\.contains\('sized'\)/.test(SRC) &&
  /const u = unitsOf\(descOf\(c\)\); return u > 1 \? Math\.max\(1, Math\.min\(cols, Math\.round\(u\)\)\) : u;/.test(SRC));
t('an item folded to its header line is a chip, and chips sit three to a row',
  /if \(d\.folded\) return 1 \/ 3;/.test(SRC) &&
  /folded: c\.classList\.contains\('compact'\) \|\| c\.classList\.contains\('overfold'\)/.test(SRC));
t('a kind you READ rather than glance at spans columns when there are columns to span',
  /const WIDE = \{ code: 1, html: 1, explode: 1, markdown: 1, panel: 1, session: 1, table: 1 \};/.test(SRC) &&
  /if \(WIDE\[ty\] \|\| sz === 'l'\) return 2;/.test(SRC));
t('the width is applied before the height is measured, or every height is a different item\'s',
  /WIDTH BEFORE HEIGHT/.test(SRC) && /cards\.forEach\(\(c, i\) => \{ const ww = wants\[i\];/.test(SRC) &&
  /c\.style\.width = \(ww < 1 \? Math\.round\(ww \* w\) - gap : span \* w \+ \(span - 1\) \* gap\) \+ 'px'/.test(SRC));
t('and the placed width is applied back to the card', /if \(p\.w > 0\) c\.style\.width = p\.w \+ 'px';/.test(SRC));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
